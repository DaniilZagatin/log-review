"""Зависимости и файлы конфигурации: чем система пользуется и на чём настраивается."""

from __future__ import annotations

import json
import re
import tomllib
from typing import Iterator

from pko.extractors.base import Fact, FileTree, is_vendor

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-\[\]]+)\s*([=<>!~]=[^;#\s]*)?")
_ENV_NAME = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*[:=]")

CONFIG_SUFFIXES = (".yaml", ".yml", ".toml", ".ini", ".cfg")
CONFIG_NAMES = {".env", ".env.example", ".env.sample", "dockerfile", "docker-compose.yml",
                "docker-compose.yaml", "makefile"}


def extract(tree: FileTree) -> list[Fact]:
    facts: list[Fact] = []
    facts.extend(_python_deps(tree))
    facts.extend(_node_deps(tree))
    facts.extend(_config_files(tree))
    return facts


def _python_deps(tree: FileTree) -> Iterator[Fact]:
    for path in tree.files:
        if is_vendor(path):
            continue
        base = path.rsplit("/", 1)[-1]
        if base == "pyproject.toml":
            text = tree.read(path)
            if not text:
                continue
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            deps = list(data.get("project", {}).get("dependencies", []) or [])
            poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
            deps.extend(f"{k}{v if isinstance(v, str) else ''}" for k, v in poetry.items())
            for raw in deps:
                yield from _dep_fact(raw, path)
        elif base.startswith("requirements") and base.endswith(".txt"):
            text = tree.read(path)
            if not text:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if not line.strip() or line.lstrip().startswith(("#", "-")):
                    continue
                yield from _dep_fact(line.strip(), path, line_no=i)


def _dep_fact(raw: str, path: str, line_no: int | None = None) -> Iterator[Fact]:
    m = _REQ_LINE.match(raw)
    if not m:
        return
    name = m.group(1)
    version = (m.group(2) or "").strip()
    yield Fact(
        kind="DEP",
        key=name.lower(),
        value=version or "версия не закреплена",
        path=path,
        line=line_no or 1,
        basis=f"{name} {version}".strip(),
    )


def _node_deps(tree: FileTree) -> Iterator[Fact]:
    for path in tree.files:
        if is_vendor(path) or not path.endswith("package.json"):
            continue
        text = tree.read(path)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for name, version in (data.get("dependencies") or {}).items():
            yield Fact(
                kind="DEP", key=name.lower(), value=str(version), path=path, line=1,
                basis=f"{name} {version}",
            )


def _config_files(tree: FileTree) -> Iterator[Fact]:
    for path in tree.files:
        if is_vendor(path):
            continue
        base = path.rsplit("/", 1)[-1].lower()
        if not (base in CONFIG_NAMES or path.lower().endswith(CONFIG_SUFFIXES)):
            continue
        if base in {"pyproject.toml"}:
            continue
        text = tree.read(path)
        if text is None:
            continue
        names: list[str] = []
        for line in text.splitlines():
            m = _ENV_NAME.match(line)
            if m:
                names.append(m.group(1))
        yield Fact(
            kind="SETTING",
            key=f"file:{path}",
            value=sorted(set(names))[:40],
            path=path,
            line=1,
            basis=f"файл конфигурации, параметров: {len(set(names))}",
        )
