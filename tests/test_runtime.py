"""Standalone runtime checks: no retired pipeline, external model or Phoenix server."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from pko import audit, cli
from pko.errors import LlmError, PkoError
from pko.llm import tracing
from pko.llm.client import ChatClient, ChatResult
from pko.llm.registry import ModelSpec
from pko.output import publisher
from pko.scenario_map.config import researcher_spec


SPEC = ModelSpec("scenario_researcher", "https://model.invalid/v1", "stub", "test-key")


def response(content="ready", *, calls=None, reasoning="", finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=calls, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10))


class AuditRuntimeTests(unittest.TestCase):
    def test_sanitization_preserves_public_data_without_mutating_input(self):
        value = {"api_key": "direct-secret", "reasoning_content": "private-one",
                 "output.value": json.dumps({"thinking": "private-two", "text": "public"}),
                 "content": "<think>private-three</think>answer password=hunter2 Bearer abc.def",
                 "blocks": [{"type": "thinking", "text": "private-four"}],
                 "url": "https://alice:password123@example.invalid/v1",
                 "error": "failure environment-secret"}
        original = copy.deepcopy(value)
        with patch.dict(os.environ, {"PKO_SCENARIO_API_KEY": "environment-secret"}):
            rendered = json.dumps(audit.sanitize(value))
        for private in ("direct-secret", "private-one", "private-two", "private-three",
                        "private-four", "hunter2", "abc.def", "password123", "environment-secret"):
            self.assertNotIn(private, rendered)
        self.assertIn("public", rendered)
        self.assertEqual(value, original)

    def test_last_tool_result_and_context_survive_session_exit_without_phoenix(self):
        answer = ChatResult("public", {}, reasoning_content="hidden-thought")
        delegate = SimpleNamespace(spec=SPEC, chat=Mock(return_value=answer))
        messages = [{"role": "system", "content": "task"}]
        with tempfile.TemporaryDirectory() as raw, patch.object(tracing, "_tracer", None):
            path = Path(raw) / "audit.txt"
            with audit.audit_run(path, "run-test") as journal:
                self.assertIs(audit.current_audit(), journal)
                client = audit.AuditedClient(delegate)
                with tracing.trace_span("scenario_research") as span:
                    self.assertIs(client.chat(messages, tools=[{"type": "function"}]), answer)
                    messages.append({"role": "tool", "tool_call_id": "last",
                                     "content": "finish rejected: missing evidence"})
                    span.add_event("finish_rejected", count=1)
                content = path.read_text(encoding="utf-8")
                self.assertIn("finish rejected: missing evidence", content)
                self.assertIn("finish_rejected", content)
            self.assertIsNone(audit.current_audit())
            content = path.read_text(encoding="utf-8")
            self.assertIn('"result": "success"', content)
            self.assertNotIn("hidden-thought", content)
            self.assertEqual(messages[0]["content"], "task")

    def test_nested_context_restores_after_interrupt_and_unwritable_audit_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as raw:
            outer_path, inner_path = Path(raw) / "outer.txt", Path(raw) / "inner.txt"
            with audit.audit_run(outer_path, "outer") as outer:
                with self.assertRaises(KeyboardInterrupt), audit.audit_run(inner_path, "inner"):
                    raise KeyboardInterrupt()
                self.assertIs(audit.current_audit(), outer)
                audit.record("outer_resumed")
            self.assertIsNone(audit.current_audit())
            self.assertIn('"result": "interrupted"', inner_path.read_text(encoding="utf-8"))
            self.assertIn("outer_resumed", outer_path.read_text(encoding="utf-8"))
            with redirect_stderr(io.StringIO()), audit.audit_run(Path(raw), "unwritable") as journal:
                self.assertTrue(journal.failed)
            self.assertIsNone(audit.current_audit())


class ChatClientRuntimeTests(unittest.TestCase):
    def make_client(self, result=None, error=None):
        create = Mock(return_value=result or response(), side_effect=error)
        sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        factory = patch("pko.llm.client.openai.OpenAI", return_value=sdk)
        mocked = factory.start()
        self.addCleanup(factory.stop)
        return ChatClient(SPEC), create, mocked

    def test_tool_only_reply_is_normalized_and_usage_is_preserved(self):
        call = SimpleNamespace(id="call-1", function=SimpleNamespace(name="read_file", arguments='{"path":"a.py"}'))
        client, create, factory = self.make_client(response(None, calls=[call], reasoning="private"))
        schemas = [{"type": "function", "function": {"name": "read_file"}}]
        result = client.chat([{"role": "user", "content": "inspect"}], tools=schemas, max_tokens=99)
        self.assertEqual(result.text, "")
        self.assertEqual(result.tool_calls, [{"id": "call-1", "type": "function",
                                             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}])
        self.assertEqual(result.usage["total_tokens"], 10)
        self.assertEqual(result.reasoning_content, "private")
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(create.call_args.kwargs["tool_choice"], "auto")
        self.assertIs(create.call_args.kwargs["tools"], schemas)
        self.assertEqual(create.call_args.kwargs["max_tokens"], 99)

    def test_reasoning_or_whitespace_alone_is_a_failed_response(self):
        for content in (None, "   "):
            with self.subTest(content=content):
                client, create, _ = self.make_client(response(content, reasoning="private", finish="length"))
                with self.assertRaises(LlmError) as caught:
                    client.chat([{"role": "user", "content": "inspect"}])
                self.assertIn("max_tokens", caught.exception.hint)
                self.assertEqual(create.call_count, 1)

    def test_missing_choice_is_a_controlled_provider_error(self):
        client, _, _ = self.make_client(SimpleNamespace(choices=[]))
        with self.assertRaisesRegex(LlmError, "Неожиданный ответ"):
            client.chat([])

    def test_provider_failure_is_masked_and_is_not_retried(self):
        client, create, _ = self.make_client(error=RuntimeError("password=hunter2 Bearer abc.def failed"))
        with self.assertRaises(LlmError) as caught:
            client.chat([])
        self.assertEqual(create.call_count, 1)
        self.assertNotIn("hunter2", caught.exception.render())
        self.assertNotIn("abc.def", caught.exception.render())
        self.assertIn("failed", caught.exception.hint)

    def test_repeated_calls_never_reuse_or_write_a_durable_conversation_cache(self):
        client, create, _ = self.make_client(response("public", reasoning="private-continuation"))
        messages = [{"role": "user", "content": "inspect"}]
        with patch.object(Path, "read_text", side_effect=AssertionError("no disk cache reads")), \
                patch.object(Path, "write_text", side_effect=AssertionError("no disk cache writes")):
            first, second = client.chat(messages), client.chat(messages)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(second.reasoning_content, "private-continuation")
        self.assertFalse(first.from_cache or second.from_cache)


class PublishingRuntimeTests(unittest.TestCase):
    def test_success_writes_the_complete_artifact_set(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "out"
            written = publisher.write_outputs(out, {"map": ("scenario_map.html", "<html>map</html>"),
                                                    "model": ("scenario_map.json", '{"nodes":[]}')})
            self.assertEqual({item.kind for item in written}, {"map", "model"})
            self.assertEqual({path.name for path in out.iterdir()}, {"scenario_map.html", "scenario_map.json"})
            self.assertEqual(json.loads((out / "scenario_map.json").read_text()), {"nodes": []})

    def test_failed_replace_restores_old_files_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "out"
            out.mkdir()
            (out / "old.txt").write_text("previous", encoding="utf-8")
            (out / "untouched.txt").write_text("keep", encoding="utf-8")
            real_replace, failed = publisher._replace, False

            def flaky(source, target):
                nonlocal failed
                if target.name == "last.txt" and not failed:
                    failed = True
                    raise OSError("fault injection")
                return real_replace(source, target)

            with patch.object(publisher, "_replace", side_effect=flaky), self.assertRaises(PkoError):
                publisher.write_outputs(out, {"first": ("old.txt", "replacement"),
                                              "new": ("new.txt", "created"), "last": ("last.txt", "last")})
            self.assertEqual((out / "old.txt").read_text(), "previous")
            self.assertEqual((out / "untouched.txt").read_text(), "keep")
            self.assertEqual({path.name for path in out.iterdir()}, {"old.txt", "untouched.txt"})

    def test_invalid_names_or_empty_artifacts_never_replace_previous_outputs(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "out"
            out.mkdir()
            (out / "map.html").write_text("previous", encoding="utf-8")
            for files in ({"x": ("../escape.txt", "bad")},
                          {"x": ("map.html", "new"), "y": ("map.html", "duplicate")},
                          {"x": ("map.html", "new"), "y": ("empty.json", "")}):
                with self.subTest(files=files), self.assertRaises(PkoError):
                    publisher.write_outputs(out, files)
                self.assertEqual((out / "map.html").read_text(), "previous")
            self.assertFalse((Path(raw) / "escape.txt").exists())


class CliConfigRuntimeTests(unittest.TestCase):
    def test_cli_only_accepts_scenario_input_and_repeated_code_sources(self):
        parser = cli._build_parser()
        args = parser.parse_args(["map-scenario", "--scenario-text", "issue card", "--source", "a", "--source", "b.zip"])
        self.assertEqual(args.source, ["a", "b.zip"])
        for argv in (["progress"], ["map-scenario", "--source", "a"],
                     ["map-scenario", "--scenario-text", "x", "--scenario-file", "x.md", "--source", "a"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_cli_maps_expected_failure_and_interruption_to_exit_codes(self):
        for error, expected in ((PkoError("bad source", "provide a directory"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(expected=expected), patch.object(cli, "load_env"), \
                    patch("pko.scenario_map.runner.command_main", side_effect=error), redirect_stderr(io.StringIO()) as stderr:
                code = cli.main(["map-scenario", "--scenario-text", "x", "--source", "a"])
                self.assertEqual(code, expected)
                self.assertTrue(stderr.getvalue())

    def test_new_endpoint_never_inherits_old_credentials_or_partial_mtls(self):
        env = {"PKO_MATCHER_BASE_URL": "https://old.invalid/v1", "PKO_MATCHER_API_KEY": "old-secret",
               "PKO_MATCHER_CLIENT_CERT": "old.pem", "PKO_SCENARIO_BASE_URL": "https://new.invalid/v1",
               "PKO_SCENARIO_MODEL": "new-model"}
        with patch.dict(os.environ, env, clear=True):
            spec = researcher_spec()
        self.assertEqual((spec.api_key, spec.client_cert, spec.client_key), ("not-needed", "", ""))
        self.assertEqual(spec.base_url, "https://new.invalid/v1")
        self.assertIsNone(spec.extra_body)
        with patch.dict(os.environ, {**env, "PKO_SCENARIO_CLIENT_CERT": "new.pem"}, clear=True):
            with self.assertRaisesRegex(PkoError, "частично"):
                researcher_spec()


class _Span:
    def __init__(self, name, number, parent):
        self.name, self.parent = name, parent
        self.context = SimpleNamespace(trace_id=77, span_id=number)
        self.attributes, self.events, self.status = {}, [], None
        self.ended, self.raw_exception_calls = 0, 0

    def get_span_context(self):
        return self.context

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append({"name": name, "attributes": attributes or {}})

    def record_exception(self, error):
        self.raw_exception_calls += 1
        self.events.append({"raw_exception": str(error)})

    def set_status(self, status):
        self.status = status

    def update_name(self, name):
        self.name = name

    def end(self):
        self.ended += 1


@contextmanager
def fake_phoenix():
    """Minimal local OTel interfaces exercise production tracing without optional packages."""
    modules = {name: ModuleType(name) for name in (
        "opentelemetry", "opentelemetry.trace", "opentelemetry.sdk", "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export", "phoenix", "phoenix.otel")}
    spans, active = [], []

    @contextmanager
    def use_span(span, **kwargs):
        active.append(span)
        try:
            yield span
        finally:
            active.pop()

    def start_span(name):
        span = _Span(name, len(spans) + 1, active[-1] if active else None)
        spans.append(span)
        return span

    tracer = SimpleNamespace(start_span=start_span)
    provider = SimpleNamespace(get_tracer=Mock(return_value=tracer), add_span_processor=Mock(), force_flush=Mock(return_value=True))
    modules["opentelemetry"].trace = modules["opentelemetry.trace"]
    modules["opentelemetry.trace"].use_span = use_span
    modules["opentelemetry.trace"].Status = lambda code, description: SimpleNamespace(code=code, description=description)
    modules["opentelemetry.trace"].StatusCode = SimpleNamespace(ERROR="ERROR")
    modules["opentelemetry.sdk.trace"].SpanLimits = lambda **kw: kw
    modules["opentelemetry.sdk.trace.export"].BatchSpanProcessor = Mock()
    phoenix = modules["phoenix.otel"]
    phoenix.PROJECT_NAME = "project.name"
    phoenix.HTTPSpanExporter = Mock()
    phoenix.Resource = SimpleNamespace(create=lambda value: value)
    phoenix.TracerProvider = Mock(return_value=provider)
    with patch.dict(sys.modules, modules), patch.object(tracing, "_tracer", None), \
            patch.object(tracing, "_provider", None), patch.object(tracing, "_initialized", False):
        yield SimpleNamespace(spans=spans, provider=provider, tracer=tracer, phoenix=phoenix)


class TracingRuntimeTests(unittest.TestCase):
    def test_disabled_tracing_is_passthrough_and_never_probes_network(self):
        with fake_phoenix(), patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}, clear=True), \
                patch.object(tracing, "_reachable") as reachable:
            self.assertIsNone(tracing.init_phoenix_tracing())
            reachable.assert_not_called()
            delegate, reply = object(), {"role": "tool", "content": "ok"}
            self.assertIs(tracing.trace_client(delegate, None), delegate)
            self.assertIs(tracing.trace_tool_call(lambda state, call: reply, None, {}), reply)
            with tracing.trace_span("scenario_research") as span:
                self.assertEqual(span.trace_id, "")
                span.set_input("x")
                span.set_output({"nodes": 1})

    def test_enabled_fake_phoenix_records_one_hierarchy_without_private_reasoning(self):
        call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
        answer = ChatResult("", {"total_tokens": 10}, tool_calls=[call], reasoning_content="hidden-thought")
        messages = [{"role": "system", "content": "task"}, {"role": "user", "content": "password=hunter2"}]
        with tempfile.TemporaryDirectory() as raw, fake_phoenix() as fake, \
                patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "true"}, clear=True), \
                patch.object(tracing, "_reachable", return_value=True), redirect_stdout(io.StringIO()):
            self.assertIs(tracing.init_phoenix_tracing(), fake.provider)
            self.assertIs(tracing.init_phoenix_tracing(), fake.provider)
            fake.phoenix.TracerProvider.assert_called_once()
            delegate = SimpleNamespace(spec=SPEC, chat=Mock(return_value=answer))
            with audit.audit_run(Path(raw) / "audit.txt", "run-trace"):
                with tracing.trace_span("scenario_research") as session:
                    with tracing.trace_span("scenario_step", step=1):
                        self.assertIs(tracing.trace_client(delegate, SPEC).chat(messages), answer)
                        def tool(state, call):
                            session.add_event("citation_accepted", count=1)
                            return {"role": "tool", "content": "found"}
                        tracing.trace_tool_call(tool, None, call)
            session, step, llm, tool = fake.spans
            self.assertIs(step.parent, session)
            self.assertIs(llm.parent, step)
            self.assertIs(tool.parent, step)
            self.assertEqual(step.name, "шаг 1 · read_file")
            self.assertEqual([s.attributes["openinference.span.kind"] for s in fake.spans], ["AGENT", "CHAIN", "LLM", "TOOL"])
            self.assertTrue(all(s.attributes["session.id"] == "run-trace" and s.ended == 1 for s in fake.spans))
            self.assertTrue(any(e["name"] == "citation_accepted" for e in tool.events))
            exported = json.dumps([s.attributes for s in fake.spans])
            self.assertNotIn("hunter2", exported)
            self.assertNotIn("hidden-thought", exported)
            self.assertIn("read_file", exported)
            self.assertIsNone(tracing._SESSION.get())
            self.assertIsNone(tracing._STEP.get())
            tracing.flush_tracing()
            fake.provider.force_flush.assert_called_once()

    def test_unavailable_exporter_does_not_block_execution(self):
        with fake_phoenix(), patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "true"}, clear=True), \
                patch.object(tracing, "_reachable", return_value=False), redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(tracing.init_phoenix_tracing())
            self.assertFalse(tracing.tracing_enabled())
            self.assertIn("трассировка", stderr.getvalue())

    def test_model_and_tool_exceptions_are_masked_in_every_enclosing_span(self):
        for operation in ("model", "tool"):
            with self.subTest(operation=operation), fake_phoenix() as fake:
                tracing._tracer = fake.tracer
                error = RuntimeError("password=hunter2 Bearer abc.def unavailable")
                with self.assertRaises(RuntimeError) as caught:
                    with tracing.trace_span("scenario_research"):
                        with tracing.trace_span("scenario_step", step=1):
                            if operation == "model":
                                delegate = SimpleNamespace(spec=SPEC, chat=Mock(side_effect=error))
                                tracing.trace_client(delegate, SPEC).chat([])
                            else:
                                tracing.trace_tool_call(Mock(side_effect=error), None, {"function": {"name": "read_file"}})
                self.assertIs(caught.exception, error)
                self.assertTrue(all(span.ended == 1 and span.status.code == "ERROR" for span in fake.spans))
                self.assertTrue(all(span.raw_exception_calls == 0 for span in fake.spans))
                exported = json.dumps([{ "events": s.events, "status": s.status.description } for s in fake.spans])
                self.assertNotIn("hunter2", exported)
                self.assertNotIn("abc.def", exported)
                self.assertIn("unavailable", exported)

    def test_export_failure_diagnostics_mask_credentials(self):
        with fake_phoenix() as fake, patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "true"}, clear=True), \
                patch.object(tracing, "_reachable", return_value=True), redirect_stderr(io.StringIO()) as stderr:
            fake.phoenix.TracerProvider.side_effect = RuntimeError("password=hunter2 unavailable")
            self.assertIsNone(tracing.init_phoenix_tracing())
            tracing._provider = fake.provider
            fake.provider.force_flush.side_effect = RuntimeError("Bearer abc.def failed")
            tracing.flush_tracing()
            self.assertNotIn("hunter2", stderr.getvalue())
            self.assertNotIn("abc.def", stderr.getvalue())
            self.assertIn("unavailable", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
