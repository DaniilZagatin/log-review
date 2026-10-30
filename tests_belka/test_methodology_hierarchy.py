"""A new action profile groups business operations without relabelling old maps."""
import copy
import hashlib
import json
import time
import unittest

from pko.stages.contracts import HierarchyError, validate_hierarchy
from pko.stages.grouping import build_hierarchy, unavailable_hierarchy
from pko.stages.inputs import (ACTION_PROFILE, METHODOLOGY, OPERATIONS_STAGE, STAGE, build_payload,
                               input_fingerprint, map_revision, methodology)
from pko.stages.model import RequestBudget, StageModel
from stage_script import ScriptedStageClient
from test_business_stages import STAGES, payment_map


def action_map():
    model = payment_map()
    model["semantic_profile"] = ACTION_PROFILE
    model["scenario"].update(variant_context="Зачисление внешнего платежа", instance_unit="Один платёж",
                             variant_factors=["Платёж по кредитному договору"])
    model["research_coverage"] = {"status": "partial", "unmapped_actions": ["action-7"]}
    model["run"].update(status="partial", reason="Продолжение не исследовано")
    return model


def business_operations():
    answer = copy.deepcopy(STAGES)
    for row in answer["stages"]:
        row["boundary"] = "Результат установлен либо сформировано решение о невозможности продолжения."
    return answer


