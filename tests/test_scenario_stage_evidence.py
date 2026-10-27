"""Regressions for range-based evidence, AS boundaries, and stage-only maps.

The Manager.java ranges reproduce the off-by-one failure from the supplied run
without copying private source code or invoking an LLM.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from pko.scenario_map.schema import MapState
from pko.scenario_map.tools import TOOLS
import test_scenario_schema as fixtures
from quality_helpers import explain_args


class EvidenceRangeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = fixtures.workspace()
        self.path = "source-1/Manager.java"
        lines = [f"// source line {number}" for number in range(1, 76)]
        lines[42] = "public void process(Request request) {"
        lines[43] = "    validate(request);"
        lines[65] = "    store(request);"
        lines[66] = "    return;"
        lines[67] = "}"
        self.workspace.tree.content[self.path] = "\n".join(lines)
        self.workspace.tree.files.append(self.path)
        self.state = MapState(self.workspace, "Оформление обращения")

    def ref(self, start=43, end=67, **extra):
        return {"path": self.path, "line_start": start, "line_end": end, **extra}

    def system(self, evidence, **extra):
        return self.state.dispatch("upsert_step", explain_args({
            "name": "Обработка обращения",
            "evidence_status": "observed", "evidence": evidence, **extra,
        }))

    def test_manager_range_uses_exact_delivered_lines_without_rereading_source(self):
        self.state.observe_read(self.path, 43, 25)
        read_before = deepcopy(self.state._reads)
        with patch.object(self.workspace.tree, "read", side_effect=AssertionError("Source reread")):
            result = self.system([self.ref()])
        self.assertTrue(result["ok"], result)
        citation = self.state.model["nodes"][0]["evidence"][0]
        expected = "\n".join(read_before[self.path][number] for number in range(43, 68)).strip()
        self.assertEqual(citation, {**self.ref(), "quote": expected})
        self.assertNotIn("}", citation["quote"])
        self.assertEqual(self.state._reads, read_before)

    def test_unread_closing_brace_cannot_be_claimed_by_range_only(self):
        self.state.observe_read(self.path, 43, 25)
        before = self.state.snapshot()
        with patch.object(self.workspace.tree, "read", side_effect=AssertionError("Source reread")):
            result = self.system([self.ref(end=68)])
        self.assertFalse(result["ok"])
        self.assertIn("read_file", result["error"])
        self.assertIn(self.path, result["error"])
        self.assertEqual(before, self.state.snapshot())
        self.assertNotIn(68, self.state._reads[self.path])

    def test_bad_legacy_quote_is_rejected_with_actionable_range_not_echoed_code(self):
        self.state.observe_read(self.path, 43, 26)
        wrong_quote = "\n".join(self.workspace.tree.content[self.path].splitlines()[42:68])
        before = self.state.snapshot()
        result = self.system([self.ref(quote=wrong_quote)])
        self.assertFalse(result["ok"])
        self.assertIn(self.path, result["error"])
        self.assertIn("43", result["error"])
        self.assertIn("67", result["error"])
        self.assertIn("quote", result["error"])
        self.assertNotIn("public void process", json.dumps(result, ensure_ascii=False))
        self.assertEqual(before, self.state.snapshot())
        # The addressed repair needs only existing coordinates, not another read.
        with patch.object(self.workspace.tree, "read", side_effect=AssertionError("Source reread")):
            corrected = self.system([self.ref()])
        self.assertTrue(corrected["ok"], corrected)

    def test_exact_legacy_quote_remains_compatible(self):
        self.state.observe_read(self.path, 43, 2)
        quote = "\n".join(self.state._reads[self.path][number] for number in (43, 44)).strip()
        result = self.system([self.ref(end=44, quote=quote)])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.state.model["nodes"][0]["evidence"][0]["quote"], quote)

    def test_range_requires_each_line_even_if_surrounding_lines_were_read(self):
        self.state.observe_read(self.path, 43, 1)
        self.state.observe_read(self.path, 45, 1)
        before = self.state.snapshot()
        result = self.system([self.ref(end=45)])
        self.assertFalse(result["ok"])
        self.assertIn("read_file", result["error"])
        self.assertEqual(before, self.state.snapshot())

    def test_range_cannot_read_unseen_file_or_blocked_source(self):
        for path in (self.path, "source-1/.env", "source-1/not-there.java"):
            with self.subTest(path=path):
                before = self.state.snapshot()
                with patch.object(self.workspace.tree, "read", side_effect=AssertionError("Source reread")):
                    result = self.system([{**self.ref(1, 1), "path": path}])
                self.assertFalse(result["ok"])
                self.assertEqual(before, self.state.snapshot())

    def test_range_bounds_remain_strict(self):
        self.state.observe_read(self.path, 43, 25)
        for start, end in ((0, 43), (43, 42), (True, 43), (43, 67.0), (43, 10000)):
            with self.subTest(start=start, end=end):
                result = self.system([self.ref(start, end)])
                self.assertFalse(result["ok"])
                self.assertEqual(self.state.model["nodes"], [])

    def test_range_only_citations_keep_masked_secrets_and_are_deep_copied(self):
        path = "source-1/channel.py"
        self.state.observe_read(path, 3, 1)
        refs = [{"path": path, "line_start": 3, "line_end": 3}]
        result = self.system(refs)
        self.assertTrue(result["ok"], result)
        refs[0]["line_start"] = 1
        model = self.state.snapshot()
        self.assertNotIn("not-for-output", json.dumps(model))
        self.assertEqual(model["nodes"][0]["evidence"][0]["line_start"], 3)
        self.assertEqual(model["nodes"][0]["evidence"][0]["quote"], self.state._reads[path][3].strip())
        model["nodes"][0]["evidence"][0]["quote"] = "changed in copy"
        self.assertNotEqual(self.state.model["nodes"][0]["evidence"][0]["quote"], "changed in copy")

    def test_mixed_invalid_evidence_does_not_partially_apply_or_consume_ids(self):
        self.state.observe_read(self.path, 43, 25)
        before = self.state.snapshot()
        rejected = self.system([self.ref(43, 44), self.ref(67, 68)])
        self.assertFalse(rejected["ok"])
        self.assertEqual(before, self.state.snapshot())
        accepted = self.system([self.ref(43, 44)])
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["id"], "step-1")

    def test_range_only_works_for_notes_groups_nodes_and_transition_endpoints(self):
        self.state.observe_read(self.path, 43, 25)
        evidence = [self.ref()]
        system = self.system(evidence)
        self.assertTrue(system["ok"], system)
        note = self.state.dispatch("save_research_note", {"kind": "fact", "finding": "Проверка выполняется перед сохранением", "evidence": evidence})
        group = self.state.dispatch("upsert_group", {"kind": "stage", "name": "Обработка обращения", "result": "Обращение сохранено", "evidence": evidence})
        self.assertTrue(note["ok"], note)
        self.assertTrue(group["ok"], group)
        nodes = []
        for name in ("Проверить обращение", "Сохранить обращение"):
            result = self.state.dispatch("upsert_step", explain_args({"name": name, "group_id": group["id"],
                "evidence_status": "observed", "evidence": evidence}))
            self.assertTrue(result["ok"], result)
            nodes.append(result["id"])
        transition = self.state.dispatch("upsert_transition", explain_args({"source": nodes[0], "target": nodes[1], "kind": "sequence",
            "evidence_status": "observed", "evidence": evidence}))
        self.assertTrue(transition["ok"], transition)
        for collection in ("research_notes", "groups", "nodes", "edges"):
            for item in self.state.model[collection]:
                self.assertTrue(item["evidence"][0]["quote"])

    def test_schema_evidence_is_reference_only_everywhere(self):
        # The model passes ranges; the harness resolves quotes. Old payloads
        # with quote stay accepted by schema.py, but the contract does not ask.
        evidence_shapes = []

        def visit(value):
            if isinstance(value, dict):
                properties = value.get("properties", {})
                if {"path", "line_start", "line_end"} <= set(properties):
                    evidence_shapes.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(TOOLS)
        self.assertGreater(len(evidence_shapes), 8)
        for shape in evidence_shapes:
            self.assertNotIn("quote", shape["properties"])
            self.assertTrue({"path", "line_start", "line_end"} <= set(shape["required"]))


class ReviewedMapPresentationTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ScenarioMapTests()
        self.helper.setUp()
        self.state = self.helper.state
        self.system_id, self.start_id, self.end_id = self.helper.complete_small_map()
        self.helper.review_current_operations()
        self.node = deepcopy(next(node for node in self.state.model["nodes"] if node["id"] not in self.state.model["scenario"]["entry_operation_ids"] + self.state.model["scenario"]["terminal_operation_ids"]))

    def finish(self):
        # Refresh only the separate semantic assessment; execution review
        # fingerprints must still reject changes in behavior/evidence.
        self.helper.good('review_semantics', **fixtures.semantic_payload(self.state))
        return self.state.dispatch("finish", {"summary": "Проверен результат уточнения карты"})

    def test_business_name_and_basis_can_be_refined_without_repeating_execution_review(self):
        before = deepcopy(self.state.model["execution_reviews"])
        self.helper.good("upsert_step", **{**self.node, "name": "Проверить полноту обращения",
            "basis": "До передачи обращения проверяются обязательные сведения; результат определяет возможность продолжения."})
        edge = deepcopy(self.state.model["edges"][0])
        self.helper.good("upsert_transition", **{**edge, "basis": "Приём заявки предшествует проверке её данных."})
        self.assertEqual(self.state.model["execution_reviews"], before)
        result = self.finish()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")

    def test_moving_action_between_stages_keeps_review_valid(self):
        old_stage = self.node["group_id"]
        new_stage = self.helper.good("upsert_group", name="Проверка обращения", kind="stage", result="Сведения проверены", evidence=self.node["evidence"])["id"]
        self.helper.good("upsert_step", **{**self.node, "group_id": new_stage})
        result = self.finish()
        self.assertTrue(result["ok"], result)

    def test_semantic_action_changes_require_new_execution_review(self):
        for field, value in (("description", "Другое действие"), ("input", "Параметры нового обращения"),
                             ("output", "Сохранённое обращение"), ("condition", "Обязательные сведения заполнены")):
            with self.subTest(field=field):
                self.helper.good("upsert_step", **{**self.node, field: value})
                result = self.finish()
                self.assertFalse(result["ok"])
                self.assertIn("после обзора", result["error"])
                self.helper.good("upsert_step", **self.node)
        self.assertTrue(self.finish()["ok"])

    def test_changed_evidence_invalidates_execution_review(self):
        replacement = self.helper.cite(start=2, end=2)
        explanations = [{'kind':'fragment','symbol':'','explanation':'Изменённое тестовое основание','evidence':replacement}]
        self.helper.good("upsert_step", **{**self.node, "evidence": replacement, 'code_explanations':explanations})
        result = self.finish()
        self.assertFalse(result["ok"])
        self.assertIn("после обзора", result["error"])

    def test_transition_business_explanation_is_distinct_from_semantic_description(self):
        edge = deepcopy(self.state.model["edges"][0])
        self.helper.good("upsert_transition", **{**edge, "description": "Продолжение выполняется новым способом"})
        result = self.finish()
        self.assertFalse(result["ok"])
        self.assertIn("после обзора", result["error"])


class GraphFrontierTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ScenarioMapTests()
        self.helper.setUp()
        self.state = self.helper.state
        self.system_id = self.helper.system()

    def test_full_roster_keeps_every_existing_node_without_quotes_or_prose(self):
        node_ids = []
        for number in range(45):
            node_ids.append(self.helper.good("upsert_step", name=f"Действие {number}", kind="operation",
                 description="Описание " * 100, evidence_status="observed",
                evidence=self.helper.cite())["id"])
        frontier = self.state.graph_frontier()
        self.assertEqual({node["id"] for node in frontier["nodes"]}, set(node_ids))
        for node in frontier["nodes"]:
            self.assertEqual(set(node), {"id", "name", "kind", "group_id", "continuation_mode", "join_mode", "completion_condition"})
        self.assertEqual(set(frontier["unlinked"]), set(node_ids))
        self.assertNotIn("quote", json.dumps(frontier))
        frontier["nodes"][0]["name"] = "Changed by caller"
        self.assertEqual(self.state.model["nodes"][0]["name"], "Действие 0")

    def test_frontier_distinguishes_data_links_from_flow_and_tracks_open_exits(self):
        start = self.helper.node(self.system_id, "start")
        action = self.helper.node(self.system_id)
        end = self.helper.node(self.system_id, "end")
        detached = self.helper.node(self.system_id)
        self.helper.edge(start, action)
        self.state.model["edges"].append({"id":"legacy-data", "source":action, "target":end, "kind":"data", "condition":""})
        gap = self.helper.good("add_gap", description="Не найден переход к завершению", node_ids=[action, end])["id"]
        frontier = self.state.graph_frontier()
        self.assertEqual(frontier["starts"], [start])
        self.assertEqual(frontier["ends"], [end])
        self.assertEqual(set(frontier["unlinked"]), {end, detached})
        self.assertIn(action, frontier["open_exits"])
        self.assertNotIn(start, frontier["open_exits"])
        self.assertNotIn(end, frontier["open_exits"])
        self.assertEqual(frontier["pending_gaps"], [{"id": gap, "node_ids": [action, end], "description": "Не найден переход к завершению"}])
        self.helper.edge(action, end)
        updated = self.state.graph_frontier()
        self.assertEqual(updated["unlinked"], [detached])
        self.assertNotIn(action, updated["open_exits"])

    def test_frontier_inspection_is_paginated_and_contains_recovery_boundaries(self):
        start = self.helper.node(self.system_id, "start")
        action = self.helper.node(self.system_id)
        end = self.helper.node(self.system_id, "end")
        self.helper.edge(start, action)
        page = self.helper.good("inspect_map", section="frontier", limit=1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["id"], start)
        # The paged roster must not erase the global start/end context.
        self.assertIn(start, json.dumps(page))
        self.assertIn(end, json.dumps(page))
        tail = self.helper.good("inspect_map", section="frontier", offset=3, limit=1)
        self.assertEqual(tail["items"][0]["id"], end)
        self.assertIsNone(tail["next_offset"])

    def test_frontier_page_does_not_repeat_every_large_gap_description(self):
        action = self.helper.node(self.system_id)
        gap_ids = []
        for number in range(40):
            gap_ids.append(self.helper.good("add_gap", description=f"Вопрос {number}: " + "д" * 5800,
                                           node_ids=[action])["id"])
        page = self.helper.good("inspect_map", section="frontier", limit=1)
        self.assertLess(len(json.dumps(page, ensure_ascii=False)), 24000)
        # Truncation is navigation only; all detailed questions remain readable.
        saved = self.state.model["gaps"]
        self.assertEqual(len(saved), 40)
        self.assertEqual({gap["id"] for gap in saved}, set(gap_ids))
        tail = self.helper.good("inspect_map", section="gaps", offset=40, limit=1)
        self.assertEqual(tail["items"][0]["id"], gap_ids[-1])


if __name__ == "__main__":
    unittest.main()
