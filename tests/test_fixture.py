"""Independent checks of the controlled fixture, never execution of app.py."""

import ast
from collections import Counter
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic"
spec = importlib.util.spec_from_file_location("synthetic_test_generator", FIXTURE / "generate.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class SyntheticFixtureTests(unittest.TestCase):
    def setUp(self):
        self.records = [json.loads(line) for line in (FIXTURE / "logs.jsonl").read_text().splitlines()]
        self.oracle = json.loads((FIXTURE / "ground_truth.json").read_text())

    def test_hand_checked_population_and_oracle(self):
        expected = {"cases": 3, "operation_occurrences": 10, "technical_attempts": 11,
                    "logical_events": 19, "delivered_records": 20}
        self.assertEqual(self.oracle["totals"], expected)
        self.assertEqual(len(self.records), 20)
        self.assertEqual(len({r["event_id"] for r in self.records}), 19)
        self.assertEqual(len({r["occurrence_id"] for r in self.records}), 10)
        self.assertEqual(len({r["attempt_id"] for r in self.records}), 11)
        self.assertEqual([c["duration_seconds"] for c in self.oracle["cases"]], [300, 1200, 480])
        self.assertTrue(self.oracle["oracle_for_tests_only"])
        self.assertTrue(all(r["data_origin"] == "synthetic" for r in self.records))

    def test_return_is_another_review_occurrence_in_the_same_case(self):
        reviews = [r for r in self.records if r["case_id"] == "synthetic-002" and r["event_key"].startswith("review.")]
        self.assertEqual(len(reviews), 4)
        self.assertEqual(len({r["occurrence_id"] for r in reviews}), 2)
        self.assertEqual(Counter(r["event_key"] for r in reviews),
                         {"review.started": 2, "review.returned": 1, "review.approved": 1})

    def test_retry_and_duplicate_are_distinct_from_business_repeat(self):
        fulfil = [r for r in self.records if r["case_id"] == "synthetic-003" and r["event_key"].startswith("fulfil.")]
        self.assertEqual(len({r["occurrence_id"] for r in fulfil}), 1)
        self.assertEqual(len({r["attempt_id"] for r in fulfil}), 2)
        completed = [r for r in fulfil if r["event_key"] == "fulfil.completed"]
        self.assertEqual(len(completed), 2)
        self.assertEqual(completed[0]["event_id"], completed[1]["event_id"])
        self.assertNotEqual(completed[0]["delivery_id"], completed[1]["delivery_id"])
        self.assertEqual(completed[0]["occurred_at"], completed[1]["occurred_at"])
        self.assertNotEqual(completed[0]["received_at"], completed[1]["received_at"])

    def test_regeneration_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            generator.write_artifacts(Path(temp))
            for name in ("logs.jsonl", "ground_truth.json"):
                self.assertEqual((Path(temp) / name).read_bytes(), (FIXTURE / name).read_bytes())

    def test_opportunities_follow_controlled_inputs_not_absence_of_logs(self):
        table = json.loads((FIXTURE / "scenarios.json").read_text())
        table["cases"][0]["cancellation_requested"] = True
        records, oracle = generator.generate(table)
        self.assertFalse(any(r["event_key"].startswith("cancel.") for r in records))
        self.assertEqual(oracle["cancellation_opportunities"]["count"], 1)
        self.assertEqual(oracle["cancellation_opportunities"]["state"], "known_n")
        table["population_complete"] = False
        with self.assertRaises(ValueError):
            generator.generate(table)

    def test_source_symbols_and_keys_checked_without_importing_application(self):
        tree = ast.parse((FIXTURE / "app.py").read_text())
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for activity in self.oracle["required_activities"]:
            node = functions[activity["symbol"]]
            keys = {call.args[0].value for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "emit" and call.args and isinstance(call.args[0], ast.Constant)}
            self.assertEqual(keys, set(activity["event_keys"]))
        self.assertEqual(self.oracle["cancellation_opportunities"]["count"], 0)
        self.assertEqual(self.oracle["cancellation_opportunities"]["state"], "known_zero")
        self.assertFalse(any(r["event_key"].startswith("cancel.") for r in self.records))
        self.assertFalse(any("activity_id" in r for r in self.records))

    def test_hand_checked_per_case_oracle_fields(self):
        cases = self.oracle["cases"]
        self.assertEqual([c["returned_for_revision"] for c in cases], [False, True, False])
        self.assertEqual([c["retried_occurrences"] for c in cases], [[], [], ["synthetic-003:fulfil-1"]])
        self.assertEqual([c["activity_occurrences"] for c in cases],
                         [{"receive": 1, "review": 1, "fulfil": 1},
                          {"receive": 1, "review": 2, "fulfil": 1},
                          {"receive": 1, "review": 1, "fulfil": 1}])


if __name__ == "__main__":
    unittest.main()
