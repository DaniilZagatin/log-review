#!/usr/bin/env python3
"""Build a portable patch against the private 0.1.0 release ZIP.

  python3 scripts/release_patch.py build --baseline PATH --out PATH
  python3 scripts/release_patch.py verify --patch PATH

Only own-project files accepted by allowed_path() are compared and shipped; config, certs, process
inputs, runs and other local files are never read or shipped. The code between the applier markers
is copied verbatim into the patch as apply_patch.py, so build and apply share one path policy.
"""

import difflib
import io
import zipfile

# >>> standalone applier
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

PROTOCOL = "process-observability-patch/1"
# Own project files only; docs are an explicit selection.
FIXED = frozenset({"run.py", "pyproject.toml", "README.md", "AGENT-START.md", "docs/PILOT.md",
                   "docs/GRAPH-REVIEW-PATCH.md", "docs/STATUS.md", "docs/CODEX-NAVIGATION-GUIDE.md",
                   "docs/METRIC-SOURCE-CHECK.md", "guidance/process-observability/references/metrics.md",
                   "guidance/process-observability/references/coverage.md",
                   "guidance/process-observability/references/metric-formulas.md", "docs/METRIC-CATALOG.md",
                   "guidance/process-observability/sources.json",
                   "docs/SKILLS-AND-TOOLS.md", "docs/REQUEST-AUDIT.md", "plans/implementation-plan.md",
                   "docs/ONBOARDING-EXPORT.md", "docs/ACCEPTANCE-MATRIX.md",
                   "plans/code-first-acceptance.md",
                   "plans/pre-pilot-review.md",
                   "plans/code-first-completion.md",
                   "plans/atomic-operations.md", "docs/ATOMIC-ACTIONS.md", "docs/RUN14-REVIEW.md",
                   "docs/BUSINESS-CONTEXT.md", "docs/DIRECT-ATOMIC.md",
                   "plans/business-token-context.md", "plans/direct-atomic-context.md",
                   "examples/synthetic/model-responses.json"})
PATTERN = re.compile(r"src/process_observability/[A-Za-z0-9_]+\.py|tests/test_[A-Za-z0-9_]+\.py"
                     r"|scripts/[A-Za-z0-9_-]+\.py|plans/graph-review-patch[A-Za-z0-9_-]*\.md")
HEX = re.compile(r"[0-9a-f]{64}")
MANIFEST_KEYS = {"protocol", "id", "baseline_sha256", "files"}
ENTRY_KEYS = {"path", "pre_sha256", "post_sha256"}


class PatchError(Exception):
    """A refusal. Raised during preflight, it means nothing was written."""


