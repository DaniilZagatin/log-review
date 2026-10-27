"""Regressions from the review of the merge: coverage, repairs, readiness, trace, inputs, reuse."""

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.observability.coverage import assess_fact
from pko.observability.contracts import validate_report
from pko.observability.inputs import load_business_text
from pko.observability.log_discovery import discover_logging
from pko.observability.metrics import readiness
from pko.observability.model_stage import ReportBudget, StageModel
from pko.observability.pipeline import ReportOptions, run_observability
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.agent import run_agent
from pko.scenario_map.runner import run_report_from_map, run_scenario
from pko.scenario_map.sources import build_workspace
from observability_script import ScriptedReportClient
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources
from test_process_report import Tree, model, tree


def run(client, data=None, source=None, requests=24, options=None):
    stage_model = StageModel(client, ReportBudget(max_requests=requests, deadline=time.monotonic() + 60))
    return run_observability(data or model(), source or tree(), stage_model=stage_model, options=options)


def stage(report, stage_id):
    return next(row for row in report["stages"] if row["id"] == stage_id)


def scripted(tool, change):
    def handler(payload, messages):
        answer = getattr(ScriptedReportClient(), tool)(payload)
        change(answer, payload)
        return answer
    return handler


class CoverageByRecordTests(unittest.TestCase):
    sites = {"s-1": {"id": "s-1", "kind": "log_call", "fields": ["case_id"]},
             "s-2": {"id": "s-2", "kind": "log_call", "fields": []}}

    def test_two_insufficient_records_do_not_make_a_fact_observable(self):
        fact = {"id": "f-1", "lifecycle": "result", "operation_ids": ["step-1"], "outside_application": False,
                "needed": ["case_id"], "observed_by": ["b-req", "b-res"], "field_sources": []}
        bindings = {"b-req": {"id": "b-req", "site_id": "s-1", "event_role": "request",
                              "lifecycle_moment": "after_action", "case_fields": ["case_id"]},
                    "b-res": {"id": "b-res", "site_id": "s-2", "event_role": "result",
                              "lifecycle_moment": "before_commit", "case_fields": []}}
        row = assess_fact(fact, bindings, self.sites, None)
        self.assertFalse(row["observable_in_code"])
        self.assertEqual(row["record_id"], "b-res")
        self.assertEqual(set(row["gaps"]), {"correlation", "timing"})
        self.assertEqual(len(row["records"]), 2)

    def test_one_complete_record_is_enough(self):
        fact = {"id": "f-1", "lifecycle": "result", "operation_ids": ["step-1"], "outside_application": False,
                "needed": ["case_id"], "observed_by": ["b-res"], "field_sources": []}
        bindings = {"b-res": {"id": "b-res", "site_id": "s-1", "event_role": "result",
                              "lifecycle_moment": "after_commit", "case_fields": ["case_id"]}}
        self.assertTrue(assess_fact(fact, bindings, self.sites, None)["observable_in_code"])


class RepairAndDependencyTests(unittest.TestCase):
    def test_empty_repair_keeps_items_accepted_first(self):
        def partly(payload, messages):
            answer = ScriptedReportClient().submit_fact_requirements(payload)
            answer["facts"][-1]["question_ids"] = ["q-99"]
            return answer
        client = ScriptedReportClient({"submit_fact_requirements": [partly, lambda payload, messages: {"facts": []}]})
        report = run(client)
        self.assertTrue(report["facts"])
        self.assertEqual(stage(report, "fact_requirements")["execution_status"], "completed")
        self.assertIn("Отклонено", " ".join(stage(report, "fact_requirements")["reasons"]))

    def test_broken_dependency_chain_is_dropped_transitively_not_the_report(self):
        def chained(answer, payload):
            answer["changes"][0]["depends_on"] = ["c-99"]
            answer["changes"][1]["depends_on"] = ["c-1"]
        data = model()
        client = ScriptedReportClient({"submit_instrumentation_changes": [scripted("submit_instrumentation_changes", chained)] * 2})
        report = run(client, data)
        validate_report(report, data)
        self.assertNotIn("c-1", [change["id"] for change in report["instrumentation"]])
        self.assertNotIn("c-2", [change["id"] for change in report["instrumentation"]])
        self.assertEqual(stage(report, "instrumentation_plan")["result_status"], "partial")
        self.assertIn("metrics", report)

    def test_section_that_breaks_the_contract_takes_only_itself_and_successors(self):
        data = model()
        from pko.observability import metrics as metric_module
        original = metric_module.build_metrics

        def broken(*args, **kwargs):
            result, errors = original(*args, **kwargs)
            result[0]["fact_ids"] = ["f-404"]
            return result, errors
        with patch("pko.observability.pipeline.build_metrics", broken):
            report = run(ScriptedReportClient(), data)
        validate_report(report, data)
        self.assertNotIn("metrics", report)
        self.assertEqual(stage(report, "metric_catalog")["execution_status"], "failed")
        self.assertIn("контракт", stage(report, "metric_catalog")["reasons"][0])
        self.assertIn("instrumentation", report)
        self.assertEqual(stage(report, "requirement_reconciliation")["execution_status"], "blocked")


