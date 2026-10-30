"""Exact no-model reuse of replies a prior local run already captured (--resume-from), bounded and fail-closed.

Own scripted prior runs and traces only (the code-led page replies of test_declaration_completion.decide); no target,
network or execution. The code-led map sends no skeleton request, so no earlier
skeleton is ever replayed. A prior page is reused only for exactly the same request bytes: the fixture writes its prior
run with the current page prompt and labels it as direct-atomic-validator/7 wrote its record (atomic-map/1, no wire
decoding), which exercises the exact-bytes lookup; a prior page sent under another page prompt, which every real
earlier run used, is always a miss and is asked for real. Any binding difference or damaged, ambiguous, oversized or
symlinked input is a miss. A recorded reply proves only that the same bytes were recorded locally, never provider
authenticity or semantic truth.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from process_observability import business_replay, direct_atomic
from process_observability.action_refinement import _digest
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import _atomic_accounting
from process_observability.tracing import TraceRecorder
from test_action_refinement import Code
from test_declaration_completion import FILES, FOUND, CompletionCase, decide, handlers
from test_direct_atomic import Business, atomic_requests

ROOT = Path(__file__).resolve().parents[1]
ATOMIC, SKELETON = "TASK: map-atomic", "TASK: map-skeleton"
V7 = "direct-atomic-validator/7"
V9 = "direct-atomic-validator/9"
# An earlier page prompt: the same task line, other bytes (earlier runs assigned actions to skeleton stages).
EARLIER_PROMPT = direct_atomic.ATOMIC_SYSTEM + "\nSynthetic earlier wording: put every action into a supplied stage."


def stripped(exclusion=()):
    """decide() with the exclusion key removed from accounting items of the given dispositions, as sent."""
    def reply(brief, attempt):
        result = decide(brief, attempt)
        for item in result["unit_accounting"]:
            if item["disposition"] in exclusion:
                item.pop("exclusion")
        return result
    return reply


def as_v7_record(directory):
    """Label a saved record as direct-atomic-validator/7 wrote it (atomic-map/1); its pages keep their exact bytes."""
    path = Path(directory) / "atomic_map.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["schema"] = "atomic-map/1"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def events(directory):
    return [json.loads(line) for line in (Path(directory) / "trace.jsonl").read_text(encoding="utf-8").splitlines()]


def files_of(directory):
    return {path.relative_to(directory): path.read_bytes() for path in Path(directory).rglob("*") if path.is_file()}


class ReplayCase(CompletionCase):
    def traced(self, reply, **settings):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        recorder = TraceRecorder(directory.name)
        self.business = Business(**handlers(map_atomic=reply))
        bundle, self.out = self.run_case(self.business, files=dict(FILES), found=copy.deepcopy(FOUND), trace=recorder,
                                         **settings)
        shutil.copyfile(recorder.path, self.out / "trace.jsonl")  # the CLI writes its trace into the run directory
        return bundle

    def prior_v7(self, reply, *, prompt=None, **settings):
        """A prior run labelled as direct-atomic-validator/7 wrote it (no wire decoding, atomic-map/1 record), its
        pages sent with prompt (default: the current page prompt, so that the exact-bytes lookup can hit)."""
        with patch.object(direct_atomic, "VALIDATOR_VERSION", V7), \
                patch.object(direct_atomic, "_wire_defaults", lambda value, units: (value, [])), \
                patch.object(direct_atomic, "ATOMIC_SYSTEM", prompt or direct_atomic.ATOMIC_SYSTEM):
            bundle = self.traced(reply, **settings)
        as_v7_record(self.out)
        return bundle

    def calls(self, task=ATOMIC):
        return [call for call in self.business.calls if call["task"] == task]

    @staticmethod
    def first_failed(bundle):
        return next(item for item in atomic_requests(bundle) if item["outcome"] == "rejected_after_repair")


class RecoveryTests(ReplayCase):
    def test_a_page_failed_only_for_inapplicable_nulls_is_reused_for_exactly_the_same_request(self):
        prior = self.prior_v7(stripped(exclusion=("supporting",)))
        prior_out = self.out
        by_hash = {item["api_request_sha256"]: item for item in atomic_requests(prior)}
        failed = self.first_failed(prior)
        self.assertEqual((failed["attempts"][1]["code"], failed["attempts"][1]["missing_keys"]),
                         ("missing_keys", ["exclusion"]))
        before = files_of(prior_out)
        code = Code(copy.deepcopy(FOUND))
        current = self.traced(stripped(exclusion=("supporting",)), code=code, resume_from=prior_out)
        self.assertEqual(code.calls, [])  # the technical review is reused as before
        self.assertNotIn(SKELETON, self.business.tasks())  # no frame request exists to be replayed
        requests = atomic_requests(current)
        # The pages before the first failed one are byte-identical in both runs, so that page is recovered; every
        # later page whose request changed (other known operations) is asked for real and accepted.
        [recovered] = [item for item in requests if item["api_request_sha256"] == failed["api_request_sha256"]]
        self.assertEqual(recovered["outcome"], "accepted_from_checkpoint")
        for item in requests:
            if "recovered" in item:
                self.assertIn(item["api_request_sha256"], by_hash)
                self.assertEqual(item["recovered"]["prior_request_id"], by_hash[item["api_request_sha256"]]["id"])
            else:
                self.assertEqual(item["outcome"], "accepted")
        self.assertEqual(len(self.calls()), sum("recovered" not in item for item in requests))
        original = failed["attempts"][1]
        self.assertEqual(recovered["accepted"], {"attempt": 2, "api_request_sha256": original["api_request_sha256"],
                                                 "response_sha256": original["response_sha256"]})
        self.assertEqual(recovered["attempts"], [{"attempt": 0, "outcome": "accepted_from_checkpoint",
                                                  "api_request_sha256": failed["api_request_sha256"],
                                                  "response_sha256": original["response_sha256"],
                                                  "original": recovered["accepted"]}])
        source = recovered["recovered"]
        self.assertEqual((source["prior_validator"], source["prior_request_id"], source["prior_outcome"],
                          source["original_attempt"], source["original_api_request_sha256"],
                          source["original_response_sha256"], source["revalidated_under"]),
                         (V7, failed["id"], "rejected_after_repair", 2, original["api_request_sha256"],
                          original["response_sha256"], direct_atomic.VALIDATOR_VERSION))
        self.assertEqual(source["prior_final_issue"], {"layer": "schema", "code": "missing_keys",
                                                       "path": original["path"], "missing_keys": ["exclusion"]})
        self.assertEqual(source["trace_sha256"], hashlib.sha256((prior_out / "trace.jsonl").read_bytes()).hexdigest())
        self.assertTrue(recovered["wire_decoding"]["defaults"])
        self.assertTrue(all(path.endswith(".exclusion") for path in recovered["wire_decoding"]["defaults"]))
        record = current["atomic_map"]
        self.assertEqual(({state["state"] for state in record["unit_states"].values()}, record["status"]),
                         ({"accounted"}, "complete"))
        self.assertNotIn("replayed_response", current["validation"]["stages"]["map"])
        names = [row["event"] for row in events(self.out)]
        self.assertIn("direct_atomic.prior_reply_found", names)
        self.assertNotIn("business_replay.skeleton_hit", names)
        # The recorded reply joins this run as a checkpoint, with where it came from; prior files are untouched.
        saved = json.loads((self.out / direct_atomic.CHECKPOINTS /
                            f"{recovered['checkpoint']['identity']}.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["response_sha256"], saved["recovered"]["original_response_sha256"]),
                         (original["response_sha256"], original["response_sha256"]))
        self.assertEqual(_digest(saved["response"]), original["response_sha256"])
        self.assertEqual(files_of(prior_out), before)
        _atomic_accounting(current, ProcessMap.from_dict(current["process_map"]), record["catalog"]["snapshot_id"])
        result, document = self.exported(current, "packet")
        self.assertIn(result["status"], ("all_stages_completed", "partial"))
        self.assertFalse([text for text in document["partial_reasons"] if text.startswith("Atomic map")])
        # A run resuming from this one reuses its checkpoints with the same provenance.
        again = self.traced(stripped(exclusion=("supporting",)), resume_from=self.out)
        self.assertEqual(self.calls(), [])
        [first] = [item for item in atomic_requests(again) if item["api_request_sha256"] == failed["api_request_sha256"]]
        self.assertEqual((first["outcome"], first["recovered"]["original_response_sha256"]),
                         ("accepted_from_checkpoint", original["response_sha256"]))

    def test_a_prior_page_sent_under_another_page_prompt_is_never_rebound(self):
        prior = self.prior_v7(decide, prompt=EARLIER_PROMPT)
        prior_out = self.out
        self.assertTrue(all(item["outcome"] == "accepted" for item in atomic_requests(prior)))
        current = self.traced(decide, resume_from=prior_out)
        requests = atomic_requests(current)
        self.assertTrue(requests)
        self.assertFalse({item["api_request_sha256"] for item in requests}
                         & {item["api_request_sha256"] for item in atomic_requests(prior)})
        self.assertFalse([item for item in requests if "recovered" in item])
        self.assertEqual({item["outcome"] for item in requests}, {"accepted"})
        self.assertEqual(len(self.calls()), len(requests))  # every page asked for real, once
        self.assertNotIn("direct_atomic.prior_reply_found", [row["event"] for row in events(self.out)])

    def test_a_recorded_reply_failing_current_validation_is_a_miss_with_both_real_attempts_left(self):
        # Only the page of the excluded unit fails in the prior run, so every earlier page is the same request in
        # both runs; its recorded repair omits a required exclusion and fails the current validation.
        prior = self.prior_v7(stripped(exclusion=("excluded",)))
        [failed] = [item for item in atomic_requests(prior) if item["outcome"] == "rejected_after_repair"]
        current = self.traced(decide, resume_from=self.out)
        [first] = [item for item in atomic_requests(current) if item["api_request_sha256"] == failed["api_request_sha256"]]
        self.assertEqual((first["outcome"], len(first["attempts"])), ("accepted", 1))  # the miss used no attempt
        self.assertNotIn("recovered", first)
        rejected = [row["data"] for row in events(self.out) if row["event"] == "direct_atomic.checkpoint_rejected"]
        self.assertTrue(rejected and rejected[0]["recovered"])

    def test_another_business_model_reuses_nothing(self):
        self.prior_v7(stripped(exclusion=("supporting",)))
        current = self.traced(stripped(exclusion=("supporting",)), resume_from=self.out,
                              business_model_fingerprint="another-business-model-v1")
        requests = atomic_requests(current)
        self.assertFalse([item for item in requests if "recovered" in item])
        self.assertEqual(len(self.calls()), len(requests))
        self.assertNotIn("replayed_response", current["validation"]["stages"]["map"])


class LookupTests(ReplayCase):
    def setUp(self):
        self.prior_v7(stripped(exclusion=("supporting",)))
        self.prior = self.out
        record = json.loads((self.prior / "atomic_map.json").read_text(encoding="utf-8"))
        self.entry = next(item for item in record["requests"] if item["outcome"] == "rejected_after_repair")
        self.fields = {**self.entry["checkpoint"]["fields"], "validator": direct_atomic.VALIDATOR_VERSION}

    def lookup(self, directory=None, **changes):
        return business_replay.prior_atomic_reply(directory or self.prior, api_hash=changes.pop(
            "api_hash", self.entry["api_request_sha256"]), fields={**self.fields, **changes})

    def copy(self, edit_trace=None, edit_record=None):
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target, True)
        directory = target / "prior"
        shutil.copytree(self.prior, directory)
        if edit_trace is not None:
            path = directory / "trace.jsonl"
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            rows = edit_trace(rows)
            path.write_text("".join((row if type(row) is str else json.dumps(row, ensure_ascii=False)) + "\n"
                                    for row in rows), encoding="utf-8")
        if edit_record is not None:
            path = directory / "atomic_map.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            edit_record(data)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return directory

    def repair_rows(self, rows):
        """Indexes of the recorded repair request and response of the failed page."""
        request = next(index for index, row in enumerate(rows) if row["event"] == "model.request"
                       and row["data"].get("api_request_sha256") == self.entry["attempts"][1]["api_request_sha256"])
        response = next(index for index in range(request + 1, len(rows)) if rows[index]["event"] == "model.response")
        return request, response

    def test_the_exact_bindings_hit_and_every_changed_binding_misses(self):
        found = self.lookup()
        self.assertIsNotNone(found)
        self.assertEqual(_digest(found["response"]), self.entry["attempts"][1]["response_sha256"])
        self.assertFalse(hasattr(business_replay, "prior_skeleton_reply"))  # no skeleton reader is left to call
        for label, changes in (("request", {"api_hash": "0" * 64, "api_request_sha256": "0" * 64}),
                               ("model", {"business_model_sha256": "1" * 64}), ("snapshot", {"snapshot_id": "2" * 64}),
                               ("catalog", {"catalog_sha256": "3" * 64}), ("units", {"units_sha256": "4" * 64}),
                               ("coverage", {"coverage_sha256": "5" * 64}), ("shape", {"shape_sha256": "6" * 64}),
                               ("no model identity", {"business_model_sha256": None})):
            with self.subTest(binding=label):
                self.assertIsNone(self.lookup(**changes))

        def code_led(data):  # a current code-led record is not an earlier record to recover from
            data["schema"] = direct_atomic.SCHEMA
        self.assertIsNone(self.lookup(self.copy(edit_record=code_led)))

    def test_damaged_ambiguous_or_foreign_inputs_miss(self):
        def proposal(rows):
            request, response = self.repair_rows(rows)
            rows[response]["data"]["proposal"]["unknowns"] = ["changed"]
            return rows

        def request_hash(rows):
            request, _ = self.repair_rows(rows)
            rows[request]["data"]["api_request_sha256"] = "8" * 64
            return rows

        def attempt(rows):
            _, response = self.repair_rows(rows)
            rows[response]["data"]["attempt"] = 1
            return rows

        def duplicate(rows):
            request, response = self.repair_rows(rows)
            last = rows[-1]["seq"]
            return rows + [{**copy.deepcopy(rows[request]), "seq": last + 1},
                           {**copy.deepcopy(rows[response]), "seq": last + 2}]

        def unpaired(rows):
            _, response = self.repair_rows(rows)
            return rows + [{**copy.deepcopy(rows[response]), "seq": rows[-1]["seq"] + 1}]

        def repeated_seq(rows):
            rows[-1]["seq"] = rows[-2]["seq"]
            return rows

        def malformed(rows):
            return rows + ["{not json"]
        for edit in (proposal, request_hash, attempt, duplicate, unpaired, repeated_seq, malformed):
            with self.subTest(trace=edit.__name__):
                self.assertIsNone(self.lookup(self.copy(edit_trace=edit)))

        def relabelled(data):
            data["settings"]["validator"] = direct_atomic.VALIDATOR_VERSION

        def response_hash(data):
            entry = next(item for item in data["requests"] if item["id"] == self.entry["id"])
            entry["attempts"][1]["response_sha256"] = "9" * 64

        def core_issue(data):
            entry = next(item for item in data["requests"] if item["id"] == self.entry["id"])
            entry["attempts"][1]["missing_keys"] = ["exclusion", "reason"]

        def other_code(data):
            entry = next(item for item in data["requests"] if item["id"] == self.entry["id"])
            entry["attempts"][1]["code"] = "missing_units"

        def claimed_success(data):
            entry = next(item for item in data["requests"] if item["id"] == self.entry["id"])
            entry["outcome"] = "accepted_after_repair"
            entry["accepted"] = {key: entry["attempts"][1][key] for key in ("api_request_sha256", "response_sha256")}
            entry["accepted"]["attempt"] = 2

        def two_requests(data):
            entry = next(item for item in data["requests"] if item["id"] == self.entry["id"])
            data["requests"].append({**copy.deepcopy(entry), "id": "adq-duplicate"})
        for edit in (relabelled, response_hash, core_issue, other_code, claimed_success, two_requests):
            with self.subTest(record=edit.__name__):
                self.assertIsNone(self.lookup(self.copy(edit_record=edit)))
        with patch.object(business_replay, "MAX_TRACE_BYTES", 1024):
            self.assertIsNone(self.lookup())
        with patch.object(business_replay, "MAX_EVENT_BYTES", 64):
            self.assertIsNone(self.lookup())
        linked = self.copy()
        (linked / "trace.jsonl").unlink()
        os.symlink(self.prior / "trace.jsonl", linked / "trace.jsonl")
        self.assertIsNone(self.lookup(linked))
        missing = self.copy()
        (missing / "trace.jsonl").unlink()
        self.assertIsNone(self.lookup(missing))
        self.assertIsNotNone(self.lookup())  # the untouched prior run still hits

    def test_the_strict_recorded_demo_replies_are_unchanged(self):
        from test_operation_metadata import R07_DEMO_REPLIES
        data = (ROOT / "examples/synthetic/model-responses.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), R07_DEMO_REPLIES)


class CodeLedPriorTests(ReplayCase):
    """direct-atomic-validator/9 (atomic-map/2): /10 changed only host transition support and the composition, so the
    same page request may revalidate its recorded reply; the /9 provenance is kept and never relabelled."""

    def test_a_validator_9_page_reply_is_a_prior_only_of_another_validator(self):
        with patch.object(direct_atomic, "VALIDATOR_VERSION", V9):
            prior = self.traced(decide)
        record = json.loads((self.out / "atomic_map.json").read_text(encoding="utf-8"))
        self.assertEqual((record["schema"], record["settings"]["validator"]), ("atomic-map/2", V9))
        entry = atomic_requests(prior)[0]
        fields = {**entry["checkpoint"]["fields"], "validator": direct_atomic.VALIDATOR_VERSION}

        def lookup(directory=None, **changes):
            return business_replay.prior_atomic_reply(directory or self.out, api_hash=entry["api_request_sha256"],
                                                      fields={**fields, **changes})
        found = lookup()
        self.assertEqual((found["recovered"]["prior_validator"], found["accepted"]), (V9, entry["accepted"]))
        self.assertEqual(_digest(found["response"]), entry["accepted"]["response_sha256"])
        self.assertIsNone(lookup(validator=V9))  # a record of the same validator is no prior
        self.assertIsNone(lookup(shape_sha256="6" * 64))
        relabelled = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, relabelled, True)
        shutil.copytree(self.out, relabelled / "prior")
        record["settings"]["validator"] = direct_atomic.VALIDATOR_VERSION  # a /10 record is never a /9 prior
        (relabelled / "prior" / "atomic_map.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.assertIsNone(lookup(relabelled / "prior"))
        self.assertIsNotNone(lookup())  # the untouched prior run still hits


if __name__ == "__main__":
    unittest.main()