def allowed_path(path):
    """One allowlist for build and apply; absolute, traversal, backslash and private paths never match."""
    return isinstance(path, str) and (path in FIXED or PATTERN.fullmatch(path) is not None)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def is_sha256(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def load_json(data):
    def unique(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise PatchError("duplicate JSON key")
        return dict(pairs)
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except ValueError as error:
        raise PatchError(f"malformed JSON: {error}") from None


def patch_id(baseline_sha256, entries):
    return sha256(json.dumps([baseline_sha256, entries], sort_keys=True).encode())[:16]


def load_manifest(data):
    manifest = load_json(data)
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise PatchError("manifest fields must be exactly: " + ", ".join(sorted(MANIFEST_KEYS)))
    if manifest["protocol"] != PROTOCOL or not is_sha256(manifest["baseline_sha256"]):
        raise PatchError("unsupported protocol or malformed baseline_sha256")
    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise PatchError("manifest lists no files")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise PatchError("file entry fields must be exactly: " + ", ".join(sorted(ENTRY_KEYS)))
        path, pre, post = entry["path"], entry["pre_sha256"], entry["post_sha256"]
        if not allowed_path(path):
            raise PatchError(f"path outside own-project policy: {path!r}")
        if path.casefold() in seen:
            raise PatchError(f"duplicate path: {path}")
        seen.add(path.casefold())
        if not is_sha256(post) or not (pre is None or is_sha256(pre)) or pre == post:
            raise PatchError(f"malformed hashes: {path}")
    if manifest["id"] != patch_id(manifest["baseline_sha256"], entries):
        raise PatchError("patch id does not match manifest content")
    return manifest


def kind(path):
    """Classify without following links; Windows directory junctions count as links."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == 0xA0000003:  # IO_REPARSE_TAG_MOUNT_POINT
        return "link"
    return "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"


def load_payloads(patch_dir, manifest):
    """Payload tree must be exactly the manifest paths, regular files only, with matching post hashes."""
    files = patch_dir / "files"
    if kind(files) != "dir":
        raise PatchError("patch files/ directory is missing or a link")
    found = set()
    for current, dirs, names in os.walk(files):
        for name in dirs + names:
            item = Path(current, name)
            item_kind = kind(item)
            if item_kind not in ("dir", "file"):
                raise PatchError(f"unsupported patch entry: {item.relative_to(files).as_posix()}")
            if item_kind == "file":
                found.add(item.relative_to(files).as_posix())
    expected = {entry["path"] for entry in manifest["files"]}
    if found != expected:
        raise PatchError(f"payload files differ from manifest: {sorted(found ^ expected)}")
    payloads = {}
    for entry in manifest["files"]:
        data = (files / entry["path"]).read_bytes()
        if sha256(data) != entry["post_sha256"]:
            raise PatchError(f"corrupt payload: {entry['path']}")
        payloads[entry["path"]] = data
    return payloads


def check_target(target, entry):
    path = entry["path"]
    current = target
    for part in path.split("/")[:-1]:
        current = current / part
        state = kind(current)
        if state is None:
            break
        if state != "dir":
            raise PatchError(f"{path}: parent {current.relative_to(target).as_posix()} is a link or not a directory")
    state = kind(target / path)
    if entry["pre_sha256"] is None:
        if state is not None:
            raise PatchError(f"{path}: new file already exists")
        return
    if state != "file":
        raise PatchError(f"{path}: expected regular file, found {state or 'nothing'}")
    actual = sha256((target / path).read_bytes())
    if actual != entry["pre_sha256"]:
        found = "already patched" if actual == entry["post_sha256"] else "locally modified"
        raise PatchError(f"{path}: preimage mismatch ({found}); refusing to overwrite")


def preflight(patch_dir, target):
    """Validate the manifest, every payload and every target path before any write."""
    if kind(patch_dir / "PATCH-MANIFEST.json") != "file":
        raise PatchError("PATCH-MANIFEST.json is missing")
    manifest = load_manifest((patch_dir / "PATCH-MANIFEST.json").read_bytes())
    payloads = load_payloads(patch_dir, manifest)
    if kind(target) != "dir" or kind(target / "RELEASE-SHA256.json") != "file":
        raise PatchError("target must be a real extracted release directory containing RELEASE-SHA256.json")
    problems = []
    for entry in manifest["files"]:
        try:
            check_target(target, entry)
        except (OSError, PatchError) as error:
            problems.append(str(error))
    if problems:
        raise PatchError("\n".join(problems))
    return manifest, payloads


def write_file(target, entry, data):
    check_target(target, entry)  # again, immediately before this write
    location = target / entry["path"]
    current = target
    for part in entry["path"].split("/")[:-1]:
        current = current / part
        if kind(current) is None:
            current.mkdir()
    mode = stat.S_IMODE(location.lstat().st_mode) if entry["pre_sha256"] else 0o644
    handle, temp = tempfile.mkstemp(prefix=f".{location.name}.", suffix=".tmp", dir=location.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        if entry["pre_sha256"] is None and kind(location) is not None:
            raise PatchError(f"{entry['path']}: new file appeared during apply")
        os.replace(temp, location)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def apply_patch(patch_dir, target, dry_run=False):
    """Preflight everything, then replace each file atomically. Returns the plan."""
    patch_dir, target = Path(patch_dir), Path(target)
    manifest, payloads = preflight(patch_dir, target)
    plan = [{"path": entry["path"], "action": "add" if entry["pre_sha256"] is None else "modify"}
            for entry in manifest["files"]]
    if not dry_run:
        written = []
        for entry in manifest["files"]:
            try:
                write_file(target, entry, payloads[entry["path"]])
            except (OSError, PatchError) as error:
                raise PatchError(f"{error}\nstopped; already written: {', '.join(written) or 'nothing'}") from None
            written.append(entry["path"])
    return {"patch": manifest["id"], "dry_run": dry_run, "files": plan}


def apply_main(patch_dir, argv=None):
    parser = argparse.ArgumentParser(description="Preflight every path and hash, then apply this patch.")
    parser.add_argument("--target", required=True, help="extracted process-observability-mvp release directory")
    parser.add_argument("--dry-run", action="store_true", help="check everything and write nothing")
    args = parser.parse_args(argv)
    try:
        result = apply_patch(patch_dir, Path(args.target), args.dry_run)
    except (OSError, PatchError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
# <<< standalone applier

BASELINE_SHA256 = "deb153d1c80b5d390094005912210af13336701647e77440b0d37810f7e388cc"
BASELINE_PREFIX = "process-observability-mvp/"
PATCH_PREFIX = "process-observability-mvp-patch/"
SUPPORT = ("PATCH-MANIFEST.json", "APPLY.md", "changes.diff", "apply_patch.py")
FOLDERS = ("", "docs", "src/process_observability", "tests", "scripts", "plans",
           "guidance/process-observability", "guidance/process-observability/references", "examples/synthetic")
ROOT = Path(__file__).resolve().parents[1]
APPLIER_BEGIN, APPLIER_END = "# >>> standalone applier\n", "# <<< standalone applier\n"


def applier_source():
    lines = Path(__file__).read_text(encoding="utf-8").splitlines(keepends=True)
    body = "".join(lines[lines.index(APPLIER_BEGIN) + 1:lines.index(APPLIER_END)]).rstrip()
    return ('#!/usr/bin/env python3\n"""Standalone applier for a process-observability release patch. '
            'Generated by scripts/release_patch.py; standard library only. See APPLY.md."""\n\n'
            + body + '\n\n\nif __name__ == "__main__":\n    sys.exit(apply_main(Path(__file__).resolve().parent))\n')


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_member(relative):
    return not any(char in relative for char in "\\:\0") and all(
        part not in ("", ".", "..") for part in relative.split("/"))


def baseline_files(path):
    """Own-project files of the exact baseline, each verified against its RELEASE-SHA256.json."""
    if file_sha256(path) != BASELINE_SHA256:
        raise PatchError("baseline archive SHA256 is not the expected 0.1.0 release")
    with zipfile.ZipFile(path) as archive:
        members = {}
        for info in archive.infolist():
            relative = info.filename[len(BASELINE_PREFIX):]
            name = relative[:-1] if info.is_dir() else relative
            if (not info.filename.startswith(BASELINE_PREFIX) or relative in members
                    or stat.S_ISLNK(info.external_attr >> 16) or (name and not safe_member(name))):
                raise PatchError(f"unexpected baseline entry: {info.filename!r}")
            if not info.is_dir():
                members[relative] = info
        if "RELEASE-SHA256.json" not in members:
            raise PatchError("baseline has no RELEASE-SHA256.json")
        hashes = load_json(archive.read(members["RELEASE-SHA256.json"]))
        if not isinstance(hashes, dict) or not all(is_sha256(value) for value in hashes.values()):
            raise PatchError("malformed RELEASE-SHA256.json")
        result = {}
        for relative in sorted(name for name in set(members) | set(hashes) if allowed_path(name)):
            if relative not in members or relative not in hashes:
                raise PatchError(f"baseline archive and RELEASE-SHA256.json disagree: {relative}")
            data = archive.read(members[relative])
            if sha256(data) != hashes[relative]:
                raise PatchError(f"baseline member hash mismatch: {relative}")
            result[relative] = data
    return result


def project_files(root):
    """Current own-project files; other names are listed but never opened."""
    result = {}
    for folder in FOLDERS:
        directory = root / folder
        state = kind(directory)
        if state is None:
            continue
        if state != "dir":
            raise PatchError(f"project folder is a link or not a directory: {folder or '.'}")
        for item in sorted(directory.iterdir()):
            relative = item.relative_to(root).as_posix()
            if allowed_path(relative):
                if kind(item) != "file":
                    raise PatchError(f"own-project path is not a regular file: {relative}")
                result[relative] = item.read_bytes()
    return result


def diff_lines(data):
    try:
        parts = data.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        return None
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1] + "\n\\ No newline at end of file\n")
    return lines


