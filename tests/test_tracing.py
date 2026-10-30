import json
from pathlib import Path
import tempfile
from threading import Thread
import hashlib
import unittest
import zipfile

from process_observability.tracing import TraceRecorder, package_feedback
from process_observability.llm import ModelConfig,OpenAICompatibleModel,TransportResponse


class TracingTests(unittest.TestCase):
    def test_incremental_safe_events_and_allowlisted_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/"run";run.mkdir()
            trace=TraceRecorder(run)
            trace("model.request",{"system":"rules","user":'password="CANARY_SECRET"'})
            rows=[json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual(rows[0]["seq"],1)
            self.assertNotIn("CANARY",trace.path.read_text())
            (run/"diagnostics.json").write_text('{}')
            (run/".env").write_text("PRIVATE-DO-NOT-PACK")
            (run/"unrelated.py").write_text("DO-NOT-PACK")
            output=root/"feedback.zip"
            self.assertEqual(package_feedback(run,output),2)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()),{"trace.jsonl","diagnostics.json"})
            with self.assertRaises(FileExistsError):package_feedback(run,output)

    def test_api_trace_has_answer_content_and_usage_without_headers_or_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=TraceRecorder(directory)
            payload={"choices":[{"message":{"content":'{"ok":true}',"reasoning_content":"PRIVATE-REASONING"},"finish_reason":"stop"}],
                     "usage":{"prompt_tokens":12,"completion_tokens":4}}
            transport=lambda *args:TransportResponse(200,json.dumps(payload).encode())
            model=OpenAICompatibleModel(ModelConfig(base_url="https://example.test/v1",model="mock"),transport=transport,observer=trace)
            self.assertEqual(model.complete_json("rules","input"),{"ok":True})
            text=trace.path.read_text()
            self.assertNotIn("PRIVATE-REASONING",text)
            self.assertNotIn("Authorization",text)
            row=json.loads(text)
            self.assertEqual(row["data"]["usage"]["prompt_tokens"],12)
            self.assertEqual(row["data"]["request_sha256"],hashlib.sha256(b'rules\ninput').hexdigest())
            # Review model.request events carry the same value under this name for correlation.
            self.assertEqual(row["data"]["api_request_sha256"],row["data"]["request_sha256"])

    def test_concurrent_events_are_complete_ordered_and_late_events_are_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=TraceRecorder(directory)
            trace.protect_secret('trace-canary')
            def write(worker):
                for index in range(25):
                    trace('worker.event',{'worker':worker,'index':index,'value':'trace-canary'})
            workers=[Thread(target=write,args=(i,)) for i in range(4)]
            for worker in workers:worker.start()
            for worker in workers:worker.join()
            trace.close();before=trace.path.read_bytes()
            trace('api.response',{'content':'too late'})
            trace.close()
            self.assertEqual(trace.path.read_bytes(),before)
            self.assertNotIn(b'trace-canary',before)
            rows=[json.loads(line) for line in before.splitlines()]
            self.assertEqual([r['seq'] for r in rows],list(range(1,101)))
            self.assertEqual(len({(r['data']['worker'],r['data']['index']) for r in rows}),100)


if __name__ == "__main__":unittest.main()
