"""R08 offline comparison with a human-authored oracle (R9) and optional atomicity hints (R10).

Tests-first. New API (lazy imports): process_observability.map_evaluation.{evaluate_map, evaluation_is_current},
the offline CLI command evaluate-map, and operation_views(...)[id]["hints"]. No model, network or new runtime. A
source span overlap is a navigation candidate for a human reviewer: never semantic correctness, never completeness
of anything beyond the labelled area, and never derived from operation counts or names.
"""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from process_observability.contracts import (ArtifactStatus, EvidenceRef, ProcessMap, ProcessOperation, SourceAnchor,
                                             SourceKind, Stage, ValidationState)
from process_observability.report import build_html
from test_operation_inspector import (SITES, WEBHOOK, InspectorCase, card_of, number_of, operation_ids, refund_reply,
                                      section, text_of, views)

SNAP, OTHER = "5" * 64, "6" * 64
W, T = WEBHOOK, "shop/payment/tasks.py"
# A saved map: operation ID, name and cited source ranges.
MAP = [("m-request", "Зарегистрировать уведомление", [(W, 7, 7)]),
       ("m-dup-a", "Проверить повтор уведомления", [(W, 8, 9)]),
       ("m-dup-b", "Отклонить повтор уведомления", [(W, 9, 10)]),
       ("m-refund", "Выполнить возврат", [(W, 11, 18)]),
       ("m-release", "Освободить средства", [(T, 6, 14)])]
# Human expectations: one candidate match, one ambiguity, one collapse of two actions and one missing action.
EXPECTED = [("exp-request", "Принять уведомление о возврате", [(W, 7, 7)]),
            ("exp-duplicate", "Отклонить повторное уведомление", [(W, 9, 9)]),
            ("exp-start", "Начать возврат в шлюзе", [(W, 11, 12)]),
            ("exp-complete", "Зафиксировать выполненный возврат", [(W, 17, 17)]),
            ("exp-notify", "Уведомить покупателя о возврате", [(W, 20, 21)])]


def code_ref(identifier, path, first, last, snapshot=SNAP):
    anchor = SourceAnchor(snapshot_id=snapshot, path=path, symbol="<module>", start_line=first, end_line=last)
    return EvidenceRef(id=identifier, kind=SourceKind.CODE, snapshot_id=snapshot, locator=f"{path}:{first}-{last}",
                       safe_quote="pass", raw_span_hash="a" * 64, source_anchor=anchor,
                       text_validation=ValidationState.VERIFIED)


def saved_map(spec, snapshot=SNAP):
    """A saved bundle reduced to what the comparison reads: the process map and its evidence registry."""
    registry, operations = [], []
    for identifier, name, spans in spec:
        refs = [code_ref(f"src-{identifier}-{number}", path, first, last, snapshot)
                for number, (path, first, last) in enumerate(spans, 1)]
        registry += refs
        operations.append(ProcessOperation(id=identifier, name=name, stage_id="payment",
                                           evidence_ids=[item.id for item in refs]))
    process = ProcessMap(run_id="saved-run", input_fingerprints={"repo/" + W: "b" * 64}, status=ArtifactStatus.DRAFT,
                         evidence_ids=[item.id for item in registry], purpose=None, boundaries=None,
                         case_definition=None, stages=[Stage(id="payment", name="Оплата")], transitions=[],
                         event_mappings=[], unknowns=[], operations=operations)
    return {"process_map": process.to_dict(), "evidence": [item.to_dict() for item in registry]}


def oracle(spec, snapshot=SNAP):
    """Public oracle shape operation-oracle/1: explicitly human-authored and bound to one source snapshot."""
    return {"schema": "operation-oracle/1", "id": "synthetic-refund-oracle", "authored_by": "human",
            "source_snapshot_id": snapshot, "scope": "Обработка уведомления о возврате в " + W,
            "operations": [{"id": identifier, "label": label,
                            "anchors": [{"path": path, "start_line": first, "end_line": last}
                                        for path, first, last in spans]}
                           for identifier, label, spans in spec]}


def evaluate(bundle, expected):
    from process_observability.map_evaluation import evaluate_map
    return evaluate_map(bundle, expected)


