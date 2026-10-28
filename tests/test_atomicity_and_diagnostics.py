"""Addressed diagnostics, inline roles and atomicity hints from run logs6.

The run stopped after 21 identical rejections of a route review that named no
case, 92 tasks pointed at a library builder, and every transition lacked a
basis. These tests pin the harness behaviour, not the model's judgement.
"""
import json
import unittest

from pko.scenario_map.context import compact_context
from pko.scenario_map.agent import _brief, _progress
from pko.scenario_map.schema import MapState
from quality_helpers import explain_args, review_payload
from test_scenario_schema import workspace

ROLES = "\n".join([
    "public static Agent createAgent() {",                 # 1
    "  Role checkRole = Role.builder()",                    # 2
    "      .name(\"check\")",                               # 3
    "      .handler(ctx -> ctx.runAgent(PprbAgent.createAgent()))",  # 4
    "      .build();",                                      # 5
    "  agent.addChild(checkRole);",                         # 6
    "  agent.addChild(Role.builder().name(\"x\").build());", # 7
    "  return agent;",                                      # 8
    "}",                                                    # 9
])
BRANCHY = "\n".join([
    "public void decide(Claim claim) {",            # 1
    "  if (claim.mcc() == 6011) {",                 # 2
    "    refuse(claim);",                           # 3
    "  } else if (claim.days() > 120) {",           # 4
    "    postpone(claim);",                         # 5
    "  } else if (claim.date() == null) {",         # 6
    "    noDecision(claim);",                       # 7
    "  }",                                          # 8
    "}",                                            # 9
])


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        w = workspace(1)
        w.tree.content.update({"source-1/Roles.java": ROLES, "source-1/Decide.java": BRANCHY,
                               "source-1/PprbAgent.java": "public static Agent createAgent() {\n return agent;\n}"})
        w.tree.files = list(w.tree.content)
        self.state = MapState(w, "Рассмотрение претензии")
        self.state.observe_read("source-1/channel.py", 1, 2)
        self.ref = {"path": "source-1/channel.py", "line_start": 1, "line_end": 2}

    def node(self, evidence=None, **extra):
        result = self.state.dispatch("upsert_step", explain_args({"name": "Получение результата", "evidence_status": "observed",
                                                                  "evidence": evidence or [self.ref], **extra}))
        self.assertTrue(result["ok"], result)
        return result

    def edge(self, source, target, **extra):
        result = self.state.dispatch("upsert_transition", explain_args({"source": source, "target": target, "kind": "sequence",
                                                                        "evidence_status": "observed", "evidence": [self.ref], **extra}))
        self.assertTrue(result["ok"], result)
        return result["id"]

    def test_all_failing_route_cases_are_reported_with_address_at_once(self):
        a, b, c = (self.node()["id"] for _ in range(3))
        self.state.model["scenario"]["entry_operation_ids"] = [a]
        self.edge(a, b)
        self.edge(b, c)
        self.state.observe_read("source-1/Decide.java", 1, 9)
        payload = review_payload(self.state)
        good = payload["route_cases"][0]
        unrelated = [{"path": "source-1/Decide.java", "line_start": 2, "line_end": 3}]
        payload["route_cases"] = [good, {**good, "name": "Второй случай", "evidence": unrelated},
                                  {**good, "name": "Третий случай", "path": [a, c], "edge_ids": None}]
        payload["route_cases"][2].pop("edge_ids")
        result = self.state.dispatch("review_semantics", payload)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "quality_check")
        cases = {row["case_index"]: row for row in result["details"]["cases"]}
        self.assertEqual(set(cases), {2, 3})
        self.assertIn("expected_ranges", cases[2])
        self.assertEqual(cases[3]["missing_transitions"], [[a, c]])

    def test_route_case_need_not_repeat_every_transition_evidence(self):
        a, b, c = (self.node()["id"] for _ in range(3))
        self.state.model["scenario"]["entry_operation_ids"] = [a]
        first, second = self.edge(a, b), self.edge(b, c)
        payload = review_payload(self.state)
        payload["route_cases"] = [{"name": "Полный маршрут", "input": "Заявка принята", "path": [a, b, c],
                                   "edge_ids": [first, second], "expected_result": "Обращение обработано",
                                   "evidence": [self.ref]}]
        result = self.state.dispatch("review_semantics", payload)
        self.assertTrue(result["ok"], result)
        case = self.state.model["semantic_review"]["route_cases"][0]
        self.assertEqual(case["transition_evidence"], [self.ref])

    def test_new_objects_need_basis_but_edits_by_id_do_not(self):
        missing = self.state.dispatch("upsert_step", {"name": "Получение результата", "description": "Шаг",
                                                       "evidence_status": "observed", "evidence": [self.ref]})
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "missing_fields")
        self.assertIn("basis", missing["error"])
        self.assertIn("code_explanations", missing["error"])
        a, b = self.node()["id"], self.node()["id"]
        bare = self.state.dispatch("upsert_transition", {"source": a, "target": b, "evidence_status": "observed",
                                                          "evidence": [self.ref]})
        self.assertEqual(bare["code"], "missing_fields")
        self.assertFalse(self.state.model["edges"])
        self.assertTrue(self.state.dispatch("upsert_step", {"id": a, "name": "Получение уточнённого результата"})["ok"])

    def test_large_operation_gets_atomicity_hint(self):
        self.state.observe_read("source-1/Decide.java", 1, 9)
        large = self.node([{"path": "source-1/Decide.java", "line_start": 1, "line_end": 9}], name="Рассмотрение претензии")
        self.assertTrue(any("Каждый шаг процесса" in hint for hint in large.get("hints", [])), large)
        conj = self.node(name="Проверка срока и перенос рассмотрения")
        self.assertTrue(any("«и»" in hint for hint in conj.get("hints", [])), conj)
        small = self.node([{"path": "source-1/Decide.java", "line_start": 2, "line_end": 3}], name="Проверка категории торговой точки")
        self.assertNotIn("hints", small)

    def test_choice_operation_with_outcome_and_terminal_continuation_are_flagged(self):
        decide = self.node(name="Проверка категории торговой точки", continuation_mode="choice", outcome="success")
        self.assertTrue(any("outcome не указывай" in hint for hint in decide.get("hints", [])))
        a, b = decide["id"], self.node()["id"]
        self.state.model["scenario"]["terminal_operation_ids"] = [a]
        result = self.state.dispatch("upsert_transition", explain_args({"source": a, "target": b, "kind": "sequence",
            "evidence_status": "observed", "evidence": [self.ref]}))
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("completion_condition" in hint for hint in result.get("hints", [])))


