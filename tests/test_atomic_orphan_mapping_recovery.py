"""Bounded local recovery of an UNASSIGNED supplied event site after the one real repair of an atomic page.

Tests-first; own synthetic fixtures and scripted models only. After the repair (attempt 2) still lists a supplied,
keyed event site with new_operation null and known_operation null (and any role or refs valid on their own; code-led
pages carry no facts), the host omits exactly that event_mappings entry from a COPY of the reply, adds an addressed
question with the site's identity to the reply's unknowns and re-runs the complete validation. The site stays in the
full logging inventory without that mapping; nothing is inferred, no fact is closed. Both rejected attempts, their
hashes and the repair request stay recorded apart from the derived reply. Everything else stays strict: a fact claim
(no page field), an unsupplied or unkeyed site, an unsupplied ref, two targets, a role without basis, a transition,
unit, group or action error.
"""

import copy
import hashlib
import html
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from process_observability import direct_atomic
from process_observability.action_catalog import _site
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import ExportError, _atomic_accounting, export_onboarding
from test_action_refinement import Code
from test_atomic_details_recovery import MIXED, RecoveryCase
from test_direct_atomic import AtomicCase, Business, atomic_handlers, atomic_reply, atomic_requests, first_attempts
from test_operation_inspector import refund_reply

ROOT = Path(__file__).resolve().parents[1]
ATOMIC = "TASK: map-atomic"
SITE_FIELD = "event_mappings"


def orphan_of(mapping, **changes):
    """The reproduced orphan: a copy of a valid mapping with both targets null (code-led pages carry no facts)."""
    orphan = copy.deepcopy(mapping)
    orphan.update(new_operation=None, known_operation=None, lifecycle_role="unknown", role_basis=None,
                  refs=[], ambiguity="Not assigned to an operation")
    orphan.update(changes)
    return orphan


def with_orphans(count=1, attempts=(1, 2), replace_first=False, edit=None, **changes):
    """atomic_reply plus orphans on the given attempts; edit(payload, brief) adds a further change to every attempt."""
    def build(brief, attempt):
        payload = atomic_reply(brief, attempt)
        if attempt in attempts and payload["event_mappings"]:
            orphans = [orphan_of(payload["event_mappings"][0], **changes) for _ in range(count)]
            if replace_first:
                payload["event_mappings"][0] = orphans.pop()
            payload["event_mappings"] += orphans
        if edit is not None:
            edit(payload, brief)
        return payload
    return build


def edited(edit):
    """atomic_reply with edit(payload, brief) applied to every attempt (no orphan)."""
    def build(brief, attempt):
        payload = atomic_reply(brief, attempt)
        edit(payload, brief)
        return payload
    return build


class Recording(Business):
    """Keeps every map-atomic reply object as handed to the host, with a private copy for comparison."""

    def __init__(self, **handlers):
        super().__init__(**handlers)
        self.returned = []

    def complete_json(self, system, user):
        reply = super().complete_json(system, user)
        if system.startswith(ATOMIC):
            self.returned.append((reply, copy.deepcopy(reply)))
        return reply


class OrphanCase(AtomicCase):
    """Helpers only. One page: atomic_page_units=16 puts every unit of the default fixture on it."""

    def paged(self, reply, business=None, **settings):
        settings.setdefault("atomic_page_units", 16)
        settings.setdefault("triage", "off")
        self.business = business or Business(**atomic_handlers(map_atomic=reply))
        bundle, self.out = self.run_case(self.business, **settings)
        return bundle

    def atomic_calls(self):
        return [call for call in self.business.calls if call["task"] == ATOMIC]

    @staticmethod
    def only_request(bundle):
        [request] = atomic_requests(bundle)
        return request

    def exported(self, bundle, name):
        from process_observability.cli import _write_bundle
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        return export_onboarding(self.out, self.out.parent / name)


