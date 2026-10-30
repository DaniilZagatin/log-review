"""An old genuine repair can use the existing bounded auxiliary recovery, never skip core validation."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from process_observability import direct_atomic
from process_observability.action_refinement import _digest
from process_observability.tracing import TraceRecorder
from test_atomic_details_recovery import RecoveryCase, MIXED, always
from test_business_replay import V7, as_v7_record, files_of


def lean(values, *, corrupt_core=False):
    reply = always({"refund": copy.deepcopy(values)})

    def respond(brief, attempt):
        result = reply(brief, attempt)
        for item in result["unit_accounting"]:
            if item["disposition"] != "excluded":
                item.pop("exclusion", None)
            if item["disposition"] != "unresolved":
                item.pop("question", None)
        if corrupt_core:
            result["operations"][0]["name"] = ""
        return result
    return respond


class PriorAuxiliaryTests(RecoveryCase):
    def traced(self, reply, **settings):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        recorder = TraceRecorder(directory.name)
        result = self.recovered_run(reply, trace=recorder, **settings)
        shutil.copyfile(recorder.path, self.out / "trace.jsonl")
        return result

    def prior(self, values, **options):
        """A prior run labelled as direct-atomic-validator/7 wrote it, with the current page bytes (exact replay)."""
        with patch.object(direct_atomic, "VALIDATOR_VERSION", V7), \
                patch.object(direct_atomic, "_wire_defaults", lambda value, units: (value, [])):
            bundle = self.traced(lean(values, **options))
        as_v7_record(self.out)
        return bundle

    def test_prior_real_repair_is_derived_exported_and_replayed_with_no_atomic_call(self):
        before = self.prior(MIXED)
        prior_out = self.out
        prior_files = files_of(prior_out)
        failed = self.request_of(before)
        self.assertEqual(failed["attempts"][-1]["code"], "missing_keys")
        current = self.traced(lean(MIXED), resume_from=prior_out)
        self.assertEqual(self.atomic_calls(), [])
        request = self.request_of(current)
        self.assertEqual(request["outcome"], "accepted_from_checkpoint")
        self.assertEqual(request["recovered"]["original_attempt"], 2)
        self.assertEqual([x["code"] for x in request["local_revalidation"]["omissions"]],
                         ["external_without_contract", "explanation_symbol_not_in_source"])
        self.assertTrue(request["wire_decoding"]["defaults"])
        self.assertEqual(request["local_revalidation"]["raw_response_sha256"],
                         failed["attempts"][-1]["response_sha256"])
        self.assertNotEqual(request["accepted"]["response_sha256"], failed["attempts"][-1]["response_sha256"])
        saved = json.loads((self.out / direct_atomic.CHECKPOINTS /
                            f"{request['checkpoint']['identity']}.json").read_text())
        self.assertEqual(_digest(saved["local_revalidation"]["raw_response"]),
                         request["recovered"]["original_response_sha256"])
        self.assertEqual(saved["response_sha256"], _digest(saved["response"]))
        self.assertEqual(files_of(prior_out), prior_files)
        self.exported(current, "packet")
        again = self.traced(lean(MIXED), resume_from=self.out)
        self.assertEqual(self.atomic_calls(), [])
        self.assertEqual(self.request_of(again)["local_revalidation"]["omissions"],
                         request["local_revalidation"]["omissions"])
        self.exported(again, "packet-again")

    def test_prior_repair_with_a_core_error_is_a_miss_and_keeps_both_real_attempts(self):
        self.prior(MIXED, corrupt_core=True)
        current = self.traced(lean(MIXED, corrupt_core=True), resume_from=self.out)
        self.assertEqual(len(self.atomic_calls()), 2)
        request = self.request_of(current)
        self.assertEqual(request["outcome"], "rejected_after_repair")
        self.assertNotIn("recovered", request)

    def test_prior_repair_cannot_exceed_the_existing_auxiliary_omission_limit(self):
        values = copy.deepcopy(MIXED)
        values["external_interactions"] *= direct_atomic.MAX_AUXILIARY_OMISSIONS + 1
        self.prior(values)
        current = self.traced(lean(values), resume_from=self.out)
        self.assertEqual(len(self.atomic_calls()), 2)
        request = self.request_of(current)
        self.assertEqual(request["outcome"], "rejected_after_repair")
        self.assertTrue(request["local_revalidation"]["omission_limit_reached"])
        self.assertNotIn("recovered", request)
