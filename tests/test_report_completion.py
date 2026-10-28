"""Completed report dependencies, explicit omissions and durable delivery outcomes."""

import copy
import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.audit import current_audit
from pko.observability.contracts import validate_report
from pko.observability.metrics import readiness
from pko.observability.pipeline import ReportOptions
from pko.scenario_map import runner
from observability_script import ScriptedReportClient
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources
from test_process_stages import run, scripted, stage


class CompletionTests(unittest.TestCase):
    def test_reconciled_fact_links_its_changes_back_to_the_requesting_metric(self):
        def extra(answer, payload):
            answer["metrics"][0]["missing_facts"] = [{"meaning": "Зафиксировать передачу результата",
                "lifecycle": "result", "operation_ids": ["step-1"]}]
        report = run(ScriptedReportClient({"submit_metric_definitions": scripted("submit_metric_definitions", extra)}))
        requested = report["requested_facts"][0]
        self.assertEqual(requested["status"], "assessed")
        metric = next(row for row in report["metrics"] if row["id"] == requested["requested_by"])
        changes = {row["id"] for row in report["instrumentation"] if requested["fact_id"] in row["fact_ids"]}
        self.assertTrue(changes)
        self.assertIn(requested["fact_id"], metric["fact_ids"])
        self.assertTrue(changes.issubset(metric["change_ids"]))
        self.assertFalse(metric["missing_facts"])

    def test_question_and_metric_limits_remain_explicit_after_repair(self):
        def questions(answer, payload):
            answer["measurement_questions"] = [dict(answer["measurement_questions"][0], id=f"q-{n}")
                                                 for n in range(1, 14)]
        report = run(ScriptedReportClient({"submit_process_context": scripted("submit_process_context", questions)}))
        row = stage(report, "process_context")
        self.assertEqual(row["result_status"], "partial")
        self.assertEqual(len(report["process_context"]["measurement_questions"]), 12)
        self.assertIn("лимит 12", " ".join(row["reasons"]))

        def metrics(answer, payload):
            answer["metrics"] = [dict(answer["metrics"][0], id=f"m-{n}") for n in range(1, 17)]
        report = run(ScriptedReportClient({"submit_metric_definitions": scripted("submit_metric_definitions", metrics)}))
        self.assertEqual(stage(report, "metric_catalog")["result_status"], "partial")
        self.assertEqual(len(report["metrics"]), 15)
        self.assertIn("15", " ".join(stage(report, "metric_catalog")["reasons"]))

    def test_readiness_explains_missing_rules_and_explicit_duration_boundaries(self):
        metric = {"fact_ids": [], "missing_facts": [], "rules": {}, "requires_interval": True}
        result = readiness(metric, {}, None)
        text = " ".join(result["blockers"])
        for expected in ("незавершённых", "поздних", "повторных попыток", "повторных доставок",
                         "факт начала", "факт окончания"):
            self.assertIn(expected, text)
        self.assertEqual(result["components"]["definition"], "incomplete")
        metric.update(requires_interval=False, measure_unit="минуты", name="Длительность")
        self.assertNotIn("интервала", " ".join(readiness(metric, {}, None)["blockers"]))

    def test_duration_question_drives_boundaries_without_parsing_metric_text(self):
        def duration(answer, payload):
            answer["metrics"][0].update(question_ids=["q-2"], name="Показатель обработки", measure_unit="единица")
        report = run(ScriptedReportClient({"submit_metric_definitions": scripted("submit_metric_definitions", duration)}))
        metric = report["metrics"][0]
        self.assertTrue(metric["requires_interval"])
        self.assertIn("факт начала", " ".join(metric["readiness"]["blockers"]))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name) / "report"
        self.environment = patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def publish(self, client=None, **kwargs):
        return runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient(),
                                   observability=True, report_options=ReportOptions(client=client or ScriptedReportClient()),
                                   **kwargs)

    def snapshot(self):
        model = json.loads((self.out / "scenario_map.json").read_text(encoding="utf-8"))
        report = json.loads((self.out / "process_report.json").read_text(encoding="utf-8"))
        html = (self.out / "scenario_map.html").read_text(encoding="utf-8")
        embedded = {name: json.loads(content) for name, content in re.findall(
            r'<script id="(scenario-model|process-report)" type="application/json">(.*?)</script>', html, re.S)}
        self.assertEqual(embedded["scenario-model"], model)
        self.assertEqual(embedded["process-report"], report)
        self.assertEqual(model["run"]["delivery"], report["delivery"])
        validate_report(report, model)
        return model, report

    def assert_manifest(self, report):
        manifest = json.loads((self.out / "handoff/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["delivery"], report["delivery"])
        for row in manifest["files"]:
            raw = (self.out / "handoff" / row["name"]).read_bytes()
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (row["bytes"], row["sha256"]), row["name"])

    def test_demo_snapshot_matches_returned_state_html_and_manifest(self):
        result = self.publish(demo=True)
        model, report = self.snapshot()
        self.assertEqual(model, result)
        self.assertTrue(model["run"]["demo"])
        self.assertEqual(report["delivery"]["handoff"]["status"], "completed")
        self.assertEqual(report["delivery"]["trace_export"]["status"], "disabled")
        self.assert_manifest(report)

    def test_failed_handoff_is_persisted_without_a_success_path(self):
        with patch.object(runner, "build_handoff", side_effect=OSError("handoff disk unavailable")):
            result = self.publish()
        model, report = self.snapshot()
        self.assertEqual(result, model)
        self.assertEqual(report["delivery"]["handoff"]["status"], "failed")
        self.assertNotIn("handoff", model["run"])
        self.assertIn("handoff disk unavailable", model["run"]["handoff_error"])

    def test_final_publication_retries_once_and_rebuilds_exact_hashes(self):
        real_write, failures = runner.write_outputs, []
        def write(destination, files):
            if "process-report" in files and not failures:
                failures.append(True)
                raise OSError("one transient publication failure")
            return real_write(destination, files)
        with patch.object(runner, "write_outputs", side_effect=write):
            self.publish()
        _, report = self.snapshot()
        self.assertEqual(len(failures), 1)
        self.assertEqual(report["delivery"]["report"]["status"], "partial")
        self.assert_manifest(report)

    def test_late_audit_failure_is_consistent_with_exported_hashes(self):
        real_handoff = runner.build_handoff
        def export(*args):
            current_audit().failed = True
            return real_handoff(*args)
        with patch.object(runner, "build_handoff", side_effect=export):
            self.publish()
        model, report = self.snapshot()
        self.assertEqual(model["run"]["audit_status"], "partial")
        self.assertEqual(report["delivery"]["audit"]["status"], "partial")
        self.assert_manifest(report)

    def test_interrupt_after_completed_stages_preserves_report_and_cache(self):
        def interrupt(payload, messages):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.publish(ScriptedReportClient({"submit_fact_requirements": interrupt}))
        model, report = self.snapshot()
        self.assertEqual(model["run"]["status"], "completed")
        self.assertEqual(stage(report, "process_context")["execution_status"], "completed")
        self.assertEqual(stage(report, "fact_requirements")["execution_status"], "interrupted")
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["delivery"]["report"]["status"], "partial")
        checkpoint = json.loads((self.out / "process_report.checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["process_context"], report["process_context"])
        self.assertTrue(json.loads((self.out / "report_cache.json").read_text(encoding="utf-8")))
        self.assert_manifest(report)
