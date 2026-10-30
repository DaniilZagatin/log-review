"""Tests for the run manifest contract and the skeleton CLI."""

import io
import itertools
import json
import os
import platform
import subprocess
import sys
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from process_observability import __version__
from process_observability.__main__ import main
from process_observability.manifest import (
    SCHEMA_VERSION,
    STAGE_NAMES,
    DataOrigin,
    InputFingerprint,
    ManifestError,
    ModelMode,
    Provenance,
    RunManifest,
    Stages,
    StageState,
    StageStatus,
)

ROOT = Path(__file__).resolve().parents[1]
SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64


def valid_dict():
    """A fresh valid manifest in JSON form covering every stage status."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": "demo-001",
        "data_origin": "synthetic",
        "model_mode": "fake",
        "input_fingerprints": [
            {"path": "fixtures/app/main.py", "sha256": SHA_A},
            {"path": "fixtures/logs/events.jsonl", "sha256": SHA_B},
        ],
        "provenance": {"tool_version": "0.1.0", "python_version": "3.11.9", "config_sha256": SHA_C},
        "stages": {
            "map": {"status": "completed", "blockers": []},
            "coverage": {"status": "incomplete", "blockers": ["delivery records not provided"]},
            "instrumentation": {"status": "blocked", "blockers": ["coverage is incomplete"]},
            "metrics": {"status": "not_started", "blockers": []},
        },
    }


def _parent(data, path):
    for key in path[:-1]:
        data = data[key]
    return data


def replaced(path, value):
    """valid_dict() with the value at ``path`` set, or added if the key is new."""
    data = valid_dict()
    _parent(data, path)[path[-1]] = value
    return data


def without(path):
    data = valid_dict()
    del _parent(data, path)[path[-1]]
    return data


def parse(data):
    return RunManifest.from_json(json.dumps(data))


def make_manifest(**overrides):
    values = {
        "run_id": "demo-001",
        "data_origin": DataOrigin.SYNTHETIC,
        "model_mode": ModelMode.FAKE,
        "input_fingerprints": (InputFingerprint("fixtures/app/main.py", SHA_A),),
        "provenance": Provenance("0.1.0", "3.11.9", SHA_C),
    }
    values.update(overrides)
    return RunManifest(**values)


class ManifestContractTests(unittest.TestCase):
    def assertRejected(self, data, error_path, fragment):
        with self.assertRaises(ManifestError) as ctx:
            parse(data)
        self.assertEqual(ctx.exception.path, error_path)
        self.assertIn(fragment, str(ctx.exception))

    def test_defaults_leave_all_four_stages_not_started(self):
        self.assertEqual(STAGE_NAMES, ("map", "coverage", "instrumentation", "metrics"))
        self.assertEqual(StageState(), StageState(StageStatus.NOT_STARTED, ()))
        stages = Stages()
        for name in STAGE_NAMES:
            self.assertEqual(getattr(stages, name), StageState(StageStatus.NOT_STARTED, ()))

        manifest = make_manifest()
        self.assertEqual(manifest.schema_version, SCHEMA_VERSION)
        self.assertEqual(manifest.stages, Stages())
        self.assertEqual(
            manifest.to_dict()["stages"],
            {name: {"status": "not_started", "blockers": []} for name in STAGE_NAMES},
        )

    def test_json_round_trip_preserves_every_field(self):
        manifest = parse(valid_dict())
        self.assertEqual(manifest.to_dict(), valid_dict())
        self.assertEqual(
            manifest.stages.coverage,
            StageState(StageStatus.INCOMPLETE, ("delivery records not provided",)),
        )
        self.assertEqual(
            manifest.input_fingerprints[1], InputFingerprint("fixtures/logs/events.jsonl", SHA_B)
        )

        text = manifest.to_json()
        self.assertEqual(json.loads(text), valid_dict())
        restored = RunManifest.from_json(text)
        self.assertEqual(restored, manifest)
        self.assertEqual(restored.to_json(), text)

    def test_all_origin_and_mode_combinations_round_trip_independently(self):
        self.assertEqual({member.value for member in DataOrigin}, {"synthetic", "real"})
        self.assertEqual({member.value for member in ModelMode}, {"fake", "live"})
        combinations = list(itertools.product(DataOrigin, ModelMode))
        self.assertEqual(len(combinations), 4)
        for origin, mode in combinations:
            with self.subTest(data_origin=origin.value, model_mode=mode.value):
                manifest = make_manifest(data_origin=origin, model_mode=mode)
                text = manifest.to_json()
                data = json.loads(text)
                self.assertEqual((data["data_origin"], data["model_mode"]), (origin.value, mode.value))
                restored = RunManifest.from_json(text)
                self.assertEqual(restored, manifest)
                self.assertIs(restored.data_origin, origin)
                self.assertIs(restored.model_mode, mode)

    def test_constructor_requires_enum_members_and_tuples(self):
        cases = [
            (lambda: make_manifest(data_origin="synthetic"), ("data_origin",)),
            (lambda: make_manifest(model_mode="live"), ("model_mode",)),
            (lambda: make_manifest(input_fingerprints=[]), ("input_fingerprints",)),
            (lambda: make_manifest(stages={}), ("stages",)),
            (lambda: StageState("blocked", ("no logs",)), ("status",)),
            (lambda: StageState(StageStatus.BLOCKED, ["no logs"]), ("blockers",)),
            (lambda: Stages(map=None), ("map",)),
        ]
        for build, error_path in cases:
            with self.subTest(error_path=error_path):
                with self.assertRaises(ManifestError) as ctx:
                    build()
                self.assertEqual(ctx.exception.path, error_path)

    def test_blocked_and_incomplete_stages_require_nonblank_reasons(self):
        for status in (StageStatus.BLOCKED, StageStatus.INCOMPLETE):
            with self.subTest(status=status.value):
                with self.assertRaises(ManifestError) as ctx:
                    StageState(status)
                self.assertEqual(ctx.exception.path, ("blockers",))
                with self.assertRaises(ManifestError) as ctx:
                    StageState(status, ("   ",))
                self.assertEqual(ctx.exception.path, ("blockers[0]",))
                state = StageState(status, ("no runtime logs", "case key unconfirmed"))
                self.assertEqual(state.blockers, ("no runtime logs", "case key unconfirmed"))

        self.assertRejected(
            replaced(("stages", "instrumentation", "blockers"), []),
            ("stages", "instrumentation", "blockers"),
            "at least one reason is required when status is blocked",
        )
        self.assertRejected(
            replaced(("stages", "coverage", "blockers"), ["ok", ""]),
            ("stages", "coverage", "blockers[1]"),
            "must not be blank",
        )

    def test_blockers_rejected_for_not_started_and_completed(self):
        for status in (StageStatus.NOT_STARTED, StageStatus.COMPLETED):
            with self.subTest(status=status.value):
                with self.assertRaises(ManifestError) as ctx:
                    StageState(status, ("leftover reason",))
                self.assertEqual(ctx.exception.path, ("blockers",))
        self.assertRejected(
            replaced(("stages", "map", "blockers"), ["note"]),
            ("stages", "map", "blockers"),
            "must be empty when status is completed",
        )

    def test_missing_fields_rejected_at_every_level(self):
        cases = [
            (("run_id",), ()),
            (("schema_version",), ()),
            (("provenance", "config_sha256"), ("provenance",)),
            (("input_fingerprints", 0, "sha256"), ("input_fingerprints[0]",)),
            (("stages", "metrics"), ("stages",)),
            (("stages", "coverage", "blockers"), ("stages", "coverage")),
        ]
        for path, error_path in cases:
            with self.subTest(path=path):
                self.assertRejected(without(path), error_path, f"missing field(s): {path[-1]}")

    def test_unknown_fields_rejected_at_every_level(self):
        cases = [
            (("verified",), ()),
            (("provenance", "model_id"), ("provenance",)),
            (("input_fingerprints", 0, "verified"), ("input_fingerprints[0]",)),
            (("stages", "report"), ("stages",)),
            (("stages", "map", "score"), ("stages", "map")),
        ]
        for path, error_path in cases:
            with self.subTest(path=path):
                self.assertRejected(replaced(path, True), error_path, f"unknown field(s): {path[-1]}")

    def test_invalid_types_rejected_with_nested_path(self):
        cases = [
            (("run_id",), 7, ("run_id",), "expected a string, got number"),
            (("data_origin",), True, ("data_origin",), "expected one of synthetic, real"),
            (("model_mode",), None, ("model_mode",), "expected one of fake, live"),
            (("input_fingerprints",), {}, ("input_fingerprints",), "expected a JSON array, got object"),
            (
                ("input_fingerprints", 0),
                "fixtures/app/main.py",
                ("input_fingerprints[0]",),
                "expected a JSON object, got string",
            ),
            (("input_fingerprints", 0, "sha256"), "A" * 64, ("input_fingerprints[0]", "sha256"), "64 lowercase"),
            (("input_fingerprints", 1, "sha256"), "a" * 63, ("input_fingerprints[1]", "sha256"), "64 lowercase"),
            (("provenance",), [], ("provenance",), "expected a JSON object, got array"),
            (("provenance", "tool_version"), 0.1, ("provenance", "tool_version"), "expected a string"),
            (("provenance", "config_sha256"), None, ("provenance", "config_sha256"), "64 lowercase"),
            (("stages",), [], ("stages",), "expected a JSON object, got array"),
            (("stages", "map"), "completed", ("stages", "map"), "expected a JSON object, got string"),
            (("stages", "map", "status"), 1, ("stages", "map", "status"), "expected one of"),
            (
                ("stages", "coverage", "blockers"),
                "delivery records not provided",
                ("stages", "coverage", "blockers"),
                "expected a JSON array, got string",
            ),
            (
                ("stages", "coverage", "blockers", 0),
                5,
                ("stages", "coverage", "blockers[0]"),
                "expected a string, got number",
            ),
        ]
        for path, value, error_path, fragment in cases:
            with self.subTest(path=path, value=value):
                self.assertRejected(replaced(path, value), error_path, fragment)

    def test_invalid_enum_values_rejected(self):
        cases = [
            (("data_origin",), "mixed"),
            (("data_origin",), "Synthetic"),
            (("model_mode",), "mock"),
            (("model_mode",), "LIVE"),
            (("stages", "map", "status"), "done"),
            (("stages", "metrics", "status"), "NOT_STARTED"),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                self.assertRejected(replaced(path, value), path, "expected one of")

    def test_blank_and_unsafe_identifiers_rejected(self):
        for run_id in ("", "   ", "../escape", "runs/demo", " demo-001", "demo-001\n", "x" * 129):
            with self.subTest(run_id=run_id):
                self.assertRejected(replaced(("run_id",), run_id), ("run_id",), "run_id")
        for path in ("", "  ", ".", "/etc/passwd", "../outside.txt", "fixtures/../../x", "fixtures\\app.py"):
            with self.subTest(input_path=path):
                self.assertRejected(
                    replaced(("input_fingerprints", 0, "path"), path),
                    ("input_fingerprints[0]", "path"),
                    "path",
                )
        for name in ("tool_version", "python_version"):
            with self.subTest(provenance=name):
                self.assertRejected(replaced(("provenance", name), " "), ("provenance", name), "must not be blank")

    def test_duplicate_input_paths_rejected(self):
        self.assertRejected(
            replaced(("input_fingerprints", 1, "path"), "fixtures/app/main.py"),
            ("input_fingerprints[1]",),
            "duplicate path",
        )

    def test_unsupported_schema_version_reported_before_field_checks(self):
        self.assertRejected(replaced(("schema_version",), "0.2"), ("schema_version",), "unsupported")
        future = replaced(("schema_version",), "0.2")
        future["new_field"] = True
        self.assertRejected(future, ("schema_version",), "unsupported")
        with self.assertRaises(ManifestError) as ctx:
            make_manifest(schema_version="0.2")
        self.assertEqual(ctx.exception.path, ("schema_version",))

    def test_nonfinite_numbers_rejected(self):
        text = json.dumps(valid_dict())
        for token in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(token=token):
                with self.assertRaises(ManifestError) as ctx:
                    RunManifest.from_json(text.replace('"demo-001"', token))
                self.assertIn("non-finite", str(ctx.exception))

    def test_malformed_json_rejected(self):
        texts = ["", "{", "not json", "[]", "null", '"manifest"', '{"n": ' + "1" * 5000 + "}", "[" * 100_000]
        for text in texts:
            with self.subTest(text=text[:20]):
                with self.assertRaises(ManifestError):
                    RunManifest.from_json(text)
        with self.assertRaises(ManifestError) as ctx:
            RunManifest.from_json('{"stages": {"map": {"status": "completed", "status": "blocked"}}}')
        self.assertIn("duplicate JSON key 'status'", str(ctx.exception))
        with self.assertRaises(ManifestError):
            RunManifest.from_json(json.dumps(valid_dict()).encode())

    def test_parse_error_is_deliberate_value_error_subclass(self):
        self.assertTrue(issubclass(ManifestError, ValueError))
        self.assertIsNot(ManifestError, ValueError)
        with self.assertRaises(ManifestError) as ctx:
            RunManifest.from_json("{")
        self.assertNotIsInstance(ctx.exception, json.JSONDecodeError)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_provenance_current_records_running_versions(self):
        provenance = Provenance.current(SHA_C)
        self.assertEqual(provenance.tool_version, __version__)
        self.assertEqual(provenance.python_version, platform.python_version())
        with self.assertRaises(ManifestError):
            Provenance.current("not-a-hash")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_status_reports_map_first_logging_without_claiming_live_validation(self):
        code, out, err = run_cli("status")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(
            set(payload),
            {
                "package",
                "version",
                "state",
                "analysis_pipeline_implemented",
                "implemented",
                "manifest_schema_version",
                "stages",
                "message",
                "default_command",
                "business_metrics",
            },
        )
        self.assertEqual(payload["state"], "map_first_logging")
        self.assertIs(payload["analysis_pipeline_implemented"], True)
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["manifest_schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload['default_command'], 'review-logging')
        self.assertEqual(payload['business_metrics'], 'frozen')
        self.assertEqual(payload['stages']['metrics'], 'frozen')
        self.assertNotIn('map', payload['stages'])
        self.assertIn('independent acceptance', payload['message'])

    def test_version(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"process_observability {__version__}")

    def test_help_lists_mvp_commands(self):
        code, out, _ = run_cli("--help")
        self.assertEqual(code, 0)
        text = " ".join(out.split())
        self.assertIn("status", text)
        self.assertIn("pilot MVP", text)
        for command in ("demo", "analyze", "check-model"):
            self.assertIn(command, text)

    def test_unknown_commands_are_not_offered(self):
        for command in ("run", "deploy"):
            with self.subTest(command=command):
                code, out, err = run_cli(command)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("invalid choice", err)

    def test_command_is_required(self):
        code, out, err = run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("required", err)

    def test_module_entry_point_runs_with_pythonpath_only(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run(
            [sys.executable, "-B", "-m", "process_observability", "status"],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)["analysis_pipeline_implemented"], True)

    def test_pyproject_metadata_matches_package(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["version"], __version__)
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], [])


if __name__ == "__main__":
    unittest.main()
