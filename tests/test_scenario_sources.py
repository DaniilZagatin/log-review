"""Source boundaries are retained before the scenario agent sees any code."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pko.errors import PkoError
from pko.scenario_map.sources import build_workspace


class ScenarioSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def directory(self, name: str, files: dict[str, str | bytes]) -> Path:
        root = self.root / name
        root.mkdir(parents=True)
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return root

    def archive(self, name: str, files: dict[str, str | bytes]) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, content in files.items():
                archive.writestr(relative, content)
        return path

    def test_same_named_directories_have_separate_evidence_paths(self) -> None:
        first = self.directory("one/service", {"src/main.py": "def issue():\n    return 1\n"})
        second = self.directory("two/service", {"src/main.py": "def issue():\n    return 2\n"})
        workspace = build_workspace([first, second], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/src/main.py", "source-2/src/main.py"])
        self.assertIn("return 1", workspace.tree.read("source-1/src/main.py"))
        self.assertIn("return 2", workspace.tree.read("source-2/src/main.py"))
        self.assertEqual([item["name"] for item in workspace.sources], ["service", "service"])
        self.assertNotEqual(workspace.sources[0]["digest"], workspace.sources[1]["digest"])
        self.assertTrue(all(item["file_count"] == 1 for item in workspace.sources))

    def test_same_named_archives_keep_paths_and_never_overlay(self) -> None:
        first = self.archive("one/code.zip", {"application/main.sql": "SELECT * FROM first_table;"})
        second = self.archive("two/code.zip", {"application/main.sql": "SELECT * FROM second_table;"})
        workspace = build_workspace([first, second], self.root / "snapshot")
        self.assertEqual(workspace.tree.files,
                         ["source-1/application/main.sql", "source-2/application/main.sql"])
        self.assertIn("first_table", workspace.tree.read(workspace.tree.files[0]))
        self.assertIn("second_table", workspace.tree.read(workspace.tree.files[1]))
        self.assertEqual({fact.path for fact in workspace.extraction.facts}, set(workspace.tree.files))

    def test_snapshot_blocks_unknown_paths_and_survives_source_and_disk_edits(self) -> None:
        source = self.directory("code", {"main.py": "value = 1\n"})
        workspace = build_workspace([source], self.root / "snapshot")
        (source / "main.py").write_text("value = 2\n", encoding="utf-8")
        (self.root / "snapshot/source-1/main.py").write_text("value = 3\n", encoding="utf-8")
        self.assertEqual(workspace.tree.read("source-1/main.py"), "value = 1\n")
        self.assertIsNone(workspace.tree.read("../code/main.py"))
        self.assertIsNone(workspace.tree.read(str(source / "main.py")))

    def test_secrets_and_generated_files_are_excluded_before_reading(self) -> None:
        source = self.directory("code", {
            ".env": "API_KEY=never_read",
            ".env.local": "PASSWORD=never_read",
            "cert.PEM": "never_read",
            "client.jks": "never_read",
            "vendor/library.py": "never_read",
            "node_modules/lib/main.js": "never_read",
            "build/main.py": "never_read",
            ".git/config": "never_read",
            "main.py": 'api_token = "do_not_expose"\nvalue = 2\n',
            "image.png": b"\x89PNG\x00\x01\xff",
        })
        workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])
        self.assertEqual(workspace.tree.read("source-1/main.py"), 'api_token = "***"\nvalue = 2\n')
        self.assertEqual((source / "main.py").read_text(), 'api_token = "do_not_expose"\nvalue = 2\n')
        snapshot = (self.root / "snapshot/source-1/main.py").read_text()
        self.assertNotIn("do_not_expose", snapshot)
        self.assertNotIn("do_not_expose", repr(workspace.extraction))
        self.assertIn("пропущены", " ".join(workspace.notes))
        self.assertEqual(workspace.omissions, [])

    def test_zip_secrets_and_binary_are_also_excluded(self) -> None:
        source = self.archive("code.zip", {".env": "hidden", "client.key": "hidden",
                                           "bin.dat": b"\x00abc", "src/main.py": "pass\n"})
        workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/src/main.py"])

    def test_quoted_configuration_credentials_and_url_auth_are_masked(self) -> None:
        source = self.directory("code", {
            "settings.json": '{\n  "api_key": "demo-json-sensitive",\n'
                             '  "password": "demo-escaped-\\"-sensitive",\n'
                             '  "client_secret": 123456789,\n'
                             '  "Authorization": "Bearer demo-bearer-sensitive",\n'
                             '  "endpoint": "https://demo-user:demo-pass@example.invalid/v1"\n}\n',
            "settings.yaml": "'api_token': 'demo-yaml-sensitive'\n"
                             '"password": "demo-yaml-password"\n'
                             'dsn: postgresql://db-user:db-pass@database.invalid/db\n',
            "client.py": 'headers = {"Authorization": "Bearer demo-python-sensitive"}\n'
                         'endpoint = "https://other-user:other-pass@example.invalid"\n',
        })
        workspace = build_workspace([source], self.root / "snapshot")
        originals = {path.name: path.read_text(encoding="utf-8") for path in source.iterdir()}
        for path in workspace.tree.files:
            text = workspace.tree.read(path)
            self.assertIn("***", text)
            self.assertNotIn("sensitive", text)
            self.assertNotIn("123456789", text)
            self.assertNotIn("demo-yaml-password", text)
            self.assertNotIn("demo-user:demo-pass", text)
            self.assertNotIn("db-user:db-pass", text)
            self.assertNotIn("other-user:other-pass", text)
            self.assertEqual(len(text.splitlines()), len(originals[Path(path).name].splitlines()))
            self.assertEqual((self.root / "snapshot" / path).read_text(encoding="utf-8"), text)
        self.assertNotIn("sensitive", repr(workspace.extraction))
        self.assertIn("demo-json-sensitive", (source / "settings.json").read_text())

    def test_zip_rejects_traversal_absolute_and_windows_alias_paths(self) -> None:
        paths = ["../escape.py", "/absolute.py", "C:/drive.py", "src/../../escape.py",
                 "src\\..\\escape.py", "src/file.py:secret", "NUL.py", "src/trailing. /file.py"]
        for index, member in enumerate(paths):
            with self.subTest(member=member):
                source = self.archive(f"bad-{index}.zip", {member: "pass\n"})
                with self.assertRaises(PkoError):
                    build_workspace([source], self.root / f"snapshot-{index}")
        self.assertFalse((self.root / "escape.py").exists())

    def test_zip_case_collision_is_rejected(self) -> None:
        source = self.archive("code.zip", {"main.py": "first = 1", "MAIN.PY": "second = 2"})
        with self.assertRaises(PkoError):
            build_workspace([source], self.root / "snapshot")
        self.assertEqual((self.root / "snapshot/source-1/main.py").read_text(), "first = 1")

    def test_zip_symlink_is_not_followed_or_copied(self) -> None:
        source = self.root / "code.zip"
        with zipfile.ZipFile(source, "w") as archive:
            link = zipfile.ZipInfo("external.py")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../../outside.py")
            archive.writestr("main.py", "pass\n")
        workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])
        self.assertIn("нерегулярные элементы ZIP", " ".join(workspace.notes))

    def test_directory_symlinks_are_not_followed(self) -> None:
        source = self.directory("code", {"main.py": "pass\n"})
        outside = self.directory("outside", {"secret.py": "do_not_read = 1"})
        try:
            os.symlink(outside, source / "linked", target_is_directory=True)
            os.symlink(outside / "secret.py", source / "linked.py")
        except OSError:
            self.skipTest("Creating symlinks is not available on this host")
        workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_windows_junction_is_not_followed(self) -> None:
        source = self.directory("code", {"main.py": "pass\n"})
        outside = self.directory("outside", {"secret.py": "do_not_read = 1"})
        # Junction creation does not require the symbolic-link privilege. Both
        # explicit paths are under this test's private temporary directory.
        junction = source / "linked"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                                 str(junction), str(outside)], capture_output=True)
        if result.returncode:
            self.skipTest("Creating a junction is not available on this host")
        workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])
        self.assertIn("ссылки и junction", " ".join(workspace.notes))
        self.assertTrue((outside / "secret.py").exists())

    def test_snapshot_inside_source_is_not_recursively_ingested(self) -> None:
        source = self.directory("code", {"main.py": "pass\n"})
        workspace = build_workspace([source], source / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])
        self.assertIn("каталог выходного снимка", " ".join(workspace.notes))

    def test_explicit_output_subtrees_are_never_opened(self) -> None:
        source = self.directory("code", {
            "main.py": "pass\n",
            "scenario-map-out/checkpoint.json": '{"old": true}',
            "scenario-map-out/nested/audit.jsonl": '{"event": "old"}',
            "pko-scenario-out/scenario_map.html": "previous report",
            "reports/TESTS.txt": "previous tests",
            "reports-api/main.py": "def report():\n    return 1\n",
        })
        excluded = [source / name for name in ("scenario-map-out", "pko-scenario-out", "reports")]
        original_open = os.open
        opened = []

        def record_open(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                resolved = Path(path).resolve()
                self.assertFalse(any(resolved.is_relative_to(root) for root in excluded))
                opened.append(resolved)
            return original_open(path, *args, **kwargs)

        with patch("pko.scenario_map.sources.os.open", side_effect=record_open):
            workspace = build_workspace([source], self.root / "snapshot", exclude_paths=excluded)
        self.assertEqual(workspace.tree.files, ["source-1/main.py", "source-1/reports-api/main.py"])
        self.assertEqual(len(opened), 2)
        self.assertIn("явно исключённые выходные каталоги: 3", " ".join(workspace.notes))
        self.assertEqual(workspace.omissions, [])

    def test_explicit_file_and_entire_source_exclusions(self) -> None:
        source = self.directory("code", {"main.py": "pass\n", "audit.jsonl": "old evidence"})
        archive = self.archive("previous-report/code.zip", {"should_not_read.py": "pass\n"})
        with patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("must not read")):
            workspace = build_workspace([source, archive], self.root / "snapshot",
                                        exclude_paths=[source / "audit.jsonl", archive.parent])
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])
        self.assertEqual(workspace.sources[1]["file_count"], 0)
        self.assertIn("источник внутри явно исключённого", " ".join(workspace.notes))

    def test_exclusions_do_not_depend_on_output_already_existing(self) -> None:
        source = self.directory("code", {"main.py": "pass\n"})
        workspace = build_workspace([source], self.root / "snapshot",
                                    exclude_paths=[source / "future-out"])
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])

    def test_digest_depends_on_snapshot_and_not_source_location(self) -> None:
        source = self.directory("code", {"z.py": "value = 2\n", "a.py": "value = 1\n"})
        archive = self.archive("same.zip", {"a.py": "value = 1\n", "z.py": "value = 2\n"})
        workspace = build_workspace([source, archive], self.root / "snapshot")
        self.assertEqual(workspace.sources[0]["digest"], workspace.sources[1]["digest"])
        self.assertTrue(workspace.sources[0]["digest"].startswith("sha256:"))

    def test_file_limit_is_shared_across_sources(self) -> None:
        first = self.directory("one", {"main.py": "pass\n"})
        second = self.archive("two.zip", {"main.py": "pass\n"})
        with patch("pko.scenario_map.sources.MAX_WORKSPACE_FILES", 1):
            with self.assertRaises(PkoError):
                build_workspace([first, second], self.root / "snapshot")

    def test_byte_limit_is_checked_before_decompressing(self) -> None:
        source = self.archive("bomb.zip", {"repeated.py": "a" * 200_000})
        with patch("pko.scenario_map.sources.MAX_WORKSPACE_BYTES", 100_000):
            with patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("must not read")):
                with self.assertRaises(PkoError):
                    build_workspace([source], self.root / "snapshot")

    def test_individual_file_limit_records_omission(self) -> None:
        source = self.directory("code", {"main.py": "pass\n", "large.txt": "a" * 100})
        with patch("pko.scenario_map.sources.MAX_FILE_BYTES", 20):
            workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/main.py"])
        self.assertIn("индивидуального лимита", " ".join(workspace.notes))
        self.assertEqual([(item["source_id"], item["kind"], item["count"])
                          for item in workspace.omissions], [("source-1", "oversized_file", 1)])

    def test_parse_failures_are_structured_omissions_but_code_remains_readable(self) -> None:
        source = self.directory("code", {"main.py": "def broken(\n"})
        workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.read("source-1/main.py"), "def broken(\n")
        self.assertEqual(len(workspace.omissions), 1)
        self.assertEqual(workspace.omissions[0]["kind"], "parse_failure")
        self.assertEqual(workspace.omissions[0]["path"], "source-1/main.py")

    def test_unreadable_files_are_structured_omissions(self) -> None:
        source = self.directory("code", {"good.py": "pass\n", "private.py": "pass\n"})
        original_open = os.open

        def unreadable(path, *args, **kwargs):
            if Path(path).name == "private.py":
                raise PermissionError("simulated unreadable code")
            return original_open(path, *args, **kwargs)

        with patch("pko.scenario_map.sources.os.open", side_effect=unreadable):
            workspace = build_workspace([source], self.root / "snapshot")
        self.assertEqual(workspace.tree.files, ["source-1/good.py"])
        self.assertEqual(workspace.omissions[0]["kind"], "unreadable_file")

    def test_entry_limit_bounds_skipped_elements(self) -> None:
        source = self.archive("code.zip", {"first.key": "secret", "second.key": "secret"})
        with patch("pko.scenario_map.sources.MAX_INPUT_ENTRIES", 1):
            with self.assertRaises(PkoError):
                build_workspace([source], self.root / "snapshot")

    def test_existing_destination_is_never_overwritten(self) -> None:
        source = self.directory("code", {"main.py": "pass\n"})
        dest = self.directory("snapshot", {"keep.txt": "existing"})
        with self.assertRaises(PkoError):
            build_workspace([source], dest)
        self.assertEqual((dest / "keep.txt").read_text(), "existing")

    def test_invalid_inputs_have_domain_errors(self) -> None:
        for index, paths in enumerate([[], [self.root / "missing"],
                                        [self.archive("broken.zip", {})]]):
            if index == 2:
                paths[0].write_bytes(b"not a ZIP")
            with self.subTest(index=index), self.assertRaises(PkoError):
                build_workspace(paths, self.root / f"snapshot-{index}")


if __name__ == "__main__":
    unittest.main()
