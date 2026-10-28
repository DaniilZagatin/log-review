"""Public map-only mode: no report work or stale report tabs, with tracing intact."""
import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pko.cli import _build_parser
from pko.errors import PkoError
from pko.observability.pipeline import ReportOptions
from pko.scenario_map import runner
from observability_script import ScriptedReportClient
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources


class MapOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "out"
        self.enterContext(patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}))

    def test_disabled_cli_flags_stop_before_inputs_or_models(self):
        parser = _build_parser()
        flags = [["--observability"], ["--runtime-log", "missing.jsonl"],
                 ["--runtime-fields", "case_id=id"], ["--domain-profile", "bank-process-kpi"],
                 ["--observability-max-requests", "24"], ["--observability-max-seconds", "1800"]]
        with patch.object(runner, "read_process_description") as read, \
                patch.object(runner, "run_scenario") as run, \
                patch.object(runner, "profile_runtime_log") as logs:
            for extra in flags:
                with self.subTest(extra=extra):
                    args = parser.parse_args(["map-scenario", "--process-file", "missing.txt", *extra])
                    with self.assertRaisesRegex(PkoError, "временно отключены"):
                        args.handler(args)
            read.assert_not_called()
            run.assert_not_called()
            logs.assert_not_called()

    def test_python_entry_points_cannot_start_the_report(self):
        with patch.object(runner, "researcher_spec") as spec, patch.object(runner, "_read_saved_map") as load:
            for kwargs in ({"observability": True}, {"report_options": ReportOptions()}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(PkoError, "временно отключены"):
                    runner.run_scenario(SCENARIO, sources(), self.out, **kwargs)
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(PkoError, "временно отключены"):
                    runner.rebuild_from_map("missing.json", [], self.out, **kwargs)
            spec.assert_not_called()
            load.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_normal_cli_passes_no_report_options_and_rebuild_ignores_old_report_error(self):
        parser = _build_parser()
        model = {"run": {"id": "previous", "status": "completed", "process_report_error": "historical failure"}}
        for extra, method in ((["--scenario-text", SCENARIO, "--source", "source.zip"], "run_scenario"),
                              (["--from-map", "saved/scenario_map.json"], "rebuild_from_map")):
            with self.subTest(method=method), patch.object(runner, method, return_value=model) as run, \
                    redirect_stdout(io.StringIO()) as stdout:
                args = parser.parse_args(["map-scenario", *extra, "--out", str(self.out), "--quiet"])
                self.assertEqual(args.handler(args), 0)
                self.assertFalse(run.call_args.kwargs["observability"])
                self.assertIsNone(run.call_args.kwargs["report_options"])
                self.assertNotIn("historical failure", stdout.getvalue())

    def test_map_has_both_views_and_local_audit_without_report_calls(self):
        with patch.object(runner, "run_observability") as report:
            result = runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient())
        report.assert_not_called()
        self.assertEqual(result["run"]["status"], "completed")
        self.assertTrue((self.out / "map_hierarchy.json").exists())
        self.assertFalse((self.out / "process_report.json").exists())
        self.assertFalse((self.out / "handoff").exists())
        html = (self.out / "scenario_map.html").read_text(encoding="utf-8")
        self.assertIn("Бизнес-этапы", html)
        self.assertNotIn('role="tablist"', html)
        audit = next((self.out / "audit").glob("run_*.txt")).read_text(encoding="utf-8")
        self.assertIn("business_stages", audit)
        self.assertIn("scenario_map_run", audit)
        self.assertNotIn('"name": "process_observability"', audit)

    def test_rebuild_ignores_saved_report_but_preserves_its_files_and_map(self):
        # Create a historical report without making the feature available to normal runs.
        with patch.object(runner, "OBSERVABILITY_ENABLED", True):
            runner.run_scenario(SCENARIO, sources(), self.out, spec=SPEC, client=ScriptedClient(),
                                observability=True, report_options=ReportOptions(client=ScriptedReportClient()))
        self.assertIn('role="tablist"', (self.out / "scenario_map.html").read_text(encoding="utf-8"))
        original = {name: (self.out / name).read_bytes() for name in
                    ("scenario_map.json", "process_report.json", "handoff/manifest.json")}
        with patch.object(runner, "run_observability") as report:
            runner.rebuild_from_map(self.out / "scenario_map.json", [], self.out, spec=SPEC, client=ScriptedClient())
        report.assert_not_called()
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
        with patch.object(runner, "run_observability") as report:
            runner.rebuild_from_map(source, [], target, spec=SPEC, client=ScriptedClient())
        report.assert_not_called()
        self.assertEqual((target / "scenario_map.json").read_bytes(), source.read_bytes())
        self.assertTrue((target / "scenario_map.html").exists())
        self.assertTrue((target / "map_hierarchy.json").exists())
        # An explicitly named saved map can also be rebuilt in its own folder.
        renamed = target / "published.json"
        renamed.write_bytes(source.read_bytes())
        (target / "scenario_map.json").unlink()
        runner.rebuild_from_map(renamed, [], target, spec=SPEC, client=ScriptedClient())
        self.assertEqual((target / "scenario_map.json").read_bytes(), renamed.read_bytes())

    def test_late_trace_failure_is_saved_in_map_only_outputs(self):
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
