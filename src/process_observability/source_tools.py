"""Bounded read-only frozen source snapshots. Never execute/import target code."""

from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat

from .contracts import EvidenceRef, SourceAnchor, SourceKind, ValidationState
from .redaction import redact_text


class SourceError(ValueError):
    pass


MAX_CITATION_LINES = 1000


_EXTENSIONS = {".py", ".java", ".kt", ".scala", ".js", ".jsx", ".ts", ".tsx", ".go", ".cs", ".html", ".htm",
               ".rs", ".php", ".rb", ".sql", ".xml", ".json", ".yaml", ".yml", ".toml", ".md", ".properties", ".conf", ".ini", ".cfg", ".config"}
_SKIP_DIRS = {".git", ".svn", ".hg", ".venv", "venv", "node_modules", "vendor", "dist", "build", "target",
              "__pycache__", ".pytest_cache", ".mypy_cache", ".local", "runs", ".idea", ".next",
              ".codex", ".claude", ".config", ".cache", ".npm", ".m2", ".vscode"}
_SKIP_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock"}


def blocked_path(path):
    parts = PurePosixPath(path).parts
    for part in parts:
        low = part.lower()
        if PurePosixPath(low).stem in {"secret", "secrets", "credentials", "credential"}:
            return True
        if low.startswith(".env") or low in {".ssh", ".aws", ".azure", "credentials", "credentials.json", "secrets", "secrets.json", "id_rsa", "id_ed25519"}:
            return True
        if low.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".key.txt", ".pem.txt", ".crt.txt")):
            return True
    return any(parts[i:i + 2] == ("materials", "private") for i in range(len(parts) - 1))


def covering_ranges(ranges, path, first, last):
    """Supplied ranges that share a line with path:first-last, or None unless together they cover every line.

    ranges are host-supplied dicts with path, start_line and end_line from one frozen snapshot. Only ranges
    of the same path count; touching (end_line + 1 == start_line) and overlapping ranges join, and a single
    line that no range contains rejects the whole citation. Line limits, redaction and hashes stay with
    RepoSnapshot.evidence. Every returned range is one that supplied text of a cited line, in input order.
    """
    if type(path) is not str or type(first) is not int or type(last) is not int or not 1 <= first <= last:
        return None
    shared = [item for item in ranges
              if item["path"] == path and item["start_line"] <= last and first <= item["end_line"]]
    covered = first  # the first line not yet shown to be supplied
    for item in sorted(shared, key=lambda item: item["start_line"]):
        if item["start_line"] > covered:
            return None
        covered = max(covered, item["end_line"] + 1)
    return shared if covered > last else None


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str
    text: str
    _raw_text: str = field(repr=False)


@dataclass
class RepoSnapshot:
    files: dict[str, SourceFile]
    scope: dict
    logging_calls: tuple[str, ...] = ()
    # Whole-scope snapshots only: every policy exclusion, eligible file and resource failure.
    # Kept out of scope/to_context so legacy replay fingerprints stay unchanged.
    inventory: dict | None = field(default=None, repr=False)

    @property
    def fingerprints(self):
        return {path: source.sha256 for path, source in sorted(self.files.items())}

    @property
    def snapshot_id(self):
        content = "\n".join(f"{path}:{digest}" for path, digest in self.fingerprints.items())
        return hashlib.sha256(content.encode()).hexdigest()

    def to_context(self, *, include_text=True):
        files = []
        for path, source in sorted(self.files.items()):
            item = {"path": path, "sha256": source.sha256}
            if include_text:
                item["text"] = "\n".join(f"{i}: {line}" for i, line in enumerate(source.text.splitlines(), 1))
            files.append(item)
        from .log_discovery import discover_logging
        inventory = discover_logging(self, call_names=self.logging_calls)
        return {"files": files, "candidates": inventory["sites"], "scope": dict(self.scope),
                "logging_inventory": inventory}

    def evidence(self, ref, *, max_lines=80):
        # A larger citation budget is a trusted host choice. It does not authorize reading new source.
        if type(max_lines) is not int or not 1 <= max_lines <= MAX_CITATION_LINES:
            raise SourceError("Invalid source citation budget")
        if type(ref) is not dict or set(ref) - {"path", "start_line", "end_line", "symbol"}:
            raise SourceError("Invalid source reference fields")
        path = ref.get("path")
        if type(path) is not str or path not in self.files or blocked_path(path):
            raise SourceError("Reference is outside the selected frozen snapshot")
        first, last = ref.get("start_line"), ref.get("end_line")
        source = self.files[path]
        raw_lines = source._raw_text.splitlines()
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(raw_lines) or last - first + 1 > max_lines:
            raise SourceError("Invalid or oversized source range")
        symbol = ref.get("symbol", "<module>")
        if type(symbol) is not str or not symbol.strip():
            raise SourceError("Invalid source symbol")
        raw_span = "".join(source._raw_text.splitlines(keepends=True)[first - 1:last])
        span_hash = hashlib.sha256(raw_span.encode()).hexdigest()
        # Redact the complete frozen file before slicing: a range inside a PEM
        # block must not reveal its body merely because it omits BEGIN/END.
        safe_span = "\n".join(source.text.splitlines()[first - 1:last])
        anchor = SourceAnchor(snapshot_id=self.snapshot_id, path=path, symbol=symbol, start_line=first, end_line=last)
        evidence_id = "src-" + hashlib.sha256(f"{self.snapshot_id}:{path}:{first}:{last}:{symbol}".encode()).hexdigest()[:20]
        return EvidenceRef(id=evidence_id, kind=SourceKind.CODE, snapshot_id=self.snapshot_id,
                           locator=f"{path}:{first}-{last}", source_anchor=anchor,
                           safe_quote=safe_span if safe_span.strip() else None, raw_span_hash=span_hash,
                           text_validation=ValidationState.VERIFIED)


