"""Offline tests for scripts/release_patch.py and the standalone applier it generates."""

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_patch.py"
SPEC = importlib.util.spec_from_file_location("release_patch", SCRIPT)
release_patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_patch)

BASE = {
    "run.py": b"print('run')\n",
    "pyproject.toml": b"[project]\nname = 'demo'\n",
    "README.md": b"# Demo\n",
    "scripts/package_mvp.py": b"# packager\n",
    "src/process_observability/__init__.py": b"",
    "src/process_observability/core.py": b"VALUE = 1\n",
    "tests/test_core.py": b"import unittest\n",
    "config/model.corporate.json": b'{"role": "baseline"}\n',
    "certs/deepseek/client.key.txt": b"baseline key\n",
    "inputs/processes/p3283/description.md": b"baseline process\n",
}
# Workspace state; only changed/new own-project files may reach the patch.
LOCAL = {
    "README.md": b"# Demo\n\nPatched.\n",
    "src/process_observability/core.py": b"VALUE = 2\n",
    "src/process_observability/graph.py": b"GRAPH = True",
    "tests/test_graph.py": b"import unittest\n",
    "plans/graph-review-patch.md": b"# Graph review patch\n",
    "config/model.corporate.json": b'{"role": "CONFIG-MARKER"}\n',
    "certs/deepseek/client.key.txt": b"FAKE-KEY-MARKER\n",
    "inputs/processes/p3283/description.md": b"PROCESS-INPUT-MARKER\n",
    ".env": b"TOKEN=ENV-MARKER\n",
    "docs/private-notes.md": b"STATUS-MARKER\n",
    "runs/demo/trace.jsonl": b"RUN-MARKER\n",
    "scripts/.hidden.py": b"HIDDEN-MARKER\n",
}
MARKERS = (b"CONFIG-MARKER", b"FAKE-KEY-MARKER", b"PROCESS-INPUT-MARKER", b"ENV-MARKER", b"STATUS-MARKER",
           b"RUN-MARKER", b"HIDDEN-MARKER")
PATCHED = {"README.md", "src/process_observability/core.py", "src/process_observability/graph.py",
           "tests/test_graph.py", "plans/graph-review-patch.md"}


