"""Bounded local recovery of invalid AUXILIARY operation details on atomic pages, after the one repair.

Synthetic refund fixture of test_operation_inspector; scripted models only, no live call. After the designated repair
(attempt 2) returned conforming JSON that still fails strict validation only because of external_without_contract or
explanation_symbol_not_in_source at their exact paths, the host omits exactly those optional items from a COPY, adds an
addressed question to that action and re-runs the complete shape and page validation. Every other failure keeps the
page rejected. Both rejected attempts and the raw repair hash stay recorded apart from the locally derived response.
A valid quote is still no semantic proof.
"""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from process_observability import direct_atomic, workflow
from process_observability.contracts import ProcessMap
from process_observability.llm import LLMError
from process_observability.onboarding_export import ExportError, _atomic_accounting, export_onboarding
from test_action_refinement import Code
from test_direct_atomic import Business, anchors, atomic_handlers
from test_operation_inspector import (EXPLANATION, EXTERNAL, FILES, FOUND, NAMES, REFUND_DETAILS, TASKS, WEBHOOK,
                                      InspectorCase, cards, details, metrics_reply, operation_ids, ref, refund_compose,
                                      refund_reply)

ROOT = Path(__file__).resolve().parents[1]
ATOMIC = "TASK: map-atomic"
# Synthetic analogues of the two auxiliary mistakes a real model repeated after its repair.
NO_CONTRACT = {**EXTERNAL, "contract": None}  # the receiver's endpoint and contract are genuinely unknown
FOREIGN_SYMBOL = {"symbol": "RefundGateway", "explanation": "Клиент шлюза выполняет возврат",
                  "refs": [ref(WEBHOOK, 13)]}  # line 13 only calls payment.refund(...); no such symbol there
MIXED = details(input="Уведомление и сумма возврата", expected_result="Сумма возвращена через шлюз",
                effect_kind="command", refs=[ref(WEBHOOK, 8, 13)], external_interactions=[NO_CONTRACT],
                code_explanations=[FOREIGN_SYMBOL, EXPLANATION], questions=["Подтверждает ли шлюз возврат?"])


def always(values):
    """The same reply on the first attempt and on the repair (fresh copies: a test may edit its reply)."""
    return lambda brief, attempt: refund_reply(brief, attempt, with_details=copy.deepcopy(values))


class Recording(Business):
    """Keeps every map-atomic reply object exactly as the host received it, with a private copy for comparison."""

    def __init__(self, **handlers):
        super().__init__(**handlers)
        self.returned = []

    def complete_json(self, system, user):
        reply = super().complete_json(system, user)
        if system.startswith(ATOMIC):
            self.returned.append((reply, copy.deepcopy(reply)))
        return reply


class RecoveryCase(InspectorCase):
    """Helpers only (no tests)."""

    def recovered_run(self, reply, **settings):
        # The shared composition, logging changes and metric of the refund fixture (fact IDs read by meaning).
        self.business = Recording(**atomic_handlers(map_atomic=reply, map_compose=refund_compose(),
                                                    instrumentation=cards(), metrics=metrics_reply))
        bundle, self.out = self.run_case(self.business, files=dict(FILES), found=copy.deepcopy(FOUND), **settings)
        return bundle

    def atomic_calls(self):
        return [call for call in self.business.calls if call["task"] == ATOMIC]

    @staticmethod
    def request_of(bundle, symbol="handle_refund"):
        unit = next(key for key, state in bundle["atomic_map"]["unit_states"].items() if state["symbol"] == symbol)
        [request] = [item for item in bundle["atomic_map"]["requests"]
                     if item["kind"] == "atomic" and unit in item["unit_ids"]]
        return request

    def details_of(self, bundle, key):
        identifier = operation_ids(bundle)[key]
        return next(op for op in bundle["process_map"]["operations"] if op["id"] == identifier)["details"]

    def exported(self, bundle, name):
        from process_observability.cli import _write_bundle
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        result = export_onboarding(self.out, self.out.parent / name)
        return result, json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))


