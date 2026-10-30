"""Validate fixture annotations only; never execute snippets or call a model."""
import json
from pathlib import Path, PurePosixPath
import unittest


BANK = Path(__file__).with_name("fixtures") / "variant_methodology_cases.json"


class MethodologyCaseBankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = json.loads(BANK.read_text(encoding="utf-8"))

    def assert_text(self, value):
        self.assertIsInstance(value, str)
        self.assertTrue(value.strip())

    def test_preliminary_authorship_and_non_execution_are_explicit(self):
        self.assertEqual(self.bank["schema_version"], "1.0")
        self.assertEqual(self.bank["authorship"], "assistant-authored")
        self.assertEqual(self.bank["status"], "provisional")
        self.assertIs(self.bank["requires_expert_review"], True)
        self.assertIs(self.bank["semantic_verification"], False)
        self.assertEqual(self.bank["execution_policy"], "data_only_do_not_execute")

    def test_cases_have_unique_ids_sources_and_explicit_partitions(self):
        cases = self.bank["cases"]
        self.assertEqual(len(cases), 12)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertEqual({case["partition"] for case in cases}, {"development", "holdout-candidate"})
        for case in cases:
            with self.subTest(case=case["id"]):
                for field in ("id", "language", "variant", "instance_unit"):
                    self.assert_text(case[field])
                source = case["source"]
                path = PurePosixPath(source["path"])
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                self.assertNotIn("\\", source["path"])
                self.assertIsInstance(source["lines"], list)
                self.assertTrue(source["lines"])
                for line in source["lines"]:
                    self.assertIsInstance(line, str)
                    self.assertNotIn("\n", line)

    def test_action_anchors_and_routes_refer_to_existing_annotations(self):
        for case in self.bank["cases"]:
            with self.subTest(case=case["id"]):
                actions = case["expected_actions"]
                self.assertTrue(actions)
                ids = {action["id"] for action in actions}
                self.assertEqual(len(ids), len(actions))
                for action in actions:
                    self.assert_text(action["id"])
                    self.assert_text(action["label"])
                    self.assertIs(type(action["line"]), int)
                    self.assertGreaterEqual(action["line"], 1)
                    self.assertLessEqual(action["line"], len(case["source"]["lines"]))
                routes = case["expected_routes"]
                self.assertTrue(routes)
                self.assertEqual(len({route["id"] for route in routes}), len(routes))
                for route in routes:
                    for field in ("id", "when", "expectation"):
                        self.assert_text(route[field])
                    self.assertIsInstance(route["path"], list)
                    self.assertTrue(route["path"])
                    self.assertTrue(set(route["path"]) <= ids)

    def test_each_case_has_prohibited_claims_and_distinct_counterexample(self):
        for case in self.bank["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["prohibited_claims"])
                for claim in case["prohibited_claims"]:
                    self.assert_text(claim)
                counter = case["counterexample"]
                self.assert_text(counter["expected_difference"])
                self.assertIsInstance(counter["lines"], list)
                self.assertTrue(counter["lines"])
                self.assertNotEqual(counter["lines"], case["source"]["lines"])
                for line in counter["lines"]:
                    self.assertIsInstance(line, str)
                    self.assertNotIn("\n", line)

    def test_optional_grouping_has_results_boundaries_and_valid_membership(self):
        found = False
        for case in self.bank["cases"]:
            if "expected_business_operations" not in case:
                continue
            found = True
            with self.subTest(case=case["id"]):
                groups = case["expected_business_operations"]
                self.assertEqual(len({group["id"] for group in groups}), len(groups))
                actions = {action["id"] for action in case["expected_actions"]}
                assigned = []
                for group in groups:
                    for field in ("id", "result", "boundary"):
                        self.assert_text(group[field])
                    self.assertTrue(group["action_ids"])
                    self.assertTrue(set(group["action_ids"]) <= actions)
                    assigned.extend(group["action_ids"])
                self.assertEqual(len(assigned), len(set(assigned)))
                self.assertEqual(set(assigned), actions)
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