class ReadinessTests(unittest.TestCase):
    def test_every_coverage_gap_is_named_per_fact(self):
        row = {"fact_id": "f-1", "observable_in_code": False, "axes": {
            "emit": "found", "semantics": "compatible", "fields": "missing", "correlation": "identified",
            "timing": "premature", "time_source": "not_required", "runtime": "not_provided", "quality": "not_provided"}}
        result = readiness({"fact_ids": ["f-1"], "missing_facts": []}, {"f-1": row}, None,
                           {"f-1": {"meaning": "Решение об отказе"}})
        self.assertEqual(result["components"]["facts"], "incomplete")
        self.assertEqual((result["components"]["fields"], result["components"]["timing"]), ("gaps", "gaps"))
        line = next(blocker for blocker in result["blockers"] if blocker.startswith("f-1"))
        self.assertIn("нет нужных полей", line)
        self.assertIn("раньше", line)


class TraceStatusTests(unittest.TestCase):
    def test_failed_and_blocked_stages_end_with_their_status_and_calls_join_the_tree(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            output = Path(folder) / "out"
            client = ScriptedReportClient({"submit_fact_requirements": [lambda payload, messages: {"facts": 1}] * 2})
            run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                         report_options=ReportOptions(client=client))
            text = next((output / "audit").glob("run_*.txt")).read_text(encoding="utf-8")
        entries = [json.loads(part.split("\n", 1)[1]) for part in text.split("\n=== ")[1:]]
        started = {row["span_id"]: row for row in entries if row["event"] == "span_started"}
        finished = {row["name"]: row for row in entries if row["event"] == "span_finished"}
        self.assertEqual(finished["fact_requirements"]["status"], "error")
        self.assertEqual(finished["logging_coverage"]["status"], "blocked")
        def chain(span):
            names = []
            while span:
                names.append(started[span]["name"])
                span = started[span]["parent_span_id"]
            return names
        chains = [chain(row["span_id"]) for row in entries if row["event"] == "model_request" and row.get("span_id")]
        report_chain = next(names for names in chains if "process_context" in names)
        self.assertEqual(report_chain[:3], ["model_attempt", "report_model_request", "process_context"])
        self.assertEqual(report_chain[-2:], ["process_observability", "scenario_map_run"])
        self.assertTrue(any(names[:2] == ["model_attempt", "scenario_step"] for names in chains))


class TabsScriptTests(unittest.TestCase):
    def test_map_view_survives_resize_and_operation_panel_opens_over_the_map(self):
        data = model()
        page = render_scenario_map(data, run(ScriptedReportClient(), data))
        self.assertIn("if($('panel-map').inert){const view=$('viewport')", page)
        self.assertIn("if($('panel-map').inert&&mapTabHook)mapTabHook();", page)
        self.assertIn("mapTabHook=()=>", page)


class BusinessResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def opening(self, business):
        client = ScriptedClient()
        workspace = build_workspace(sources(), self.root / f"snap-{bool(business)}")
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            result = run_agent(workspace, SCENARIO, SPEC, client=client, max_steps=1, business=business)
        return json.loads(client.seen[1]["content"]), result

    def test_business_description_reaches_the_researcher_as_bounded_requirements(self):
        path = self.root / "business.md"
        path.write_text("Заявка на карту проходит проверку клиента и выпуск.\n" + "Термин. " * 6000, encoding="utf-8")
        business = load_business_text(path)
        opening, result = self.opening(business)
        self.assertEqual(opening["business_description"]["name"], "business.md")
        self.assertTrue(opening["business_description"]["truncated"])
        self.assertLessEqual(len(opening["business_description"]["text"]), result["run"]["context_budget_chars"] // 4)
        self.assertEqual(result["run"]["business_description"]["sha256"], business["sha256"])
        plain, _ = self.opening(None)
        self.assertNotIn("business_description", plain)


class JvmFieldTests(unittest.TestCase):
    def test_structured_arguments_and_mdc_keys_become_fields(self):
        java = ('class Issuance {\n    private static final Logger log = LoggerFactory.getLogger(Issuance.class);\n'
                '    void issue(Card card) {\n        MDC.put("case_id", card.getRequestId());\n'
                '        log.info("card.issued", kv("card_id", card.getId()), keyValue("status", "issued"));\n    }\n}\n')
        site = discover_logging(Tree({"Issuance.java": java}), ["Issuance.java"])["sites"][0]
        self.assertEqual(site["fields"], ["card_id", "case_id", "status"])
        self.assertEqual((site["context_fields"], site["fields_status"]), (["case_id"], "partial"))


class AnswerReuseTests(unittest.TestCase):
    def test_rerun_reuses_revalidated_answers_and_asks_again_when_inputs_change(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            output = Path(folder) / "out"
            first = ScriptedReportClient()
            run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                         report_options=ReportOptions(client=first))
            self.assertTrue(json.loads((output / "report_cache.json").read_text(encoding="utf-8")))
            second = ScriptedReportClient()
            run_report_from_map(output / "scenario_map.json", sources(), output, spec=SPEC,
                                report_options=ReportOptions(client=second))
            report = json.loads((output / "process_report.json").read_text(encoding="utf-8"))
            self.assertEqual(second.requests, [])
            self.assertEqual(report["budget"]["used_requests"], 0)
            self.assertGreater(report["budget"]["cache_hits"], 0)
            business = Path(folder) / "business.md"
            business.write_text("Новое бизнес-описание процесса.", encoding="utf-8")
            third = ScriptedReportClient()
            run_report_from_map(output / "scenario_map.json", sources(), output, spec=SPEC,
                                report_options=ReportOptions(client=third, business=load_business_text(business)))
            self.assertIn("submit_process_context", [tool for tool, _ in third.requests])


class ReconciliationTests(unittest.TestCase):
    def test_requested_fact_is_formalised_assessed_and_linked_once(self):
        def need(answer, payload):
            answer["metrics"][0]["missing_facts"] = [{"meaning": "Клиент оценил обслуживание", "lifecycle": "result",
                                                      "operation_ids": ["step-1"]}]
        data = model()
        client = ScriptedReportClient({"submit_metric_definitions": [scripted("submit_metric_definitions", need)]})
        report = run(client, data)
        validate_report(report, data)
        [requested] = report["requested_facts"]
        self.assertEqual(requested["status"], "assessed")
        metric = next(metric for metric in report["metrics"] if metric["id"] == "m-1")
        self.assertIn(requested["fact_id"], metric["fact_ids"])
        self.assertEqual(metric["missing_facts"], [])
        self.assertIn(requested["fact_id"], [row["fact_id"] for row in report["coverage"]["facts"]])
        self.assertTrue(any(requested["fact_id"] in change["fact_ids"] for change in report["instrumentation"]))
        self.assertEqual(sum(tool == "submit_metric_definitions" for tool, _ in client.requests), 1, "no second metric round")
        self.assertEqual(stage(report, "requirement_reconciliation")["execution_status"], "completed")


if __name__ == "__main__":
    unittest.main()