def invoke(*args):
    from process_observability.cli import main
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main([str(value) for value in args])
    return code, out.getvalue(), err.getvalue()


class OracleComparisonTests(unittest.TestCase):
    def test_the_oracle_must_be_explicitly_human_authored_and_well_formed(self):
        changes = {"model-authored": lambda item: item.update(authored_by="model"),
                   "author absent": lambda item: item.pop("authored_by"),
                   "foreign schema": lambda item: item.update(schema="operation-oracle/0"),
                   "no snapshot": lambda item: item.pop("source_snapshot_id"),
                   "no expectations": lambda item: item.update(operations=[]),
                   "repeated id": lambda item: item["operations"][1].update(id=item["operations"][0]["id"]),
                   "reversed range": lambda item: item["operations"][0]["anchors"][0].update(start_line=9, end_line=7),
                   "absolute path": lambda item: item["operations"][0]["anchors"][0].update(path="/etc/passwd")}
        for label, change in changes.items():
            with self.subTest(case=label):
                broken = oracle(EXPECTED)
                change(broken)
                with self.assertRaises(ValueError):
                    evaluate(saved_map(MAP), broken)

    def test_a_foreign_source_snapshot_grades_nothing(self):
        result = evaluate(saved_map(MAP), oracle(EXPECTED, snapshot=OTHER))
        self.assertIs(result["source_snapshot_verified"], False)
        self.assertEqual(result["results"], [])
        self.assertIs(result["semantic_verification"], False)

    def test_missing_collapsed_ambiguous_and_candidate_matches_are_addressed(self):
        expected = oracle(EXPECTED)
        result = evaluate(saved_map(MAP), expected)
        self.assertEqual(result["schema"], "operation-evaluation/1")
        self.assertIs(result["source_snapshot_verified"], True)
        self.assertIs(result["semantic_verification"], False)
        self.assertEqual(result["scope"], expected["scope"])
        self.assertEqual([(item["expected_id"], item["status"], sorted(item["operation_ids"]))
                          for item in result["results"]],
                         [("exp-request", "candidate_match", ["m-request"]),
                          ("exp-duplicate", "ambiguous", ["m-dup-a", "m-dup-b"]),
                          ("exp-start", "collapsed", ["m-refund"]),
                          ("exp-complete", "collapsed", ["m-refund"]),
                          ("exp-notify", "missing", [])])
        self.assertEqual(result["counts"], {"candidate_match": 1, "ambiguous": 1, "collapsed": 2, "missing": 1})
        self.assertEqual(result["unlabelled_operation_ids"], ["m-release"])  # outside the labelled area, no verdict
        self.assertTrue(result["limitations"])

    def test_equal_operation_counts_and_names_never_make_a_match_or_completeness(self):
        same = [("m-a", "Принять уведомление о возврате", [(W, 30, 31)]), ("m-b", "Выполнить возврат", [(W, 40, 41)])]
        expected = oracle([("e-a", "Принять уведомление о возврате", [(W, 7, 7)]),
                           ("e-b", "Выполнить возврат", [(W, 9, 9)])])
        result = evaluate(saved_map(same), expected)
        self.assertEqual([item["status"] for item in result["results"]], ["missing", "missing"])
        self.assertEqual(result["counts"], {"candidate_match": 0, "ambiguous": 0, "collapsed": 0, "missing": 2})
        self.assertEqual(sorted(result["unlabelled_operation_ids"]), ["m-a", "m-b"])
        self.assertFalse({"passed", "complete", "completeness", "score", "accuracy"} & set(result))

    def test_a_result_is_bound_to_fresh_map_source_and_oracle_hashes(self):
        from process_observability.map_evaluation import evaluation_is_current
        bundle, expected = saved_map(MAP), oracle(EXPECTED)
        result = evaluate(bundle, expected)
        for key in ("map_sha256", "oracle_sha256"):
            self.assertRegex(result[key], r"^[0-9a-f]{64}$")
        self.assertEqual(result["source_snapshot_id"], SNAP)
        self.assertEqual(evaluate(copy.deepcopy(bundle), copy.deepcopy(expected)), result)  # deterministic
        self.assertTrue(evaluation_is_current(result, bundle, expected))
        renamed = copy.deepcopy(bundle)
        renamed["process_map"]["operations"][3]["name"] = "Отменить возврат"
        moved = copy.deepcopy(bundle)
        next(item for item in moved["evidence"] if item["id"] == "src-m-refund-1")["source_anchor"]["start_line"] = 12
        relabelled = copy.deepcopy(expected)
        relabelled["operations"][0]["label"] = "Отклонить уведомление"
        for label, (changed_map, changed_oracle) in {"operation renamed": (renamed, expected),
                                                     "evidence moved": (moved, expected),
                                                     "oracle relabelled": (bundle, relabelled)}.items():
            with self.subTest(change=label):
                self.assertFalse(evaluation_is_current(result, changed_map, changed_oracle))
        self.assertNotEqual(evaluate(renamed, expected)["map_sha256"], result["map_sha256"])


