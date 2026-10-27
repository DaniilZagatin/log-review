"""A rubric reports bounded mismatches without claiming semantic verification."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

from pko.scenario_map.evaluation import evaluate_map


def node(identifier, name, line=10):
    return {"id": identifier, "name": name, "kind": "operation", "effect_kind": "decision",
            "evidence": [{"path": "source-1/app/Handler.java", "line_start": line, "line_end": line + 2}]}


class ScenarioEvaluationTests(unittest.TestCase):
    def rubric(self, *operations, **rules):
        return {"id": "test", "status": "audit_derived_requires_expert_review",
                "expected_operations": list(operations), **rules}

    def test_both_name_and_source_anchor_are_required_when_supplied(self):
        rule = {"id": "check", "label": "Проверка срока", "name_pattern": "Проверка срока",
                "evidence": {"path_suffix": "app/Handler.java", "line": 11}}
        model = {"nodes": [node("wrong-name", "Получение документа"), node("wrong-code", "Проверка срока", 50),
                           node("correct", "Проверка срока")], "edges": []}
        result = evaluate_map(model, self.rubric(rule))
        self.assertEqual(result["matches"], {"check": "correct"})
        self.assertTrue(result["passed"])
        self.assertFalse(result["semantic_verification"])
        self.assertIn("Исходники и модель не запускаются", result["limitation"])
        model["nodes"].pop()
        self.assertEqual(evaluate_map(model, self.rubric(rule))["findings"][0]["code"], "missing_operation")

    def test_ambiguous_match_is_not_chosen_arbitrarily(self):
        model = {"nodes": [node("one", "Проверка срока"), node("two", "Проверка срока")]}
        result = evaluate_map(model, self.rubric({"id": "check", "label": "Проверка срока"}))
        self.assertIsNone(result["matches"]["check"])
        self.assertEqual(result["findings"][0]["code"], "ambiguous_operation")
        self.assertEqual(result["findings"][0]["candidates"], ["one", "two"])

    def test_one_large_node_cannot_cover_two_independent_operations(self):
        model = {"nodes": [node("coarse", "Проверка и сохранение")], "edges": []}
        rubric = self.rubric(
            {"id": "check", "label": "Проверка", "evidence": {"path_suffix": "Handler.java", "line": 10}},
            {"id": "save", "label": "Сохранение", "evidence": {"path_suffix": "Handler.java", "line": 12}})
        result = evaluate_map(model, rubric)
        self.assertEqual(result["findings"], [{"code": "collapsed_operations", "node_id": "coarse", "operations": ["check", "save"]}])

    def test_wrong_threshold_reports_missing_expected_and_forbidden_transition(self):
        model = {"nodes": [node("one", "Проверка срока"), node("two", "Формирование отказа")],
                 "edges": [{"id": "edge-1", "source": "one", "target": "two", "condition": "Прошло не менее 365 дней"}]}
        rubric = self.rubric({"id": "check", "label": "Проверка срока"}, {"id": "refuse", "label": "Формирование отказа"},
            expected_transitions=[{"source": "check", "target": "refuse", "condition_pattern": "более 365"}],
            forbidden_transitions=[{"source": "check", "target": "refuse", "condition_pattern": "не менее 365"}])
        result = evaluate_map(model, rubric)
        self.assertEqual([x["code"] for x in result["findings"]], ["missing_transition", "forbidden_transition"])
        model["edges"][0]["condition"] = "Прошло более 365 дней"
        self.assertTrue(evaluate_map(model, rubric)["passed"])

    def test_paths_allow_intermediate_operations_only_when_requested(self):
        model = {"nodes": [node("one", "Ожидание"), node("two", "Выбор категории"), node("three", "Получение документов")],
                 "edges": [{"source": "one", "target": "two"}, {"source": "two", "target": "one"}, {"source": "two", "target": "three"}]}
        rubric = self.rubric({"id": "wait", "label": "Ожидание"}, {"id": "receive", "label": "Получение документов"},
                             expected_paths=[{"path": ["wait", "receive"], "allow_intermediate": True}])
        self.assertTrue(evaluate_map(model, rubric)["passed"])
        rubric["expected_paths"][0]["allow_intermediate"] = False
        self.assertEqual(evaluate_map(model, rubric)["findings"][0]["code"], "missing_path")
        model["edges"][-1]["kind"] = "data"
        rubric["expected_paths"][0]["allow_intermediate"] = True
        self.assertFalse(evaluate_map(model, rubric)["passed"])

    def test_effect_claim_is_checked_without_reclassifying_source(self):
        model = {"nodes": [node("one", "Решение о возврате")], "edges": []}
        model["nodes"][0]["effect_kind"] = "command"
        before = deepcopy(model)
        rubric = self.rubric({"id": "refund", "label": "Решение о возврате"},
                             expected_properties=[{"operation": "refund", "field": "effect_kind", "values": ["decision", "recommendation"]}])
        self.assertEqual(evaluate_map(model, rubric)["findings"][0]["code"], "property_mismatch")
        self.assertEqual(model, before)

    def test_invalid_rubric_cannot_pass_as_empty_checks(self):
        for rubric in ({}, self.rubric({"id": "x", "label": "Check", "name_pattern": "["}),
                       self.rubric({"id": "x", "label": "Check"}, {"id": "x", "label": "Again"}),
                       self.rubric({"id": "x", "label": "Check"}, expected_paths=[{"path": ["x", "missing"]}])):
            with self.subTest(rubric=rubric), self.assertRaises(ValueError):
                evaluate_map({"nodes": [], "edges": []}, rubric)

    def test_rubric_anchors_are_not_evaluated_against_a_different_source_snapshot(self):
        rubric = self.rubric({"id": "x", "label": "Check"})
        rubric['source_snapshot_digest'] = 'sha256:expected'
        model = {'nodes': [node('match', 'Check')], 'edges': [], 'sources': [{'digest': 'sha256:changed'}]}
        for sources in ([], model['sources']):
            model['sources'] = sources
            result = evaluate_map(model, rubric)
            self.assertFalse(result['passed'])
            self.assertFalse(result['source_digest_verified'])
            self.assertEqual(result['matches'], {})
            self.assertEqual([finding['code'] for finding in result['findings']], ['rubric_source_mismatch'])
        model['sources'] = [{'digest': 'sha256:expected'}]
        result = evaluate_map(model, rubric)
        self.assertTrue(result['passed'])
        self.assertTrue(result['source_digest_verified'])

    def test_cli_writes_report_and_returns_failure_for_a_mismatch(self):
        source = Path(__file__).resolve().parents[1] / "scripts/evaluate_scenario_map.py"
        spec = importlib.util.spec_from_file_location("evaluate_scenario_cli", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "map.json").write_text('{"nodes":[],"edges":[]}', encoding="utf-8")
            (root / "rubric.json").write_text(json.dumps(self.rubric({"id": "x", "label": "Check"})), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                code = module.main([str(root / "map.json"), str(root / "rubric.json"), "--out", str(root / "result.json")])
            self.assertEqual(code, 1)
            report = json.loads((root / "result.json").read_text(encoding="utf-8"))
            self.assertFalse(report["passed"])
            self.assertFalse(report["semantic_verification"])


if __name__ == "__main__":
    unittest.main()