def write_tree(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def write_zip(path, items):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # some cases add duplicate names on purpose
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in items:
                archive.writestr(name, data)


def snapshot(root):
    result = {}
    for current, dirs, names in os.walk(root):
        for name in dirs + names:
            path = Path(current, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            else:
                result[relative] = path.read_bytes() if path.is_file() else "dir"
    return result


class ReleasePatchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in BASE.items()}
        self.release = {**BASE, "RELEASE-SHA256.json": json.dumps(hashes, indent=2).encode()}
        self.baseline = self.tmp / "baseline.zip"
        write_zip(self.baseline, self.prefixed(self.release))
        self.project = self.tmp / "project"
        write_tree(self.project, {**BASE, **LOCAL})

    @staticmethod
    def prefixed(files):
        return [(release_patch.BASELINE_PREFIX + name, data) for name, data in files.items()]

    def build(self, baseline=None, name="patch.zip"):
        baseline = baseline or self.baseline
        test_sha = hashlib.sha256(baseline.read_bytes()).hexdigest()
        with mock.patch.object(release_patch, "BASELINE_SHA256", test_sha):
            release_patch.build(baseline, self.tmp / "out" / name, root=self.project)
        return self.tmp / "out" / name

    def extract(self, archive):
        destination = self.tmp / "extracted"
        with zipfile.ZipFile(archive) as source:
            source.extractall(destination)
        return destination / release_patch.PATCH_PREFIX.rstrip("/")

    def make_target(self, name="release"):
        target = self.tmp / name
        write_tree(target, {**self.release, "my-notes.txt": b"custom\n", "runs/pilot-01/trace.jsonl": b"{}\n"})
        return target

    def prepared(self):
        return self.extract(self.build()), self.make_target()

    def run_applier(self, patch_dir, target, *flags):
        command = [sys.executable, "-I", str(patch_dir / "apply_patch.py"), "--target", str(target), *flags]
        return subprocess.run(command, cwd=self.tmp, capture_output=True, text=True)

    def assert_refused(self, patch_dir, target, *messages):
        before = snapshot(target)
        for flags in (("--dry-run",), ()):
            result = self.run_applier(patch_dir, target, *flags)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            for message in messages:
                self.assertIn(message, result.stderr)
        self.assertEqual(snapshot(target), before)

    def test_build_ships_only_changed_own_project_files(self):
        archive = self.build()
        manifest = release_patch.verify_patch(archive)
        entries = {entry["path"]: entry for entry in manifest["files"]}
        self.assertEqual(set(entries), PATCHED)
        self.assertEqual(manifest["baseline_sha256"], hashlib.sha256(self.baseline.read_bytes()).hexdigest())
        self.assertEqual(entries["README.md"]["pre_sha256"], hashlib.sha256(BASE["README.md"]).hexdigest())
        self.assertIsNone(entries["src/process_observability/graph.py"]["pre_sha256"])
        prefix = release_patch.PATCH_PREFIX
        with zipfile.ZipFile(archive) as source:
            contents = {info.filename: source.read(info) for info in source.infolist()}
        expected = {prefix + name for name in release_patch.SUPPORT} | {prefix + "files/" + path for path in PATCHED}
        self.assertEqual(set(contents), expected)
        blob = b"".join(contents.values())
        for marker in MARKERS:
            self.assertNotIn(marker, blob)
        diff = contents[prefix + "changes.diff"].decode()
        self.assertIn("-VALUE = 1\n+VALUE = 2\n", diff)
        self.assertIn("--- /dev/null\n+++ b/src/process_observability/graph.py\n", diff)
        self.assertIn("--dry-run", contents[prefix + "APPLY.md"].decode())

    def test_build_refuses_unexpected_baseline_and_existing_output(self):
        out = self.tmp / "out.zip"
        with self.assertRaisesRegex(release_patch.PatchError, "not the expected 0.1.0 release"):
            release_patch.build(self.baseline, out, root=self.project)  # real constant, synthetic archive
        self.assertFalse(out.exists())
        before = self.baseline.read_bytes()
        with self.assertRaisesRegex(release_patch.PatchError, "already exists"):
            release_patch.build(self.baseline, self.baseline, root=self.project)
        self.assertEqual(self.baseline.read_bytes(), before)
        archive = self.build()
        built = archive.read_bytes()
        with self.assertRaisesRegex(release_patch.PatchError, "already exists"):
            self.build()
        self.assertEqual(archive.read_bytes(), built)

    def test_build_verifies_baseline_members(self):
        prefix = release_patch.BASELINE_PREFIX
        good = self.prefixed(self.release)
        cases = {
            "member-hash": self.prefixed({**self.release, "README.md": b"tampered\n"}),
            "unlisted-member": good + [(prefix + "scripts/extra.py", b"x\n")],
            "duplicate": good + [(prefix + "README.md", BASE["README.md"])],
            "outside-prefix": good + [("other/README.md", b"x\n")],
            "traversal": good + [(prefix + "../evil.py", b"x\n")],
            "no-release-hashes": self.prefixed(BASE),
        }
        for label, items in cases.items():
            with self.subTest(label):
                baseline = self.tmp / f"{label}.zip"
                write_zip(baseline, items)
                with self.assertRaises(release_patch.PatchError):
                    self.build(baseline, name=f"{label}.zip")
                self.assertFalse((self.tmp / "out" / f"{label}.zip").exists())

    def test_dry_run_writes_nothing_then_apply_preserves_unrelated_files(self):
        patch_dir, target = self.prepared()
        before = snapshot(target)
        dry = self.run_applier(patch_dir, target, "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(snapshot(target), before)
        plan = {item["path"]: item["action"] for item in json.loads(dry.stdout)["files"]}
        self.assertEqual(plan["README.md"], "modify")
        self.assertEqual(plan["plans/graph-review-patch.md"], "add")
        applied = self.run_applier(patch_dir, target)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        after = snapshot(target)
        self.assertLessEqual(set(before), set(after))
        # Config, certs, process inputs, runs, custom files and RELEASE-SHA256.json are byte-identical; no temp files.
        self.assertEqual({path for path in after if before.get(path) != after[path]}, PATCHED | {"plans"})
        for path in PATCHED:
            self.assertEqual(after[path], LOCAL[path])

    def test_second_apply_refuses_without_writing(self):
        patch_dir, target = self.prepared()
        self.assertEqual(self.run_applier(patch_dir, target).returncode, 0)
        self.assert_refused(patch_dir, target, "README.md: preimage mismatch (already patched)",
                            "src/process_observability/graph.py: new file already exists")

    def test_preimage_mismatch_blocks_every_write(self):
        patch_dir, target = self.prepared()
        (target / "src/process_observability/core.py").write_bytes(b"VALUE = 'custom'\n")
        # README.md sorts first and is valid; preflight must still stop it from being written.
        self.assert_refused(patch_dir, target, "core.py: preimage mismatch (locally modified)")

    def test_existing_new_file_or_non_regular_path_is_refused(self):
        patch_dir = self.extract(self.build())
        collision = self.make_target("collision")
        write_tree(collision, {"src/process_observability/graph.py": b"# mine\n"})
        self.assert_refused(patch_dir, collision, "graph.py: new file already exists")
        with self.assertRaisesRegex(release_patch.PatchError, "new file already exists"):
            release_patch.apply_patch(patch_dir, collision)
        directory = self.make_target("directory")
        (directory / "README.md").unlink()
        (directory / "README.md").mkdir()
        self.assert_refused(patch_dir, directory, "README.md: expected regular file, found dir")

    def test_symlinked_target_paths_are_refused(self):
        patch_dir = self.extract(self.build())
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "README.md").write_bytes(BASE["README.md"])
        file_link = self.make_target("file-link")
        (file_link / "README.md").unlink()
        try:
            (file_link / "README.md").symlink_to(outside / "README.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        dir_link = self.make_target("dir-link")
        (dir_link / "plans").symlink_to(outside, target_is_directory=True)
        root_link = self.tmp / "root-link"
        root_link.symlink_to(self.make_target(), target_is_directory=True)
        self.assert_refused(patch_dir, file_link, "README.md: expected regular file, found link")
        self.assert_refused(patch_dir, dir_link, "parent plans is a link or not a directory")
        self.assert_refused(patch_dir, root_link, "target must be a real extracted release directory")
        self.assertEqual(snapshot(outside), {"README.md": BASE["README.md"]})

    def test_path_policy_is_an_explicit_allowlist(self):
        for path in ("docs/ATOMIC-ACTIONS.md", "plans/atomic-operations.md", "docs/RUN14-REVIEW.md",
                     "docs/BUSINESS-CONTEXT.md", "docs/DIRECT-ATOMIC.md",
                     "plans/business-token-context.md", "plans/direct-atomic-context.md"):
            self.assertTrue(release_patch.allowed_path(path), path)
        for path in PATCHED | {"run.py", "AGENT-START.md", "docs/PILOT.md", "docs/STATUS.md", "docs/METRIC-SOURCE-CHECK.md", "guidance/process-observability/references/metrics.md", "examples/synthetic/model-responses.json", "scripts/release_patch.py"}:
            self.assertTrue(release_patch.allowed_path(path), path)
        for path in ("/etc/passwd", "../run.py", "./run.py", "scripts/../run.py", "scripts\\evil.py", "C:/run.py",
                     "run.py\n", "", None, "config/model.corporate.json", "certs/deepseek/client.key.txt",
                     "inputs/processes/p3283/source.json", ".env", ".local/x.py", "runs/demo/x.py", "dist/x.py",
                     "materials/x.md", "references/x.md", "docs/private-notes.md", "scripts/.hidden.py",
                     "src/process_observability/sub/x.py", "tests/fixtures/synthetic/app.py"):
            self.assertFalse(release_patch.allowed_path(path), path)

    def test_unsafe_or_unsupported_manifest_is_refused(self):
        patch_dir, target = self.prepared()
        manifest_path = patch_dir / "PATCH-MANIFEST.json"
        original = manifest_path.read_text(encoding="utf-8")

        def variant(change):
            manifest = json.loads(original)
            change(manifest)
            manifest["id"] = release_patch.patch_id(manifest["baseline_sha256"], manifest["files"])
            return json.dumps(manifest).encode()

        def set_path(path):
            return variant(lambda manifest: manifest["files"][0].update(path=path))

        wrong_id = json.loads(original)
        wrong_id["id"] = "0" * 16
        cases = [
            ("path outside own-project policy", set_path("../outside.py")),
            ("path outside own-project policy", set_path("/tmp/outside.py")),
            ("path outside own-project policy", set_path("scripts\\outside.py")),
            ("path outside own-project policy", set_path("certs/deepseek/client.key.txt")),
            ("manifest fields must be exactly", variant(lambda manifest: manifest.update(signature="x"))),
            ("file entry fields must be exactly", variant(lambda manifest: manifest["files"][0].update(mode=493))),
            ("duplicate path", variant(lambda manifest: manifest["files"].append(dict(manifest["files"][0])))),
            ("unsupported protocol", variant(lambda manifest: manifest.update(protocol="process-observability-patch/2"))),
            ("malformed hashes", variant(lambda manifest: manifest["files"][0].update(post_sha256="0" * 63))),
            ("duplicate JSON key", original.replace('"protocol": ', '"protocol": "other", "protocol": ', 1).encode()),
            ("patch id does not match", json.dumps(wrong_id).encode()),
        ]
        for message, data in cases:
            with self.subTest(message), self.assertRaisesRegex(release_patch.PatchError, message):
                release_patch.load_manifest(data)
        manifest_path.write_bytes(cases[0][1])
        self.assert_refused(patch_dir, target, "path outside own-project policy")

    def test_corrupt_or_extra_payload_is_refused(self):
        patch_dir, target = self.prepared()
        payload = patch_dir / "files" / "README.md"
        payload.write_bytes(b"# Demo\n\nTampered.\n")
        self.assert_refused(patch_dir, target, "corrupt payload: README.md")
        payload.write_bytes(LOCAL["README.md"])
        write_tree(patch_dir / "files", {"scripts/extra.py": b"x\n"})
        self.assert_refused(patch_dir, target, "payload files differ from manifest: ['scripts/extra.py']")

    def test_verify_rejects_malformed_patch_archives(self):
        archive = self.build()
        with zipfile.ZipFile(archive) as source:
            items = [(info.filename, source.read(info)) for info in source.infolist()]
        prefix = release_patch.PATCH_PREFIX
        readme, applier = prefix + "files/README.md", prefix + "apply_patch.py"
        cases = {
            "duplicate": items + [(readme, LOCAL["README.md"])],
            "extra": items + [(prefix + "files/config/model.corporate.json", b"{}\n")],
            "traversal": items + [(prefix + "../evil.py", b"x\n")],
            "absolute": items + [("/tmp/evil.py", b"x\n")],
            "corrupt-payload": [(name, b"tampered\n" if name == readme else data) for name, data in items],
            "modified-applier": [(name, data + b"# changed\n" if name == applier else data) for name, data in items],
        }
        for label, entries in cases.items():
            with self.subTest(label):
                bad = self.tmp / f"{label}.zip"
                write_zip(bad, entries)
                with self.assertRaises(release_patch.PatchError):
                    release_patch.verify_patch(bad)

    def test_generated_applier_is_standalone(self):
        tree = ast.parse(release_patch.applier_source())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"argparse", "hashlib", "json", "os", "pathlib", "re", "stat", "sys", "tempfile"})
        defined = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        self.assertTrue({"allowed_path", "preflight", "apply_patch", "apply_main"} <= defined)
        self.assertFalse({"build", "baseline_files", "project_files"} & defined)
        self.assertNotIn("release_files", SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
