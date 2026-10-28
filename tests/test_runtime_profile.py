"""Runtime JSONL stays local; coverage uses completeness within one event key."""
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pko.errors import PkoError
from pko.observability.coverage import assess_fact
from pko.observability.inputs import MAX_EVENT_FIELDS, parse_field_map, profile_runtime_log


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runtime.jsonl"

    def row(self, event="application.accepted", **values):
        return {"event_key": event, "case_id": "private-case-1", "event_id": "private-event-1",
                "occurred_at": "2026-10-28T09:00:00+03:00", **values}

    def profile(self, rows, field_map=None):
        source = "\n".join(json.dumps(row, ensure_ascii=True) for row in rows)
        self.path.write_text(source, encoding="utf-8")
        result = profile_runtime_log(self.path, field_map)
        self.assertEqual(self.path.read_text(encoding="utf-8"), source, "input must stay unchanged")
        return result

    def coverage(self, profile, event="application.accepted", fields=(), case_field="case_id"):
        sources = [{"binding_id": "b-1", "purpose": name, "field": name} for name in fields]
        fact = {"id": "f-1", "lifecycle": "result", "operation_ids": ["op-1"],
                "outside_application": False, "needed": ["case_id", "timestamp", *fields],
                "observed_by": ["b-1"], "field_sources": sources}
        binding = {"id": "b-1", "site_id": "s-1", "event_role": "result",
                   "lifecycle_moment": "after_action", "case_fields": [case_field]}
        site = {"id": "s-1", "kind": "log_call", "event_key": event, "key_status": "literal"}
        return assess_fact(fact, {"b-1": binding}, {"s-1": site}, profile)

    def test_canonical_key_and_distinct_identities_survive_local_parse_without_values_leaking(self):
        rows = [self.row(password="private-password", token="private-token", payload={"secret": "private-payload"}),
                self.row(case_id="private-case-2", event_id="private-event-2")]
        profile = self.profile(rows)
        self.assertEqual(profile["event_counts"], {"application.accepted": 2})
        self.assertEqual(profile["identified_case_count"], 2)
        self.assertEqual(profile["duplicate_record_count"], 0)
        self.assertEqual(profile["quality_by_event"]["application.accepted"]["record_count"], 2)
        exported = json.dumps(profile)
        for value in ("private-case-1", "private-case-2", "private-event-1", "private-event-2",
                      "private-password", "private-token", "private-payload", "2026-10-28T09:00:00+03:00"):
            self.assertNotIn(value, exported)
        self.assertNotIn("***", exported)
        self.assertEqual(self.coverage(profile)["axes"]["quality"], "no_issues_found")

    def test_field_mapping_uses_actual_fields_for_identity_quality_and_coverage(self):
        mapping = parse_field_map("event_key=event,case_id=request_id,occurred_at=ts,event_id=id")
        profile = self.profile([{"event": "application.accepted", "request_id": "sensitive-request",
                                 "ts": "2026-10-28T09:00:00Z", "id": "sensitive-event", "amount": 0}], mapping)
        self.assertEqual(profile["event_counts"], {"application.accepted": 1})
        self.assertEqual(profile["source_fields"], mapping)
        self.assertEqual(self.coverage(profile, fields=["amount"], case_field="request_id")["axes"]["quality"],
                         "no_issues_found")
        self.assertNotIn("sensitive-request", json.dumps(profile))

    def test_partial_field_map_keeps_other_defaults(self):
        row = self.row()
        row["event"] = row.pop("event_key")
        profile = self.profile([row], {"event_key": "event"})
        self.assertEqual(profile["event_counts"], {"application.accepted": 1})
        self.assertEqual(profile["invalid_time_count"], 0)
        self.assertEqual(self.coverage(profile)["axes"]["quality"], "no_issues_found")

    def test_unrelated_bad_records_do_not_taint_good_event(self):
        profile = self.profile([self.row(), self.row("application.failed", case_id=None, occurred_at="bad",
                                                    event_id="private-event-2")])
        self.assertEqual((profile["missing_case_count"], profile["invalid_time_count"]), (1, 1))
        good = self.coverage(profile)
        bad = self.coverage(profile, "application.failed")
        self.assertEqual(good["axes"]["quality"], "no_issues_found")
        self.assertNotIn("quality", good["gaps"])
        self.assertEqual(bad["axes"]["quality"], "issues")

    def test_union_of_partial_records_does_not_prove_required_fields_complete(self):
        profile = self.profile([self.row(amount=1), self.row(event_id="private-event-2", currency="RUB")])
        self.assertIn("amount", profile["fields_by_event"]["application.accepted"])
        self.assertIn("currency", profile["fields_by_event"]["application.accepted"])
        counts = profile["quality_by_event"]["application.accepted"]["field_counts"]
        self.assertEqual((counts["amount"], counts["currency"]), (1, 1))
        coverage = self.coverage(profile, fields=["amount", "currency"])
        self.assertEqual(coverage["axes"]["quality"], "issues")
        self.assertIn("quality", coverage["gaps"])

    def test_null_and_blank_are_missing_but_zero_and_false_are_values(self):
        for value, expected in ((None, "issues"), ("  ", "issues"), (0, "no_issues_found"), (False, "no_issues_found")):
            with self.subTest(value=value):
                profile = self.profile([self.row(amount=value)])
                self.assertEqual(self.coverage(profile, fields=["amount"])["axes"]["quality"], expected)

    def test_missing_binding_case_field_is_detected_even_with_valid_mapped_case(self):
        profile = self.profile([self.row()])
        self.assertEqual(self.coverage(profile, case_field="request_id")["axes"]["quality"], "issues")

    def test_field_catalog_truncation_is_explicit_and_does_not_claim_missing_fields(self):
        extras = {f"field_{index:03d}": index for index in range(MAX_EVENT_FIELDS + 1)}
        profile = self.profile([self.row(**extras, zz_amount=1)])
        stats = profile["quality_by_event"]["application.accepted"]
        self.assertTrue(profile["field_names_truncated"])
        self.assertTrue(stats["field_names_truncated"])
        self.assertEqual(len(stats["field_counts"]), MAX_EVENT_FIELDS)
        self.assertNotIn("zz_amount", stats["field_counts"])
        self.assertTrue(any("64" in item for item in profile["limitations"]))
        result = self.coverage(profile, fields=["zz_amount"])
        self.assertEqual(result["axes"]["quality"], "unknown")
        self.assertIn("quality", result["unknowns"])
        self.assertNotIn("quality", result["gaps"])

    def test_truncation_does_not_hide_known_incomplete_retained_field(self):
        extras = {f"field_{index:03d}": index for index in range(MAX_EVENT_FIELDS + 1)}
        profile = self.profile([self.row(**extras, amount=1), self.row(**extras, event_id="private-event-2")])
        self.assertTrue(profile["field_names_truncated"])
        self.assertEqual(self.coverage(profile, fields=["amount"])["axes"]["quality"], "issues")

    def test_old_profile_without_per_event_quality_remains_unknown(self):
        for errors in (0, 1):
            with self.subTest(errors=errors):
                profile = {"event_counts": {"application.accepted": 2},
                           "fields_by_event": {"application.accepted": ["case_id", "amount"]},
                           "missing_case_count": errors, "invalid_time_count": errors}
                self.assertEqual(self.coverage(profile, fields=["amount"])["axes"]["quality"], "unknown")

    def test_conflicting_record_ids_affect_both_keys_but_not_other_events(self):
        profile = self.profile([self.row(), self.row("application.failed", case_id="private-case-2"),
                                self.row("application.queued", event_id="private-event-3")])
        self.assertEqual(self.coverage(profile)["axes"]["quality"], "issues")
        self.assertEqual(self.coverage(profile, "application.failed")["axes"]["quality"], "issues")
        self.assertEqual(self.coverage(profile, "application.queued")["axes"]["quality"], "no_issues_found")
        self.assertIsNone(profile["duplicate_record_count"])

    def test_missing_record_id_is_local_quality_issue(self):
        profile = self.profile([self.row(), self.row("application.failed", event_id=None)])
        self.assertEqual(self.coverage(profile)["axes"]["quality"], "no_issues_found")
        self.assertEqual(self.coverage(profile, "application.failed")["axes"]["quality"], "issues")

    def test_secret_bearing_event_name_is_rejected_instead_of_masked_into_shared_key(self):
        profile = self.profile([self.row("Bearer private-token")])
        self.assertEqual(profile["event_counts"], {})
        self.assertEqual(profile["invalid_key_count"], 1)
        self.assertNotIn("private-token", json.dumps(profile))

    def test_duplicate_json_keys_and_non_finite_numbers_are_rejected_without_raw_content(self):
        for text in ('{"event_key":"private-value","event_key":"second"}',
                     '{"event_key":"private-value","amount":NaN}'):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(PkoError) as caught:
                    profile_runtime_log(self.path)
                self.assertNotIn("private-value", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_blocked_ancestor_is_rejected_before_open(self):
        blocked = Path(self.directory.name) / ".env" / "runtime.jsonl"
        blocked.parent.mkdir()
        blocked.write_text("{}", encoding="utf-8")
        with patch.object(Path, "open", side_effect=AssertionError("must not read blocked input")):
            with self.assertRaises(PkoError):
                profile_runtime_log(blocked)

    def test_parent_link_or_windows_junction_is_rejected_before_open(self):
        self.path.write_text("{}", encoding="utf-8")
        parent = self.path.parent
        original_lstat = Path.lstat
        for attributes, mode in ((0, stat.S_IFLNK), (0x400, stat.S_IFDIR)):
            with self.subTest(attributes=attributes, mode=mode):
                def fake_lstat(path, *args, **kwargs):
                    if path == parent:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original_lstat(path, *args, **kwargs)
                with patch.object(Path, "lstat", fake_lstat), patch.object(
                        Path, "open", side_effect=AssertionError("must not follow ancestor links")):
                    with self.assertRaises(PkoError):
                        profile_runtime_log(self.path)

    def test_leaf_link_is_rejected_before_open(self):
        self.path.write_text("{}", encoding="utf-8")
        original_lstat = Path.lstat
        def fake_lstat(path, *args, **kwargs):
            if path == self.path:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", fake_lstat), patch.object(
                Path, "open", side_effect=AssertionError("must not follow leaf links")):
            with self.assertRaises(PkoError):
                profile_runtime_log(self.path)


if __name__ == "__main__":
    unittest.main()