class InlineRoleTests(unittest.TestCase):
    def setUp(self):
        w = workspace(1)
        w.tree.content.update({"source-1/Roles.java": ROLES,
                               "source-1/PprbAgent.java": "public static Agent createAgent() {\n return agent;\n}"})
        w.tree.files = list(w.tree.content)
        self.state = MapState(w, "Рассмотрение претензии")

    def test_builder_roles_point_to_local_handlers_not_to_library_builder(self):
        self.state.observe_read("source-1/Roles.java", 1, 9)
        targets = {t["target"]: t for t in self.state.model["research_tasks"]}
        self.assertNotIn("Role.builder", targets)
        role = targets["checkRole (Role.builder)"]
        self.assertEqual(role["implementation"], [{"path": "source-1/Roles.java", "line_start": 2, "line_end": 5}])
        self.assertTrue(any(t.startswith("встроенная роль в строке 7") for t in targets))
        # The handler's own dependency is discovered inside the role definition.
        self.assertIn("PprbAgent.createAgent", targets)
        self.assertEqual(self.state._dependency_implementation(role, role["evidence"]), role["implementation"])

    def test_inline_role_is_mapped_through_its_handler_and_children(self):
        self.state.observe_read("source-1/Roles.java", 1, 9)
        self.state.observe_read("source-1/PprbAgent.java", 1, 3)
        tasks = {t["target"]: t for t in self.state.model["research_tasks"]}
        role, child = tasks["checkRole (Role.builder)"], tasks["PprbAgent.createAgent"]
        node = self.state.dispatch("upsert_step", explain_args({"name": "Запрос сведений об операции", "evidence_status": "observed",
            "evidence": [{"path": "source-1/Roles.java", "line_start": 4, "line_end": 4}]}))
        self.assertTrue(node["ok"], node)
        self.assertNotIn("tasks_mapped", node)
        blocked = self.state.dispatch("save_research_task", {"id": role["id"], "status": "mapped", "node_ids": [node["id"]],
            "resolution": "Роль раскрыта операцией запроса", "child_task_ids": [child["id"]]})
        self.assertFalse(blocked["ok"])
        self.assertIn(child["id"], blocked["error"])
        # An operation citing the handler's declaration closes the child, then the
        # role, by the rule a manual mapped status passes.
        handler = self.state.dispatch("upsert_step", explain_args({"name": "Получение сведений из системы", "evidence_status": "observed",
            "evidence": [{"path": "source-1/PprbAgent.java", "line_start": 1, "line_end": 3}]}))
        self.assertEqual(handler["tasks_mapped"], [child["id"], role["id"]])
        closed = {t["id"]: t for t in self.state.model["research_tasks"]}
        self.assertEqual(closed[role["id"]]["child_task_ids"], [child["id"]])
        self.assertEqual(set(closed[role["id"]]["node_ids"]), {node["id"], handler["id"]})
        # Once the map stops citing the implementation, the automatic closure is withdrawn.
        removed = self.state.dispatch("remove_element", {"kind": "step", "id": handler["id"]})
        self.assertTrue(removed["ok"], removed)
        self.assertEqual({closed[role["id"]]["status"], closed[child["id"]]["status"]}, {"open"})

    def test_calls_of_one_resolved_implementation_share_a_task(self):
        w = self.state.workspace
        w.tree.content["source-1/Twice.java"] = "\n".join([
            "public static Agent createAgent() {", "  agent.addChild(PprbAgent.createAgent());",
            "  agent.addChild(PprbAgent.createAgent());", "}"])
        w.tree.files.append("source-1/Twice.java")
        self.state._files.add("source-1/Twice.java")
        self.state.observe_read("source-1/Twice.java", 1, 4)
        same = [t for t in self.state.model["research_tasks"] if t["target"] == "PprbAgent.createAgent"]
        self.assertEqual(len(same), 1)
        self.assertEqual([ref["line_start"] for ref in same[0]["evidence"]], [2, 3])


class RejectionMemoryTests(unittest.TestCase):
    def test_rejections_survive_compaction_and_are_not_progress(self):
        state = MapState(workspace(1), "Рассмотрение претензии")
        before = _progress(state)
        state.model["run"]["recent_rejections"] = [{"tool": "review_semantics", "code": "quality_check",
            "error": "route_cases: требуют исправления случаи 5, 6", "details": {"cases": [{"case_index": 5, "edge_id": "edge-31"}]}}]
        self.assertEqual(_progress(state), before)
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}] + [
            {"role": "assistant", "content": "x" * 4000} for _ in range(30)]
        compacted = compact_context(messages, state, 20000, brief=_brief, force=True)
        recovery = json.dumps(compacted, ensure_ascii=False)
        self.assertIn("edge-31", recovery)
        self.assertIn("unresolved_rejections", recovery)


if __name__ == "__main__":
    unittest.main()
