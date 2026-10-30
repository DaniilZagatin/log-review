"""Resume is a new bounded attempt over the exact saved research and sources."""
from copy import deepcopy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from pko.cli import _build_parser
from pko.errors import PkoError
from pko.scenario_map import runner
from pko.render.scenario_map import render_scenario_map
from scenario_script import SCENARIO, SPEC, ScriptedClient, research_calls, sources


class ResumeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"
        self.enterContext(patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}))

    def run_first(self, steps=10, source_paths=None):
        return runner.run_scenario(SCENARIO, source_paths or sources(), self.out,
                                   spec=SPEC, client=ScriptedClient(), max_steps=steps, stages=False)

    @property
    def checkpoint(self):
        return self.out / "scenario_map.checkpoint.json"

    def test_cli_resume_is_exclusive_and_requires_sources(self):
        parser = _build_parser()
        for extra in (["--scenario-text", "different"], ["--process-file", "p.txt"],
                      ["--from-map", "map.json"]):
            with self.subTest(extra=extra), patch("sys.stderr"), self.assertRaises(SystemExit):
                parser.parse_args(["map-scenario", "--resume", "checkpoint.json", *extra])
        args = parser.parse_args(["map-scenario", "--resume", "checkpoint.json"])
        with patch.object(runner, "resume_scenario") as resume, self.assertRaises(PkoError):
            args.handler(args)
        resume.assert_not_called()

    def test_cli_routes_resume_and_defaults_to_checkpoint_folder(self):
        parser = _build_parser()
        args = parser.parse_args(["map-scenario", "--resume", str(self.checkpoint), "--source", "code.zip",
                                  "--max-steps", "8", "--max-seconds", "90", "--model-timeout", "30",
                                  "--summary-max-tokens", "2048", "--no-stages", "--quiet"])
        model = {"run": {"status": "partial", "parent_run_id": "original", "attempt": 2}}
        with patch.object(runner, "resume_scenario", return_value=model) as resume, redirect_stdout(io.StringIO()):
            self.assertEqual(args.handler(args), 2)
        self.assertEqual(resume.call_args.args, (str(self.checkpoint), ["code.zip"], self.out.resolve()))
        self.assertEqual(resume.call_args.kwargs["max_steps"], 8)
        self.assertEqual(resume.call_args.kwargs["max_seconds"], 90)
        self.assertEqual(resume.call_args.kwargs["model_timeout"], 30)
        self.assertFalse(resume.call_args.kwargs["stages"])

    def test_resume_round_trip_keeps_graph_and_tracks_new_attempt(self):
        first = self.run_first()
        self.assertEqual(first["run"]["stop_kind"], "step_limit")
        self.assertTrue(first["nodes"])
        old_nodes = deepcopy(first["nodes"])
        remaining = ScriptedClient(research_calls()[10:])
        second = runner.resume_scenario(self.checkpoint, sources(), spec=SPEC, client=remaining, stages=False)
        self.assertEqual(second["run"]["status"], "completed", second["run"])
        self.assertEqual(len(second["nodes"]), 18)
        self.assertEqual(len(second["edges"]), 17)
        self.assertEqual(second["nodes"][:len(old_nodes)], old_nodes)
        self.assertEqual(second["scenario"]["description"], SCENARIO)
        self.assertEqual(second["run"]["parent_run_id"], first["run"]["id"])
        self.assertEqual(second["run"]["root_run_id"], first["run"]["id"])
        self.assertEqual(second["run"]["attempt"], 2)
        self.assertNotEqual(second["run"]["id"], first["run"]["id"])
        # OTel IDs are absent when the optional exporter is disabled; local
        # audit/run identities still distinguish every resumed attempt.
        if first["run"].get("trace_id") and second["run"].get("trace_id"):
            self.assertNotEqual(second["run"]["trace_id"], first["run"]["trace_id"])
        self.assertEqual(len(list((self.out / "audit").glob("run_*.txt"))), 2)
        saved = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        self.assertIn("_resume", saved)
        self.assertEqual(saved["run"]["id"], second["run"]["id"])
        published = json.loads((self.out / "scenario_map.json").read_text(encoding="utf-8"))
        self.assertNotIn("_resume", published)

    def test_resume_after_finish_republishes_and_groups_without_research_calls(self):
        first = runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient(), stages=False)
        self.assertEqual(first["run"]["stop_kind"], "finish")
        client = ScriptedClient(calls=[])
        result = runner.resume_scenario(self.checkpoint, sources(), spec=SPEC, client=client, max_steps=1)
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual(result["nodes"], first["nodes"])
        self.assertTrue((self.out / "map_hierarchy.json").exists())
        self.assertEqual(client.seen, [], "A finished checkpoint must not restart the researcher")

    def test_source_change_rejects_before_model_and_preserves_checkpoint(self):
        copied = []
        for source in sources():
            destination = self.root / "code" / source.name
            shutil.copytree(source, destination)
            copied.append(destination)
        self.run_first(source_paths=copied)
        previous = self.checkpoint.read_bytes()
        target = copied[0] / "app.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# altered source snapshot\n", encoding="utf-8")
        client = Mock(spec=ScriptedClient)
        client.spec = SPEC
        with self.assertRaises(PkoError):
            runner.resume_scenario(self.checkpoint, copied, spec=SPEC, client=client, stages=False)
        client.chat.assert_not_called()
        self.assertEqual(self.checkpoint.read_bytes(), previous)

    def test_model_change_rejects_before_model_and_preserves_checkpoint(self):
        self.run_first()
        previous = self.checkpoint.read_bytes()
        changed = replace(SPEC, model="different-model")
        client = Mock(spec=ScriptedClient)
        client.spec = changed
        with self.assertRaises(PkoError):
            runner.resume_scenario(self.checkpoint, sources(), spec=changed, client=client, stages=False)
        client.chat.assert_not_called()
        self.assertEqual(self.checkpoint.read_bytes(), previous)

    def test_legacy_snapshot_is_not_silently_resumed(self):
        self.out.mkdir()
        self.checkpoint.write_text(json.dumps({"schema_version": "2.0", "scenario": {"description": SCENARIO},
                                              "run": {"status": "partial"}}), encoding="utf-8")
        previous = self.checkpoint.read_bytes()
        with patch.object(runner, "run_scenario") as run, self.assertRaises(PkoError):
            runner.resume_scenario(self.checkpoint, sources(), spec=SPEC)
        run.assert_not_called()
        self.assertEqual(self.checkpoint.read_bytes(), previous)

    def test_checkpoint_write_failure_blocks_grouping_and_is_visible(self):
        model = {"schema_version": "2.0", "semantic_profile": "variant-actions-v1",
                 "scenario": {"title": "Saved work", "entry_operation_ids": ["a"], "terminal_operation_ids": ["a"]},
                 "nodes": [{"id": "a", "kind": "operation", "name": "Action"}], "edges": [],
                 "validation": {"structural_issues": []},
                 "run": {"status": "partial", "stop_kind": "checkpoint_error", "reason": "Storage failed"}}
        with patch.object(runner, "run_agent", return_value=model), patch.object(runner, "_publish_stages") as groups:
            result = runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        groups.assert_not_called()
        self.assertEqual(result["run"]["grouping_status"], "checkpoint_error")
        html = render_scenario_map(result)
        self.assertIn('class="result-lifecycle emergency"', html)
        self.assertIn("Ошибка сохранения checkpoint", html)
        self.assertIn("checkpoint не сохранён", html)

    def test_from_map_rejects_checkpoint_even_after_finished_research(self):
        runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient(), stages=False)
        with self.assertRaises(PkoError) as error:
            runner.load_saved_map(self.checkpoint)
        self.assertIn("--resume", str(error.exception.hint))


if __name__ == "__main__":
    unittest.main()
