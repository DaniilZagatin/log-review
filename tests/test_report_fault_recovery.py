"""Accepted report work survives provider faults; every physical attempt is bounded."""
import copy
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.audit import AuditedClient, audit_run, sanitize
from pko.errors import LlmError
from pko.llm.client import ChatResult
from pko.llm.tracing import TracedClient, flush_tracing, trace_span, trace_tool_call
from pko.observability.binding import review_bindings
from pko.observability.evidence import EvidenceRegistry
from pko.observability.instrumentation import build_changes
from pko.observability.log_discovery import discover_logging
from pko.observability.mapping import link_sites
from pko.observability.model_stage import BudgetExhausted, ReportBudget, StageModel
from observability_script import ScriptedReportClient
from scenario_script import SPEC
from test_process_report import Tree
from test_report_resilience import many_sites


TOOL = {"name": "submit_fact_requirements", "parameters": {"type": "object", "properties": {
    "facts": {"type": "array", "items": {"type": "object"}}}}}


def validate_facts(arguments):
    """Like the real stage, normalise accepted items and reject invalid IDs separately."""
    accepted, errors = [], []
    for item in arguments["facts"]:
        if not item.get("meaning"):
            errors.append(f"{item['id']}: нужен смысл факта")
        else:
            accepted.append({**item, "authority_status": "proposed"})
    return accepted, errors


def unavailable(*_):
    raise LlmError("Модель недоступна", "400 Bad Request")


def engine(client, requests=20, cache=None, deadline=None):
    return StageModel(client, ReportBudget(requests, time.monotonic() + 60 if deadline is None else deadline), cache)


def ask(stage):
    return stage.ask("fact_requirements", {"purpose": "test"}, TOOL, validate_facts, key=lambda row: row["id"])


def change_inputs(count):
    facts = [{"id": f"f-{n}", "meaning": f"Результат {n}", "operation_ids": ["step-1"],
              "lifecycle": "result", "observed_by": []} for n in range(1, count + 1)]
    report = {"facts": facts, "logging": {"sites": [], "event_bindings": []},
              "process_context": {"goal": "Выдать результат", "case": {"subject": "Заявка"},
                                  "measurement_questions": []},
              "coverage": {"facts": [{"fact_id": fact["id"], "gaps": ["emit"], "unknowns": [],
                                      "axes": {"emit": "not_found", "correlation": "missing"}} for fact in facts]},
              "instrumentation": []}
    model = {"nodes": [{"id": "step-1", "kind": "operation", "evidence": []}]}
    return model, report, EvidenceRegistry(Tree({}))