class EvaluationCliTests(InspectorCase):
    def test_the_cli_writes_the_same_offline_result_without_any_model(self):
        from process_observability.cli import _write_bundle
        bundle = self.inspected()
        self.assertComplete(bundle)
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        expected = oracle([("exp-accept", "Принять уведомление о возврате", [(WEBHOOK, 8, 10)]),
                           ("exp-refund", "Выполнить возврат средств", [(WEBHOOK, 15, 17)])],
                          snapshot=bundle["atomic_map"]["catalog"]["snapshot_id"])
        source = self.out.parent / "oracle.json"
        source.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
        target = self.out.parent / "evaluation.json"
        with patch("process_observability.cli._model_configs") as configs:
            code, _, err = invoke("evaluate-map", "--run", self.out, "--oracle", source, "--out", target)
            self.assertEqual(code, 0, err)
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved, evaluate(json.loads((self.out / "bundle.json").read_text(encoding="utf-8")),
                                             expected))
            self.assertIs(saved["source_snapshot_verified"], True)
            before = target.read_bytes()
            self.assertEqual(invoke("evaluate-map", "--run", self.out, "--oracle", source, "--out", target)[0], 2)
            self.assertEqual(target.read_bytes(), before)  # an existing result is never overwritten
        configs.assert_not_called()


class AtomicityHintTests(InspectorCase):
    def test_atomicity_hints_are_optional_generic_heuristics_that_never_gate_the_review(self):
        plain = self.inspected()
        self.assertComplete(plain)
        self.assertEqual({identifier: view["hints"] for identifier, view in views(plain).items()},
                         {op["id"]: [] for op in plain["process_map"]["operations"]})
        sites = tuple((path, line, key, "result" if (path, line) == (WEBHOOK, 11) else role)
                      for path, line, key, role in SITES)
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(
            brief, attempt, sites=sites, names={"refund": "Выполнить возврат и уведомить покупателя"}))
        self.assertComplete(bundle)
        ids, seen = operation_ids(bundle), views(bundle)
        hints = seen[ids["refund"]]["hints"]
        self.assertLessEqual({"conjunction_in_name", "several_result_sites"}, {item["signal"] for item in hints})
        self.assertEqual({(item["kind"], item["semantic_verification"]) for item in hints}, {("heuristic", False)})
        self.assertEqual((seen[ids["accept"]]["hints"], seen[ids["release"]]["hints"]), ([], []))
        # Non-gating: decisions, status, open questions and saved records do not depend on hints.
        for key in ("status", "status_reasons"):
            self.assertEqual(bundle["atomic_map"][key], plain["atomic_map"][key])
        self.assertEqual(bundle["atomic_map"]["summary"]["decisions"], plain["atomic_map"]["summary"]["decisions"])
        self.assertEqual(bundle["process_map"]["unknowns"], plain["process_map"]["unknowns"])
        for part in ("atomic_map", "process_map", "log_coverage", "manifest"):
            self.assertNotIn("heuristic", json.dumps(bundle[part], ensure_ascii=False))
        block = section(card_of(build_html(bundle), number_of(bundle, ids["refund"])), "hints")
        self.assertRegex(text_of(block), re.compile(r"эвристик", re.I))
        # Generic: no repository- or pilot-specific vocabulary in the hint implementation.
        import process_observability.operation_view as module
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        for word in ("saleor", "p1679", "p3283"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
