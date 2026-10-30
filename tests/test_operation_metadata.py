"""R08 structured operation metadata (R2) and strict export of the new fields (R8).

Tests-first. The optional "details" object of an operation comes from the SAME bounded map-atomic page (shape in
test_operation_inspector.details); the host validates every reference, stores it as contracts.OperationDetails on
ProcessOperation.details and in the atomic record, and the export checks both copies. New API: contracts
OperationDetails / ExternalInteraction / CodeExplanation, ProcessOperation.details, the repair codes below in
workflow._REASONS and the report labels. Synthetic only: no live model, and a source quote never proves execution.
"""

import copy
import hashlib
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from process_observability import direct_atomic, workflow
from process_observability.contracts import (ArtifactStatus, ContractError, EvidenceRef, ProcessMap, ProcessOperation,
                                             SourceKind, Stage, validate_evidence_links)
from process_observability.onboarding_export import ExportError, _atomic_accounting, export_onboarding
from process_observability.report import build_html
from test_action_refinement import Code
from test_direct_atomic import anchors
from test_operation_inspector import (EXPLANATION, EXTERNAL, FOUND, REFUND_DETAILS, WEBHOOK, InspectorCase, card_of,
                                      details, fact_id, number_of, operation_ids, ref, refund_reply, section, text_of,
                                      views)

ROOT = Path(__file__).resolve().parents[1]
# SHA-256 of the R07 recorded replies of the strict synthetic demo; they must stay byte-identical.
R07_DEMO_REPLIES = "9d4138a0622125440e92a16790058bd4024e1fbe5fb94e4a94b0c265e7e48d8f"
# Code-led: one atomic page, then the one composition (stages and facts), then the later stages; no skeleton.
STAGE_TASKS = ["TASK: map-atomic", "TASK: map-compose", "TASK: instrumentation", "TASK: metrics"]