class OrphanMappingRecoveryReproduction(AtomicCase):
    """The independent RED of the review fixture, unchanged in substance (default page size, triage off)."""

    def reply(self, *, claimed_fact=False, invalid_site=False):
        def build(brief, attempt):
            payload = atomic_reply(brief, attempt)
            if payload["event_mappings"]:
                orphan = copy.deepcopy(payload["event_mappings"][0])
                orphan.update(new_operation=None, known_operation=None, lifecycle_role="unknown", role_basis=None,
                              refs=[], ambiguity="Not assigned to an operation")
                if claimed_fact:  # a fact claim is no page field any more: the shape check rejects it first
                    orphan["facts"] = [1]
                if invalid_site:
                    orphan["site"] = 999999
                payload["event_mappings"].append(orphan)
            return payload
        return build

    def test_after_real_repair_an_unassigned_fact_free_site_does_not_discard_other_valid_actions(self):
        bundle, _ = self.run_case(Business(**atomic_handlers(map_atomic=self.reply())), triage="off")
        rejected = [item for item in bundle["atomic_map"]["requests"] if item["outcome"] == "rejected_after_repair"]
        self.assertEqual(rejected, [])
        self.assertTrue(bundle["process_map"]["operations"])
        self.assertTrue(all(item.get("operation_id") for item in bundle["process_map"]["event_mappings"]))

    def test_an_orphan_that_claims_a_fact_is_not_safely_omittable(self):
        bundle, _ = self.run_case(Business(**atomic_handlers(map_atomic=self.reply(claimed_fact=True))), triage="off")
        self.assertTrue(any(item["outcome"] == "rejected_after_repair" for item in bundle["atomic_map"]["requests"]))

    def test_an_unsupplied_site_is_not_safely_omittable(self):
        bundle, _ = self.run_case(Business(**atomic_handlers(map_atomic=self.reply(invalid_site=True))), triage="off")
        self.assertTrue(any(item["outcome"] == "rejected_after_repair" for item in bundle["atomic_map"]["requests"]))