class _Unsupported(SourceError):
    """An eligible source that failed a resource check; the reason goes into the inventory."""

    def __init__(self, reason, message, **detail):
        super().__init__(message)
        self.reason, self.detail = reason, detail


def _read_source(path, max_file_bytes):
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise _Unsupported("non_regular", "A selected source is non-regular or exceeds the explicit per-file byte limit")
        if max_file_bytes is not None and before.st_size > max_file_bytes:
            raise _Unsupported("exceeds_max_file_bytes", "A selected source is non-regular or exceeds the explicit per-file byte limit",
                               bytes=before.st_size, max_file_bytes=max_file_bytes)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_ino, opened.st_dev, opened.st_size, opened.st_mtime_ns) != (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
                raise _Unsupported("changed_during_snapshot", "A source changed while opening the snapshot")
            raw = handle.read() if max_file_bytes is None else handle.read(max_file_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise _Unsupported("read_error", "A selected source could not be read") from exc
    if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns) or len(raw) != opened.st_size:
        raise _Unsupported("changed_during_snapshot", "A source changed while reading the snapshot")
    if b"\x00" in raw:
        raise _Unsupported("binary", "Binary source input is unsupported", bytes=len(raw))
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Unsupported("not_utf8", "Source input is not UTF-8", bytes=len(raw)) from exc


def _relative(root, name):
    try:
        return Path(name).relative_to(root).as_posix()
    except (TypeError, ValueError):
        return None


def snapshot_repository(root, includes=None, max_files=60, max_chars=120000, allow_partial=False,
                        logging_calls=(), max_file_bytes=512000):
    """Bounded legacy snapshot by default; max_files=None and max_chars=None select the whole scope.

    A whole-scope snapshot carries a structured inventory. Policy exclusions (secrets,
    dependencies, symlinks, extensions, includes) never make it partial; eligible
    sources that fail a resource check do, and abort unless allow_partial is set.
    """
    if any(value is not None and (type(value) is not int or value < 1)
           for value in (max_files, max_chars, max_file_bytes)):
        raise SourceError("Source limits must be positive integers")
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise SourceError("Repository root must be a directory")
    patterns = includes or ["*"]
    if type(patterns) is not list or any(type(p) is not str or not p.strip() for p in patterns):
        raise SourceError("Includes must be nonblank glob patterns")
    whole = max_files is None and max_chars is None
    candidates, excluded, traversal_errors, exclusions, failures = [], 0, [], [], []
    def onerror(error):
        if not allow_partial:
            raise SourceError("Cannot traverse a selected repository directory") from error
        traversal_errors.append("An unreadable repository directory was omitted")
        failures.append({"path": _relative(root, error.filename), "kind": "directory",
                         "status": "resource_unsupported", "reason": "unreadable_directory"})
    for directory, directories, names in os.walk(root, followlinks=False, onerror=onerror):
        current = Path(directory)
        kept = []
        for name in sorted(directories):
            rel = (current / name).relative_to(root).as_posix()
            if name in _SKIP_DIRS or (current / name).is_symlink() or blocked_path(rel):
                excluded += 1
                exclusions.append({"path":rel,"kind":"directory","reason":"secret" if blocked_path(rel) else
                                   "symlink" if (current/name).is_symlink() else "dependency_or_generated"})
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = current / name
            rel = path.relative_to(root).as_posix()
            if (name in _SKIP_FILES or blocked_path(rel) or path.is_symlink()
                    or path.suffix.lower() not in _EXTENSIONS or not any(fnmatch(rel, p) for p in patterns)):
                excluded += 1
                exclusions.append({"path":rel,"kind":"file","reason":"secret" if blocked_path(rel) else
                                   "symlink" if path.is_symlink() else "dependency" if name in _SKIP_FILES else
                                   "unsupported_extension" if path.suffix.lower() not in _EXTENSIONS else "outside_include"})
                continue
            candidates.append((rel, path))
    selected, chars, limitations, eligible = {}, 0, list(traversal_errors), []
    for rel, path in sorted(candidates):
        try:
            if max_files is not None and len(selected) >= max_files:
                raise SourceError("Selected sources exceed the configured file limit")
            raw, text = _read_source(path, max_file_bytes)
            safe = redact_text(text)
            numbered_size = sum(len(f"{i}: {line}") + 1 for i, line in enumerate(safe.splitlines(), 1))
            if ((max_files is not None and len(selected) >= max_files)
                    or (max_chars is not None and chars + numbered_size > max_chars)):
                raise SourceError("Selected sources exceed the configured file/character limit")
            selected[rel] = SourceFile(rel, hashlib.sha256(raw).hexdigest(), safe, text)
            chars += numbered_size
            eligible.append({"path": rel, "kind": "file", "status": "eligible", "sha256": selected[rel].sha256,
                             "bytes": len(raw), "lines": len(safe.splitlines())})
        except (OSError, UnicodeError, SourceError) as exc:
            if whole:
                # Collected so one abort or partial snapshot names every failed input and reason.
                failures.append({"path": rel, "kind": "file", "status": "resource_unsupported",
                                 "reason": getattr(exc, "reason", "read_error"), **getattr(exc, "detail", {})})
                continue
            if not allow_partial:
                raise SourceError("Cannot snapshot selected inputs; narrow --include or explicitly use --allow-partial") from exc
            limitations.append(f"Selected source omitted: {rel}")
    if whole and failures:
        reasons = Counter(item["reason"] for item in failures)
        detail = ", ".join(f"{reason}: {count}" for reason, count in sorted(reasons.items()))
        if not allow_partial:
            named = ", ".join(f"{item['path']} ({item['reason']})" for item in failures[:5])
            error = SourceError(f"Cannot snapshot {len(failures)} eligible inputs ({detail}): {named}"
                                f"{', ...' if len(failures) > 5 else ''}; narrow --include or explicitly use --allow-partial")
            error.failures = failures
            raise error
        limitations = [f"{len(failures)} eligible inputs unsupported ({detail}); see the snapshot inventory"]
    if not selected:
        raise SourceError("No readable source files selected")
    scope = {"eligible_files": len(candidates), "selected_files": len(selected), "selected_chars": chars,
             "excluded_entries": excluded, "partial_input": bool(limitations), "limitations": limitations,
             "include_patterns": patterns, "selection_policy": "Allowlisted UTF-8 source files; secrets, dependencies and symlinks excluded"}
    # Keep the legacy bounded replay stable; the new whole-scope path exposes the full inventory.
    inventory = None
    if whole:
        scope.update(exclusions=exclusions, max_file_bytes=max_file_bytes)
        entries = [dict(item, status="policy_excluded") for item in exclusions] + eligible + failures
        inventory = {"tool": "snapshot_inventory/1", "include_patterns": patterns, "max_file_bytes": max_file_bytes,
                     "allow_partial": allow_partial, "partial_input": bool(failures),
                     "counts": {"eligible": len(candidates), "selected": len(selected),
                                "policy_excluded": len(exclusions), "resource_unsupported": len(failures)},
                     "entries": sorted(entries, key=lambda item: (item["path"] or "", item["kind"]))}
    return RepoSnapshot(selected, scope, tuple(logging_calls), inventory)
