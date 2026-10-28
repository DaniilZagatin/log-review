"""Phoenix startup failures stay diagnosable without blocking the local audit."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import builtins
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pko.audit import audit_run
from pko.llm import tracing
from test_runtime import fake_phoenix


def entries(path):
    return [json.loads(block) for block in re.split(r"\n=== [^\n]+ ===\n", path.read_text(encoding="utf-8"))[1:]]


class TraceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.fake = self.stack.enter_context(fake_phoenix())
        self.stack.enter_context(patch.object(tracing, "_initialization_failure", None))
        self.stack.enter_context(patch.dict(os.environ, {
            "PKO_PHOENIX_ENABLED": "true", "PKO_PHOENIX_ENDPOINT": "http://phoenix.invalid/v1/traces"}))
        self.network = self.stack.enter_context(patch.object(
            tracing.urllib.request, "urlopen", side_effect=AssertionError("Network is forbidden in this test")))
        self.reachable = self.stack.enter_context(patch.object(tracing, "_reachable", return_value=True))
        self.stderr = self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def assert_failure(self, health, operation):
        self.assertEqual(health["status"], "failed")
        self.assertEqual(len(health["failures"]), 1)
        self.assertEqual(health["failures"][0]["operation"], operation)
        self.assertTrue(health["failures"][0]["reason"])
        self.network.assert_not_called()

    def test_missing_optional_package_is_recorded_without_a_network_probe(self):
        path = self.folder / "import.txt"
        with audit_run(path, "import"), patch.dict(sys.modules, {"phoenix.otel": None}):
            self.assertIsNone(tracing.init_phoenix_tracing())
            health = tracing.flush_tracing()
            self.assert_failure(health, "import")
            self.assertEqual(tracing.tracing_health(), health)
        self.reachable.assert_not_called()
        failures = [row for row in entries(path) if row["event"] == "trace_export_failure"]
        self.assertEqual(len(failures), 1)
        self.assertIn("зависимости Phoenix", failures[0]["reason"])

    def test_unreachable_server_reason_survives_cached_initialization_in_another_run(self):
        self.reachable.return_value = False
        reasons = []
        for run_id in ("first", "second"):
            path = self.folder / f"{run_id}.txt"
            with audit_run(path, run_id):
                self.assertIsNone(tracing.init_phoenix_tracing())
                health = tracing.flush_tracing()
                self.assert_failure(health, "probe")
                reasons.append(health["failures"])
                tracing.flush_tracing()
                with tracing.trace_span("local_work"):
                    pass
            rows = entries(path)
            self.assertEqual(sum(row["event"] == "trace_export_failure" for row in rows), 1)
            self.assertEqual(sum(row["event"] == "span_started" for row in rows), 1)
            self.assertEqual(sum(row["event"] == "span_finished" for row in rows), 1)
        self.assertEqual(reasons[0], reasons[1])
        self.reachable.assert_called_once()

    def test_broken_optional_package_import_is_nonfatal_and_diagnosed(self):
        original_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "phoenix.otel":
                raise RuntimeError("incompatible optional dependency")
            return original_import(name, *args, **kwargs)

        with audit_run(self.folder / "broken-import.txt", "broken-import"), \
                patch.object(builtins, "__import__", side_effect=broken_import):
            self.assertIsNone(tracing.init_phoenix_tracing())
            health = tracing.flush_tracing()
            self.assert_failure(health, "import")
            self.assertIn("incompatible optional dependency", health["failures"][0]["reason"])
        self.reachable.assert_not_called()

    def test_failure_before_audit_is_attached_when_a_run_opens(self):
        self.reachable.return_value = False
        tracing.init_phoenix_tracing()
        outside = tracing.flush_tracing()
        self.assert_failure(outside, "probe")
        path = self.folder / "late-audit.txt"
        with audit_run(path, "late-audit"):
            tracing.init_phoenix_tracing()
            self.assertEqual(tracing.flush_tracing(), outside)
        self.assertEqual(sum(row["event"] == "trace_export_failure" for row in entries(path)), 1)

    def test_probe_exception_is_reported_instead_of_interrupting_the_run(self):
        self.reachable.side_effect = RuntimeError("probe unavailable")
        with audit_run(self.folder / "probe.txt", "probe"):
            self.assertIsNone(tracing.init_phoenix_tracing())
            health = tracing.flush_tracing()
            self.assert_failure(health, "probe")
            self.assertIn("probe unavailable", health["failures"][0]["reason"])

    def test_provider_initialization_failure_is_masked_in_health_and_audit(self):
        self.fake.phoenix.TracerProvider.side_effect = RuntimeError(
            "password=private-canary Bearer secret-canary <think>hidden-canary</think> unavailable")
        path = self.folder / "initialization.txt"
        with audit_run(path, "initialization"):
            self.assertIsNone(tracing.init_phoenix_tracing())
            health = tracing.flush_tracing()
            self.assert_failure(health, "initialize")
        emitted = json.dumps(health) + path.read_text(encoding="utf-8") + self.stderr.getvalue()
        for private in ("private-canary", "secret-canary", "hidden-canary"):
            self.assertNotIn(private, emitted)
        self.assertIn("unavailable", emitted)

    def test_provider_returning_no_tracer_is_not_reported_as_initialized(self):
        self.fake.provider.get_tracer.return_value = None
        with audit_run(self.folder / "no-tracer.txt", "no-tracer"):
            self.assertIsNone(tracing.init_phoenix_tracing())
            self.assert_failure(tracing.flush_tracing(), "initialize")
        self.assertIsNone(tracing._tracer)
        self.assertIsNone(tracing._provider)

    def test_missing_tracer_or_provider_always_has_an_explicit_reason(self):
        for name, tracer, provider in (("both", None, None), ("provider", object(), None),
                                       ("tracer", None, Mock(force_flush=Mock(return_value=True)))):
            with self.subTest(name=name), patch.object(tracing, "_tracer", tracer), \
                    patch.object(tracing, "_provider", provider), \
                    audit_run(self.folder / f"missing-{name}.txt", name):
                health = tracing.flush_tracing()
                self.assert_failure(health, "initialize")
                self.assertIn("отсутствует", health["failures"][0]["reason"])
                self.assertEqual(tracing.tracing_health(), health)

    def test_successful_export_has_no_failure_diagnostics(self):
        with audit_run(self.folder / "success.txt", "success"):
            self.assertIs(tracing.init_phoenix_tracing(), self.fake.provider)
            self.assertEqual(tracing.tracing_health(), {"status": "pending", "failures": []})
            self.assertEqual(tracing.flush_tracing(), {"status": "complete", "failures": []})
        self.fake.provider.force_flush.assert_called_once()
        self.network.assert_not_called()

    def test_disabled_phoenix_keeps_local_audit_without_false_failures(self):
        path = self.folder / "disabled.txt"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}), audit_run(path, "disabled"):
            self.assertIsNone(tracing.init_phoenix_tracing())
            with tracing.trace_span("local_work"):
                pass
            self.assertEqual(tracing.flush_tracing(), {"status": "disabled", "failures": []})
        rows = entries(path)
        self.assertFalse(any(row["event"] == "trace_export_failure" for row in rows))
        self.assertEqual(sum(row["event"] == "span_finished" for row in rows), 1)
        self.reachable.assert_not_called()
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