class UnassignedSiteRecoveryTests(OrphanCase):
    def test_every_valid_action_mapping_and_decision_survives_and_the_omission_is_explicit(self):
        baseline = self.paged(atomic_reply)
        recovered = self.paged(with_orphans())
        self.assertEqual(len(self.atomic_calls()), 2)  # the first attempt got its feedback; no third call
        first, repair = self.atomic_calls()
        [error] = repair["brief"].pop("validation_feedback")["errors"]
        self.assertEqual(repair["brief"], first["brief"])
        self.assertEqual(error["code"], "operation_choice")
        request = self.only_request(recovered)
        self.assertEqual(request["outcome"], "accepted_after_local_revalidation")
        self.assertEqual([(item["attempt"], item["outcome"], item["code"]) for item in request["attempts"]],
                         [(1, "rejected", "operation_choice"), (2, "rejected", "operation_choice")])
        self.assertEqual(request["attempts"][1]["path"], error["path"])
        [omission] = request["local_revalidation"]["omissions"]
        self.assertEqual((omission["field"], omission["code"], omission["path"]),
                         (SITE_FIELD, "operation_choice", error["path"]))
        self.assertIn(omission["site_id"], request["site_ids"])
        # Every otherwise valid action, mapping and unit decision is exactly the clean run's.
        self.assertEqual(recovered["process_map"]["operations"], baseline["process_map"]["operations"])
        self.assertEqual(recovered["process_map"]["event_mappings"], baseline["process_map"]["event_mappings"])
        self.assertEqual({key: (state["state"], state["disposition"], state["operation_ids"])
                          for key, state in recovered["atomic_map"]["unit_states"].items()},
                         {key: (state["state"], state["disposition"], state["operation_ids"])
                          for key, state in baseline["atomic_map"]["unit_states"].items()})
        self.assertTrue(all(item.get("operation_id") for item in recovered["process_map"]["event_mappings"]))
        self.assertEqual(recovered["atomic_map"]["summary"]["auxiliary_omissions"], 1)
        # The duplicate entry's site keeps its other mapping; the question says so instead of claiming it is unmapped.
        self.assertIn("другие привязки этого места сохранены", omission["question"])
        self.assertIn(omission["question"], recovered["process_map"]["unknowns"])

    def test_an_unassigned_site_stays_in_the_inventory_without_a_mapping_and_with_an_addressed_question(self):
        from process_observability.report import build_html
        from process_observability.report_sources import collect_report_sources
        from process_observability.source_tools import snapshot_repository
        bundle = self.paged(with_orphans(replace_first=True))
        request = self.only_request(bundle)
        [omission] = request["local_revalidation"]["omissions"]
        [page] = first_attempts(self.business)
        site = page["brief"]["sites"][request["site_ids"].index(omission["site_id"])]  # site_ids are in page order
        self.assertNotIn(omission["site_id"], {item["site_id"] for item in bundle["atomic_map"]["event_mappings"]})
        self.assertIn(omission["site_id"], {_site(item)["id"] for item in bundle["logging_inventory"]["sites"]})
        for part in (omission["site_id"], f"{site['path']}:{site['start_line']}", site["event_key"],
                     "без привязки к операции"):
            self.assertIn(part, omission["question"])
        self.assertIn(omission["question"], bundle["process_map"]["unknowns"])
        self.assertTrue(any("мест логирования без операции и без фактов — 1" in line
                            for line in bundle["process_map"]["unknowns"]))
        bundle["report_sources"] = collect_report_sources(
            snapshot_repository(self.out.parent / "repo", max_files=None, max_chars=None), bundle)
        self.assertIn(html.escape(omission["question"]), build_html(bundle))

    def test_a_role_claim_is_omittable_only_with_its_basis_and_supplied_refs(self):
        def cite_site(payload, brief):
            orphan = payload["event_mappings"][-1]
            site = brief["sites"][orphan["site"] - 1]
            orphan.update(lifecycle_role="result", role_basis="Запись после сохранения",
                          refs=[{"path": site["path"], "start_line": site["start_line"], "end_line": site["end_line"]}])
        bundle = self.paged(with_orphans(edit=cite_site))
        self.assertEqual(self.only_request(bundle)["outcome"], "accepted_after_local_revalidation")

    def test_the_repair_comes_first_and_a_valid_repair_needs_no_derivation(self):
        bundle = self.paged(with_orphans(attempts=(1,)))
        request = self.only_request(bundle)
        self.assertEqual((request["outcome"], len(self.atomic_calls())), ("accepted_after_repair", 2))
        self.assertNotIn("local_revalidation", request)
        self.assertNotIn("derivation", request["accepted"])
        [error] = self.atomic_calls()[1]["brief"]["validation_feedback"]["errors"]
        self.assertEqual(error["code"], "operation_choice")
        self.assertEqual(self.only_request(self.paged(atomic_reply))["outcome"], "accepted")

    def test_the_omission_count_is_bounded_and_one_more_keeps_the_page_rejected(self):
        bound = direct_atomic.MAX_AUXILIARY_OMISSIONS
        within = self.only_request(self.paged(with_orphans(count=bound)))
        self.assertEqual((within["outcome"], len(within["local_revalidation"]["omissions"])),
                         ("accepted_after_local_revalidation", bound))
        bundle = self.paged(with_orphans(count=bound + 1))
        request = self.only_request(bundle)
        local = request["local_revalidation"]
        self.assertEqual((request["outcome"], request["accepted"], local["outcome"], local["omission_limit_reached"],
                          len(local["omissions"]), local["final_code"]),
                         ("rejected_after_repair", None, "rejected", True, bound, "operation_choice"))
        self.assertEqual(len(self.atomic_calls()), 2)
        self.assertEqual(bundle["process_map"]["operations"], [])
        _atomic_accounting(bundle, ProcessMap.from_dict(bundle["process_map"]),
                           bundle["atomic_map"]["catalog"]["snapshot_id"])