class MethodologyHierarchyTests(unittest.TestCase):
    def ask(self, model, *answers):
        messages_seen, schemas_seen = [], []

        class Client(ScriptedStageClient):
            def chat(self, messages, **kwargs):
                messages_seen.append(copy.deepcopy(messages))
                schemas_seen.append(copy.deepcopy(kwargs["tools"][0]["function"]))
                return super().chat(messages, **kwargs)

        client = Client({"submit_business_stages": [lambda *_args, value=value: value for value in answers]})
        engine = StageModel(client, RequestBudget(4, time.monotonic() + 60))
        before = copy.deepcopy(model)
        hierarchy = build_hierarchy(model, engine)
        self.assertEqual(before, model)
        return hierarchy, client, messages_seen, schemas_seen

    def test_profile_selects_operations_and_requires_detachable_result_boundary(self):
        model = action_map()
        hierarchy, client, messages, schemas = self.ask(model, business_operations())
        self.assertEqual(hierarchy["hierarchy_version"], "1.2")
        self.assertEqual(hierarchy["hierarchy_kind"], "business_operations")
        self.assertEqual(hierarchy["semantic_profile"], ACTION_PROFILE)
        self.assertEqual(hierarchy["authority_status"], "proposed")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertTrue(all(row["boundary"] for row in hierarchy["stages"]))
        self.assertIn("boundary", schemas[0]["parameters"]["properties"]["stages"]["items"]["required"])
        self.assertIn("отделимый результат", messages[0][0]["content"])
        self.assertIn("этапы здесь не строятся", messages[0][0]["content"])
        self.assertEqual(hierarchy["methodology_sha256"], methodology(OPERATIONS_STAGE, METHODOLOGY)["sha256"])
        self.assertEqual(model["run"]["status"], "partial", "grouping cannot promote research readiness")

    def test_missing_boundary_gets_one_addressed_repair(self):
        answer = business_operations()
        del answer["stages"][0]["boundary"]
        hierarchy, client, messages, _ = self.ask(action_map(), answer, business_operations())
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertIn("boundary", messages[1][-1]["content"])

    def test_failed_boundary_repair_keeps_valid_groups_only(self):
        answer = business_operations()
        answer["stages"][0]["boundary"] = ""
        hierarchy, client, _, _ = self.ask(action_map(), answer, answer)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(hierarchy["status"]["result"], "partial")
        self.assertNotIn("stage-1", {row["id"] for row in hierarchy["stages"]})
        self.assertEqual({row["operation_id"] for row in hierarchy["unassigned"]}, {"step-1", "step-2", "step-3"})

    def test_legacy_map_keeps_original_semantics_and_revision(self):
        model = payment_map()
        hierarchy, _, messages, schemas = self.ask(model, STAGES)
        self.assertEqual(hierarchy["hierarchy_version"], "1.1")
        self.assertEqual(hierarchy["hierarchy_kind"], "business_stages")
        self.assertNotIn("semantic_profile", hierarchy)
        self.assertNotIn("boundary", schemas[0]["parameters"]["properties"]["stages"]["items"]["required"])
        self.assertEqual(messages[0][0]["content"], methodology(STAGE, METHODOLOGY)["text"])
        original = {key: model.get(key) for key in ("schema_version", "scenario", "nodes", "edges")}
        expected = "sha256:" + hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(map_revision(model), expected)
        del hierarchy["hierarchy_kind"]
        validate_hierarchy(hierarchy, model)

    def test_profiles_are_not_interchangeable_and_cannot_be_hidden_by_new_revision(self):
        old = payment_map()
        new = copy.deepcopy(old)
        new["semantic_profile"] = ACTION_PROFILE
        self.assertNotEqual(map_revision(old), map_revision(new))
        self.assertNotEqual(input_fingerprint(old, "full"), input_fingerprint(new, "full"))
        hierarchy, _, _, _ = self.ask(old, STAGES)
        hierarchy["map_revision"] = map_revision(new)
        with self.assertRaises(HierarchyError):
            validate_hierarchy(hierarchy, new)
        fresh, _, _, _ = self.ask(new, business_operations())
        for key, value in (("hierarchy_kind", "business_stages"), ("semantic_profile", "different")):
            damaged = copy.deepcopy(fresh)
            damaged[key] = value
            with self.assertRaises(HierarchyError):
                validate_hierarchy(damaged, new)

    def test_empty_profile_keeps_legacy_grouping_and_validation(self):
        model = payment_map()
        model["semantic_profile"] = ""
        hierarchy, client, messages, schemas = self.ask(model, STAGES)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(hierarchy["hierarchy_version"], "1.1")
        self.assertEqual(hierarchy["hierarchy_kind"], "business_stages")
        self.assertNotIn("semantic_profile", hierarchy)
        self.assertNotIn("boundary", schemas[0]["parameters"]["properties"]["stages"]["items"]["required"])
        self.assertEqual(messages[0][0]["content"], methodology(STAGE, METHODOLOGY)["text"])
        validate_hierarchy(hierarchy, model)

    def test_compact_input_keeps_scope_and_coverage(self):
        model = action_map()
        full, compact = build_payload(model), build_payload(model, compact=True)
        for key in ("semantic_profile", "hierarchy_kind", "scenario", "research_coverage", "map_status"):
            self.assertEqual(compact[key], full[key])
        self.assertEqual(compact["scenario"]["instance_unit"], "Один платёж")
        self.assertEqual(compact["research_coverage"]["unmapped_actions"], ["action-7"])
        self.assertEqual(compact["map_status"]["run_status"], "partial")
        changed = copy.deepcopy(model)
        changed["research_coverage"]["unmapped_actions"].append("action-8")
        self.assertNotEqual(input_fingerprint(model, "compact"), input_fingerprint(changed, "compact"))

    def test_unknown_profile_is_not_silently_reinterpreted(self):
        model = action_map()
        model["semantic_profile"] = "future-unknown-profile"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            build_payload(model)
        hierarchy, _, _, _ = self.ask(payment_map(), STAGES)
        with self.assertRaisesRegex(HierarchyError, "unsupported"):
            validate_hierarchy(hierarchy, model)

    def test_unavailable_new_grouping_keeps_explicit_semantics(self):
        model = action_map()
        hierarchy = unavailable_hierarchy(model, "Provider unavailable")
        self.assertEqual(hierarchy["status"]["result"], "none")
        self.assertEqual(hierarchy["hierarchy_kind"], "business_operations")
        self.assertEqual(hierarchy["semantic_profile"], ACTION_PROFILE)
        self.assertEqual(len(hierarchy["unassigned"]), len(model["nodes"]))
        validate_hierarchy(hierarchy, model)


if __name__ == "__main__":
    unittest.main()
