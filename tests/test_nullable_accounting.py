"""Inapplicable omitted nullable accounting keys are read as null in a copy (direct-atomic-validator/8).

Tests-first. Synthetic fixtures of test_declaration_completion; scripted models only. A unit_accounting item with a
known disposition may omit exclusion (when not excluded) or question (when not unresolved): the host reads null in a
COPY, records the exact paths and runs the complete validation. The reply as received, its hash and its checkpoint
stay unchanged; the accounting item shape is unchanged (the code-led pages of /9 keep it). An excluded unit without
exclusion, an unresolved unit without question, every other omitted key and every present invalid value stay
rejected. The fixtures are the code-led page replies of test_declaration_completion.decide.
"""

import copy
import json
import unittest
from unittest.mock import patch

from process_observability import direct_atomic
from process_observability.action_refinement import _digest
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import _atomic_accounting, export_onboarding
from test_action_refinement import Code
from test_declaration_completion import FILES, FOUND, CompletionCase, decide, handlers
from test_direct_atomic import Business, atomic_requests

NOT_EXCLUDED = ("realized", "supporting", "outside_process", "unresolved")
NOT_UNRESOLVED = ("realized", "supporting", "excluded", "outside_process")
ATOMIC = "TASK: map-atomic"


def stripped(exclusion=(), question=(), other=None):
    """decide(), then the given keys removed from items of the given dispositions; replies kept as sent."""
    sent = []

    def reply(brief, attempt):
        result = decide(brief, attempt)
        for item in result["unit_accounting"]:
            disposition = item["disposition"]
            if disposition in exclusion:
                item.pop("exclusion")
            if disposition in question:
                item.pop("question")
            if other is not None:
                other(item)
        sent.append(copy.deepcopy(result))
        return result
    reply.sent = sent
    return reply


def expected_paths(reply):
    return [f"unit_accounting[{index}].{key}" for index, item in enumerate(reply["unit_accounting"])
            for key in ("exclusion", "question") if key not in item]


class WireDefaultsUnitTests(unittest.TestCase):
    def item(self, unit, disposition, **values):
        base = {"unit": unit, "disposition": disposition, "new_operations": [], "known_operations": [], "serves": [],
                "reason": "r"}
        return {**base, **values}

    def test_only_known_dispositions_of_supplied_units_get_inapplicable_nulls(self):
        reply = {"unit_accounting": [self.item(1, "supporting"), self.item(2, "excluded"), self.item(3, "unresolved"),
                                     self.item(4, "realized", exclusion=None, question=None)]}
        before = copy.deepcopy(reply)
        decoded, paths = direct_atomic._wire_defaults(reply, 4)
        self.assertEqual(reply, before)  # the reply as received is never changed
        self.assertEqual(paths, ["unit_accounting[0].exclusion", "unit_accounting[0].question",
                                 "unit_accounting[1].question", "unit_accounting[2].exclusion"])
        self.assertNotIn("exclusion", decoded["unit_accounting"][1])  # excluded keeps its omission
        self.assertNotIn("question", decoded["unit_accounting"][2])   # unresolved keeps its omission
        self.assertEqual((decoded["unit_accounting"][0]["exclusion"], decoded["unit_accounting"][0]["question"]),
                         (None, None))
        complete = {"unit_accounting": [self.item(1, "realized", exclusion=None, question=None)]}
        self.assertIs(direct_atomic._wire_defaults(complete, 1)[0], complete)

    def test_unknown_units_dispositions_and_oversized_lists_get_no_default(self):
        for label, items, units in (("unit out of range", [self.item(3, "supporting")], 2),
                                    ("unit zero", [self.item(0, "supporting")], 2),
                                    ("boolean unit", [self.item(True, "supporting")], 2),
                                    ("unit as text", [self.item("1", "supporting")], 2),
                                    ("unknown disposition", [self.item(1, "maybe")], 2),
                                    ("no disposition", [{"unit": 1}], 2),
                                    ("more items than units", [self.item(1, "supporting"), self.item(1, "supporting")],
                                     1)):
            with self.subTest(case=label):
                reply = {"unit_accounting": items}
                self.assertEqual(direct_atomic._wire_defaults(reply, units), (reply, []))
        for reply in (None, [], {"unit_accounting": None}, {"operations": []}):
            self.assertEqual(direct_atomic._wire_defaults(reply, 3), (reply, []))