class StrictNegativeTests(OrphanCase):
    def assertRejectedWithoutDerivation(self, bundle, code):
        request = self.only_request(bundle)
        self.assertEqual((request["outcome"], request["accepted"]), ("rejected_after_repair", None))
        self.assertEqual([item["code"] for item in request["attempts"]], [code, code])
        self.assertNotIn("local_revalidation", request)
        self.assertEqual(len(self.atomic_calls()), 2)
        self.assertEqual(bundle["process_map"]["operations"], [])
        self.assertEqual({state["state"] for state in bundle["atomic_map"]["unit_states"].values()}, {"request_failed"})

    def test_core_claims_and_unsafe_orphans_are_never_omitted(self):
        def header_ref(payload, brief):
            site = brief["sites"][payload["event_mappings"][-1]["site"] - 1]
            payload["event_mappings"][-1]["refs"] = [{"path": site["path"], "start_line": 1, "end_line": 2}]

        def null_transition(payload, brief):
            payload["operation_transitions"].append({"from_new": None, "from_known": None, "to_new": 1,
                                                     "to_known": None, "condition": None, "basis": "source_condition",
                                                     "contract": None, "basis_note": "Синтетический порядок",
                                                     "refs": []})
        cases = {  # label: (reply, code of the first strict failure on both attempts)
            "a fact claim, no page field any more": (with_orphans(facts=[1]), "unexpected_keys"),
            "an unsupplied site": (with_orphans(site=999999), "unknown_site"),
            "both targets set": (with_orphans(new_operation=1, known_operation=1), "operation_choice"),
            "a role without its basis": (with_orphans(lifecycle_role="result"), "operation_choice"),
            "an orphan ref outside the supplied lines": (with_orphans(edit=header_ref), "operation_choice"),
            "a transition without a source": (edited(null_transition), "operation_choice"),
            "an unknown unit beside an orphan": (with_orphans(
                edit=lambda payload, brief: payload["unit_accounting"][0].update(unit=99)), "unknown_unit"),
            "a missing unit decision beside an orphan": (with_orphans(
                edit=lambda payload, brief: payload["unit_accounting"].pop()), "missing_units"),
            "an unknown group beside an orphan": (with_orphans(
                edit=lambda payload, brief: payload["operations"][0].update(group=99)), "unknown_group"),
            "an unknown action number beside an orphan": (with_orphans(
                edit=lambda payload, brief: payload["unit_accounting"][0].update(new_operations=[99])),
                "unknown_new_operation"),
        }
        for label, (reply, code) in cases.items():
            with self.subTest(case=label):
                self.assertRejectedWithoutDerivation(self.paged(reply), code)

    def test_a_core_error_after_an_omittable_orphan_still_rejects_the_page_after_revalidation(self):
        def bad_site(payload, brief):
            wrong = copy.deepcopy(payload["event_mappings"][0])
            wrong["site"] = 999999
            payload["event_mappings"].append(wrong)  # after the orphan: reached only by the complete revalidation
        bundle = self.paged(with_orphans(edit=bad_site))
        request = self.only_request(bundle)
        local = request["local_revalidation"]
        self.assertEqual((request["outcome"], request["accepted"], local["outcome"], len(local["omissions"]),
                          local["final_code"], local["sanitized_response_sha256"]),
                         ("rejected_after_repair", None, "rejected", 1, "unknown_site", None))
        self.assertEqual(bundle["process_map"]["operations"], [])