class ReportRecoveryTests(unittest.TestCase):
    def test_repair_fault_keeps_accepted_items_and_public_reason(self):
        client = ScriptedReportClient({"submit_fact_requirements": [
            lambda *_: {"facts": [{"id": "f-1", "meaning": "Заявка принята"}, {"id": "f-2", "meaning": ""}]},
            unavailable]})
        stage = engine(client)
        accepted, errors = ask(stage)
        self.assertEqual([row["id"] for row in accepted], ["f-1"])
        self.assertTrue(any(error.startswith("f-2:") for error in errors))
        self.assertTrue(any("LlmError" in error for error in errors))
        self.assertEqual(stage.budget.used, 2)
        self.assertFalse(stage.cache)

    def test_repaired_union_is_cached_as_provider_arguments_after_revalidation(self):
        client = ScriptedReportClient({"submit_fact_requirements": [
            lambda *_: {"facts": [{"id": "f-1", "meaning": "Заявка принята"}, {"id": "f-2", "meaning": ""}]},
            lambda *_: {"facts": [{"id": "f-2", "meaning": "Результат выдан"}]}]})
        stage = engine(client)
        accepted, errors = ask(stage)
        self.assertFalse(errors)
        self.assertEqual({row["id"] for row in accepted}, {"f-1", "f-2"})
        self.assertEqual(len(stage.cache), 1)
        cached = next(iter(stage.cache.values()))
        self.assertTrue(all("authority_status" not in item for item in cached["facts"]))
        resumed = engine(client, requests=0, cache=stage.cache)
        self.assertEqual(ask(resumed), (accepted, []))
        self.assertEqual(resumed.cache_hits, 1)
        self.assertEqual(len(client.requests), 2)

    def test_late_binding_packet_fault_keeps_prior_packet_and_defers_rest(self):
        model, source = many_sites(8)
        inventory = discover_logging(source, source.files)
        linked = link_sites(model, inventory, "map-v1")
        client = ScriptedReportClient({"submit_event_bindings": [None, unavailable]})
        reviewed = review_bindings(model, linked["sites"], inventory.get("wrappers", []),
                                   EvidenceRegistry(source), engine(client), "map-v1")
        self.assertEqual(len(reviewed["bindings"]), 6)
        self.assertEqual(len(reviewed["deferred"]), 2)
        self.assertEqual(reviewed["counts"]["confirmed"], 6)
        self.assertEqual(reviewed["counts"]["deferred"], 2)
        for identity in reviewed["deferred"]:
            self.assertIn("LlmError", reviewed["reasons"][identity][0])

    def test_private_extra_keys_in_accepted_provider_items_never_enter_cache(self):
        client = ScriptedReportClient({"submit_fact_requirements": lambda *_: {"facts": [{
            "id": "f-1", "meaning": "Заявка принята", "reasoning_content": "private-canary-1",
            "extra": {"thinking": "private-canary-2"}}]}})

        def normalise(arguments):
            # Real validators project known fields; unknown provider fields are ignored.
            return [{"id": item["id"], "meaning": item["meaning"]} for item in arguments["facts"]], []

        stage = engine(client)
        result, errors = stage.ask("fact_requirements", {}, TOOL, normalise, key=lambda item: item["id"])
        self.assertEqual(result, [{"id": "f-1", "meaning": "Заявка принята"}])
        self.assertFalse(errors)
        self.assertEqual(len(stage.cache), 1)
        cached = json.dumps(stage.cache, ensure_ascii=False)
        for private in ("reasoning_content", "thinking", "private-canary-1", "private-canary-2"):
            self.assertNotIn(private, cached)

    def test_late_instrumentation_packet_fault_keeps_prior_changes(self):
        model, report, registry = change_inputs(11)
        client = ScriptedReportClient({"submit_instrumentation_changes": [None, unavailable]})
        changes, errors, pending = build_changes(model, report, engine(client), registry)
        self.assertEqual(len(changes), 10)
        self.assertEqual(pending, ["f-11"])
        self.assertTrue(any("LlmError" in error for error in errors))

    def test_oversized_instrumentation_answer_is_explicitly_partial(self):
        model, report, registry = change_inputs(1)
        base = ScriptedReportClient()

        def oversized(payload, _):
            template = base.submit_instrumentation_changes(payload)["changes"][0]
            return {"changes": [{**copy.deepcopy(template), "id": f"c-{number}"} for number in range(1, 42)]}

        client = ScriptedReportClient({"submit_instrumentation_changes": oversized})
        changes, errors, pending = build_changes(model, report, engine(client), registry)
        self.assertEqual(len(changes), 40)
        self.assertTrue(any("41" in error and "не рассмотрено 1" in error for error in errors))
        self.assertFalse(pending)
        self.assertEqual(len(client.requests), 2)


class PhysicalRequestBudgetTests(unittest.TestCase):
    def test_transport_retries_cannot_spend_the_reserved_requests(self):
        calls = []

        class Failing:
            spec = SPEC

            def chat(self, *_args, **_kwargs):
                calls.append(1)
                raise ConnectionError("connection reset")

        stage = engine(Failing(), requests=3)
        with patch("pko.scenario_map.model_retry.retry_delay", return_value=0), \
                patch("pko.scenario_map.model_retry.time.sleep"), self.assertRaises(BudgetExhausted):
            stage.ask("fact_requirements", {}, TOOL, validate_facts, reserve=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stage.budget.snapshot(), {"max_requests": 3, "used_requests": 2,
                                                 "by_stage": {"fact_requirements": 2}})
        # The one reserved dispatch remains usable by the next stage.
        stage.budget.take("metric_catalog")
        self.assertEqual(stage.budget.used, 3)

    def test_expired_deadline_does_not_dispatch(self):
        client = ScriptedReportClient()
        stage = engine(client, deadline=time.monotonic() - 1)
        with self.assertRaises(BudgetExhausted):
            ask(stage)
        self.assertFalse(client.requests)
        self.assertEqual(stage.budget.used, 0)

    def test_budget_exhausted_during_repair_retains_accepted_items(self):
        client = ScriptedReportClient({"submit_fact_requirements": lambda *_: {
            "facts": [{"id": "f-1", "meaning": "Принято"}, {"id": "f-2", "meaning": ""}]}})
        stage = engine(client, requests=1)
        result, errors = ask(stage)
        self.assertEqual([item["id"] for item in result], ["f-1"])
        self.assertTrue(any("Исчерпан" in error for error in errors))
        self.assertEqual(len(client.requests), 1)


def entries(path):
    return [json.loads(part) for part in re.split(r"\n=== [^\n]+ ===\n", path.read_text(encoding="utf-8")) if part.strip()]


