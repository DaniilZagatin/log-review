"""Faults in report stages never cost the map, earlier results or terminal statuses."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.errors import LlmError
from pko.llm.client import ChatResult
from pko.observability.contracts import validate_report
from pko.observability.model_stage import ReportBudget, StageModel
from pko.observability.pipeline import ReportOptions, run_observability
from pko.scenario_map.runner import run_scenario
from observability_script import ScriptedReportClient
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources
from test_process_report import Tree, model, tree


def many_sites(count):
    lines = "\n".join(f"    logger.info('event.{number}', request_id=request)" for number in range(count))
    source = f"import logging\nlogger = logging.getLogger(__name__)\n\n\ndef accept(request):\n{lines}\n"
    data = model()
    data["nodes"] = [{"id": "step-1", "kind": "operation", "name": "Принять заявку",
                      "evidence": [{"path": "src/app.py", "line_start": 5, "line_end": 5 + count, "quote": ""}]}]
    data["edges"], data["read_coverage"] = [], [{"path": "src/app.py", "ranges": [[1, 5 + count]]}]
    return data, Tree({"src/app.py": source})


def stage(report, stage_id):
    return next(row for row in report["stages"] if row["id"] == stage_id)


class Unavailable:
    spec = SPEC

    def chat(self, messages, **kwargs):
        raise LlmError("Провайдер отклонил запрос.", "400 Bad Request")


# Retained report implementation stays covered while its product entry points are disabled.
@patch("pko.scenario_map.runner.OBSERVABILITY_ENABLED", True)
class RunFaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_unavailable_report_model_keeps_map_report_and_handoff(self):
        output = self.root / "out"
        result = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                              report_options=ReportOptions(client=Unavailable()))
        self.assertEqual(result["run"]["status"], "completed")
        saved = json.loads((output / "scenario_map.json").read_text(encoding="utf-8"))
        report = json.loads((output / "process_report.json").read_text(encoding="utf-8"))
        validate_report(report, saved)
        self.assertEqual(stage(report, "logging_inventory")["execution_status"], "completed")
        self.assertEqual(stage(report, "process_context")["execution_status"], "failed")
        self.assertEqual({stage(report, name)["execution_status"] for name in
                          ("fact_requirements", "logging_coverage", "instrumentation_plan", "metric_catalog")}, {"blocked"})
        self.assertTrue((output / "handoff" / "manifest.json").exists())

    def test_interrupt_during_report_leaves_the_published_map(self):
        output = self.root / "interrupted"
        with patch("pko.scenario_map.runner.run_observability", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                             report_options=ReportOptions(client=ScriptedReportClient()))
        saved = json.loads((output / "scenario_map.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["run"]["status"], "completed")
        audit = next((output / "audit").glob("run_*.txt")).read_text(encoding="utf-8")
        self.assertIn('"result": "interrupted"', audit)

    def test_failed_local_journal_is_visible_in_the_result(self):
        output = self.root / "journal"
        with patch("pko.audit.harden_file", side_effect=OSError("disk is read-only")):
            result = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient())
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual(result["run"]["audit_status"], "partial")
        self.assertTrue((output / "scenario_map.json").exists())


class ModelFaultTests(unittest.TestCase):
    def run_with(self, client, data=None, source=None, requests=24):
        stage_model = StageModel(client, ReportBudget(max_requests=requests, deadline=time.monotonic() + 60))
        return run_observability(data or model(), source or tree(), stage_model=stage_model)

    def test_transient_network_error_counts_every_physical_request(self):
        scripted, calls = ScriptedReportClient(), []

        class Flaky:
            spec = SPEC

            def chat(self, messages, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionError("connection reset")
                return scripted.chat(messages, **kwargs)
        with patch("pko.scenario_map.model_retry.time.sleep"):
            report = self.run_with(Flaky())
        self.assertEqual(stage(report, "event_binding_review")["result_status"], "complete")
        self.assertEqual(report["budget"]["by_stage"]["event_binding"], 2)
        self.assertEqual(len(calls), report["budget"]["used_requests"])

    def test_answer_as_fenced_json_text_is_accepted(self):
        scripted = ScriptedReportClient()

        class Fenced:
            spec = SPEC

            def chat(self, messages, **kwargs):
                reply = scripted.chat(messages, **kwargs)
                arguments = reply.tool_calls[0]["function"]["arguments"]
                return ChatResult(text=f"```json\n{arguments}\n```", usage={})
        report = self.run_with(Fenced())
        self.assertEqual({row["execution_status"] for row in report["stages"]}, {"completed"})

    def test_packet_larger_than_the_window_is_split_not_failed(self):
        data, source = many_sites(8)
        probe = ScriptedReportClient()
        self.run_with(probe, data, source)
        first = json.dumps(probe.requests[0][1], ensure_ascii=False, separators=(",", ":"))
        system = len(json.dumps({"role": "system", "content": "x"})) + 3000
        # A limit just below the six-site packet forces halves of three sites.
        limit = len(json.dumps({"role": "user", "content": first}, ensure_ascii=False)) + system - 1
        client = ScriptedReportClient()
        with patch("pko.observability.model_stage.context_budget", return_value=(limit, 1024)):
            report = self.run_with(client, data, source)
        sizes = [len(payload["sites"]) for tool, payload in client.requests if tool == "submit_event_bindings"]
        self.assertTrue(max(sizes) < 6, sizes)
        self.assertEqual(sum(sizes), 8)
        self.assertEqual({site["review_status"] for site in report["logging"]["sites"]}, {"confirmed"})

    def test_site_that_never_fits_is_deferred_with_a_reason(self):
        data, source = many_sites(2)
        with patch("pko.observability.model_stage.context_budget", return_value=(100, 1024)):
            report = self.run_with(ScriptedReportClient(), data, source)
        sites = report["logging"]["sites"]
        self.assertEqual({site["review_status"] for site in sites}, {"deferred"})
        self.assertTrue(all("не помещается" in site["review_reasons"][0] for site in sites))
        self.assertEqual(stage(report, "process_context")["execution_status"], "failed")
        self.assertIn("не помещается", stage(report, "process_context")["reasons"][0])

    def test_binding_review_cannot_starve_later_stages(self):
        data, source = many_sites(40)
        client = ScriptedReportClient()
        with patch("pko.observability.binding.MAX_BATCHES", 20):
            report = self.run_with(client, data, source, requests=10)
        self.assertEqual(report["budget"]["by_stage"]["event_binding"], 5)
        self.assertEqual(stage(report, "event_binding_review")["result_status"], "partial")
        for name in ("process_context", "fact_requirements", "instrumentation_plan", "metric_catalog"):
            self.assertNotEqual(stage(report, name)["execution_status"], "interrupted", name)
        self.assertEqual(set(report["budget"]["by_stage"]), {"event_binding", "process_context", "fact_requirements",
                                                             "instrumentation", "metric_catalog"})


if __name__ == "__main__":
    unittest.main()