def changes_diff(before, after, entries):
    chunks = []
    for entry in entries:
        path = entry["path"]
        old, new = diff_lines(before.get(path, b"")), diff_lines(after[path])
        if old is None or new is None:
            chunks.append(f"Binary files a/{path} and b/{path} differ\n")
        else:
            chunks.extend(difflib.unified_diff(old, new, f"a/{path}" if path in before else "/dev/null", f"b/{path}"))
    return "".join(chunks).encode()


def apply_md(manifest):
    listing = "\n".join(f"- `{entry['path']}` — {'новый' if entry['pre_sha256'] is None else 'изменён'}"
                        for entry in manifest["files"])
    return f"""# Патч {manifest['id']} для process-observability-mvp 0.1.0

Базовый архив: SHA-256 `{manifest['baseline_sha256']}`. Хэши до и после — в `PATCH-MANIFEST.json`, изменения для просмотра — в `changes.diff`.

Это накопленный патч для чистой 0.1.0. Если уже применяли другой патч (в том числе R01), используйте новую распаковку исходного ZIP. Скрипт не перезаписывает изменённые файлы.

{listing}

Распакуйте этот ZIP в отдельный каталог (не внутрь поставки) и перейдите в `{PATCH_PREFIX.rstrip('/')}`. Сначала проверка без записи, затем применение:

```bash
python3 apply_patch.py --target /path/to/process-observability-mvp --dry-run
python3 apply_patch.py --target /path/to/process-observability-mvp
```

На Windows замените `python3` на `py -3`.

До любой записи проверяются все пути и SHA-256: изменяемые файлы должны совпадать с базовой версией, новых файлов ещё не должно быть; ссылки, специальные файлы и пути вне списка файлов проекта отклоняются. При любом расхождении ничего не записывается, повторное применение отклоняется. Каждый файл заменяется атомарно через временный файл в том же каталоге. `config/`, `certs/`, `inputs/`, `runs/` и другие ваши файлы не читаются и не изменяются; `RELEASE-SHA256.json` по-прежнему описывает базовую поставку. Сеть и shell не используются.
"""