class NullableCase(CompletionCase):
    def paged(self, reply, **settings):
        self.business = Business(**handlers(map_atomic=reply))
        bundle, self.out = self.run_case(self.business, files=dict(FILES), found=copy.deepcopy(FOUND), **settings)
        return bundle

    def atomic_calls(self):
        return [call for call in self.business.calls if call["task"] == ATOMIC]


class NullableAccountingTests(NullableCase):
    def test_inapplicable_omitted_keys_give_exactly_the_full_reply_result_without_a_repair(self):
        full = self.paged(decide)
        full_calls = len(self.atomic_calls())
        lean_reply = stripped(exclusion=NOT_EXCLUDED, question=NOT_UNRESOLVED)
        lean = self.paged(lean_reply)
        self.assertEqual(len(self.atomic_calls()), full_calls)  # no repair was needed
        requests = atomic_requests(lean)
        self.assertEqual({item["outcome"] for item in requests}, {"accepted"})
        for key in ("unit_states", "operations", "event_mappings", "transitions", "candidates", "status"):
            self.assertEqual(lean["atomic_map"][key], full["atomic_map"][key], key)
        for key in ("operations", "event_mappings", "stages"):
            self.assertEqual(lean["process_map"][key], full["process_map"][key], key)
        self.assertFalse([item for item in atomic_requests(full) if "wire_decoding" in item])
        self.assertEqual(len(requests), len(lean_reply.sent))
        for request, sent in zip(requests, lean_reply.sent):
            self.assertTrue(expected_paths(sent))
            self.assertEqual(request["wire_decoding"], {"policy": direct_atomic.WIRE_DECODING, "attempt": 1,
                                                        "defaults": expected_paths(sent), "model_calls": 0})
            # The hash is that of the reply as received, and the checkpoint keeps that reply.
            self.assertEqual(request["accepted"]["response_sha256"], _digest(sent))
            saved = json.loads((self.out / direct_atomic.CHECKPOINTS /
                                f"{request['checkpoint']['identity']}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["response"], sent)
            self.assertEqual(set(saved), {"schema", "identity", "identity_fields", "accepted", "response_sha256",
                                          "response"})
        # Canonical values in the ledger; nothing invented.
        for state in lean["atomic_map"]["unit_states"].values():
            self.assertIn("exclusion", state)
            self.assertIn("question", state)
        _atomic_accounting(lean, ProcessMap.from_dict(lean["process_map"]), lean["atomic_map"]["catalog"]["snapshot_id"])
        result, _ = self.exported(lean, "packet")
        self.assertIn(result["status"], ("all_stages_completed", "partial"))

    def test_an_excluded_unit_without_exclusion_and_an_unresolved_unit_without_question_stay_rejected(self):
        for key, disposition, symbol in (("exclusion", "excluded", "Gate.tally"),
                                         ("question", "unresolved", "Dock.inspect")):
            with self.subTest(key=key):
                bundle = self.paged(stripped(**{key: (disposition,)}))
                [failed] = [item for item in atomic_requests(bundle) if item["outcome"] == "rejected_after_repair"]
                self.assertEqual([(item["code"], item["missing_keys"]) for item in failed["attempts"]],
                                 [("missing_keys", [key]), ("missing_keys", [key])])
                self.assertEqual(len([call for call in self.atomic_calls()
                                      if any(unit["symbol"] == symbol for unit in call["brief"]["units"])]), 2)
                for attempt in failed["attempts"]:
                    self.assertNotIn("wire_defaults", attempt)
                self.assertNotIn("wire_decoding", failed)
                states = {state["state"] for key_, state in bundle["atomic_map"]["unit_states"].items()
                          if key_ in failed["unit_ids"]}
                self.assertEqual(states, {"request_failed"})
                self.assertEqual(bundle["atomic_map"]["status"], "partial")

    def test_every_other_omitted_key_and_every_present_invalid_value_stays_strict(self):
        def drop(key):
            def change(item):
                if item["disposition"] == "supporting":
                    item.pop(key)
            return change
        for key in ("unit", "disposition", "new_operations", "known_operations", "serves", "reason"):
            with self.subTest(missing=key):
                bundle = self.paged(stripped(exclusion=NOT_EXCLUDED, question=NOT_UNRESOLVED, other=drop(key)))
                failed = [item for item in atomic_requests(bundle) if item["outcome"] == "rejected_after_repair"]
                self.assertTrue(failed)
                first = failed[0]["attempts"][0]
                self.assertEqual(first["code"], "missing_keys")
                self.assertIn(key, first["missing_keys"])

        def present(key, value, disposition):
            def change(item):
                if item["disposition"] == disposition:
                    item[key] = value
            return change
        for label, change in (("unknown exclusion", present("exclusion", "bogus", "realized")),
                              ("exclusion on a realized unit", present("exclusion", "read_only", "realized")),
                              ("question of another type", present("question", 5, "supporting")),
                              ("null reason of a supporting unit", present("reason", None, "supporting"))):
            with self.subTest(present=label):
                bundle = self.paged(stripped(other=change))
                self.assertTrue([item for item in atomic_requests(bundle) if item["outcome"] == "rejected_after_repair"])
                self.assertIn(bundle["atomic_map"]["status"], ("failed", "partial"))

    def test_checkpoints_replay_the_same_decoding_from_the_reply_as_received(self):
        lean_reply = stripped(exclusion=NOT_EXCLUDED, question=NOT_UNRESOLVED)
        first = self.paged(lean_reply)
        again = self.paged(stripped(exclusion=NOT_EXCLUDED, question=NOT_UNRESOLVED), resume_from=self.out)
        self.assertEqual(self.atomic_calls(), [])
        requests = atomic_requests(again)
        self.assertEqual({item["outcome"] for item in requests}, {"accepted_from_checkpoint"})
        for request, earlier in zip(requests, atomic_requests(first)):
            self.assertEqual(request["wire_decoding"], {**earlier["wire_decoding"], "attempt": 0})
            self.assertNotIn("recovered", request)
        self.assertEqual(again["atomic_map"]["unit_states"], first["atomic_map"]["unit_states"])

    def test_the_business_identity_is_new_while_the_technical_review_and_prompts_are_unchanged(self):
        # Intentional expectation change (code-led map, direct-atomic-validator/9): the page prompt and shape changed
        # again (no stage or fact on pages); the decoding policy of /8 is kept unchanged and the accounting item
        # shape below is still exactly the one /8 decoded. /10 (transition support) changed only host validation and
        # the composition request: the page prompt, shape and decoding below are the /9 ones.
        self.assertEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/11")
        # The requested shape is unchanged: both keys are still asked for in every accounting item.
        self.assertEqual(set(direct_atomic._ACCOUNT), {"unit", "disposition", "new_operations", "known_operations",
                                                       "serves", "exclusion", "reason", "question"})
        self.assertIn(direct_atomic._render(direct_atomic.ATOMIC_SHAPE), direct_atomic.ATOMIC_SYSTEM)
        with patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/7"):
            earlier = self.paged(decide)
        earlier_out, code = self.out, Code(copy.deepcopy(FOUND))
        current = self.paged(decide, code=code, resume_from=earlier_out)
        self.assertEqual(code.calls, [])  # technical review and reduction reused exactly
        self.assertTrue(self.atomic_calls())  # a page of the earlier business identity is no checkpoint hit
        self.assertNotEqual(earlier["manifest"]["provenance"]["config_sha256"],
                            current["manifest"]["provenance"]["config_sha256"])
        settings = current["atomic_map"]["settings"]
        self.assertEqual((settings["validator"], settings["wire_decoding"]["policy"]),
                         ("direct-atomic-validator/11", "atomic-nullable-accounting/1"))
        self.assertEqual(direct_atomic.WIRE_DECODING, "atomic-nullable-accounting/1")
        # A record whose settings name the earlier validator still exports as it was.
        from process_observability.cli import _write_bundle
        earlier["diagnostics"] = {}
        _write_bundle(earlier, earlier_out)
        result = export_onboarding(earlier_out, earlier_out.parent / "earlier-export")
        self.assertTrue(result["export"])


if __name__ == "__main__":
    unittest.main()
