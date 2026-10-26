"""Bounded, read-only source snapshots for one scenario spanning several systems.

Source identities describe uploaded materials, not automated systems. Every
accepted path retains its source namespace; similarly named repositories never
overlay each other. Digests identify the included, masked text snapshot, not a
Git revision or the original archive.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from pko.errors import PkoError
from pko.extractors.base import FileTree, is_vendor
from pko.extractors.runner import Extraction, extract_all
from pko.code_tools.tools import _is_blocked, _mask_secrets
from pko.code_tools.local_tree import (
    MAX_FILE_BYTES, MAX_WORKSPACE_BYTES, MAX_WORKSPACE_FILES, LocalTree,
)

MAX_INPUT_ENTRIES = 50_000
_EXTRA_EXCLUDED = {"vendor", ".svn", ".hg", ".ssh", ".aws"}
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
_MATERIAL_OMISSIONS = {
    "недоступные каталоги": "unreadable_directory",
    "недоступные файлы": "unreadable_file",
    "файлы больше индивидуального лимита": "oversized_file",
    "пути за пределами источника": "external_path",
    "нерегулярные файлы и внешние пути": "unavailable_path",
    "ссылки и junction": "unfollowed_link",
    "ссылки и нерегулярные элементы ZIP": "unfollowed_link",
}
_QUOTED_SECRET = re.compile(
    r'''(?i)(["'][\w.-]*(?:key|token|secret|password|passwd|credential|authorization)[\w.-]*["']\s*[:=]\s*)'''
    r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,}\]]+)'''
)
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/@\s\"']+:[^/@\s\"']+@", re.I)
_BEARER = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.I)


@dataclass
class SourceWorkspace:
    sources: list[dict]
    tree: FileTree
    extraction: Extraction
    notes: list[str]
    # Neutral notes, excluded secrets/vendor/binary data and output folders must
    # not make a map partial. Only potentially relevant unreadable material does.
    omissions: list[dict] = field(default_factory=list)


@dataclass
class _Budget:
    files: int = 0
    bytes: int = 0
    entries: int = 0

    def visit(self) -> None:
        self.entries += 1
        if self.entries > MAX_INPUT_ENTRIES:
            raise PkoError("В источниках слишком много элементов для безопасного обхода.",
                           hint=f"Ограничение: {MAX_INPUT_ENTRIES} элементов.")

    def reserve(self, size: int) -> None:
        # Count examined data too: a binary or undecodable file still costs I/O.
        if self.files + 1 > MAX_WORKSPACE_FILES:
            raise PkoError("Слишком много файлов в исходных материалах.",
                           hint=f"Ограничение: {MAX_WORKSPACE_FILES} файлов.")
        if self.bytes + size > MAX_WORKSPACE_BYTES:
            raise PkoError("Исходные материалы превышают общий лимит размера.",
                           hint=f"Ограничение: {MAX_WORKSPACE_BYTES} байт.")
        self.files += 1
        self.bytes += size


def _safe_path(name: str) -> str:
    """Reject traversal and Windows aliases before writing on any platform."""
    normalized = name.replace("\\", "/")
    parts = normalized.rstrip("/").split("/")
    if (not normalized or normalized.startswith("/") or
            any(not part or part in {".", ".."} or ":" in part or
                part.endswith((" ", ".")) or
                part.split(".", 1)[0].lower() in _WINDOWS_RESERVED or
                any(ord(ch) < 32 or ch in '<>"|?*' for ch in part)
                for part in parts)):
        raise PkoError("Источник содержит небезопасный путь.",
                       hint=f"Элемент: {name!r}")
    return "/".join(parts)


def _excluded(relative: str) -> bool:
    parts = relative.lower().split("/")
    return (is_vendor(relative.lower() + "/") or
            any(part in _EXTRA_EXCLUDED or _is_blocked(part) for part in parts))


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, "st_file_attributes", 0) &
                 getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _under_excluded(path: Path, excluded: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in excluded)


def _decode(data: bytes) -> str | None:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeError:
        return None
    if "\x00" in text:
        return None
    controls = sum(ord(char) < 32 and char not in "\r\n\t\f" for char in text)
    if controls > max(0, len(text) // 100):
        return None
    # The legacy masker handles assignment-style keys but not JSON/YAML quoted
    # field names. Extend it here without changing the frozen reviewer's tools.
    # Keep every original newline so extraction and evidence anchors still agree.
    def mask_line(line: str) -> str:
        line = _QUOTED_SECRET.sub(lambda match: match.group(1) + '"***"', line)
        line = _URL_CREDENTIALS.sub(r"\1***:***@", line)
        line = _BEARER.sub(r"\1***", line)
        return _mask_secrets(line)

    return "\n".join(mask_line(line) for line in text.split("\n"))


def _directory_files(root: Path, dest: Path, budget: _Budget,
                     skipped: Counter, excluded: tuple[Path, ...]) -> Iterator[tuple[str, bytes]]:
    def on_error(error: OSError) -> None:
        skipped["недоступные каталоги"] += 1

    for current, directories, filenames in os.walk(root, topdown=True,
                                                   followlinks=False, onerror=on_error):
        current_path = Path(current)
        retained = []
        for name in sorted(directories):
            budget.visit()
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            try:
                if _is_link(path):
                    skipped["ссылки и junction"] += 1
                elif path.resolve() == dest:
                    skipped["каталог выходного снимка"] += 1
                elif _under_excluded(path, excluded):
                    skipped["явно исключённые выходные каталоги"] += 1
                elif _excluded(relative):
                    skipped["служебные и защищённые каталоги"] += 1
                elif not path.resolve().is_relative_to(root):
                    skipped["пути за пределами источника"] += 1
                else:
                    retained.append(name)
            except OSError:
                skipped["недоступные каталоги"] += 1
        directories[:] = retained
        for name in sorted(filenames):
            budget.visit()
            path = current_path / name
            relative = _safe_path(path.relative_to(root).as_posix())
            if _excluded(relative):
                skipped["служебные и защищённые файлы"] += 1
                continue
            try:
                if _is_link(path):
                    skipped["ссылки и junction"] += 1
                    continue
                if _under_excluded(path, excluded):
                    skipped["явно исключённые выходные файлы"] += 1
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or not path.resolve().is_relative_to(root):
                    skipped["нерегулярные файлы и внешние пути"] += 1
                    continue
                if info.st_size > MAX_FILE_BYTES:
                    skipped["файлы больше индивидуального лимита"] += 1
                    continue
                budget.reserve(info.st_size)
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                                     getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or not path.resolve().is_relative_to(root):
                        skipped["нерегулярные файлы и внешние пути"] += 1
                        continue
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != info.st_size:
                    raise PkoError("Исходный файл изменился во время создания снимка.",
                                   hint=relative)
            except OSError:
                skipped["недоступные файлы"] += 1
                continue
            yield relative, data


def _zip_files(path: Path, budget: _Budget,
               skipped: Counter) -> Iterator[tuple[str, bytes]]:
    if path.stat().st_size > MAX_WORKSPACE_BYTES:
        raise PkoError("ZIP превышает допустимый размер исходных материалов.")
    try:
        with zipfile.ZipFile(path) as archive:
            seen: set[str] = set()
            for info in archive.infolist():
                budget.visit()
                relative = _safe_path(info.filename)
                # A case-insensitive namespace is portable to Windows; duplicate
                # records must never silently replace an earlier piece of evidence.
                identity = relative.casefold()
                if identity in seen:
                    raise PkoError("В ZIP повторяется путь файла или каталога.", hint=relative)
                seen.add(identity)
                if _excluded(relative):
                    skipped["служебные и защищённые элементы ZIP"] += 1
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    skipped["ссылки и нерегулярные элементы ZIP"] += 1
                    continue
                if info.is_dir():
                    continue
                if info.flag_bits & 1:
                    raise PkoError("Зашифрованный ZIP не поддерживается.")
                if info.file_size > MAX_FILE_BYTES:
                    skipped["файлы больше индивидуального лимита"] += 1
                    continue
                budget.reserve(info.file_size)
                with archive.open(info) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != info.file_size:
                    raise PkoError("Размер элемента ZIP не соответствует его содержимому.",
                                   hint=relative)
                yield relative, data
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
            EOFError, OSError) as error:
        raise PkoError("Не удалось безопасно прочитать ZIP.", hint=str(error)) from error


def build_workspace(paths: list[str | Path], dest: Path, *,
                    exclude_paths: Iterable[Path] = ()) -> SourceWorkspace:
    """Create a bounded masked snapshot without modifying any input source.

    ``dest`` must be absent or empty. It may be inside a source directory and is
    then excluded from traversal. The returned tree reads only cached admitted
    paths, so later source edits cannot change the evidence under investigation.
    ``exclude_paths`` excludes entire local subtrees before opening files, for
    example current/previous report folders inside the inspected source root.
    It does not interpret arbitrary names inside ZIPs as local output paths.
    """
    if not paths:
        raise PkoError("Нужна хотя бы одна директория с кодом или ZIP.")
    try:
        excluded = tuple(Path(path).resolve() for path in exclude_paths)
        sources_paths: list[tuple[Path, str]] = []
        for value in paths:
            path = Path(value).absolute()
            if _is_link(path):
                raise PkoError("Источник не должен быть символической ссылкой или junction.",
                               hint=str(path))
            if path.is_dir():
                kind = "directory"
            elif path.is_file() and path.suffix.lower() == ".zip":
                kind = "zip"
            else:
                raise PkoError("Источник должен быть директорией или ZIP.", hint=str(path))
            sources_paths.append((path.resolve(), kind))
        destination = Path(dest).absolute()
        if destination.exists() and (_is_link(destination) or
                                     not destination.is_dir() or any(destination.iterdir())):
            raise PkoError("Каталог снимка должен быть пустым и не являться ссылкой.",
                           hint=str(destination))
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve()
        if any(path == destination for path, _ in sources_paths):
            raise PkoError("Источник и каталог снимка должны различаться.")

        tree = LocalTree(destination)
        sources: list[dict] = []
        omissions: list[dict] = []
        notes = ["Источники — загруженные материалы, а не перечень АС. Digest — SHA-256 "
                 "включённых замаскированных текстов и путей; это не Git commit и не хеш ZIP.",
                 f"Лимиты загрузки: {MAX_FILE_BYTES} байт на файл, "
                 f"{MAX_WORKSPACE_FILES} файлов и {MAX_WORKSPACE_BYTES} байт суммарно."]
        budget = _Budget()
        for index, (path, kind) in enumerate(sources_paths, 1):
            source_id = f"source-{index}"
            skipped: Counter = Counter()
            if _under_excluded(path, excluded):
                skipped["источник внутри явно исключённого выходного пути"] += 1
                entries = iter(())
            else:
                entries = (_directory_files(path, destination, budget, skipped, excluded)
                           if kind == "directory" else _zip_files(path, budget, skipped))
            included: list[tuple[str, bytes]] = []
            for relative, data in entries:
                text = _decode(data)
                if text is None:
                    skipped["бинарные файлы или неподдерживаемая кодировка"] += 1
                    continue
                snapshot_path = f"{source_id}/{relative}"
                target = destination / snapshot_path
                target.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation also rejects path collisions on the host FS.
                masked_data = text.encode("utf-8")
                with target.open("xb") as stream:
                    stream.write(masked_data)
                tree.files.append(snapshot_path)
                tree._text_cache[snapshot_path] = text
                included.append((relative, hashlib.sha256(masked_data).digest()))
            digest = hashlib.sha256()
            for relative, content_digest in sorted(included):
                encoded_path = relative.encode("utf-8")
                digest.update(len(encoded_path).to_bytes(8, "big"))
                digest.update(encoded_path)
                digest.update(content_digest)
            sources.append({"id": source_id, "name": path.name, "kind": kind,
                            "digest": "sha256:" + digest.hexdigest(),
                            "file_count": len(included)})
            if skipped:
                notes.append(f"{source_id} ({path.name}): пропущены " + "; ".join(
                    f"{reason}: {count}" for reason, count in sorted(skipped.items())))
                omissions.extend({"source_id": source_id, "kind": _MATERIAL_OMISSIONS[reason],
                                  "count": count, "detail": reason}
                                 for reason, count in sorted(skipped.items())
                                 if reason in _MATERIAL_OMISSIONS)
            if not included:
                notes.append(f"{source_id} ({path.name}): нет доступных текстовых файлов.")
        tree.files.sort()
        extraction = extract_all(tree)
        notes.extend(extraction.notes)
        omissions.extend({"source_id": path.split("/", 1)[0], "kind": "parse_failure",
                          "count": 1, "path": path,
                          "detail": "Статический экстрактор не разобрал файл; исходный текст доступен агенту."}
                         for path in sorted(set(extraction.parse_failures)))
        return SourceWorkspace(sources, tree, extraction, notes, omissions)
    except PkoError:
        raise
    except OSError as error:
        raise PkoError("Не удалось создать снимок исходных материалов.", hint=str(error)) from error