def verify_patch(source):
    """Check a patch ZIP: exact entries, no duplicates or links, manifest, payload hashes and applier source."""
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or PATCH_PREFIX + "PATCH-MANIFEST.json" not in names:
            raise PatchError("duplicate patch entries or missing PATCH-MANIFEST.json")
        manifest = load_manifest(archive.read(PATCH_PREFIX + "PATCH-MANIFEST.json"))
        expected = {PATCH_PREFIX + name for name in SUPPORT}
        expected |= {PATCH_PREFIX + "files/" + entry["path"] for entry in manifest["files"]}
        if set(names) != expected or any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
            raise PatchError(f"unexpected patch entries: {sorted(set(names) ^ expected)}")
        for entry in manifest["files"]:
            if sha256(archive.read(PATCH_PREFIX + "files/" + entry["path"])) != entry["post_sha256"]:
                raise PatchError(f"corrupt payload: {entry['path']}")
        if archive.read(PATCH_PREFIX + "apply_patch.py") != applier_source().encode():
            raise PatchError("apply_patch.py differs from the generated applier")
    return manifest


def build(baseline, out, root=ROOT):
    """Write a patch ZIP of changed/new own-project files. Never overwrites an existing path."""
    baseline, out, root = Path(baseline), Path(out), Path(root)
    if os.path.lexists(out):
        raise PatchError(f"output already exists: {out}")
    before, after = baseline_files(baseline), project_files(root)
    entries = [{"path": path, "pre_sha256": sha256(before[path]) if path in before else None, "post_sha256": sha256(data)}
               for path, data in sorted(after.items()) if before.get(path) != data]
    if not entries:
        raise PatchError("no changed or new own-project files")
    manifest = {"protocol": PROTOCOL, "id": patch_id(BASELINE_SHA256, entries), "baseline_sha256": BASELINE_SHA256,
                "files": entries}
    contents = {"PATCH-MANIFEST.json": (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
                "APPLY.md": apply_md(manifest).encode(), "changes.diff": changes_diff(before, after, entries),
                "apply_patch.py": applier_source().encode()}
    contents.update({"files/" + entry["path"]: after[entry["path"]] for entry in entries})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(PATCH_PREFIX + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    data = buffer.getvalue()
    verify_patch(io.BytesIO(data))
    out.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), "wb") as stream:
        stream.write(data)
    return {"archive": str(out), "id": manifest["id"], "files": [entry["path"] for entry in entries],
            "bytes": len(data), "sha256": sha256(data), "not_deleted": sorted(set(before) - set(after))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="build a patch ZIP from the exact baseline and this workspace")
    build_parser.add_argument("--baseline", required=True)
    build_parser.add_argument("--out", required=True)
    verify_parser = commands.add_parser("verify", help="check a patch ZIP before extraction")
    verify_parser.add_argument("--patch", required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.baseline, args.out) if args.command == "build" else verify_patch(args.patch)
    except (OSError, zipfile.BadZipFile, PatchError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