class ProvenanceTests(OrphanCase):
    def test_the_raw_repair_stays_rejected_and_the_derived_reply_is_bound_to_it(self):
        business = Recording(**atomic_handlers(map_atomic=with_orphans()))
        bundle = self.paged(None, business=business)
        request = self.only_request(bundle)
        local, accepted = request["local_revalidation"], request["accepted"]
        (_, _), (raw, raw_copy) = business.returned
        self.assertEqual(raw, raw_copy)  # the received repair reply was never mutated
        raw_hash = direct_atomic._digest(raw_copy)
        repair = self.atomic_calls()[1]
        repair_hash = hashlib.sha256((repair["system"] + "\n" + repair["user"]).encode("utf-8")).hexdigest()
        self.assertEqual((request["attempts"][1]["response_sha256"], request["attempts"][1]["api_request_sha256"]),
                         (raw_hash, repair_hash))
        self.assertNotEqual(request["attempts"][0]["api_request_sha256"], repair_hash)
        self.assertEqual(accepted, {"attempt": 2, "api_request_sha256": repair_hash,
                                    "response_sha256": local["sanitized_response_sha256"],
                                    "derivation": "local_auxiliary_omission", "raw_response_sha256": raw_hash})
        [omission] = local["omissions"]
        derived = copy.deepcopy(raw_copy)
        self.assertEqual(omission["item_sha256"],
                         direct_atomic._digest(derived["event_mappings"].pop(omission["original_index"])))
        derived["unknowns"].append(omission["question"])
        self.assertEqual(direct_atomic._digest(derived), local["sanitized_response_sha256"])
        self.assertNotEqual(local["sanitized_response_sha256"], raw_hash)

    def test_a_derived_checkpoint_is_reused_only_when_its_raw_reply_reproduces_it(self):
        first = self.paged(with_orphans())
        source = self.out
        [path] = list((source / direct_atomic.CHECKPOINTS).glob("*.json"))
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["local_revalidation"]["omissions"],
                         self.only_request(first)["local_revalidation"]["omissions"])
        reused = self.paged(with_orphans(), resume_from=source)
        self.assertNotIn(ATOMIC, self.business.tasks())
        again = self.only_request(reused)
        self.assertEqual((again["outcome"], again["local_revalidation"]["source"]),
                         ("accepted_from_checkpoint", "checkpoint"))
        self.assertEqual(reused["process_map"]["operations"], first["process_map"]["operations"])
        _atomic_accounting(reused, ProcessMap.from_dict(reused["process_map"]),
                           reused["atomic_map"]["catalog"]["snapshot_id"])

        def reseal(data):
            data["response_sha256"] = direct_atomic._digest(data["response"])
            data["accepted"]["response_sha256"] = data["response_sha256"]
            data["local_revalidation"]["sanitized_response_sha256"] = data["response_sha256"]

        def question_dropped(data):
            data["response"]["unknowns"].pop()
            reseal(data)

        def raw_replaced(data):
            local = data["local_revalidation"]
            local["raw_response"] = copy.deepcopy(data["response"])
            local["raw_response_sha256"] = direct_atomic._digest(local["raw_response"])
            data["accepted"]["raw_response_sha256"] = local["raw_response_sha256"]

        def site_changed(data):
            data["local_revalidation"]["omissions"][0]["site_id"] = "log-0000000000000000"
        tampers = {"omission dropped": lambda data: data["local_revalidation"]["omissions"].pop(),
                   "site identity changed": site_changed, "derived question dropped": question_dropped,
                   "raw reply replaced": raw_replaced}
        for label, tamper in tampers.items():
            with self.subTest(tamper=label):
                data = copy.deepcopy(stored)
                tamper(data)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                bundle = self.paged(with_orphans(), resume_from=source)
                self.assertIn(ATOMIC, self.business.tasks())
                self.assertEqual(self.only_request(bundle)["outcome"], "accepted_after_local_revalidation")


