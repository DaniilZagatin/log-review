"""Belka publishes the map and its business stages only, with its own audit and trace."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pko.cli import _build_parser
from pko.errors import PkoError
from pko.scenario_map import runner
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources


def entries(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(part.split("\n", 1)[1]) for part in text.split("\n=== ")[1:]]


class MapOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "out"
        self.enterContext(patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}))

    def test_cli_has_no_report_options(self):
        parser = _build_parser()
        for extra in (["--observability"], ["--runtime-log", "log.jsonl"], ["--domain-profile", "bank"]):
            with self.subTest(extra=extra), patch("sys.stderr"), self.assertRaises(SystemExit):
                parser.parse_args(["map-scenario", "--process-file", "p.txt", "--source", "a.zip", *extra])

    def test_cli_runs_and_rebuilds_with_the_map_options_only(self):
        parser = _build_parser()
        model = {"run": {"id": "previous", "status": "completed"}}
        for extra, method in ((["--scenario-text", SCENARIO, "--source", "source.zip"], "run_scenario"),
                              (["--from-map", "saved/scenario_map.json"], "rebuild_from_map")):
            with self.subTest(method=method), patch.object(runner, method, return_value=model) as run, \
                    redirect_stdout(io.StringIO()):
                args = parser.parse_args(["map-scenario", *extra, "--out", str(self.out), "--quiet"])
                self.assertEqual(args.handler(args), 0)
                run.assert_called_once()

    def test_rebuild_rejects_inputs_it_does_not_use(self):
        parser = _build_parser()
        for extra in (["--no-stages"], ["--source", "source.zip"]):
            with self.subTest(extra=extra), patch.object(runner, "rebuild_from_map") as rebuild:
                args = parser.parse_args(["map-scenario", "--from-map", "saved/scenario_map.json", *extra])
                with self.assertRaises(PkoError):
                    args.handler(args)
                rebuild.assert_not_called()

    def test_run_publishes_map_stages_and_audit_only(self):
        result = runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual({path.name for path in self.out.iterdir()},
                         {"scenario_map.json", "scenario_map.html", "scenario_map.checkpoint.json",
                          "map_hierarchy.json", "audit"})
        html = (self.out / "scenario_map.html").read_text(encoding="utf-8")
        self.assertIn("Бизнес-операции", html)
        self.assertNotIn('role="tablist"', html)

    def test_grouping_joins_the_run_trace_and_every_span_ends(self):
        result = runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        self.assertEqual(result["run"]["audit_status"], "complete")
        path = next((self.out / "audit").glob("run_*.txt"))
        data = entries(path)
        started = {row["span_id"]: row for row in data if row["event"] == "span_started"}
        finished = {row["span_id"]: row for row in data if row["event"] == "span_finished"}
        self.assertEqual(set(started), set(finished), "every started span ends")
        roots = [row for row in started.values() if row["parent_span_id"] is None]
        self.assertEqual([row["name"] for row in roots], ["scenario_map_run"])
        by_name = {row["name"]: row for row in started.values()}
        self.assertEqual(started[by_name["stage_model_request"]["parent_span_id"]]["name"], "business_stages")
        self.assertEqual(started[by_name["business_stages"]["parent_span_id"]]["name"], "scenario_map_run")
        self.assertTrue(any(row["event"] == "stage_model_answer" for row in data))
        self.assertNotIn("reasoning_chars", path.read_text(encoding="utf-8"))

    def test_failed_local_journal_is_visible_in_the_result(self):
        with patch("pko.audit.harden_file", side_effect=OSError("disk is read-only")):
            result = runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        self.assertEqual(result["run"]["status"], "completed")
        self.assertEqual(result["run"]["audit_status"], "partial")
        self.assertTrue((self.out / "scenario_map.json").exists())

    def test_saved_map_must_be_a_finished_current_map(self):
        path = Path(self.temp.name) / "map.json"
        for content in ({"schema_version": "1.0", "run": {"status": "completed"}, "nodes": [{}]},
                        {"schema_version": "2.0", "run": {"status": "running"}, "nodes": [{}]}):
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(PkoError):
                runner.load_saved_map(path)

    def test_rebuild_leaves_other_files_of_the_folder_untouched(self):
        runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        # Files of earlier Belka versions are neither read nor removed.
        (self.out / "process_report.json").write_text('{"report_version": "0.1"}', encoding="utf-8")
        original = {name: (self.out / name).read_bytes() for name in ("scenario_map.json", "process_report.json")}
        runner.rebuild_from_map(self.out / "scenario_map.json", self.out, spec=SPEC, client=ScriptedClient())
        self.assertNotIn('role="tablist"', (self.out / "scenario_map.html").read_text(encoding="utf-8"))
        for name, content in original.items():
            self.assertEqual(content, (self.out / name).read_bytes(), name)

    def test_rebuild_in_another_folder_copies_exact_map_bytes(self):
        runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        source = self.out / "scenario_map.json"
        # Preserve formatting too, including Windows newlines.
        original = json.dumps(json.loads(source.read_text(encoding="utf-8")), ensure_ascii=False, indent=4)
        source.write_bytes((original.replace("\n", "\r\n") + "\r\n").encode("utf-8"))
        target = self.out.parent / "rebuilt"
        runner.rebuild_from_map(source, target, spec=SPEC, client=ScriptedClient())
        self.assertEqual((target / "scenario_map.json").read_bytes(), source.read_bytes())
        self.assertTrue((target / "scenario_map.html").exists())
        self.assertTrue((target / "map_hierarchy.json").exists())
        # An explicitly named saved map can also be rebuilt in its own folder.
        renamed = target / "published.json"
        renamed.write_bytes(source.read_bytes())
        (target / "scenario_map.json").unlink()
        runner.rebuild_from_map(renamed, target, spec=SPEC, client=ScriptedClient())
        self.assertEqual((target / "scenario_map.json").read_bytes(), renamed.read_bytes())

    def test_late_trace_failure_is_saved_in_the_published_map(self):
        original = runner._publish_stages
        completed = False

        def stages(*args, **kwargs):
            nonlocal completed
            result = original(*args, **kwargs)
            completed = True
            return result

        def health():
            return {"status": "failed" if completed else "not_configured", "failures":
                    [{"operation": "export", "message": "stage export failed"}] if completed else []}

        with patch.object(runner, "_publish_stages", side_effect=stages), \
                patch.object(runner, "flush_tracing", side_effect=health):
            runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        saved = json.loads((self.out / "scenario_map.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["run"]["trace_export"]["status"], "failed")
        self.assertIn("stage export failed", (self.out / "scenario_map.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