class AuditSanitizeTests(unittest.TestCase):
    def test_nested_values_and_json_share_one_environment_snapshot(self):
        value = {"rows": [{"label": "public", "value": "snapshot-canary-1"} for _ in range(60)],
                 "nested": json.dumps({"args": json.dumps({"value": "snapshot-canary-1"}),
                                       "thinking": "must be omitted"})}
        original = copy.deepcopy(value)
        with patch.dict(os.environ, {"PKO_TEST_API_KEY": "snapshot-canary-1"}), \
                patch.object(os.environ, "items", wraps=os.environ.items) as environment_items:
            result = sanitize(value)
            self.assertEqual(environment_items.call_count, 1)
        self.assertEqual(value, original)
        self.assertTrue(all(row == {"label": "public", "value": "[REDACTED]"} for row in result["rows"]))
        nested = json.loads(result["nested"])
        self.assertEqual(json.loads(nested["args"]), {"value": "[REDACTED]"})
        self.assertNotIn("thinking", nested)

    def test_next_call_reads_changed_environment_without_reusing_old_snapshot(self):
        value = {"old": "snapshot-canary-1", "new": "snapshot-canary-2"}
        with patch.dict(os.environ, {"PKO_TEST_API_KEY": "snapshot-canary-1"}):
            self.assertEqual(sanitize(value), {"old": "[REDACTED]", "new": "snapshot-canary-2"})
            os.environ["PKO_TEST_API_KEY"] = "snapshot-canary-2"
            self.assertEqual(sanitize(value), {"old": "snapshot-canary-1", "new": "[REDACTED]"})


class TraceRecoveryTests(unittest.TestCase):
    def test_failed_exporter_admission_and_flush_preserve_model_and_tool(self):
        class BrokenTracer:
            def start_span(self, _):
                raise RuntimeError("api_key=tracing-test-secret")

        class BrokenProvider:
            def force_flush(self, _):
                raise RuntimeError("exporter unavailable")

        class Client:
            spec = SPEC

            def chat(self, *_args, **_kwargs):
                return ChatResult("result", {})

        with tempfile.TemporaryDirectory() as directory, \
                patch("pko.llm.tracing._tracer", BrokenTracer()), \
                patch("pko.llm.tracing._provider", BrokenProvider()):
            path = Path(directory) / "audit.txt"
            with audit_run(path, "run-1"):
                with trace_span("scenario_run"):
                    client = AuditedClient(TracedClient(Client()))
                    self.assertEqual(client.chat([{"role": "user", "content": "hello"}]).text, "result")
                    call = {"id": "call-7", "function": {"name": "read_file", "arguments": '{"path":"app.py"}'}}
                    reply = trace_tool_call(lambda *_: {"content": '{"ok":true,"lines":["public"]}'}, None, call)
                    self.assertIn("public", reply["content"])
                health = flush_tracing()
                self.assertEqual(health["status"], "partial")
                self.assertEqual({failure["operation"] for failure in health["failures"]}, {"start_span", "flush"})
            data = entries(path)
            started = {row["span_id"] for row in data if row["event"] == "span_started"}
            finished = {row["span_id"] for row in data if row["event"] == "span_finished"}
            self.assertEqual(started, finished)
            tools = [row for row in data if row["event"] in ("tool_started", "tool_result")]
            self.assertEqual(len(tools), 2)
            self.assertEqual({row["tool_call_id"] for row in tools}, {"call-7"})
            self.assertEqual(len({row["span_id"] for row in tools}), 1)
            self.assertNotIn("tracing-test-secret", path.read_text(encoding="utf-8"))

    def test_local_tool_rejection_and_failure_have_terminal_spans_without_phoenix(self):
        with tempfile.TemporaryDirectory() as directory, patch("pko.llm.tracing._tracer", None), \
                patch("pko.llm.tracing._provider", None), patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            path = Path(directory) / "audit.txt"
            with audit_run(path, "run-2"), trace_span("scenario_run"):
                call = {"id": "call-9", "function": {"name": "read_file", "arguments": "{}"}}
                trace_tool_call(lambda *_: {"content": '{"ok":false,"error":"unknown path"}'}, None, call)
                with self.assertRaises(LlmError):
                    trace_tool_call(unavailable, None, {**call, "id": "call-10"})
            data = entries(path)
            ended = [row for row in data if row["event"] == "span_finished" and row["name"] == "read_file"]
            self.assertEqual([row["status"] for row in ended], ["rejected", "error"])
            failure = next(row for row in data if row["event"] == "tool_failed")
            self.assertEqual(failure["tool_call_id"], "call-10")
            self.assertEqual(failure["span_id"], ended[-1]["span_id"])


if __name__ == "__main__":
    unittest.main()