class ExportTests(OrphanCase):
    def test_export_checks_the_site_omission_and_its_declared_policy(self):
        bundle = self.paged(with_orphans())
        process, snapshot = ProcessMap.from_dict(bundle["process_map"]), bundle["atomic_map"]["catalog"]["snapshot_id"]
        _atomic_accounting(bundle, process, snapshot)
        self.exported(copy.deepcopy(bundle), "packet")

        def omission(data):
            return self.only_request(data)["local_revalidation"]["omissions"][0]
        edits = {"foreign site": lambda data: omission(data).update(site_id="log-0000000000000000"),
                 "another field": lambda data: omission(data).update(field="external_interactions"),
                 "another code": lambda data: omission(data).update(code="unknown_site"),
                 "path of another field": lambda data: omission(data).update(path="event_mappings[0].known_operation"),
                 "blank question": lambda data: omission(data).update(question=" "),
                 "undeclared policy": lambda data: data["atomic_map"]["settings"]["auxiliary_recovery"].pop(
                     "unassigned_sites"),
                 "count changed": lambda data: data["atomic_map"]["summary"].update(auxiliary_omissions=0)}
        for label, edit in edits.items():
            with self.subTest(edit=label):
                changed = copy.deepcopy(bundle)
                edit(changed)
                with self.assertRaises(ExportError):
                    _atomic_accounting(changed, process, snapshot)


class LegacyAndMixedTests(RecoveryCase):
    def test_a_saved_record_with_only_details_omissions_and_no_site_policy_still_exports(self):
        bundle = self.recovered_run(lambda brief, attempt: refund_reply(
            brief, attempt, with_details={"refund": copy.deepcopy(MIXED)}))
        old = copy.deepcopy(bundle)
        old["atomic_map"]["settings"]["auxiliary_recovery"].pop("unassigned_sites")
        old["atomic_map"]["settings"]["validator"] = "direct-atomic-validator/4"
        _atomic_accounting(old, ProcessMap.from_dict(old["process_map"]), old["atomic_map"]["catalog"]["snapshot_id"])

    def test_details_and_an_unassigned_site_are_omitted_together_in_validation_order(self):
        def reply(brief, attempt):
            payload = refund_reply(brief, attempt, with_details={"refund": copy.deepcopy(MIXED)})
            payload["event_mappings"].append(orphan_of(payload["event_mappings"][0]))
            return payload
        bundle = self.recovered_run(reply)
        request = self.request_of(bundle)
        self.assertEqual(request["outcome"], "accepted_after_local_revalidation")
        self.assertEqual([item["code"] for item in request["local_revalidation"]["omissions"]],
                         ["external_without_contract", "explanation_symbol_not_in_source", "operation_choice"])
        self.assertEqual(bundle["atomic_map"]["summary"]["auxiliary_omissions"], 3)
        self.assertEqual(len([call for call in self.business.calls if call["task"] == ATOMIC]), 2)
        _atomic_accounting(bundle, ProcessMap.from_dict(bundle["process_map"]),
                           bundle["atomic_map"]["catalog"]["snapshot_id"])


class IdentityTests(OrphanCase):
    def test_the_new_semantics_invalidate_business_checkpoints_but_reuse_the_technical_review(self):
        # /10 (transition support) is the current identity; a /5 page is still no checkpoint hit.
        self.assertEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/11")
        with patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/5"):
            earlier = self.paged(atomic_reply)
        source = self.out
        code = Code(copy.deepcopy(self.code.findings))
        current = self.paged(atomic_reply, code=code, resume_from=source)
        self.assertEqual(code.calls, [])  # technical review and reduction reused exactly
        self.assertIn(ATOMIC, self.business.tasks())  # an accepted page of the earlier semantics is no hit
        self.assertNotEqual(earlier["manifest"]["provenance"]["config_sha256"],
                            current["manifest"]["provenance"]["config_sha256"])
        recovery = current["atomic_map"]["settings"]["auxiliary_recovery"]
        self.assertEqual(recovery["codes"], ["explanation_symbol_not_in_source", "external_without_contract"])
        self.assertEqual(recovery["unassigned_sites"]["code"], "operation_choice")

    def test_the_strict_recorded_demo_replies_are_unchanged(self):
        from test_operation_metadata import R07_DEMO_REPLIES
        data = (ROOT / "examples/synthetic/model-responses.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), R07_DEMO_REPLIES)


if __name__ == "__main__":
    unittest.main()
