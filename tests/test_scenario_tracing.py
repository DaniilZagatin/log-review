"""Real OTLP transport over loopback; no model service or Phoenix server needed."""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import httpx
import openai
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

from pko.llm import tracing
from pko.scenario_map.runner import run_scenario
from scenario_script import SCENARIO, SPEC, ScriptedClient, research_calls, sources

try:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    import phoenix.otel
    HAS_OTLP = True
except ImportError:
    HAS_OTLP = False


@unittest.skipUnless(HAS_OTLP, "Install requirements-phoenix.txt to check real OTLP export")
class ScenarioTracingIntegrationTests(unittest.TestCase):
    def test_full_runner_exports_one_tree_per_run_and_flushes_last_span(self):
        batches = []

        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                request = ExportTraceServiceRequest()
                request.ParseFromString(body)
                batches.append(request)
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-protobuf')
                self.end_headers()

        class FinalBatchClient(ScriptedClient):
            connection_failed = False

            def chat(self, messages, **kwargs):
                if len(messages) > 8 and not self.connection_failed:
                    self.connection_failed = True
                    raise openai.APIConnectionError(request=httpx.Request('POST', 'http://model.invalid'))
                result = super().chat(messages, **kwargs)
                result.reasoning_content = 'private-thinking-trace-test'
                if result.tool_calls and result.tool_calls[0]['function']['name'] == 'finish':
                    result.tool_calls.append({'id': 'after-finish', 'type': 'function',
                                             'function': {'name': 'inspect_map', 'arguments': '{}'}})
                return result

        server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw, \
                    patch('pko.scenario_map.model_retry.time', SimpleNamespace(monotonic=time.monotonic, sleep=Mock())), \
                    patch.multiple(tracing, _tracer=None, _provider=None, _initialized=False), \
                    patch.dict(os.environ, {
                        'PKO_PHOENIX_ENABLED': 'true',
                        'PKO_PHOENIX_ENDPOINT': f'http://127.0.0.1:{server.server_port}/v1/traces',
                        'PKO_PHOENIX_PROJECT': 'belka-tracing-test',
                        'NO_PROXY': '127.0.0.1,localhost',
                        'PHOENIX_API_KEY': '',
                        'PHOENIX_CLIENT_HEADERS': '',
                    }), redirect_stdout(io.StringIO()):
                output = Path(raw)
                try:
                    completed = run_scenario(SCENARIO, sources(), output / 'completed',
                                             spec=SPEC, client=FinalBatchClient())
                    provider = tracing._provider
                    self.assertIsNotNone(provider)
                    self.assertEqual(completed['run']['status'], 'completed', completed['run'])
                    failing = SimpleNamespace(spec=SPEC, chat=Mock(side_effect=RuntimeError('password=private-test unavailable')))
                    failed = run_scenario(SCENARIO, sources(), output / 'failed', spec=SPEC, client=failing)
                    self.assertEqual(failed['run']['status'], 'failed')
                    for model, folder in ((completed, 'completed'), (failed, 'failed')):
                        saved = json.loads((output / folder / 'scenario_map.checkpoint.json').read_text(encoding='utf-8'))
                        self.assertEqual(saved['run']['trace_id'], model['run']['trace_id'])
                    # These assertions run before provider shutdown: run_scenario must flush.
                    spans = [span for batch in batches for resource in batch.resource_spans
                             for scope in resource.scope_spans for span in scope.spans]
                    self.assertEqual(len({s.trace_id for s in spans}), 2)
                    self.assertEqual(len({s.span_id for s in spans}), len(spans), 'No duplicate exporter')
                    for model in (completed, failed):
                        tree = [s for s in spans if s.trace_id.hex() == model['run']['trace_id']]
                        roots = [s for s in tree if not s.parent_span_id]
                        self.assertEqual(len(roots), 1)
                        root = roots[0]
                        self.assertEqual(root.name, 'scenario_map_run')
                        ids = {s.span_id for s in tree}
                        self.assertTrue(all(s.parent_span_id in ids for s in tree if s is not root))
                        self.assertTrue(all(s.end_time_unix_nano >= s.start_time_unix_nano for s in tree))
                        attrs = lambda span: {a.key: a.value for a in span.attributes}
                        self.assertEqual(attrs(root)['pko.run.status'].string_value, model['run']['status'])
                        result = json.loads(attrs(root)['output.value'].string_value)
                        self.assertEqual(result['run']['trace_id'], model['run']['trace_id'])
                        names = {s.name for s in tree}
                        self.assertTrue({'prepare_sources', 'save_checkpoint', 'render_map', 'publish_outputs'} <= names)
                        tools = [s for s in tree if attrs(s)['openinference.span.kind'].string_value == 'TOOL']
                        llms = [s for s in tree if attrs(s)['openinference.span.kind'].string_value == 'LLM']
                        if model is completed:
                            self.assertEqual(len(tools), len(research_calls()) + 1)
                            # Research steps, one retried request, summaries and one business-stage request.
                            self.assertEqual(len(llms), model['run']['steps'] + 1 + model['run'].get('context_summary_calls', 0) + 1)
                            self.assertIn('business_stages', names)
                            self.assertGreater(model['run'].get('context_summaries', 0), 0)
                            self.assertEqual(model['run'].get('context_summary_fallbacks', 0), 0)
                            compact_spans = [s for s in tree if attrs(s).get('pko.span') and attrs(s)['pko.span'].string_value == 'compact_context']
                            summary_spans = [s for s in tree if attrs(s).get('pko.span') and attrs(s)['pko.span'].string_value == 'context_summary']
                            self.assertEqual(len(summary_spans), model['run']['context_summary_calls'])
                            self.assertTrue(all(s.parent_span_id in {c.span_id for c in compact_spans} for s in summary_spans))
                            for summary_span in summary_spans:
                                self.assertEqual(summary_span.trace_id, root.trace_id)
                                self.assertEqual(sum(s.parent_span_id == summary_span.span_id for s in llms), 1,
                                                 'Each summary LLM call must stay under compaction in the same trace')
                            self.assertIn('model_retry_wait', names)
                            self.assertEqual(sum(s.status.code == 2 for s in llms), 1)
                            self.assertNotEqual(root.status.code, 2)
                            steps = [s for s in tree if attrs(s).get('pko.span') and attrs(s)['pko.span'].string_value == 'scenario_step']
                            self.assertTrue(all(s.status.code != 2 for s in steps), 'Recovered step must not remain failed')
                            rejected = [s for s in tools if attrs(s)['pko.tool_call.id'].string_value == 'after-finish']
                            self.assertEqual(len(rejected), 1)
                            self.assertFalse(attrs(rejected[0])['pko.tool.ok'].bool_value)
                        else:
                            self.assertEqual(root.status.code, 2)  # OTel ERROR
                            self.assertEqual(len(llms), 1)
                            self.assertEqual(llms[0].status.code, 2)
                    self.assertNotIn(b'private-test', b''.join(b.SerializeToString() for b in batches))
                    self.assertNotIn(b'private-thinking-trace-test', b''.join(b.SerializeToString() for b in batches))
                finally:
                    if tracing._provider is not None:
                        tracing._provider.shutdown()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
