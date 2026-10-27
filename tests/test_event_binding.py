"""Model-reviewed event bindings: host checks, one repair, conflicts and budget."""

import copy
import time
import unittest

from pko.observability.contracts import ReportError, validate_report
from pko.observability.model_stage import ReportBudget, StageModel
from pko.observability.pipeline import run_observability
from observability_script import ScriptedReportClient
from test_process_report import Tree, model, tree


def stage_model(client, requests=10):
    return StageModel(client, ReportBudget(max_requests=requests, deadline=time.monotonic() + 60))


def by_site(report):
    return {(site["path"], site["line_start"]): site for site in report["logging"]["sites"]}


def stage_row(report, stage_id):
    return next(row for row in report["stages"] if row["id"] == stage_id)


def bindings_for(report, site):
    return [row for row in report["logging"]["event_bindings"] if row["site_id"] == site["id"]]


class EventBindingTests(unittest.TestCase):
    def test_confirmed_binding_carries_role_case_fields_and_exact_quote(self):
        client = ScriptedReportClient()
        report = run_observability(model(), tree(), stage_model=stage_model(client))
        sites = by_site(report)
        [binding] = bindings_for(report, sites[("src/app.py", 7)])
        self.assertEqual((binding["status"], binding["operation_id"], binding["event_role"], binding["authority_status"]),
                         ("confirmed", "step-1", "result", "proposed"))
        self.assertEqual(binding["case_fields"], ["request_id"])
        self.assertIn('logger.info(ACCEPTED', binding["evidence"][0]["quote"])
        self.assertEqual(sites[("src/app.py", 18)]["review_status"], "not_reviewed")
        self.assertEqual(sites[("src/Issuance.java", 4)]["review_status"], "not_reviewed")
        stage = stage_row(report, "event_binding_review")
        self.assertEqual((stage["execution_status"], stage["result_status"]), ("completed", "complete"))
        # Fragments shown to the model are numbered masked lines of the enclosing function.
        tool, payload = client.requests[0]
        self.assertEqual(tool, "submit_event_bindings")
        self.assertTrue(any(fragment["text"].startswith("6| def accept") for fragment in payload["fragments"]))
        self.assertEqual(report["budget"]["by_stage"]["event_binding"], 1)

    def test_citation_outside_issued_lines_gets_one_repair_then_is_rejected(self):
        def outside(payload, messages):
            answer = ScriptedReportClient().submit_event_bindings(payload)
            for item in answer["bindings"]:
                item["evidence"] = [{"path": "src/app.py", "line_start": 1, "line_end": 3}]
            return answer
        client = ScriptedReportClient({"submit_event_bindings": [outside, outside]})
        report = run_observability(model(), tree(), stage_model=stage_model(client))
        site = by_site(report)[("src/app.py", 7)]
        self.assertEqual(site["review_status"], "rejected")
        self.assertTrue(any("не входит в выданные фрагменты" in reason for reason in site["review_reasons"]))
        self.assertEqual(sum(tool == "submit_event_bindings" for tool, _ in client.requests), 2, "exactly one repair turn")
        self.assertEqual(stage_row(report, "event_binding_review")["result_status"], "partial")

    def test_repair_turn_receives_addressed_errors_and_can_fix_the_answer(self):
        seen = {}

        def wrong_operation(payload, messages):
            answer = ScriptedReportClient().submit_event_bindings(payload)
            answer["bindings"][0]["operation_id"] = "step-99"
            return answer

        def repaired(payload, messages):
            seen["feedback"] = messages[-1]["content"]
            return ScriptedReportClient().submit_event_bindings(payload)
        client = ScriptedReportClient({"submit_event_bindings": [wrong_operation, repaired]})
        report = run_observability(model(), tree(), stage_model=stage_model(client))
        self.assertIn("операций-кандидатов", seen["feedback"])
        self.assertEqual(by_site(report)[("src/app.py", 7)]["review_status"], "confirmed")

    def test_several_interpretations_need_distinct_conditions(self):
        def twice(payload, messages):
            answer = ScriptedReportClient().submit_event_bindings(payload)
            ambiguous = next(site for site in payload["sites"] if len(site["candidate_operation_ids"]) == 2)
            first = next(item for item in answer["bindings"] if item["site_id"] == ambiguous["site_id"])
            second = copy.deepcopy(first)
            second["operation_id"] = ambiguous["candidate_operation_ids"][1]
            answer["bindings"].append(second)
            return answer
        client = ScriptedReportClient({"submit_event_bindings": [twice]})
        report = run_observability(model(), tree(), stage_model=stage_model(client))
        site = by_site(report)[("src/app.py", 13)]
        self.assertEqual(site["review_status"], "conflict")
        self.assertEqual({row["status"] for row in bindings_for(report, site)}, {"conflict"})

    def test_wrapper_call_requires_the_wrapper_lines(self):
        source = ('import logging\nlogger = logging.getLogger(__name__)\n\n\ndef audit(message):\n'
                  '    logger.info(message)\n\n\ndef accept(request):\n    audit("application.accepted")\n'
                  '    return request\n')
        data = model()
        data["nodes"] = [{"id": "step-1", "kind": "operation", "name": "Принять заявку",
                          "evidence": [{"path": "src/app.py", "line_start": 9, "line_end": 11, "quote": ""}]}]
        data["edges"], data["read_coverage"] = [], [{"path": "src/app.py", "ranges": [[1, 11]]}]

        def call_site_only(payload, messages):
            site = payload["sites"][0]
            return {"bindings": [{"site_id": site["site_id"], "decision": "bind", "operation_id": "step-1",
                                  "event_role": "result", "lifecycle_moment": "after_action",
                                  "repeat_semantics": "once_per_execution", "business_meaning": "Заявка принята",
                                  "evidence": [{"path": site["path"], "line_start": site["line_start"],
                                                "line_end": site["line_end"]}]}]}
        client = ScriptedReportClient({"submit_event_bindings": [call_site_only, call_site_only]})
        report = run_observability(data, Tree({"src/app.py": source}), stage_model=stage_model(client))
        site = by_site(report)[("src/app.py", 10)]
        self.assertEqual(site["kind"], "wrapper_call")
        self.assertEqual(site["review_status"], "rejected")
        self.assertTrue(any(fragment["line_start"] == 5 for fragment in client.requests[0][1]["fragments"]),
                        "the wrapper declaration is issued to the model")

    def test_budget_exhaustion_defers_remaining_sites(self):
        data = model()
        # Eight cited sites make two packets; one request leaves the second deferred.
        lines = "\n".join(f"    logger.info('event.{n}')" for n in range(8))
        source = f"import logging\nlogger = logging.getLogger(__name__)\n\n\ndef accept(request):\n{lines}\n"
        data["nodes"] = [{"id": "step-1", "kind": "operation", "name": "Принять заявку",
                          "evidence": [{"path": "src/app.py", "line_start": 5, "line_end": 13, "quote": ""}]}]
        data["edges"], data["read_coverage"] = [], [{"path": "src/app.py", "ranges": [[1, 13]]}]
        report = run_observability(data, Tree({"src/app.py": source}),
                                   stage_model=stage_model(ScriptedReportClient(), requests=1))
        statuses = [site["review_status"] for site in report["logging"]["sites"]]
        self.assertEqual(statuses.count("confirmed"), 6)
        self.assertEqual(statuses.count("deferred"), 2)
        stage = stage_row(report, "event_binding_review")
        self.assertEqual((stage["execution_status"], stage["result_status"]), ("completed", "partial"))
        self.assertEqual(stage_row(report, "process_context")["execution_status"], "interrupted")

    def test_report_rejects_accepted_or_uncited_interpretations(self):
        data = model()
        report = run_observability(data, tree(), stage_model=stage_model(ScriptedReportClient()))
        accepted = copy.deepcopy(report)
        accepted["logging"]["event_bindings"][0]["authority_status"] = "accepted"
        with self.assertRaises(ReportError):
            validate_report(accepted, data)
        uncited = copy.deepcopy(report)
        uncited["logging"]["event_bindings"][0]["evidence"] = []
        with self.assertRaises(ReportError):
            validate_report(uncited, data)


if __name__ == "__main__":
    unittest.main()
