"""Экстрактор Java/Kotlin по аннотациям и объявлениям — задача T4."""

from __future__ import annotations

import re

from pko.extractors.base import Fact, FileTree, is_vendor

JAVA_SUFFIXES = (".java", ".kt")

_TYPE = re.compile(
    r"^\s*(?:public|private|protected|final|abstract|open|internal|data|\s)*"
    r"\b(class|interface|enum|record|object)\s+(\w+)",
    re.MULTILINE,
)
_ANNOTATION = re.compile(r"^\s*@(\w+)\s*(?:\(([^)]*)\))?", re.MULTILINE)
_METHOD = re.compile(
    r"^\s*(?:public|private|protected|static|final|synchronized|override|suspend|fun|\s)+"
    r"[\w<>\[\],.?]+\s+(\w+)\s*\(",
    re.MULTILINE,
)
_MAPPING_PATH = re.compile(r'"([^"]*)"')

_COMPONENT_ANNOTATIONS = {
    "Service": "service", "Component": "component",
    "Repository": "repository", "Configuration": "configuration",
}
_ROUTE_ANNOTATIONS = {
    "RestController": "", "Controller": "", "RequestMapping": "",
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "PatchMapping": "PATCH", "DeleteMapping": "DELETE", "FeignClient": "FEIGN",
}
_JOB_ANNOTATIONS = {
    "Scheduled": "scheduler", "KafkaListener": "listener",
    "RabbitListener": "listener", "EventListener": "listener",
    "JmsListener": "listener",
}
_SKIP_METHOD_NAMES = {"if", "for", "while", "switch", "catch", "return", "new"}


def extract(tree: FileTree) -> tuple[list[Fact], list[str], list[str]]:
    facts: list[Fact] = []
    parsed: list[str] = []
    failed: list[str] = []
    for path in tree.files:
        if is_vendor(path) or not path.lower().endswith(JAVA_SUFFIXES):
            continue
        text = tree.read(path)
        if text is None:
            failed.append(path)
            continue
        parsed.append(path)
        facts.extend(_scan(text, path))
    return facts, parsed, failed


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _scan(text: str, path: str) -> list[Fact]:
    out: list[Fact] = []
    lines = text.split("\n")

    for match in _TYPE.finditer(text):
        name = match.group(2)
        out.append(Fact(
            kind="JAVA_CLASS", key=name, value=match.group(1),
            path=path, line=_line_of(text, match.start()), basis=name,
            category="code", action="define", mechanism="java",
        ))

    for match in _ANNOTATION.finditer(text):
        name = match.group(1)
        args = match.group(2) or ""
        line = _line_of(text, match.start())
        owner = _nearest_name(lines, line)

        if name in _COMPONENT_ANNOTATIONS:
            out.append(Fact(
                kind="JAVA_COMPONENT", key=owner or name, value=name,
                path=path, line=line, basis=owner or name,
                category="code", action="define",
                mechanism=_COMPONENT_ANNOTATIONS[name],
            ))
        elif name in _ROUTE_ANNOTATIONS:
            paths = _MAPPING_PATH.findall(args)
            route = paths[0] if paths else ""
            method = _ROUTE_ANNOTATIONS[name]
            out.append(Fact(
                kind="ROUTE", key=f"{method} {route}".strip() or (owner or name), value=route,
                path=path, line=line, basis=owner or name,
                category="interface", action="serve", mechanism="http_server",
            ))
        elif name in _JOB_ANNOTATIONS:
            out.append(Fact(
                kind="JAVA_JOB", key=owner or name, value=args.strip()[:120],
                path=path, line=line, basis=owner or name,
                category="runtime", action="schedule",
                mechanism=_JOB_ANNOTATIONS[name],
            ))
        elif name in {"Autowired", "Inject"}:
            out.append(Fact(
                kind="JAVA_WIRE", key=owner or name, value="",
                path=path, line=line, basis=owner or name,
                category="code", action="call", mechanism="di",
            ))

    for match in _METHOD.finditer(text):
        name = match.group(1)
        if name in _SKIP_METHOD_NAMES:
            continue
        out.append(Fact(
            kind="JAVA_METHOD", key=name, value="",
            path=path, line=_line_of(text, match.start()), basis=name,
            category="code", action="define", mechanism="java",
        ))

    return out


def _nearest_name(lines: list[str], line: int) -> str:
    """Имя класса или метода на следующих строках после аннотации."""
    for offset in range(line - 1, min(line + 6, len(lines))):
        candidate = lines[offset]
        type_match = _TYPE.match(candidate)
        if type_match:
            return type_match.group(2)
        method_match = _METHOD.match(candidate)
        if method_match:
            return method_match.group(1)
    return ""
