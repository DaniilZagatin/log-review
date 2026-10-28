"""The local journal masks secrets and keeps every span terminal when tracing fails."""
import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.audit import AuditedClient, audit_run, sanitize
from pko.errors import LlmError
from pko.llm.client import ChatResult
from pko.llm.tracing import TracedClient, flush_tracing, trace_span, trace_tool_call
from scenario_script import SPEC


def unavailable(*_):
    raise LlmError("Модель недоступна", "400 Bad Request")


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