class AuxiliaryRecoveryTests(RecoveryCase):
    def test_both_auxiliary_mistakes_after_repair_keep_the_valid_actions_with_explicit_omissions(self):
        bundle = self.recovered_run(always({"refund": MIXED}))
        self.assertEqual(self.business.tasks(), [ATOMIC, ATOMIC, "TASK: map-compose", "TASK: instrumentation",
                                                 "TASK: metrics"])  # exactly two API calls for the page
        first, repair = self.atomic_calls()
        [error] = repair["brief"].pop("validation_feedback")["errors"]  # attempt 1 still gets the exact feedback
        self.assertEqual(repair["brief"], first["brief"])
        self.assertEqual((error["code"], error["path"]),
                         ("external_without_contract", "operations[1].details.external_interactions[0]"))
        request = self.request_of(bundle)
        self.assertEqual(request["outcome"], "accepted_after_local_revalidation")
        self.assertEqual([(item["attempt"], item["outcome"], item["code"]) for item in request["attempts"]],
                         [(1, "rejected", "external_without_contract"), (2, "rejected", "external_without_contract")])
        local = request["local_revalidation"]
        self.assertEqual((local["outcome"], local["source"], local["omission_limit_reached"]),
                         ("accepted", "repair_response", False))
        self.assertEqual([(item["operation"], item["field"], item["original_index"], item["code"], item["path"])
                          for item in local["omissions"]],
                         [(1, "external_interactions", 0, "external_without_contract",
                           "operations[1].details.external_interactions[0]"),
                          (1, "code_explanations", 0, "explanation_symbol_not_in_source",
                           "operations[1].details.code_explanations[0].symbol")])
        # Every valid core action, unit decision, mapping and transition of the page is kept.
        self.assertEqual(bundle["atomic_map"]["status"], "complete")
        self.assertEqual(set(operation_ids(bundle)), {"accept", "refund", "release"})
        self.assertEqual(len(bundle["process_map"]["event_mappings"]), 7)
        self.assertEqual(len(bundle["process_map"]["operation_transitions"]), 1)
        self.assertEqual({state["state"] for state in bundle["atomic_map"]["unit_states"].values()}, {"accounted"})
        saved = self.details_of(bundle, "refund")
        self.assertEqual((saved["input"], saved["expected_result"], saved["effect_kind"]),
                         (MIXED["input"], MIXED["expected_result"], "command"))
        self.assertEqual(saved["external_interactions"], [])
        self.assertEqual([item["symbol"] for item in saved["code_explanations"]], ["handle_refund"])
        # Each omission is an addressed question of that action and one of the map's open questions.
        self.assertIn("Подтверждает ли шлюз возврат?", saved["questions"])
        for omission in local["omissions"]:
            self.assertIn(omission["question"], saved["questions"])
            self.assertIn(f"Операция «{NAMES['refund']}»: {omission['question']}", bundle["process_map"]["unknowns"])
        self.assertIn(EXTERNAL["system"], local["omissions"][0]["question"])
        self.assertIn("RefundGateway", local["omissions"][1]["question"])
        self.assertTrue(any("локально" in line for line in bundle["process_map"]["unknowns"]))
        self.assertEqual(bundle["atomic_map"]["summary"]["auxiliary_omissions"], 2)

    def test_the_raw_repair_response_stays_rejected_and_the_derived_response_is_bound_to_it(self):
        bundle = self.recovered_run(always({"refund": MIXED}))
        request = self.request_of(bundle)
        local, accepted = request["local_revalidation"], request["accepted"]
        (_, _), (raw, raw_copy) = self.business.returned
        self.assertEqual(raw, raw_copy)  # the received repair reply object was never mutated
        raw_hash = direct_atomic._digest(raw_copy)
        repair = self.atomic_calls()[1]
        repair_hash = hashlib.sha256((repair["system"] + "\n" + repair["user"]).encode("utf-8")).hexdigest()
        attempt = request["attempts"][1]
        self.assertEqual((attempt["response_sha256"], attempt["api_request_sha256"]), (raw_hash, repair_hash))
        self.assertNotEqual(request["attempts"][0]["api_request_sha256"], repair_hash)
        self.assertEqual((local["raw_response_sha256"], local["api_request_sha256"]), (raw_hash, repair_hash))
        self.assertNotEqual(local["sanitized_response_sha256"], raw_hash)
        self.assertEqual(accepted, {"attempt": 2, "api_request_sha256": repair_hash,
                                    "response_sha256": local["sanitized_response_sha256"],
                                    "derivation": "local_auxiliary_omission", "raw_response_sha256": raw_hash})
        # The derived reply is the raw one minus exactly the omitted items, plus the host questions.
        derived = copy.deepcopy(raw_copy)
        refund = derived["operations"][1]["details"]
        omitted = [refund["external_interactions"].pop(0), refund["code_explanations"].pop(0)]
        self.assertEqual([item["item_sha256"] for item in local["omissions"]],
                         [direct_atomic._digest(item) for item in omitted])
        refund["questions"] += [item["question"] for item in local["omissions"]]
        self.assertEqual(direct_atomic._digest(derived), local["sanitized_response_sha256"])
        # No citation of an omitted item becomes accepted evidence (line 13 was cited only by the two of them).
        cited = {(item["source_anchor"]["path"], item["source_anchor"]["start_line"], item["source_anchor"]["end_line"])
                 for item in bundle["evidence"] if item.get("source_anchor")}
        self.assertNotIn((WEBHOOK, 13, 13), cited)
        saved = self.details_of(bundle, "refund")
        self.assertEqual(anchors(bundle, saved["evidence_ids"]), {(WEBHOOK, 8, 13)})
        self.assertEqual(anchors(bundle, saved["code_explanations"][0]["evidence_ids"]), {(WEBHOOK, 6, 6)})

    def test_a_valid_repair_is_accepted_exactly_as_before(self):
        bundle = self.recovered_run(lambda brief, attempt: refund_reply(
            brief, attempt, with_details={"refund": MIXED if attempt == 1 else REFUND_DETAILS}))
        request = self.request_of(bundle)
        self.assertEqual(request["outcome"], "accepted_after_repair")
        self.assertNotIn("local_revalidation", request)
        self.assertNotIn("derivation", request["accepted"])
        self.assertEqual(request["accepted"]["response_sha256"], direct_atomic._digest(self.business.returned[1][1]))
        self.assertEqual(bundle["atomic_map"]["summary"]["auxiliary_omissions"], 0)
        [saved] = list((self.out / direct_atomic.CHECKPOINTS).glob("*.json"))
        self.assertNotIn("local_revalidation", json.loads(saved.read_text(encoding="utf-8")))
        # An earlier record without the new summary key still exports.
        earlier = copy.deepcopy(bundle)
        earlier["atomic_map"]["summary"].pop("auxiliary_omissions")
        _atomic_accounting(earlier, ProcessMap.from_dict(earlier["process_map"]),
                           earlier["atomic_map"]["catalog"]["snapshot_id"])

    def test_several_omissions_are_deterministic_and_bounded(self):
        two = details(external_interactions=[{**NO_CONTRACT, "system": "Шлюз А", "refs": [ref(WEBHOOK, 9)]},
                                             {**NO_CONTRACT, "system": "Шлюз Б", "refs": [ref(WEBHOOK, 9)]}])
        records = [self.request_of(self.recovered_run(always({"accept": two, "refund": MIXED})))["local_revalidation"]
                   for _ in range(2)]
        self.assertEqual(records[0]["omissions"], records[1]["omissions"])
        self.assertEqual(records[0]["sanitized_response_sha256"], records[1]["sanitized_response_sha256"])
        self.assertEqual([(item["operation"], item["field"], item["original_index"]) for item in records[0]["omissions"]],
                         [(0, "external_interactions", 0), (0, "external_interactions", 1),
                          (1, "external_interactions", 0), (1, "code_explanations", 0)])
        # More omittable items than the bound keep the page rejected; nothing of it is accepted.
        many = details(external_interactions=[{**NO_CONTRACT, "system": f"Шлюз {number}", "refs": [ref(WEBHOOK, 9)]}
                                              for number in range(direct_atomic.MAX_AUXILIARY_OMISSIONS + 1)])
        bundle = self.recovered_run(always({"accept": many}))
        request = self.request_of(bundle)
        local = request["local_revalidation"]
        self.assertEqual((request["outcome"], request["accepted"], local["outcome"], local["omission_limit_reached"],
                          local["sanitized_response_sha256"]), ("rejected_after_repair", None, "rejected", True, None))
        self.assertEqual(len(local["omissions"]), direct_atomic.MAX_AUXILIARY_OMISSIONS)
        self.assertEqual(len(self.atomic_calls()), 2)
        self.assertEqual({state["state"] for state in bundle["atomic_map"]["unit_states"].values()}, {"request_failed"})

    def test_core_source_identity_unit_and_stage_errors_stay_failed_even_with_omittable_items(self):
        def broken(edit):
            def reply(brief, attempt):
                result = always({"refund": MIXED})(brief, attempt)
                if attempt == 2:
                    edit(result)
                return result
            return reply
        cases = {  # edit of the repair -> final code after the omissions, or None when none is attempted
            "missing unit decision": (lambda reply: reply["unit_accounting"].pop(), "missing_units"),
            "unknown site": (lambda reply: reply["event_mappings"][0].update(site=99), "unknown_site"),
            "borrowed details source": (lambda reply: reply["operations"][1]["details"].update(refs=[ref(TASKS, 7, 10)]),
                                        "details_ref_outside_operation"),
            "other details error": (lambda reply: reply["operations"][1]["details"]["code_explanations"][0].update(refs=[]),
                                    "explanation_without_refs"),
            "unknown group first": (lambda reply: reply["operations"][1].update(group=99), None),
            "core ref outside supplied first": (lambda reply: reply["operations"][0].update(refs=[ref(WEBHOOK, 40, 41)]),
                                                None),
        }
        for label, (edit, final) in cases.items():
            with self.subTest(case=label):
                bundle = self.recovered_run(broken(edit))
                request = self.request_of(bundle)
                self.assertEqual((request["outcome"], request["accepted"]), ("rejected_after_repair", None))
                self.assertEqual(len(self.atomic_calls()), 2)
                if final is None:
                    self.assertNotIn("local_revalidation", request)
                else:
                    local = request["local_revalidation"]
                    self.assertEqual((local["outcome"], local["final_code"], local["sanitized_response_sha256"]),
                                     ("rejected", final, None))
                self.assertEqual(bundle["process_map"]["operations"], [])  # nothing of the page is committed
                self.assertEqual({state["state"] for state in bundle["atomic_map"]["unit_states"].values()},
                                 {"request_failed"})

    def test_transport_and_unparseable_replies_never_reach_local_revalidation(self):
        limited = LLMError("rate limited", category="http", status=429)
        unparseable = LLMError("no JSON object", category="invalid_json")

        def repair_with(failure):
            return lambda brief, attempt: (refund_reply(brief, attempt, with_details={"refund": MIXED})
                                           if attempt == 1 else failure)
        cases = {"first attempt transport": (lambda brief, attempt: limited, 1, "rejected"),
                 "repair transport": (repair_with(limited), 2, "rejected_after_repair"),
                 "repair without JSON": (repair_with(unparseable), 2, "rejected_after_repair")}
        for label, (reply, calls, outcome) in cases.items():
            with self.subTest(case=label):
                bundle = self.recovered_run(reply)
                request = self.request_of(bundle)
                self.assertEqual((len(self.atomic_calls()), request["outcome"], request["accepted"]),
                                 (calls, outcome, None))
                self.assertNotIn("local_revalidation", request)
                self.assertEqual(bundle["process_map"]["operations"], [])

    def test_only_the_two_codes_at_their_exact_paths_qualify(self):
        target, issue = direct_atomic._auxiliary_target, workflow.ProposalIssue
        self.assertEqual(target(issue("contract", "external_without_contract",
                                      "operations[2].details.external_interactions[1]")), (2, "external_interactions", 1))
        self.assertEqual(target(issue("contract", "explanation_symbol_not_in_source",
                                      "operations[0].details.code_explanations[3].symbol")), (0, "code_explanations", 3))
        for layer, code, path in (
                ("contract", "external_without_refs", "operations[0].details.external_interactions[0].refs"),
                ("contract", "explanation_without_refs", "operations[0].details.code_explanations[0].refs"),
                ("contract", "details_without_refs", "operations[0].details.refs"),
                ("reference", "details_ref_outside_operation", "operations[0].details.code_explanations[0].refs[0]"),
                ("contract", "external_without_contract", "operations[0].details.external_interactions[0].refs[0]"),
                ("contract", "explanation_symbol_not_in_source", "operations[0].details.code_explanations[0]"),
                ("schema", "external_without_contract", "operations[0].details.external_interactions[0]")):
            with self.subTest(code=code, path=path):
                self.assertIsNone(target(issue(layer, code, path)))
        self.assertIsNone(target(ValueError("not a proposal issue")))


