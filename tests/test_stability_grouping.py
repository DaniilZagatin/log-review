"""Grouping preserves published execution and cannot hide pending research.

Scripted proposals exercise the harness, not the business judgement of a model.
"""
import copy
import json
import time
import unittest
from pathlib import Path

from pko.stages.contracts import HierarchyError, validate_hierarchy

from pko.stages.grouping import build_hierarchy
from pko.stages.inputs import build_payload, input_fingerprint, map_revision
from pko.stages.model import RequestBudget, StageModel
from stage_script import ScriptedStageClient
from test_methodology_hierarchy import action_map, business_operations


def pending_map():
    model = action_map()
    model["execution_reviews"] = [{
        "id": "review-1", "name": "Поиск договора",
        "operations": [
            {"id": "op-1", "node_id": "step-2", "name": "Поиск договора", "evidence": [{"quote": "private source"}]},
            {"id": "op-2", "node_id": "", "mapping_status": "unmapped", "kind": "computation",
             "name": "Сравнение атрибутов", "action": "Сопоставить атрибуты найденных договоров",
             "effect": "Разрешена неоднозначность", "failure": "Договор не определён",
             "condition": "Найдено несколько записей", "evidence": [{"quote": "private source"}]}],
        "route_expectations": [{"id": "route-1", "condition": "Стандартный платёж и договор не найден",
                                "expected_result": "Отказ в обработке платежа", "edge_ids": [],
                                "completion_node_id": "", "evidence": [{"quote": "private source"}]}]}]
    return model


def ask(model, *answers):
    client = ScriptedStageClient({"submit_business_stages": [
        lambda _payload, _messages, answer=answer: copy.deepcopy(answer) for answer in answers]})
    hierarchy = build_hierarchy(model, StageModel(client, RequestBudget(4, time.monotonic() + 60)))
    return hierarchy, client


