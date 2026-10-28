"""Input: one process description file plus one or many ZIP archives of code."""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pko.cli import _build_parser
from pko.code_tools.local_tree import MAX_FILE_BYTES
from pko.errors import PkoError
from pko.scenario_map.agent import TOOL_SCHEMAS, run_agent
from pko.scenario_map.context import context_budget
from pko.scenario_map.runner import run_scenario
from pko.scenario_map.sources import build_workspace, read_process_description
from pko.scenario_map.tools import TOOLS
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources


class ProcessInputTests(unittest.TestCase):
    """Input: one process description file plus one or many ZIP archives of code."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_description_file_is_masked_and_has_no_fixed_character_limit(self):
        path = self.root / "process.txt"
        path.write_text(SCENARIO + '\npassword = "hunter2hunter2"\n' + "Полезные сведения о процессе. " * 1500,
                        encoding="utf-8")
        text = read_process_description(path)
        self.assertGreater(len(text), 30000)
        self.assertNotIn("hunter2hunter2", text)
        path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(PkoError):
            read_process_description(path)
        path.write_text("   \n", encoding="utf-8")
        with self.assertRaises(PkoError):
            read_process_description(path)

    def test_long_description_reaches_the_researcher_whole(self):
        description = SCENARIO + "\n" + "Полезные сведения о процессе. " * 1000
        output = self.root / "out"
        # The production model has a 300 000-token window; 64 000 would not leave half for research.
        result = run_scenario(description, sources(), output, spec=replace(SPEC, context_window=300000),
                              client=ScriptedClient())
        self.assertEqual(result["run"]["status"], "completed", result["run"].get("reason"))
        self.assertEqual(result["scenario"]["description"], description.strip())

    def test_description_that_does_not_fit_the_window_stops_before_research(self):
        window = SPEC.context_window
        budget, _ = context_budget(window, TOOL_SCHEMAS + TOOLS)
        description = SCENARIO + " " + "х" * budget
        client = ScriptedClient()
        workspace = build_workspace(sources(), self.root / "snap")
        result = run_agent(workspace, description, SPEC, client=client, max_steps=5)
        self.assertEqual(result["run"]["status"], "failed")
        self.assertIn("не помещаются в окно", result["run"]["reason"])
        self.assertEqual(client.seen, [], "no model request is made")

    def test_cli_takes_one_description_file_and_many_zip_archives(self):
        parser = _build_parser()
        args = parser.parse_args(["map-scenario", "--process-file", "process.txt",
                                  "--source", "front.zip", "back.zip", "--source", "worker.zip"])
        self.assertEqual((args.process_file, args.source), ("process.txt", ["front.zip", "back.zip", "worker.zip"]))
        self.assertEqual(parser.parse_args(["map-scenario", "--scenario-file", "p.txt", "--source", "a.zip"]).process_file,
                         "p.txt")
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["map-scenario", "--process-file", "p.txt", "--business-file", "b.md", "--source", "a.zip"])


class DefaultWindowTests(unittest.TestCase):
    def test_unknown_model_gets_the_deepseek_window_and_overrides_still_win(self):
        from pko.llm.registry import DEFAULT_CONTEXT_WINDOW, context_window_for
        with patch.dict(os.environ, {"PKO_CONTEXT_WINDOW": ""}):
            self.assertEqual(context_window_for("deepseek-v4"), DEFAULT_CONTEXT_WINDOW)
            self.assertEqual(context_window_for("GLM-5.2"), 300_000)
            self.assertEqual(context_budget(0, []), context_budget(DEFAULT_CONTEXT_WINDOW, []))
        with patch.dict(os.environ, {"PKO_CONTEXT_WINDOW": "128000"}):
            self.assertEqual(context_window_for("deepseek-v4"), 128_000)
        self.assertEqual(DEFAULT_CONTEXT_WINDOW, 524_288)


if __name__ == "__main__":
    unittest.main()