class RecoveryProvenanceTests(RecoveryCase):
    def test_a_derived_checkpoint_is_reused_only_when_its_raw_reply_reproduces_it(self):
        first = self.recovered_run(always({"refund": MIXED}))
        source = self.out
        [path] = list((source / direct_atomic.CHECKPOINTS).glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        request, local = self.request_of(first), stored["local_revalidation"]
        self.assertEqual(set(stored), {"schema", "identity", "identity_fields", "accepted", "response_sha256", "response",
                                       "local_revalidation"})
        self.assertEqual(direct_atomic._digest(local["raw_response"]), request["local_revalidation"]["raw_response_sha256"])
        self.assertEqual(local["omissions"], request["local_revalidation"]["omissions"])
        self.assertEqual((stored["response_sha256"], stored["accepted"]),
                         (request["accepted"]["response_sha256"], request["accepted"]))
        # An exact replay is reused without a model call and stays labelled as derived.
        reused = self.recovered_run(always({"refund": MIXED}), resume_from=source)
        self.assertNotIn(ATOMIC, self.business.tasks())
        again = self.request_of(reused)
        self.assertEqual((again["outcome"], again["accepted"]), ("accepted_from_checkpoint", stored["accepted"]))
        self.assertEqual((again["local_revalidation"]["source"], again["local_revalidation"]["omissions"]),
                         ("checkpoint", local["omissions"]))
        self.assertEqual([op["id"] for op in reused["process_map"]["operations"]],
                         [op["id"] for op in first["process_map"]["operations"]])
        _atomic_accounting(reused, ProcessMap.from_dict(reused["process_map"]),
                           reused["atomic_map"]["catalog"]["snapshot_id"])

        def reseal_response(data):
            data["response_sha256"] = direct_atomic._digest(data["response"])
            data["accepted"]["response_sha256"] = data["response_sha256"]
            data["local_revalidation"]["sanitized_response_sha256"] = data["response_sha256"]

        def edit_response(data):
            data["response"]["operations"][1]["details"]["questions"].pop()
            reseal_response(data)

        def valid_raw(data):  # the raw reply replaced by an already valid one: nothing would be omitted
            local = data["local_revalidation"]
            local["raw_response"] = copy.deepcopy(data["response"])
            local["raw_response_sha256"] = direct_atomic._digest(local["raw_response"])
            data["accepted"]["raw_response_sha256"] = local["raw_response_sha256"]
        tampers = {"omission dropped": lambda data: data["local_revalidation"]["omissions"].pop(),
                   "derived reply edited and resealed": edit_response,
                   "raw reply replaced and resealed": valid_raw,
                   "derivation without its local record": lambda data: data.pop("local_revalidation")}
        for label, tamper in tampers.items():
            with self.subTest(tamper=label):
                data = copy.deepcopy(stored)
                tamper(data)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                bundle = self.recovered_run(always({"refund": MIXED}), resume_from=source)
                self.assertIn(ATOMIC, self.business.tasks())
                self.assertEqual(self.request_of(bundle)["outcome"], "accepted_after_local_revalidation")

    def test_export_checks_the_local_revalidation_provenance_and_partial_stays_partial(self):
        bundle = self.recovered_run(always({"refund": MIXED}))
        process, snapshot = ProcessMap.from_dict(bundle["process_map"]), bundle["atomic_map"]["catalog"]["snapshot_id"]
        _atomic_accounting(bundle, process, snapshot)
        request = self.request_of
        edits = {
            "derived response claimed as a raw success": lambda data: request(data).update(outcome="accepted_after_repair"),
            "accepted points at the raw reply": lambda data: request(data)["accepted"].update(
                response_sha256=request(data)["local_revalidation"]["raw_response_sha256"]),
            "raw hash detached from attempt 2": lambda data: request(data)["local_revalidation"].update(
                raw_response_sha256="0" * 64),
            "repair request detached": lambda data: request(data)["local_revalidation"].update(api_request_sha256="1" * 64),
            "omission removed": lambda data: request(data)["local_revalidation"]["omissions"].pop(),
            "omission of another kind": lambda data: request(data)["local_revalidation"]["omissions"][0].update(
                code="details_without_refs"),
            "summary count changed": lambda data: data["atomic_map"]["summary"].update(auxiliary_omissions=1),
            "local record dropped": lambda data: request(data).pop("local_revalidation"),
        }
        for label, edit in edits.items():
            with self.subTest(edit=label):
                changed = copy.deepcopy(bundle)
                edit(changed)
                with self.assertRaises(ExportError):
                    _atomic_accounting(changed, process, snapshot)
        _, document = self.exported(bundle, "packet")
        self.assertFalse(any(reason.startswith("Atomic map") for reason in document["partial_reasons"]))
        self.assertTrue(any("RefundGateway" in question for question in document["open_questions"]))
        partial = self.recovered_run(lambda brief, attempt: refund_reply(
            brief, attempt, with_details={"refund": MIXED}, broken=("release_funds_task",)),
            atomic_page_units=1, triage="off")
        self.assertEqual((partial["atomic_map"]["status"], self.request_of(partial)["outcome"]),
                         ("partial", "accepted_after_local_revalidation"))
        result, document = self.exported(partial, "packet-partial")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(reason.startswith("Atomic map partial") for reason in document["partial_reasons"]))

    def test_business_identity_changes_while_the_technical_review_is_reused(self):
        self.assertNotEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/3")
        valid = lambda brief, attempt: refund_reply(brief, attempt, with_details={"refund": REFUND_DETAILS})
        with patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/3"):
            earlier = self.recovered_run(valid)
        source, code = self.out, Code(copy.deepcopy(FOUND))
        current = self.recovered_run(valid, code=code, resume_from=source)
        self.assertEqual(code.calls, [])  # technical review and reduction reused exactly
        self.assertIn(ATOMIC, self.business.tasks())  # pages of the earlier business identity are no hit
        self.assertNotEqual(earlier["manifest"]["provenance"]["config_sha256"],
                            current["manifest"]["provenance"]["config_sha256"])
        recovery = current["atomic_map"]["settings"]["auxiliary_recovery"]
        self.assertEqual((recovery["codes"], recovery["after_attempt"], recovery["max_omissions"]),
                         (["explanation_symbol_not_in_source", "external_without_contract"], 2,
                          direct_atomic.MAX_AUXILIARY_OMISSIONS))


class UntouchedTests(unittest.TestCase):
    def test_the_strict_demo_replies_and_legacy_prompts_are_unchanged(self):
        from test_operation_metadata import R07_DEMO_REPLIES
        data = (ROOT / "examples/synthetic/model-responses.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), R07_DEMO_REPLIES)
        self.assertNotIn("details", workflow._OPERATION)
        self.assertNotIn("local_revalidation", workflow.MAP_SYSTEM)


if __name__ == "__main__":
    unittest.main()
