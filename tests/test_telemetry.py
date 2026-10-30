import json
from pathlib import Path
import tempfile
import unittest

from process_observability.telemetry import TelemetryError, profile_telemetry


class TelemetryTests(unittest.TestCase):
    def test_controlled_fixture_profile_preserves_duplicate_semantics(self):
        profile = profile_telemetry(Path(__file__).parent / "fixtures/synthetic/logs.jsonl")
        self.assertEqual(profile["record_count"], 20)
        self.assertEqual(profile["logical_event_count"], 19)
        self.assertEqual(profile["duplicate_record_count"], 1)
        self.assertEqual(profile["case_count"], 3)
        self.assertEqual(profile["invalid_time_count"], 0)
        self.assertIn("review.returned", profile["event_keys"])

    def test_field_mapping_unknown_dates_and_sensitive_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "logs.jsonl"
            path.write_text(json.dumps({"name":"order.done", "case":None, "time":"bad", "payload":"CANARY_PRIVATE", "api_key":"CANARY_SECRET"})+'\n')
            profile = profile_telemetry(path, {"event_key":"name", "case_id":"case", "occurred_at":"time"})
            self.assertIsNone(profile["case_count"])
            self.assertEqual(profile["invalid_time_count"], 1)
            self.assertNotIn("CANARY", json.dumps(profile))
            with self.assertRaises(TelemetryError):
                profile_telemetry(path, max_bytes=1)

    def test_bad_records_and_limits_fail_without_echoing_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "logs.jsonl"
            path.write_text('{"CANARY_SECRET":')
            with self.assertRaises(TelemetryError) as ctx:
                profile_telemetry(path)
            self.assertNotIn("CANARY", str(ctx.exception))
            path.write_text('{}\n{}\n')
            with self.assertRaises(TelemetryError):
                profile_telemetry(path, max_records=1)


if __name__ == "__main__":
    unittest.main()
