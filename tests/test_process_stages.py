"""Context, required facts, coverage axes, changes and metric definitions."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from pko.errors import PkoError
from pko.observability.contracts import ReportError, validate_report
from pko.observability.coverage import assess_fact
from pko.observability.inputs import load_business_text, parse_field_map, profile_runtime_log
from pko.observability.instrumentation import find_cycle
from pko.observability.model_stage import ReportBudget, StageModel
from pko.observability.pipeline import ReportOptions, run_observability
from observability_script import ScriptedReportClient
from test_process_report import model, tree


def run(client=None, data=None, options=None, requests=24):
    client = client or ScriptedReportClient()
    model_stage = StageModel(client, ReportBudget(max_requests=requests, deadline=time.monotonic() + 60))
    return run_observability(data or model(), tree(), stage_model=model_stage, options=options)


def stage(report, stage_id):
    return next(row for row in report["stages"] if row["id"] == stage_id)


def scripted(tool, change):
    """Override one tool: take the scripted answer and let ``change`` edit it."""
    def handler(payload, messages):
        answer = getattr(ScriptedReportClient(), tool)(payload)
        change(answer, payload)
        return answer
    return handler


class FullReportTests(unittest.TestCase):
    def test_all_stages_complete_and_metrics_stay_unready_without_values(self):
        data = model()
        report = run(data=data)
        self.assertEqual({row["execution_status"] for row in report["stages"]}, {"completed"})
        validate_report(report, data)
        self.assertEqual([q["id"] for q in report["process_context"]["measurement_questions"]], ["q-1", "q-2"])
        self.assertEqual(report["process_context"]["case"]["identifiers"][0], {"name": "request_id", "source": "code",
                                                                               "where": "поля записи"})
        observed = next(fact for fact in report["facts"] if fact["observed_by"])
        self.assertEqual(observed["operation_ids"], ["step-1"])
        self.assertTrue(report["metrics"])
        for metric in report["metrics"]:
            self.assertIsNone(metric["value"])
            self.assertEqual(metric["readiness"]["status"], "not_ready")
            self.assertEqual(metric["readiness"]["components"]["calculator"], "not_implemented")
            self.assertEqual(metric["readiness"]["components"]["data"], "not_provided")
        self.assertEqual(report["coverage"]["summary"]["obligations_status"], "proposed")
        self.assertTrue(report["instrumentation"])
        self.assertEqual(set(report["methodology"]), {"event_binding", "process_context", "fact_requirements",
                                                      "instrumentation", "metric_catalog"})

    def test_code_identifier_must_appear_in_code_or_stay_a_hypothesis(self):
        def invent(answer, payload):
            answer["case"]["identifiers"] = [{"name": "client_uuid", "source": "code"}]
        client = ScriptedReportClient({"submit_process_context": [scripted("submit_process_context", invent)] * 2})
        report = run(client)
        self.assertEqual(report["process_context"]["case"]["identifiers"], [])
        self.assertEqual(stage(report, "process_context")["result_status"], "partial")

    def test_fact_cannot_be_observed_by_a_record_of_another_operation(self):
        def misattribute(answer, payload):
            fact = next(fact for fact in answer["facts"] if "step-1" in fact["operation_ids"] and fact["observed_by"])
            fact["operation_ids"] = ["step-3"]
        client = ScriptedReportClient({"submit_fact_requirements": [scripted("submit_fact_requirements", misattribute)] * 2})
        report = run(client)
        self.assertFalse(any("step-3" in fact["operation_ids"] for fact in report["facts"]))
        self.assertTrue(report["facts"], "other facts are kept")
        self.assertEqual(stage(report, "fact_requirements")["result_status"], "partial")

    def test_rejected_facts_block_coverage_changes_and_metrics(self):
        def broken(answer, payload):
            for fact in answer["facts"]:
                fact["question_ids"] = ["q-99"]
        client = ScriptedReportClient({"submit_fact_requirements": [scripted("submit_fact_requirements", broken)] * 2})
        report = run(client)
        self.assertEqual(stage(report, "fact_requirements")["execution_status"], "failed")
        self.assertEqual([stage(report, name)["execution_status"] for name in
                          ("logging_coverage", "instrumentation_plan", "metric_catalog")], ["blocked"] * 3)
        self.assertIn("process_context", report)


class CoverageTests(unittest.TestCase):
    def fact(self, **values):
        base = {"id": "f-1", "lifecycle": "result", "operation_ids": ["step-1"], "outside_application": False,
                "needed": ["case_id", "timestamp"], "observed_by": ["b-1"], "field_sources": []}
        return {**base, **values}

    def binding(self, **values):
        base = {"id": "b-1", "site_id": "s-1", "status": "confirmed", "event_role": "result",
                "lifecycle_moment": "after_action", "case_fields": ["request_id"]}
        return {"b-1": {**base, **values}}

    sites = {"s-1": {"id": "s-1", "kind": "log_call", "event_key": "application.accepted", "key_status": "literal",
                     "fields": ["request_id"], "fields_status": "syntactic"}}

    def test_observable_fact_uses_record_time_and_separate_axes(self):
        row = assess_fact(self.fact(), self.binding(), self.sites, None)
        self.assertTrue(row["observable_in_code"])
        self.assertEqual(row["axes"]["time_source"], "record_time")
        self.assertEqual((row["axes"]["runtime"], row["axes"]["quality"]), ("not_provided", "not_provided"))

    def test_result_written_before_commit_is_premature_and_wrong_role_is_incompatible(self):
        early = assess_fact(self.fact(), self.binding(lifecycle_moment="before_commit"), self.sites, None)
        self.assertEqual(early["axes"]["timing"], "premature")
        self.assertIn("timing", early["gaps"])
        start = assess_fact(self.fact(lifecycle="start"), self.binding(), self.sites, None)
        self.assertEqual(start["axes"]["semantics"], "incompatible")
        self.assertFalse(start["observable_in_code"])

    def test_unreviewed_candidates_make_absence_unknown_not_missing(self):
        sites = {"s-2": {"id": "s-2", "link_status": "linked", "operation_ids": ["step-1"], "review_status": "deferred"}}
        row = assess_fact(self.fact(observed_by=[]), {}, sites, None)
        self.assertEqual(row["axes"]["emit"], "unexplained_candidates")
        self.assertIn("emit", row["unknowns"])
        missing = assess_fact(self.fact(observed_by=[]), {}, {}, None)
        self.assertEqual(missing["axes"]["emit"], "not_found")

    def test_runtime_profile_reports_presence_without_claiming_delivery(self):
        runtime = {"event_counts": {"application.accepted": 3}, "fields_by_event": {"application.accepted": ["request_id"]},
                   "missing_case_count": 0, "invalid_time_count": 0}
        row = assess_fact(self.fact(), self.binding(), self.sites, runtime)
        self.assertEqual((row["axes"]["runtime"], row["axes"]["quality"]), ("records_present", "no_issues_found"))
        absent = assess_fact(self.fact(), self.binding(), self.sites, {**runtime, "event_counts": {}})
        self.assertEqual(absent["axes"]["runtime"], "no_records")

    def test_fact_outside_application_is_not_counted_as_missing(self):
        row = assess_fact(self.fact(outside_application=True, operation_ids=[], observed_by=[]), {}, {}, None)
        self.assertEqual((row["axes"]["emit"], row["gaps"]), ("outside_application", []))


class InstrumentationTests(unittest.TestCase):
    def test_anchor_outside_issued_lines_is_rejected_and_key_status_is_set_by_code(self):
        def wander(answer, payload):
            answer["changes"][0]["location"] = {"status": "anchored", "path": "src/Issuance.java", "line_start": 1, "line_end": 2}
        client = ScriptedReportClient({"submit_instrumentation_changes":
                                       [scripted("submit_instrumentation_changes", wander)] * 2})
        report = run(client)
        self.assertNotIn("c-1", [change["id"] for change in report["instrumentation"]])
        self.assertEqual(stage(report, "instrumentation_plan")["result_status"], "partial")
        for change in report["instrumentation"]:
            if change["action"] == "add_event":
                self.assertEqual(change["event"]["key_status"], "proposed")
            if change["location"]["status"] == "anchored":
                self.assertIn("quote", change["location"])

    def test_dependency_cycles_are_rejected(self):
        def cycle(answer, payload):
            answer["changes"] = answer["changes"][:2]
            answer["changes"][0]["depends_on"] = ["c-2"]
            answer["changes"][1]["depends_on"] = ["c-1"]
        client = ScriptedReportClient({"submit_instrumentation_changes":
                                       [scripted("submit_instrumentation_changes", cycle)] * 2})
        report = run(client)
        self.assertEqual(report["instrumentation"], [])
        self.assertEqual(find_cycle([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]), ["a", "b"])

    def test_report_rejects_agreed_keys(self):
        data = model()
        report = run(data=data)
        agreed = copy.deepcopy(report)
        agreed["instrumentation"][0]["event"]["key_status"] = "agreed"
        with self.assertRaises(ReportError):
            validate_report(agreed, data)


class MetricTests(unittest.TestCase):
    def test_model_cannot_supply_values_and_missing_facts_are_registered(self):
        def value(answer, payload):
            answer["metrics"][0]["value"] = 0.42
        client = ScriptedReportClient({"submit_metric_definitions": [scripted("submit_metric_definitions", value)] * 2})
        report = run(client)
        self.assertNotIn("m-1", [metric["id"] for metric in report.get("metrics") or []])
        self.assertIn("значение метрики не задаётся", " ".join(stage(report, "metric_catalog")["reasons"]))

        def need(answer, payload):
            answer["metrics"][0]["missing_facts"] = [{"meaning": "Клиент оценил обслуживание", "lifecycle": "result"}]
        report = run(ScriptedReportClient({"submit_metric_definitions": [scripted("submit_metric_definitions", need)]}))
        [requested] = report["requested_facts"]
        self.assertEqual((requested["requested_by"], requested["status"]), ("m-1", "unassessed"))
        metric = report["metrics"][0]
        self.assertEqual(metric["readiness"]["components"]["facts"], "missing")

    def test_runtime_sample_changes_only_the_data_component(self):
        runtime = {"event_counts": {}, "fields_by_event": {}, "missing_case_count": 0, "invalid_time_count": 0,
                   "record_count": 0, "limitations": []}
        report = run(options=ReportOptions(runtime=runtime))
        self.assertEqual({metric["readiness"]["components"]["data"] for metric in report["metrics"]}, {"sample_only"})
        self.assertEqual(report["runtime_profile"], runtime)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_business_text_is_masked_and_bounded(self):
        path = self.root / "business.md"
        path.write_text('Процесс выдачи карты.\npassword = "hunter2"\n', encoding="utf-8")
        business = load_business_text(path)
        self.assertNotIn("hunter2", business["text"])
        path.write_bytes(b"x" * 200_001)
        with self.assertRaises(PkoError):
            load_business_text(path)

    def test_runtime_profile_aggregates_and_rejects_invalid_records(self):
        path = self.root / "logs.jsonl"
        rows = [{"event": "application.accepted", "request_id": "r-1", "ts": "2026-10-01T10:00:00+03:00", "id": 1},
                {"event": "application.accepted", "request_id": "r-1", "ts": "2026-10-01T10:00:00+03:00", "id": 1},
                {"event": "card.issued", "ts": "2026-10-01 10:05", "id": 2}]
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        fields = parse_field_map("event_key=event,case_id=request_id,occurred_at=ts,event_id=id")
        profile = profile_runtime_log(path, fields)
        self.assertEqual(profile["event_counts"], {"application.accepted": 2, "card.issued": 1})
        self.assertEqual((profile["missing_case_count"], profile["invalid_time_count"], profile["duplicate_record_count"]),
                         (1, 1, 1))
        path.write_text("{not json}\n", encoding="utf-8")
        with self.assertRaises(PkoError):
            profile_runtime_log(path, fields)
        with self.assertRaises(PkoError):
            parse_field_map("case=id")


if __name__ == "__main__":
    unittest.main()