class StableGroupingTests(unittest.TestCase):
    def test_shared_finalizer_scope_reaches_full_and_compact_grouping_input(self):
        model = pending_map()
        model['nodes'][0].update(outcome='unknown', outcome_scope='shared')
        original = copy.deepcopy(model)
        for compact in (False, True):
            payload = build_payload(model, compact=compact)
            operation = payload['operations'][0]
            self.assertEqual(operation['outcome_scope'], 'shared')
            self.assertEqual(operation['outcome'], 'unknown')
            operation['outcome_scope'] = 'branch'
            self.assertEqual(model, original)

    def test_compact_packet_keeps_discovered_unmapped_work_and_conditions(self):
        model = pending_map()
        original = copy.deepcopy(model)
        full, compact = build_payload(model), build_payload(model, compact=True)
        self.assertEqual(full["research_findings"], compact["research_findings"])
        finding = compact["research_findings"][0]
        self.assertEqual([item["id"] for item in finding["unmapped_actions"]], ["op-2"])
        self.assertEqual(finding["unmapped_actions"][0]["effect"], "Разрешена неоднозначность")
        self.assertEqual(finding["route_expectations"][0]["expected_result"], "Отказ в обработке платежа")
        self.assertNotIn("private source", json.dumps(compact))
        self.assertNotIn("evidence", finding["route_expectations"][0])
        self.assertEqual(compact["map_revision"], map_revision(model))
        compact["research_findings"][0]["unmapped_actions"][0]["condition"] = "Изменено"
        self.assertEqual(model, original, "payload must not share mutable state with the map")

    def test_research_change_invalidates_grouping_input_without_rewriting_graph_revision(self):
        original = pending_map()
        changed = copy.deepcopy(original)
        changed["execution_reviews"][0]["route_expectations"][0]["expected_result"] = "Передача на ручную обработку"
        self.assertEqual(map_revision(original), map_revision(changed))
        for mode in ("full", "compact"):
            self.assertNotEqual(input_fingerprint(original, mode), input_fingerprint(changed, mode))

    def test_unmapped_inventory_ids_cannot_be_group_members(self):
        model = pending_map()
        before = copy.deepcopy(model)
        answer = business_operations()
        answer["stages"][0]["operation_ids"].append("op-2")
        hierarchy, client = ask(model, answer, answer)
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("op-2", hierarchy["membership"])
        self.assertEqual(hierarchy["status"]["result"], "partial")
        self.assertEqual(model, before)

    def test_same_published_map_and_proposal_give_same_hierarchy_three_times(self):
        model = pending_map()
        before = copy.deepcopy(model)
        outputs = [ask(model, business_operations())[0] for _ in range(3)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertEqual(model, before)
        self.assertEqual(model["run"]["status"], "partial")
        self.assertEqual(len(model["nodes"]), len(outputs[0]["membership"]))

    def test_repair_cannot_rewrite_result_of_a_previously_accepted_group(self):
        first = business_operations()
        first["stages"][1]["operation_ids"] = ["step-999"]
        repair = business_operations()
        repair["stages"][0]["result"] = "Кредит погашен; дальнейшая обработка не требуется."
        model = pending_map()
        original = copy.deepcopy(model)
        hierarchy, client = ask(model, first, repair)
        self.assertEqual(len(client.requests), 2)
        kept = next(row for row in hierarchy["stages"] if row["id"] == "stage-1")
        self.assertEqual(kept["result"], first["stages"][0]["result"])
        self.assertEqual(hierarchy["status"]["result"], "partial")
        self.assertTrue(any("смысл ранее принятой группы" in item for item in hierarchy["status"]["reasons"]))
        self.assertEqual(model, original)


class GroupingInputCompatibilityTests(unittest.TestCase):
    def test_scope_field_does_not_rewrite_legacy_input_fingerprints(self):
        for profile in ('variant-actions-v1', None):
            model = pending_map()
            if profile is None:
                model.pop('semantic_profile', None)
            original = copy.deepcopy(model)
            for node in model['nodes']:
                node['outcome_scope'] = 'branch'
            for mode in ('full', 'compact'):
                self.assertEqual(input_fingerprint(model, mode, input_version='1'),
                                 input_fingerprint(original, mode, input_version='1'))
                packet = build_payload(model, compact=mode == 'compact', input_version='1')
                self.assertTrue(all('outcome_scope' not in row for row in packet['operations']))

    def fixture(self):
        return json.loads((Path(__file__).with_name("fixtures") / "stability_legacy_hierarchy.json").read_text(encoding="utf-8"))

    def test_published_unversioned_action_hierarchy_keeps_its_exact_input_contract(self):
        fixture = self.fixture()
        model, hierarchy = fixture["model"], fixture["hierarchy"]
        self.assertEqual(hierarchy["hierarchy_version"], "1.2")
        self.assertNotIn("input_version", hierarchy)
        self.assertEqual(input_fingerprint(model, "full", input_version="1"), hierarchy["input_sha256"])
        self.assertNotEqual(input_fingerprint(model, "full"), hierarchy["input_sha256"])
        before = copy.deepcopy(fixture)
        validate_hierarchy(hierarchy, model)
        self.assertEqual(fixture, before, "reading does not upgrade an old proposal")

    def test_old_contract_still_rejects_changed_map_input_or_methodology(self):
        fixture = self.fixture()
        original, hierarchy = fixture["model"], fixture["hierarchy"]
        changed = copy.deepcopy(original)
        changed["edges"][0]["condition"] = "Другое условие"
        with self.assertRaisesRegex(HierarchyError, "map revision"):
            validate_hierarchy(hierarchy, changed)
        changed = copy.deepcopy(original)
        changed["gaps"] = [{"id": "gap-1", "description": "Обнаружено неизвестное продолжение"}]
        with self.assertRaisesRegex(HierarchyError, "grouping input"):
            validate_hierarchy(hierarchy, changed)
        changed_hierarchy = copy.deepcopy(hierarchy)
        changed_hierarchy["methodology_sha256"] = "0" * 64
        with self.assertRaisesRegex(HierarchyError, "methodology"):
            validate_hierarchy(changed_hierarchy, original)

    def test_old_contract_cannot_claim_to_cover_new_independent_route_expectations(self):
        fixture = self.fixture()
        fixture["model"]["execution_reviews"] = pending_map()["execution_reviews"]
        with self.assertRaisesRegex(HierarchyError, "route expectations"):
            validate_hierarchy(fixture["hierarchy"], fixture["model"])

    def test_explicit_new_contract_has_no_silent_legacy_fallback(self):
        model = pending_map()
        hierarchy, _ = ask(model, business_operations())
        self.assertEqual(hierarchy["input_version"], "2")
        self.assertEqual(build_payload(model)["input_version"], "2")
        old_digest = input_fingerprint(model, "full", input_version="1")
        hierarchy["input_sha256"] = old_digest
        with self.assertRaisesRegex(HierarchyError, "grouping input"):
            validate_hierarchy(hierarchy, model)
        hierarchy["input_version"] = "unknown"
        with self.assertRaisesRegex(HierarchyError, "input version"):
            validate_hierarchy(hierarchy, model)

    def test_new_research_findings_invalidate_new_hierarchy_without_changing_map(self):
        model = pending_map()
        hierarchy, _ = ask(model, business_operations())
        model["execution_reviews"][0]["route_expectations"][0]["condition"] = "Нестандартный платёж"
        self.assertEqual(hierarchy["map_revision"], map_revision(model))
        with self.assertRaisesRegex(HierarchyError, "grouping input"):
            validate_hierarchy(hierarchy, model)


if __name__ == "__main__":
    unittest.main()