class OperationMetadataTests(InspectorCase):
    def with_details(self, values, **settings):
        return self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, with_details=values),
                              **settings)

    def test_details_come_from_the_same_atomic_page_and_are_stored_with_host_resolved_evidence(self):
        bundle = self.with_details({"refund": REFUND_DETAILS, "release": None})
        self.assertComplete(bundle)
        self.assertEqual(self.business.tasks(), STAGE_TASKS)  # no second operation-generation request, no repair
        ids = operation_ids(bundle)
        operations = {op["id"]: op for op in bundle["process_map"]["operations"]}
        saved = operations[ids["refund"]]["details"]
        scalars = ("input", "expected_result", "precondition", "completion_condition", "effect_kind", "outcomes",
                   "continuation", "join", "questions")
        self.assertEqual({key: saved[key] for key in scalars}, {key: REFUND_DETAILS[key] for key in scalars})
        self.assertEqual(anchors(bundle, saved["evidence_ids"]), {(WEBHOOK, 8, 13)})  # reply refs -> registry IDs
        [external] = saved["external_interactions"]
        visible = ("system", "direction", "endpoint", "contract")
        self.assertEqual({key: external[key] for key in visible}, {key: EXTERNAL[key] for key in visible})
        self.assertEqual(anchors(bundle, external["evidence_ids"]), {(WEBHOOK, 13, 13)})
        [explanation] = saved["code_explanations"]
        self.assertEqual((explanation["symbol"], explanation["explanation"]),
                         (EXPLANATION["symbol"], EXPLANATION["explanation"]))
        self.assertEqual(anchors(bundle, explanation["evidence_ids"]), {(WEBHOOK, 6, 6)})
        self.assertIsNone(operations[ids["accept"]]["details"])   # key omitted in the reply
        self.assertIsNone(operations[ids["release"]]["details"])  # explicit null in the reply
        record = {op["id"]: op for op in bundle["atomic_map"]["operations"]}
        for identifier, op in operations.items():
            self.assertEqual(record[identifier]["details"], op["details"])
        self.assertEqual(ProcessMap.from_dict(bundle["process_map"]).to_dict(), bundle["process_map"])
        self.assertEqual(views(bundle)[ids["refund"]]["details"], saved)

    def test_a_page_without_details_stays_valid_and_the_card_names_the_unknown(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        for op in bundle["process_map"]["operations"]:
            self.assertIn("details", op)
            self.assertIsNone(op["details"])
        ids = operation_ids(bundle)
        card = card_of(build_html(bundle), number_of(bundle, ids["accept"]))
        words = text_of(section(card, "details"))
        self.assertRegex(words, r"не установлен")
        self.assertNotRegex(text_of(card), re.compile(r"подтвержд[её]н результат", re.I))

    def test_ungrounded_details_get_one_precise_repair_and_never_a_second_generation(self):
        def external(**change):
            return {**EXTERNAL, **change}

        def explanation(**change):
            return {**EXPLANATION, **change}
        at = "operations[1].details"
        cases = {
            "text claim without refs": (details(input="Сумма возврата"), "details_without_refs", at + ".refs"),
            "effect without refs": (details(effect_kind="command"), "details_without_refs", at + ".refs"),
            "grounded mode without refs": (details(continuation="all"), "details_without_refs", at + ".refs"),
            "unsupplied ref": (details(input="Сумма возврата", refs=[ref(WEBHOOK, 1, 3)]), "ref_outside_supplied",
                               at + ".refs[0]"),
            "external without endpoint or contract": (details(external_interactions=[external(contract=None)]),
                                                      "external_without_contract", at + ".external_interactions[0]"),
            "external without refs": (details(external_interactions=[external(refs=[])]), "external_without_refs",
                                      at + ".external_interactions[0].refs"),
            "explanation without refs": (details(code_explanations=[explanation(refs=[])]),
                                         "explanation_without_refs", at + ".code_explanations[0].refs"),
            "symbol absent from the cited source": (details(code_explanations=[explanation(symbol="issue_refund")]),
                                                    "explanation_symbol_not_in_source",
                                                    at + ".code_explanations[0].symbol"),
        }
        for label, (bad, code, path) in cases.items():
            with self.subTest(case=label):
                bundle = self.inspected(reply=lambda brief, attempt, bad=bad: refund_reply(
                    brief, attempt, with_details={"refund": bad if attempt == 1 else REFUND_DETAILS}))
                first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
                [error] = repair["brief"].pop("validation_feedback")["errors"]
                self.assertEqual(repair["brief"], first["brief"])
                self.assertEqual((error["code"], error["path"]), (code, path))
                self.assertEqual(error["reason"], workflow._REASONS[code])
                [request] = [item for item in bundle["atomic_map"]["requests"] if item["kind"] == "atomic"]
                self.assertEqual(request["outcome"], "accepted_after_repair")
                self.assertEqual([task for task in self.business.tasks() if task != "TASK: map-atomic"],
                                 [task for task in STAGE_TASKS if task != "TASK: map-atomic"])

    def test_an_outgoing_request_and_a_command_never_claim_the_receivers_result(self):
        bundle = self.with_details({"refund": REFUND_DETAILS})
        self.assertComplete(bundle)
        ids = operation_ids(bundle)
        [external] = views(bundle)[ids["refund"]]["details"]["external_interactions"]
        self.assertEqual(external["direction"], "request")
        card = card_of(build_html(bundle), number_of(bundle, ids["refund"]))
        words = text_of(section(card, "details"))
        for value in (REFUND_DETAILS["input"], REFUND_DETAILS["expected_result"], REFUND_DETAILS["precondition"],
                      EXTERNAL["system"], EXTERNAL["contract"]):
            self.assertIn(value, words)
        self.assertRegex(words, r"по реализации")  # derived from code, never an observation
        self.assertRegex(words, re.compile(r"команд", re.I))
        self.assertRegex(words, re.compile(r"получател\w*.{0,80}не подтвержд", re.I | re.S))
        self.assertNotRegex(text_of(card), re.compile(r"подтвержд[её]н результат", re.I))

    def test_business_identity_and_answer_budget_change_while_the_technical_review_is_reused(self):
        self.assertNotEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/2")
        shape = direct_atomic.ATOMIC_SYSTEM.split("Output shape:\n", 1)[1].split("\n", 1)[0]
        for key in ('"details"', '"expected_result"', '"precondition"', '"effect_kind"', '"external_interactions"',
                    '"code_explanations"'):
            self.assertIn(key, shape)
        # No skeleton request exists any more; the one composition never asks for or returns operation details.
        self.assertFalse(hasattr(direct_atomic, "SKELETON_SYSTEM"))
        self.assertNotIn('"details"', direct_atomic.COMPOSE_SYSTEM)
        with patch.object(direct_atomic, "ATOMIC_SYSTEM", direct_atomic.ATOMIC_SYSTEM + "\n- earlier wording"):
            first = self.inspected()
        first_out = self.out
        [page] = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
        keyed = sum(site["key_status"] in ("literal", "resolved_constant") for site in page["brief"]["sites"])
        [request] = [item for item in first["atomic_map"]["requests"] if item["kind"] == "atomic"]
        # R07 planned 300 + 300 per unit + 150 per keyed site; optional details can only enlarge an answer.
        self.assertGreater(request["planned_answer_tokens"], 300 + 300 * len(page["brief"]["units"]) + 150 * keyed)
        code = Code(copy.deepcopy(FOUND))
        second = self.inspected(code=code, resume_from=first_out)
        self.assertEqual(code.calls, [])  # technical review and reduction are reused exactly
        self.assertIn("TASK: map-atomic", self.business.tasks())  # business pages are asked again
        self.assertNotEqual(first["manifest"]["provenance"]["config_sha256"],
                            second["manifest"]["provenance"]["config_sha256"])


class LegacyCompatibilityTests(unittest.TestCase):
    def test_the_strict_demo_keeps_its_recorded_replies_and_legacy_prompt(self):
        data = (ROOT / "examples/synthetic/model-responses.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), R07_DEMO_REPLIES)
        self.assertNotIn("details", workflow._OPERATION)
        self.assertNotIn('"details"', workflow.MAP_SYSTEM)

    def test_the_contract_keeps_legacy_operations_and_rejects_ungrounded_or_unknown_nested_details(self):
        from process_observability.contracts import CodeExplanation, ExternalInteraction, OperationDetails
        legacy = {"id": "refund", "name": "Возврат", "stage_id": "payment", "kind": "operation", "description": None,
                  "actor": None, "event_keys": [], "evidence_ids": []}
        self.assertIsNone(ProcessOperation.from_dict(legacy).details)
        unknown = OperationDetails()
        self.assertEqual((unknown.input, unknown.expected_result, unknown.precondition, unknown.completion_condition,
                          unknown.effect_kind, unknown.outcomes, unknown.continuation, unknown.join),
                         (None, None, None, None, "unknown", [], "unknown", "unknown"))
        grounded = OperationDetails(
            input="Сумма возврата", effect_kind="command", outcomes=["success"], questions=["Подтверждает ли шлюз?"],
            external_interactions=[ExternalInteraction(system="Платёжный шлюз", direction="request", endpoint=None,
                                                       contract="Запрос возврата", evidence_ids=["src-2"])],
            code_explanations=[CodeExplanation(symbol="handle_refund", explanation="Обработка уведомления",
                                               evidence_ids=["src-3"])], evidence_ids=["src-1"])
        operation = ProcessOperation(id="refund", name="Возврат", stage_id="payment", details=grounded)
        self.assertEqual(ProcessOperation.from_dict(operation.to_dict()), operation)
        rejected = {
            "text without evidence": lambda: OperationDetails(input="Сумма", evidence_ids=[]),
            "blank text": lambda: OperationDetails(input="  ", evidence_ids=["src-1"]),
            "effect without evidence": lambda: OperationDetails(effect_kind="result", evidence_ids=[]),
            "mode without evidence": lambda: OperationDetails(continuation="all", evidence_ids=[]),
            "unknown effect": lambda: OperationDetails(effect_kind="confirmed", evidence_ids=["src-1"]),
            "unknown outcome": lambda: OperationDetails(outcomes=["observed"], evidence_ids=["src-1"]),
            "external without endpoint or contract": lambda: ExternalInteraction(
                system="Шлюз", direction="request", endpoint=None, contract=None, evidence_ids=["src-1"]),
            "external without evidence": lambda: ExternalInteraction(
                system="Шлюз", direction="request", endpoint=None, contract="Возврат", evidence_ids=[]),
            "external direction": lambda: ExternalInteraction(
                system="Шлюз", direction="push", endpoint=None, contract="Возврат", evidence_ids=["src-1"]),
            "explanation without evidence": lambda: CodeExplanation(
                symbol="handle_refund", explanation="Обработка", evidence_ids=[]),
        }
        for label, build in rejected.items():
            with self.subTest(case=label), self.assertRaises(ContractError):
                build()
        payload = operation.to_dict()
        payload["details"]["observed"] = True
        with self.assertRaises(ContractError):
            ProcessOperation.from_dict(payload)
        # Nested evidence IDs resolve through the ordinary registry check, like every other evidence_ids field.
        registry = [EvidenceRef(id=key, kind=SourceKind.DOCUMENT, snapshot_id="doc", locator=key)
                    for key in ("src-1", "src-2", "src-3")]

        def process(item):
            return ProcessMap(run_id="saved", input_fingerprints={}, status=ArtifactStatus.DRAFT, evidence_ids=[],
                              purpose=None, boundaries=None, case_definition=None,
                              stages=[Stage(id="payment", name="Оплата")], transitions=[], event_mappings=[],
                              unknowns=[], operations=[item])
        validate_evidence_links(process(operation), registry)
        with self.assertRaises(ContractError):
            validate_evidence_links(process(operation), registry[:2])


class MetadataExportTests(InspectorCase):
    def exported(self, bundle, name="packet"):
        from process_observability.cli import _write_bundle
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        result = export_onboarding(self.out, self.out.parent / name)
        return result, json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))

    def test_export_checks_nested_details_against_the_process_map(self):
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt,
                                                                          with_details={"refund": REFUND_DETAILS}))
        self.assertComplete(bundle)
        process = ProcessMap.from_dict(bundle["process_map"])
        snapshot = bundle["atomic_map"]["catalog"]["snapshot_id"]
        _atomic_accounting(bundle, process, snapshot)
        refund = operation_ids(bundle)["refund"]

        def recorded(data):
            return next(op for op in data["atomic_map"]["operations"] if op["id"] == refund)
        edits = {"external direction": lambda data: recorded(data)["details"]["external_interactions"][0].update(
                     direction="send"),
                 "details evidence": lambda data: recorded(data)["details"].update(evidence_ids=[]),
                 "details text": lambda data: recorded(data)["details"].update(input="Другой вход"),
                 "details dropped": lambda data: recorded(data).update(details=None)}
        for label, edit in edits.items():
            with self.subTest(case=label):
                changed = copy.deepcopy(bundle)
                edit(changed)
                with self.assertRaises(ExportError):
                    _atomic_accounting(changed, process, snapshot)

    def test_the_event_dictionary_keeps_one_key_on_two_sites_apart_with_exact_facts(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        ids = operation_ids(bundle)
        _, document = self.exported(bundle)
        rows = [row for row in document["event_dictionary"]
                if row["row_kind"] == "mapped_key" and row["event_key"] == "refund.event"]
        self.assertEqual(len(rows), 2)
        by_operation = {tuple(row["operation_ids"]): row for row in rows}
        self.assertEqual(set(by_operation), {(ids["accept"],), (ids["refund"],)})
        self.assertEqual(by_operation[(ids["accept"],)]["fact_ids"], [fact_id(bundle, "refund.repeat")])
        self.assertEqual(by_operation[(ids["refund"],)]["fact_ids"], [fact_id(bundle, "refund.done")])
        self.assertNotEqual(rows[0]["source_refs"], rows[1]["source_refs"])

    def test_an_honest_partial_with_details_is_exported_and_its_saved_copy_is_checked(self):
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(
            brief, attempt, with_details={"refund": REFUND_DETAILS}, broken=("release_funds_task",)),
            atomic_page_units=1, triage="off")
        record = bundle["atomic_map"]
        self.assertEqual(record["status"], "partial")
        self.assertEqual([state["symbol"] for state in record["unit_states"].values()
                          if state["state"] == "request_failed"], ["release_funds_task"])
        result, document = self.exported(bundle)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(reason.startswith("Atomic map partial") for reason in document["partial_reasons"]))
        refund = operation_ids(bundle)["refund"]
        [exported] = [op for op in document["process_map"]["operations"] if op["id"] == refund]
        [original] = [op for op in bundle["process_map"]["operations"] if op["id"] == refund]
        self.assertIsNotNone(exported["details"])
        self.assertEqual(exported["details"], original["details"])
        saved = self.out / "atomic_map.json"
        data = json.loads(saved.read_text(encoding="utf-8"))
        next(op for op in data["operations"] if op["id"] == refund)["details"]["input"] = "Изменённый вход"
        saved.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ExportError):
            export_onboarding(self.out, self.out.parent / "packet-2")


if __name__ == "__main__":
    unittest.main()
