"""No-progress guards track new research, not changing receipt IDs or wording."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.llm.client import ChatResult
from pko.scenario_map.agent import run_agent
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, ScriptedClient, research_calls, scripted_summary, sources


class GeneratedBatchClient(ScriptedClient):
    """Generate public tool proposals, including expected rejected proposals."""

    def __init__(self, batch):
        self.batch = batch
        self.requests = 0

    def chat(self, messages, **kwargs):
        if not kwargs.get("tools"):
            return scripted_summary()
        self.requests += 1
        return ChatResult(text="", usage={}, tool_calls=[
            {"id": f"request-{self.requests}-{index}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            for index, (name, arguments) in enumerate(self.batch(self.requests))
        ])


def graph_prefix():
    """A real accepted graph with an edge whose public wording can be edited."""
    prefix = []
    for call in research_calls():
        prefix.append(call)
        if call[0] == "upsert_transition":
            return prefix
    raise AssertionError("The scenario fixture must contain a transition")


class LoopGuardRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = build_workspace(sources(), self.root / "sources")

    def run_script(self, batch, *, max_steps=40, workspace=None):
        client = GeneratedBatchClient(batch)
        model = run_agent(workspace or self.workspace, SCENARIO, SPEC,
                          client=client, max_steps=max_steps)
        return client, model

    def assert_stopped_without_progress(self, client, model, *, before=40):
        self.assertEqual(model["run"]["stop_kind"], "no_progress", model["run"])
        self.assertLess(client.requests, before)
        self.assertEqual(model["run"].get("loop_recoveries"), 1)

    def test_varying_failed_reads_are_not_progress(self):
        client, model = self.run_script(lambda number: [
            ("read_file", {"path": f"source-1/missing-{number}.py"})])
        self.assert_stopped_without_progress(client, model)
        self.assertEqual(model["read_coverage"], [])
        self.assertTrue(model["run"].get("recent_rejections"))
        self.assertIn(f"missing-{client.requests}.py", model["run"]["recent_rejections"][-1]["error"])

    def test_changed_read_window_with_no_new_lines_is_not_progress(self):
        client, model = self.run_script(lambda number: [
            ("read_file", {"path": "source-1/app.py", "offset": 1, "limit": 100 + number})],
            max_steps=60)
        # Different tool arguments must still reach the bounded idle guard.
        self.assert_stopped_without_progress(client, model, before=60)
        self.assertEqual(model["read_coverage"][0]["ranges"], [[1, 21]])

    def test_varying_invalid_evidence_ranges_have_a_bounded_recovery(self):
        prefix = graph_prefix()

        def batch(number):
            if number == 1:
                return prefix
            return [("upsert_step", {"id": "step-1", "evidence": [{
                "path": "source-1/app.py", "line_start": 100 + number, "line_end": 100 + number}]})]

        client, model = self.run_script(batch)
        self.assert_stopped_without_progress(client, model, before=21)
        self.assertEqual(len(model["edges"]), 1)
        self.assertEqual(model["run"]["recent_rejections"][-1]["code"], "retry_budget_exhausted")
        self.assertEqual(model["run"]["unresolved_rejections"][0]["code"], "invalid_evidence")

    def test_edge_wording_in_same_batch_does_not_erase_a_rejection_loop(self):
        prefix = graph_prefix()
        for field in ("basis", "description"):
            with self.subTest(field=field):
                def batch(number):
                    if number == 1:
                        return prefix
                    return [
                        ("upsert_step", {"id": "step-1", "evidence": [{
                            "path": "source-1/app.py", "line_start": 100, "line_end": 100}]}),
                        ("upsert_transition", {"id": "edge-1", field:
                            f"The same transition is confirmed by the same source. Wording {number}."}),
                    ]

                client, model = self.run_script(batch)
                self.assert_stopped_without_progress(client, model, before=21)
                self.assertEqual(len(model["edges"]), 1)
                self.assertTrue(model["run"].get("recent_rejections"))

    def test_new_read_ranges_keep_research_running(self):
        def batch(number):
            path, line = (("source-1/app.py", number) if number <= 21
                          else ("source-2/app.py", number - 21))
            return [("read_file", {"path": path, "offset": line, "limit": 1})]

        client, model = self.run_script(batch, max_steps=30)
        self.assertEqual(client.requests, 30)
        self.assertEqual(model["run"]["stop_kind"], "step_limit", model["run"])
        self.assertFalse(model["run"].get("loop_recovery_attempted"))
        self.assertEqual(sum(row["line_count"] for row in model["read_coverage"]), 30)

    def test_new_discovery_pages_keep_research_running(self):
        source = self.root / "discovery-input"
        source.mkdir()
        for number in range(30):
            (source / f"handler-{number:02}.py").write_text("value = 1\n", encoding="utf-8")
        workspace = build_workspace([source], self.root / "discovery-snapshot")
        with patch("pko.code_tools.tools.MAX_LIST_FILES", 1):
            client, model = self.run_script(lambda number: [
                ("list_files", {"offset": number})], max_steps=30, workspace=workspace)
        self.assertEqual(client.requests, 30)
        self.assertEqual(model["run"]["stop_kind"], "step_limit", model["run"])
        self.assertFalse(model["run"].get("loop_recovery_attempted"))
        self.assertEqual(len(model["research_history"]), 30)

    def test_resume_remembers_discovery_but_allows_new_pages(self):
        source = self.root / "resume-discovery-input"
        source.mkdir()
        for number in range(31):
            (source / f"handler-{number:02}.py").write_text("value = 1\n", encoding="utf-8")
        workspace = build_workspace([source], self.root / "resume-discovery-snapshot")
        saved = []
        with patch("pko.code_tools.tools.MAX_LIST_FILES", 1):
            run_agent(workspace, SCENARIO, SPEC,
                      client=GeneratedBatchClient(lambda number: [("list_files", {"offset": 1})]),
                      max_steps=1, checkpoint=saved.append)
            checkpoint = saved[-1]
            repeated = GeneratedBatchClient(lambda number: [("list_files", {"offset": 1})])
            repeated_model = run_agent(workspace, SCENARIO, SPEC, client=repeated,
                                       max_steps=40, resume_checkpoint=checkpoint)
            new_pages = GeneratedBatchClient(lambda number: [("list_files", {"offset": number + 1})])
            new_model = run_agent(workspace, SCENARIO, SPEC, client=new_pages,
                                  max_steps=30, resume_checkpoint=checkpoint)
        self.assert_stopped_without_progress(repeated, repeated_model, before=7)
        self.assertEqual(new_pages.requests, 30)
        self.assertEqual(new_model["run"]["stop_kind"], "step_limit", new_model["run"])
        self.assertFalse(new_model["run"].get("loop_recovery_attempted"))
        self.assertEqual(new_model["run"]["attempt"], 2)
        self.assertEqual(len(new_model["research_history"]), 31)


if __name__ == "__main__":
    unittest.main()
