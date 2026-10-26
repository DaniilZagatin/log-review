"""The scenario gate verifies provenance and graph invariants, without an LLM."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from pko.scenario_map.schema import EXECUTION_CHECKS, MapState, MapValidationError
from pko.scenario_map.tools import TOOLS


class MemoryTree:
    def __init__(self):
        self.content = {
            "source-1/channel.py": "def submit(request):\n    return bank.post('/cards', request)\npassword = 'not-for-output'\n",
            "source-2/cards.py": "@route('/cards')\ndef accept(request):\n    bus.publish('card.issue', request.id)\n",
            "source-3/worker.py": "@consume('card.issue')\ndef issue(request_id):\n    return persist_card(request_id)\n",
            "source-1/.env": "SECRET=sensitive\n",
        }
        self.files = list(self.content)

    def read(self, path):
        return self.content.get(path)


def workspace(count=3):
    return SimpleNamespace(
        tree=MemoryTree(), notes=[], extraction=None,
        sources=[{"id": f"source-{i}", "name": f"system-{i}", "kind": "directory", "digest": str(i), "file_count": 1}
                 for i in range(1, count + 1)],
    )


class ScenarioMapTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState(workspace(), "Выпуск дебетовой карты действующему клиенту")

    def good(self, tool_name, **args):
        # Existing graph tests exercise a reviewed map. Inventory-specific tests
        # call dispatch('finish') directly to test missing/stale reviews.
        if tool_name == "finish":
            self.review_current_operations()
        response = self.state.dispatch(tool_name, args)
        self.assertTrue(response["ok"], response)
        return response

    def cite(self, path="source-1/channel.py", start=1, end=1):
        self.state.observe_read(path, start, end - start + 1)
        from pko.code_tools.tools import _mask_secrets
        quote = "\n".join(_mask_secrets(s) for s in self.state.workspace.tree.read(path).splitlines()[start - 1:end])
        return [{"path": path, "line_start": start, "line_end": end, "quote": quote}]

    def system(self, source=1):
        path = {1: "source-1/channel.py", 2: "source-2/cards.py", 3: "source-3/worker.py"}[source]
        return self.good("upsert_system", name=f"АС {source}", source_ids=[f"source-{source}"],
                         evidence_status="observed", evidence=self.cite(path))["id"]

    def node(self, system, kind="action", evidence=None):
        return self.good("upsert_step", name=f"Шаг {kind}", kind=kind, system_id=system,
                         description="Бизнес-действие", evidence_status="observed",
                         evidence=evidence or self.cite())["id"]

    def edge(self, left, right, kind="sequence", **extra):
        return self.good("upsert_transition", source=left, target=right, kind=kind,
                         description="Следующий шаг", evidence_status="observed", evidence=self.cite(), **extra)["id"]

    def complete_small_map(self):
        self.good("set_scope", title="Оформление карты", start="Получена заявка", end="Карта выпущена", exclusions=["Перевыпуск"])
        system = self.system()
        first, last = self.node(system, "start"), self.node(system, "end")
        operation = self.node(system)
        self.edge(first, operation)
        self.edge(operation, last)
        self.good("review_source", source_id="source-1", disposition="reviewed", reason="Прочитана точка входа")
        for source, path in (("source-2", "source-2/cards.py"), ("source-3", "source-3/worker.py")):
            self.state.observe_read(path, 1, 1)
            self.good("review_source", source_id=source, disposition="out_of_scope", reason="Другой вариант сценария")
        self.group_current_actions()
        return system, first, last

    def review_payload(self, system, node_ids=None):
        nodes = [node for node in self.state.model["nodes"] if node["system_id"] == system
                 and node["kind"] in ("action", "decision", "external", "wait") and node["evidence_status"] != "unknown"
                 and (node_ids is None or node["id"] in node_ids)]
        evidence = []
        for node in nodes:
            for ref in node["evidence"]:
                if ref not in evidence:
                    evidence.append(deepcopy(ref))
        operations = [{"node_id": node["id"], "name": node["name"], "kind": "validation" if node["kind"] == "decision" else "action",
                       "action": "Выполнить одно действие над заявкой", "effect": "Получен результат отдельного действия",
                       "failure": "Отказ обследуется в контексте данного участка", "evidence": deepcopy(node["evidence"])} for node in nodes]
        checks = {name: {"status": "not_found", "explanation": "В прочитанном участке не найдено",
                         "evidence": deepcopy(evidence)} for name in EXECUTION_CHECKS}
        node_set = {node["id"] for node in nodes}
        node_index = {node["id"]: node for node in self.state.model["nodes"]}
        edges = [edge for edge in self.state.model["edges"] if edge["source"] in node_set or edge["target"] in node_set]
        for category, kinds in (("branches", ("branch",)), ("failures", ("error", "retry")), ("integrations", ("call", "event", "return"))):
            selected = [edge["id"] for edge in edges if edge["kind"] in kinds or (category == "integrations" and
                         node_index[edge["source"]]["system_id"] != node_index[edge["target"]]["system_id"])]
            if selected:
                checks[category].update(status="present", explanation="Найдено и отражено переходами", edge_ids=selected)
        decisions = [node["id"] for node in nodes if node["kind"] == "decision"]
        if decisions:
            checks["branches"].update(status="present", explanation="Условие отражено отдельным узлом", node_ids=decisions)
        previous = next((review for review in self.state.model["execution_reviews"] if review["system_id"] == system), None)
        payload = {"name": "Исследованный исполнитель", "system_id": system, "evidence": evidence,
                   "operations": operations, "checks": checks}
        if previous:
            payload["id"] = previous["id"]
        return payload

    def review_current_operations(self):
        self.group_current_actions()
        for system in self.state.model["systems"]:
            payload = self.review_payload(system["id"])
            if payload["operations"]:
                self.good("review_execution", **payload)

    def group_current_actions(self):
        nodes = [n for n in self.state.model["nodes"] if n["kind"] in ("action", "decision", "wait", "external")
                 and n["evidence_status"] != "unknown" and not n.get("group_id")]
        if not nodes:
            return
        stage = self.good("upsert_group", name="Подача заявки", kind="stage", result="Обращение принято",
                          evidence=nodes[0]["evidence"])["id"]
        for node in nodes:
            self.good("upsert_step", **{**node, "group_id": stage})

    def test_unread_quote_is_rejected_and_failed_mutation_is_atomic(self):
        before = self.state.snapshot()
        response = self.state.dispatch("upsert_system", {"name": "Канал", "source_ids": ["source-1"], "evidence_status": "observed",
            "evidence": [{"path": "source-1/channel.py", "line_start": 1, "line_end": 1, "quote": "def submit(request):"}]})
        self.assertFalse(response["ok"])
        self.assertIn("read_file", response["error"])
        self.assertEqual(before, self.state.snapshot())
        self.assertEqual(self.system(), "sys-1")

    def test_stages_are_separate_from_flow_and_can_cross_systems(self):
        first, second = self.system(), self.system(2)
        evidence = self.cite() + self.cite("source-2/cards.py")
        stage = self.good("upsert_group", name="Принять заявку", kind="stage", result="Заявка доставлена", evidence=evidence)["id"]
        for system, refs in ((first, evidence[:1]), (second, evidence[1:])):
            self.good("upsert_step", name="Обработка запроса", kind="action", system_id=system,
                      group_id=stage, evidence_status="observed", evidence=refs)
        self.assertEqual(len(self.state.model["nodes"]), 2)
        self.assertEqual(self.state.model["edges"], [])
        self.assertEqual(self.good("inspect_map", section="groups")["total"], 1)
        before = self.state.snapshot()
        self.assertFalse(self.state.dispatch("remove_element", {"kind": "group", "id": stage})["ok"])
        self.assertEqual(before, self.state.snapshot())

    def test_stage_membership_and_evidence_are_checked_transactionally(self):
        evidence = self.cite()
        payload = dict(name="Этап", kind="stage", result="Результат", evidence=evidence)
        self.assertFalse(self.state.dispatch("upsert_group", {**payload, "evidence": []})["ok"])
        stage = self.good("upsert_group", **payload)["id"]
        before = self.state.snapshot()
        for parent in (None, "missing", stage):
            self.assertFalse(self.state.dispatch("upsert_group", {**payload, "kind": "operation", "parent_id": parent})["ok"])
            self.assertEqual(before, self.state.snapshot())
        self.assertFalse(self.state.dispatch("upsert_group", {**payload, "id": stage, "parent_id": stage})["ok"])
        node = self.node(self.system())
        current = next(n for n in self.state.model["nodes"] if n["id"] == node)
        self.good("upsert_step", **{**current, "group_id": stage})
        before = self.state.snapshot()
        self.assertFalse(self.state.dispatch("upsert_step", {**current, "group_id": "missing"})["ok"])
        self.assertEqual(before, self.state.snapshot())

    def test_finish_requires_action_group_or_explicit_gap(self):
        system, _, _ = self.complete_small_map()
        node = next(n for n in self.state.model["nodes"] if n["kind"] == "action")
        stage = node["group_id"]
        self.good("upsert_step", **{**node, "group_id": None})
        self.good("remove_element", kind="group", id=stage)
        self.good("review_execution", **self.review_payload(system))
        result = self.state.dispatch("finish", {"summary": "Группировка не установлена"})
        self.assertFalse(result["ok"])
        self.assertIn("group_id", result["error"])
        self.good("add_gap", description="Этап действия не установлен", node_ids=[node["id"]])
        result = self.state.dispatch("finish", {"summary": "Группировка требует уточнения"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "partial")

    def test_empty_groups_cannot_finish(self):
        self.complete_small_map()
        self.review_current_operations()
        self.good("upsert_group", name="Неиспользуемый этап", kind="stage", result="Ожидаемый результат", evidence=self.cite())
        response = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(response["ok"])
        self.assertIn("пустая группа", response["error"])

    def test_terminal_outcome_is_explicit_and_not_inferred_from_name(self):
        system = self.system()
        first = self.good("upsert_step", name="Успех", kind="end", system_id=system, evidence=self.cite(), evidence_status="observed")["id"]
        node = next(n for n in self.state.model["nodes"] if n["id"] == first)
        self.assertEqual(node["outcome"], "unknown")
        for outcome in ("success", "refusal", "failure", "cancelled"):
            self.good("upsert_step", **{**node, "outcome": outcome})
        self.assertFalse(self.state.dispatch("upsert_step", {**node, "outcome": "invented"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {**node, "kind": "action", "outcome": "success"})["ok"])

    def test_decision_requires_labels_even_for_non_branch_edges(self):
        system, first, last = self.complete_small_map()
        decision = self.node(system, "decision")
        self.edge(first, decision)
        retry = self.edge(decision, decision, "retry")
        self.edge(decision, last, "branch", condition="Проверки пройдены")
        self.review_current_operations()
        rejected = self.state.dispatch("finish", {"summary": "Проверено"})
        self.assertFalse(rejected["ok"])
        self.assertIn("условия всех исходящих", rejected["error"])
        edge = next(e for e in self.state.model["edges"] if e["id"] == retry)
        self.good("upsert_transition", **{**edge, "condition": "Временная ошибка; повтор разрешён"})
        self.assertEqual(self.good("finish", summary="Подписаны исходы")["status"], "completed")

    def test_only_masked_literal_exact_window_is_accepted(self):
        evidence = self.cite(start=3, end=3)
        self.assertNotIn("not-for-output", evidence[0]["quote"])
        args = {"name": "Канал", "source_ids": ["source-1"], "evidence_status": "observed", "evidence": evidence}
        raw = deepcopy(args)
        raw["evidence"][0]["quote"] = "password = 'not-for-output'"
        self.assertFalse(self.state.dispatch("upsert_system", raw)["ok"])
        self.assertTrue(self.state.dispatch("upsert_system", args)["ok"])
        other = deepcopy(args)
        other["evidence"][0]["line_start"] = 1
        self.assertFalse(self.state.dispatch("upsert_system", other)["ok"])

    def test_registered_ranges_do_not_bridge_unread_lines(self):
        self.cite(start=1)
        self.cite(start=3, end=3)
        response = self.state.dispatch("upsert_system", {"name": "Канал", "source_ids": ["source-1"], "evidence_status": "inferred",
            "evidence": [{"path": "source-1/channel.py", "line_start": 1, "line_end": 3, "quote": "some quote"}]})
        self.assertFalse(response["ok"])
        self.assertEqual(self.state.snapshot()["read_coverage"][0]["ranges"], [[1, 1], [3, 3]])

    def test_blocked_file_never_registers(self):
        self.state.observe_read("source-1/.env")
        response = self.state.dispatch("upsert_system", {"name": "АС", "source_ids": ["source-1"], "evidence_status": "observed",
            "evidence": [{"path": "source-1/.env", "line_start": 1, "line_end": 1, "quote": "SECRET=sensitive"}]})
        self.assertFalse(response["ok"])
        self.assertEqual(self.state.snapshot()["read_coverage"], [])

    def test_duplicate_and_unknown_sources_are_rejected(self):
        bad = workspace()
        bad.sources.append(deepcopy(bad.sources[0]))
        with self.assertRaises(MapValidationError):
            MapState(bad, "Сценарий")
        self.assertFalse(self.state.dispatch("upsert_system", {"name": "АС", "source_ids": ["source-1", "source-1"]})["ok"])
        self.assertFalse(self.state.dispatch("upsert_system", {"name": "АС", "source_ids": ["source-44"]})["ok"])

    def test_cross_system_observed_edge_needs_both_endpoints(self):
        left_system, right_system = self.system(), self.system(2)
        left = self.node(left_system)
        right = self.node(right_system, evidence=self.cite("source-2/cards.py"))
        args = {"source": left, "target": right, "kind": "call", "evidence_status": "observed",
                "evidence": self.cite(start=2, end=2), "channel": "HTTP POST /cards", "contract": "request -> accept(request)"}
        self.assertFalse(self.state.dispatch("upsert_transition", args)["ok"])
        args.update(sender_evidence=self.cite(start=2, end=2), receiver_evidence=self.cite())
        rejection = self.state.dispatch("upsert_transition", args)
        self.assertFalse(rejection["ok"])
        self.assertIn("другому источнику/АС", rejection["error"])
        args["receiver_evidence"] = self.cite("source-2/cards.py", 1, 2)
        self.assertTrue(self.state.dispatch("upsert_transition", args)["ok"])

    def test_channel_name_without_contract_does_not_confirm_cross_system_link(self):
        left, right = self.node(self.system()), self.node(self.system(2), evidence=self.cite("source-2/cards.py"))
        args = {"source": left, "target": right, "kind": "event", "evidence_status": "observed",
                "evidence": self.cite(), "sender_evidence": self.cite(), "receiver_evidence": self.cite("source-2/cards.py"),
                "channel": "card.issue", "contract": "unknown"}
        rejection = self.state.dispatch("upsert_transition", args)
        self.assertFalse(rejection["ok"])
        self.assertIn("contract", rejection["error"])

    def test_foreign_evidence_for_step_and_system_edit_is_rejected(self):
        system = self.system()
        self.node(system)
        response = self.state.dispatch("upsert_step", {"name": "Неверный шаг", "system_id": system,
                      "evidence_status": "observed", "evidence": self.cite("source-2/cards.py")})
        self.assertFalse(response["ok"])
        before = self.state.snapshot()
        response = self.state.dispatch("upsert_system", {"id": system, "name": "АС 1", "source_ids": ["source-2"],
                     "evidence_status": "observed", "evidence": self.cite("source-2/cards.py")})
        self.assertFalse(response["ok"])
        self.assertEqual(before["systems"], self.state.snapshot()["systems"])

    def test_finish_requires_scope_sources_and_boundaries(self):
        self.node(self.system())
        response = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(response["ok"])
        self.assertIn("scenario.start", response["error"])
        self.assertIn("kind=start", response["error"])
        self.assertIn("Не рассмотрены источники", response["error"])
        self.assertEqual(self.state.model["run"]["status"], "running")

    def test_completed_only_means_completed_supplied_materials(self):
        self.complete_small_map()
        result = self.good("finish", summary="Цепочка подачи заявки восстановлена")
        self.assertEqual(result["status"], "completed")
        self.assertIn("не подтверждение полноты", result["reason"])
        self.assertFalse(self.state.snapshot()["validation"]["semantic_verification"])

    def test_control_flow_cycles_are_legal_when_they_have_an_exit(self):
        system, first, last = self.complete_small_map()
        operation = self.node(system)
        decision = self.node(system, "decision")
        self.edge(first, operation)
        self.edge(operation, decision)
        self.edge(decision, operation, "retry", condition="Временный отказ; повтор разрешён")
        self.edge(decision, last, "branch", condition="Выпуск завершён")
        self.assertEqual(self.good("finish", summary="Повтор имеет выход")["status"], "completed")

    def test_data_link_does_not_hide_disconnected_operation(self):
        system, first, last = self.complete_small_map()
        orphan = self.node(system)
        self.edge(first, orphan, "data")
        self.edge(orphan, last, "data")
        response = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(response["ok"])
        self.assertIn(orphan, response["error"])
        self.good("add_gap", description="Порядок запуска операции не найден", node_ids=[orphan])
        self.assertEqual(self.good("finish", summary="Порядок части шагов неизвестен")["status"], "partial")

    def test_independent_complete_chains_do_not_form_one_connected_scenario(self):
        system, _, _ = self.complete_small_map()
        other_start = self.node(system, "start")
        other_end = self.node(system, "end")
        self.edge(other_start, other_end)
        result = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(result["ok"])
        self.assertIn("Несвязанный фрагмент", result["error"])
        self.good("add_gap", description="Связь найденного фрагмента с основным сценарием не установлена",
                  node_ids=[other_start, other_end])
        self.assertEqual(self.good("finish", summary="Связь фрагмента неизвестна")["status"], "partial")

    def test_unknown_edge_needs_explicit_gap_with_both_nodes(self):
        _, first, last = self.complete_small_map()
        last = self.state.model["edges"][0]["target"]
        existing = self.state.model["edges"][0]["id"]
        self.good("upsert_transition", id=existing, source=first, target=last, kind="sequence", evidence_status="unknown")
        self.assertFalse(self.state.dispatch("finish", {"summary": "Исследовано"})["ok"])
        self.good("add_gap", description="Не найден вызов", node_ids=[first])
        self.assertFalse(self.state.dispatch("finish", {"summary": "Исследовано"})["ok"])
        self.good("add_gap", description="Нужна реализация перехода", node_ids=[first, last])
        self.assertEqual(self.good("finish", summary="Переход не подтверждён")["status"], "partial")

    def test_inference_and_assumptions_are_not_reported_as_complete(self):
        _, first, _ = self.complete_small_map()
        original = next(n for n in self.state.model["nodes"] if n["id"] == first)
        original = deepcopy(original)
        original["evidence_status"] = "inferred"
        self.good("upsert_step", **original)
        self.assertEqual(self.good("finish", summary="Начало выведено из кода")["status"], "partial")

    def test_removal_is_not_cascading_and_stable_ids_are_not_reused(self):
        system, first, last = self.complete_small_map()
        before = self.state.snapshot()
        self.assertFalse(self.state.dispatch("remove_element", {"kind": "step", "id": first})["ok"])
        self.assertEqual(before, self.state.snapshot())
        edge = self.state.model["edges"][0]["id"]
        self.good("remove_element", kind="transition", id=edge)
        self.good("remove_element", kind="step", id=first)
        new = self.node(system, "start")
        self.assertNotEqual(first, new)
        operation = next(n["id"] for n in self.state.model["nodes"] if n["kind"] == "action")
        self.edge(new, operation)
        self.assertEqual(self.good("finish", summary="Исправлена карта")["status"], "completed")

    def test_hierarchy_cycles_rejected_while_detail_remains_editable(self):
        system = self.system()
        parent, child = self.node(system), self.node(system)
        node = deepcopy(next(n for n in self.state.model["nodes"] if n["id"] == child))
        self.good("upsert_step", **{**node, "parent_id": parent})
        parent_node = deepcopy(next(n for n in self.state.model["nodes"] if n["id"] == parent))
        self.assertFalse(self.state.dispatch("upsert_step", {**parent_node, "parent_id": child})["ok"])
        self.assertFalse(self.state.dispatch("remove_element", {"kind": "step", "id": parent})["ok"])

    def test_unread_source_cannot_be_reviewed_or_used_source_excluded(self):
        self.assertFalse(self.state.dispatch("review_source", {"source_id": "source-1", "disposition": "reviewed", "reason": "Посмотрел"})["ok"])
        self.system()
        self.assertFalse(self.state.dispatch("review_source", {"source_id": "source-1", "disposition": "out_of_scope", "reason": "Лишнее"})["ok"])

    def test_invalid_tools_fields_and_references_are_actionable(self):
        self.assertFalse(self.state.dispatch("made_up", {})["ok"])
        self.assertFalse(self.state.dispatch("set_scope", {"process": "Неверное поле"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"name": "Шаг", "system_id": "sys-999"})["ok"])
        self.assertFalse(self.state.dispatch("add_gap", {"description": "Пробел", "node_ids": ["step-999"]})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"id": "step-100", "name": "Придуманный ID"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"name": "Шаг", "kind": "imaginary"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_transition", {"source": "step-1", "target": "step-2", "kind": "branch"})["ok"])

    def test_tool_contract_lists_all_mutation_handlers(self):
        names = [tool["function"]["name"] for tool in TOOLS]
        self.assertEqual(len(set(names)), 13)
        self.assertIn("save_research_brief", names)
        self.assertIn("save_research_note", names)
        self.assertIn("inspect_map", names)
        self.assertIn("finish", names)
        for tool in TOOLS:
            self.assertFalse(tool["function"]["parameters"]["additionalProperties"])

    def test_neutral_notes_do_not_make_complete_map_partial_but_omissions_do(self):
        self.complete_small_map()
        self.state.model["source_notes"] = ["Digest относится к снимку текста, не к ZIP"]
        self.assertEqual(self.good("finish", summary="Исследовано")["status"], "completed")
        self.state.model["source_omissions"] = ["Файл реализации слишком велик и пропущен"]
        self.assertEqual(self.good("finish", summary="Часть реализации недоступна")["status"], "partial")

    def test_inspect_is_paginated_and_does_not_reemit_large_quotes(self):
        self.complete_small_map()
        summary = self.good("inspect_map")
        self.assertEqual(summary["model"]["counts"]["nodes"], 3)
        self.assertNotIn("nodes", summary["model"])
        page = self.good("inspect_map", section="nodes", limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["next_offset"], 2)
        self.assertNotIn("quote", page["items"][0]["evidence"][0])
        next_page = self.good("inspect_map", section="nodes", offset=3, limit=1)
        self.assertIsNone(next_page["next_offset"])
        self.assertNotEqual(page["items"][0]["id"], next_page["items"][0]["id"])
        self.assertIn("quote", self.state.snapshot()["nodes"][0]["evidence"][0])
        self.assertFalse(self.state.dispatch("inspect_map", {"section": "nodes", "offset": 0})["ok"])
        self.assertFalse(self.state.dispatch("inspect_map", {"section": "not_a_section"})["ok"])

    def test_cross_system_proof_cannot_be_bypassed_by_sequence_or_data_label(self):
        left, right = self.node(self.system()), self.node(self.system(2), evidence=self.cite("source-2/cards.py"))
        for kind in ("sequence", "branch", "return", "retry", "error", "data"):
            with self.subTest(kind=kind):
                args = {"source": left, "target": right, "kind": kind, "condition": "Карта готова",
                        "evidence_status": "observed", "evidence": self.cite()}
                response = self.state.dispatch("upsert_transition", args)
                self.assertFalse(response["ok"])
                self.assertIn("sender_evidence", response["error"])
                self.assertEqual(self.state.model["edges"], [])
        self.good("upsert_transition", source=left, target=right, kind="sequence", evidence_status="inferred", evidence=self.cite())

    def test_unread_source_exclusion_is_an_assumption_until_reviewed_again(self):
        self.good("set_scope", title="Оформление карты", start="Получена заявка", end="Карта выпущена")
        system = self.system()
        first, last = self.node(system, "start"), self.node(system, "end")
        operation = self.node(system)
        self.edge(first, operation)
        self.edge(operation, last)
        self.good("review_source", source_id="source-1", disposition="reviewed", reason="Прочитана точка входа")
        self.state.observe_read("source-2/cards.py", 1, 2)
        self.good("review_source", source_id="source-2", disposition="out_of_scope", reason="Другой вариант сценария")
        review = self.good("review_source", source_id="source-3", disposition="out_of_scope", reason="По названию это соседний сценарий")
        self.assertFalse(review["inspected"])
        self.assertEqual(self.good("finish", summary="Один источник исключён предположительно")["status"], "partial")
        self.state.observe_read("source-3/worker.py", 1, 3)
        # Reading alone does not silently rewrite the earlier judgment.
        self.assertEqual(self.good("finish", summary="Нужно обновить вывод по источнику")["status"], "partial")
        review = self.good("review_source", source_id="source-3", disposition="out_of_scope", reason="Прочитан обработчик, относится к соседнему варианту")
        self.assertTrue(review["inspected"])
        self.assertEqual(self.good("finish", summary="Источники рассмотрены")["status"], "completed")

    def test_unknown_system_without_steps_requires_its_own_explicit_gap(self):
        self.complete_small_map()
        external = self.good("upsert_system", name="Внешняя АС", source_ids=[], evidence_status="unknown", evidence=[])["id"]
        self.good("add_gap", description="Общий вопрос по бизнес-терминам")
        rejected = self.state.dispatch("finish", {"summary": "Внешняя система неизвестна"})
        self.assertFalse(rejected["ok"])
        self.assertIn(external, rejected["error"])
        gap = self.good("add_gap", description="Материалы внешней АС отсутствуют", system_ids=[external])["id"]
        self.assertEqual(self.good("finish", summary="Внешняя АС не исследована")["status"], "partial")
        self.assertFalse(self.state.dispatch("remove_element", {"kind": "system", "id": external})["ok"])
        self.good("remove_element", kind="gap", id=gap)
        self.good("remove_element", kind="system", id=external)

    def test_unknown_behavior_needs_node_gap_even_if_system_gap_exists(self):
        system, first, last = self.complete_small_map()
        node = self.good("upsert_step", name="Неизвестное действие", system_id=system, evidence_status="unknown", evidence=[])["id"]
        self.edge(first, node)
        self.edge(node, last)
        self.good("add_gap", description="Общий вопрос к АС", system_ids=[system])
        rejected = self.state.dispatch("finish", {"summary": "Не всё известно"})
        self.assertFalse(rejected["ok"])
        self.assertIn(node, rejected["error"])
        self.good("add_gap", description="Реализация действия недоступна", node_ids=[node])
        self.assertEqual(self.good("finish", summary="Пробел указан явно")["status"], "partial")

    def test_finish_rejects_coarse_map_without_execution_inventory(self):
        self.complete_small_map()
        response = self.state.dispatch("finish", {"summary": "Общий шаг обработан"})
        self.assertFalse(response["ok"])
        self.assertIn("review_execution", response["error"])
        self.assertEqual(self.state.snapshot()["detail_review"]["status"], "pending")

    def test_two_atomic_effects_cannot_be_hidden_in_one_map_node(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        second = deepcopy(payload["operations"][0])
        second.update(name="Отправить уведомление", action="Отправить уведомление клиенту", effect="Уведомление передано")
        payload["operations"].append(second)
        before = self.state.snapshot()
        response = self.state.dispatch("review_execution", payload)
        self.assertFalse(response["ok"])
        self.assertIn("несколько операций", response["error"])
        self.assertEqual(self.state.snapshot(), before)

    def test_decision_requires_its_own_operation_record(self):
        system, first, last = self.complete_small_map()
        decision = self.node(system, "decision")
        action = next(node["id"] for node in self.state.model["nodes"] if node["kind"] == "action")
        self.edge(first, decision)
        self.edge(decision, action, "branch", condition="Проверка пройдена")
        self.edge(decision, last, "branch", condition="Получен отказ")
        self.good("review_execution", **self.review_payload(system, [action]))
        rejected = self.state.dispatch("finish", {"summary": "Проверка не раскрыта"})
        self.assertFalse(rejected["ok"])
        self.assertIn(f"{decision}: шаг не сопоставлен", rejected["error"])
        self.review_current_operations()
        self.assertTrue(self.state.dispatch("finish", {"summary": "Проверка показана отдельно"})["ok"])
        detail = self.state.snapshot()["detail_review"]
        self.assertEqual(detail["operation_count"], 2)
        self.assertFalse(detail["semantic_completeness_verified"])

    def test_inventory_requires_read_evidence_and_an_explicit_execution_scope(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        payload["operations"][0]["evidence"] = [{"path": "source-1/channel.py", "line_start": 2, "line_end": 2,
                                               "quote": "    return bank.post('/cards', request)"}]
        self.assertFalse(self.state.dispatch("review_execution", payload)["ok"])
        self.state.observe_read("source-1/channel.py", 2, 1)
        rejected = self.state.dispatch("review_execution", payload)
        self.assertFalse(rejected["ok"])
        self.assertIn("область обзора", rejected["error"])
        payload["evidence"] = self.cite(start=1, end=2)
        self.assertTrue(self.state.dispatch("review_execution", payload)["ok"])

    def test_detail_review_requires_all_five_investigations(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        payload["checks"].pop("failures")
        response = self.state.dispatch("review_execution", payload)
        self.assertFalse(response["ok"])
        self.assertIn("failures", response["error"])

    def test_unknown_failure_analysis_needs_addressed_gap_and_stays_partial(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        payload["checks"]["failures"] = {"status": "unknown", "explanation": "Реализация обработчика исключений отсутствует", "evidence": []}
        self.assertFalse(self.state.dispatch("review_execution", payload)["ok"])
        unrelated = self.good("add_gap", description="Неясный бизнес-термин")["id"]
        payload["checks"]["failures"]["gap_ids"] = [unrelated]
        self.assertFalse(self.state.dispatch("review_execution", payload)["ok"])
        gap = self.good("add_gap", description="Неизвестно поведение при отказе вызова", system_ids=[system])["id"]
        payload["checks"]["failures"]["gap_ids"] = [gap]
        self.good("review_execution", **payload)
        result = self.state.dispatch("finish", {"summary": "Обработка отказа требует изучения"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.state.snapshot()["detail_review"]["status"], "partial")

    def test_changed_operation_requires_new_review_but_keeps_operation_id(self):
        system, _, _ = self.complete_small_map()
        first_review = self.good("review_execution", **self.review_payload(system))
        identifier = first_review["operations"][0]["id"]
        node_id = first_review["operations"][0]["node_id"]
        updated = deepcopy(next(node for node in self.state.model["nodes"] if node["id"] == node_id))
        updated["description"] = "Уточнённое действие по коду"
        self.good("upsert_step", **updated)
        response = self.state.dispatch("finish", {"summary": "Уточнена карта"})
        self.assertFalse(response["ok"])
        self.assertIn("изменён после обзора", response["error"])
        updated_review = self.good("review_execution", **self.review_payload(system))
        self.assertEqual(updated_review["operations"][0]["id"], identifier)
        self.assertTrue(self.state.dispatch("finish", {"summary": "Обзор обновлён"})["ok"])

    def test_not_found_cannot_contradict_explicit_retry_in_the_map(self):
        system, _, _ = self.complete_small_map()
        action = next(node["id"] for node in self.state.model["nodes"] if node["kind"] == "action")
        self.edge(action, action, "retry", condition="Повтор после временного сбоя")
        payload = self.review_payload(system)
        payload["checks"]["failures"] = {"status": "not_found", "explanation": "Не нашёл", "evidence": deepcopy(payload["evidence"])}
        self.good("review_execution", **payload)
        response = self.state.dispatch("finish", {"summary": "Проверено"})
        self.assertFalse(response["ok"])
        self.assertIn("not_found противоречит", response["error"])

    def test_changed_sequence_edge_not_listed_in_checks_requires_new_review(self):
        system, _, _ = self.complete_small_map()
        self.good("review_execution", **self.review_payload(system))
        edge = deepcopy(self.state.model["edges"][0])
        self.assertFalse(any(check["edge_ids"] for check in self.state.model["execution_reviews"][0]["checks"].values()))
        edge["condition"] = "Только после подтверждения заявки"
        self.good("upsert_transition", **edge)
        result = self.state.dispatch("finish", {"summary": "Уточнён порядок исполнения"})
        self.assertFalse(result["ok"])
        self.assertIn(f"переход {edge['id']} изменён после обзора", result["error"])
        self.good("review_execution", **self.review_payload(system))
        self.assertTrue(self.state.dispatch("finish", {"summary": "Обзор обновлён"})["ok"])

    def test_added_unlisted_incident_edge_requires_new_review(self):
        system, _, last = self.complete_small_map()
        self.good("review_execution", **self.review_payload(system))
        action = next(node["id"] for node in self.state.model["nodes"] if node["kind"] == "action")
        edge_id = self.edge(action, last, "data", data="Идентификатор заявки")
        result = self.state.dispatch("finish", {"summary": "Уточнена передача данных"})
        self.assertFalse(result["ok"])
        self.assertIn(f"переход {edge_id} добавлен после обзора", result["error"])
        self.good("review_execution", **self.review_payload(system))
        self.assertTrue(self.state.dispatch("finish", {"summary": "Обзор обновлён"})["ok"])

    def test_deleted_unlisted_incident_edge_requires_new_review(self):
        system, _, last = self.complete_small_map()
        action = next(node["id"] for node in self.state.model["nodes"] if node["kind"] == "action")
        edge_id = self.edge(action, last, "data", data="Идентификатор заявки")
        self.good("review_execution", **self.review_payload(system))
        self.assertFalse(any(check["edge_ids"] for check in self.state.model["execution_reviews"][0]["checks"].values()))
        self.good("remove_element", kind="transition", id=edge_id)
        result = self.state.dispatch("finish", {"summary": "Ошибочная передача данных удалена"})
        self.assertFalse(result["ok"])
        self.assertIn(f"переход {edge_id} удалён или перенесён после обзора", result["error"])
        self.good("review_execution", **self.review_payload(system))
        self.assertTrue(self.state.dispatch("finish", {"summary": "Обзор обновлён"})["ok"])

    def test_review_references_prevent_silent_removal_and_operations_are_paged(self):
        system, first, last = self.complete_small_map()
        self.review_current_operations()
        operation = self.state.model["execution_reviews"][0]["operations"][0]
        for edge in list(self.state.model["edges"]):
            self.good("remove_element", kind="transition", id=edge["id"])
        response = self.state.dispatch("remove_element", {"kind": "step", "id": operation["node_id"]})
        self.assertFalse(response["ok"])
        page = self.good("inspect_map", section="operations", limit=1)
        self.assertEqual(page["items"][0]["id"], operation["id"])
        self.assertIn("effect", page["items"][0])
        self.assertNotIn("quote", page["items"][0]["evidence"][0])
        self.good("remove_element", kind="execution_review", id=self.state.model["execution_reviews"][0]["id"])
        self.good("remove_element", kind="step", id=operation["node_id"])

    def test_unknown_external_operation_does_not_require_an_invented_review(self):
        _, first, last = self.complete_small_map()
        external = self.good("upsert_step", name="Неизвестная внешняя обработка", kind="external", system_id="unknown",
                             evidence_status="unknown", evidence=[])["id"]
        self.good("upsert_transition", source=first, target=external, kind="call", evidence_status="unknown", evidence=[])
        self.good("upsert_transition", source=external, target=last, kind="return", evidence_status="unknown", evidence=[])
        self.good("add_gap", description="Не передана реализация внешней обработки", node_ids=[first, external, last])
        self.review_current_operations()
        result = self.state.dispatch("finish", {"summary": "Доступная часть детализирована, внешняя неизвестна"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "partial")
        self.assertIn(external, self.state.snapshot()["detail_review"]["unknown_node_ids"])


if __name__ == "__main__":
    unittest.main()
