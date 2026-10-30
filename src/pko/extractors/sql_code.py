"""Экстрактор SQL. Регулярками, без полного парсера — задача T4.

Целевые репозитории состоят из SQL-процедур. Без этого экстрактора `show_facts`
на них почти пуст, `find_unclaimed_paths` строится на пустом множестве, и агент
работает вслепую через `search`/`read_file`, сваливая оценки в «почти».
"""

from __future__ import annotations

import re

from pko.extractors.base import Fact, FileTree, is_vendor

SQL_SUFFIXES = (".sql", ".pls", ".pks", ".pkb", ".ddl", ".hql")

_IDENT = r'[\w".$#]+'

_CREATE = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+|TEMPORARY\s+|TEMP\s+|MATERIALIZED\s+)?"
    rf"(TABLE|VIEW|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|INDEX|SEQUENCE|TYPE)\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?({_IDENT})",
    re.IGNORECASE,
)
_COMMENT_ON = re.compile(
    rf"\bCOMMENT\s+ON\s+(TABLE|COLUMN|VIEW)\s+({_IDENT})\s+IS\s+(['\"])(.*?)\3",
    re.IGNORECASE | re.DOTALL,
)
_INSERT = re.compile(rf"\bINSERT\s+(?:INTO|OVERWRITE(?:\s+TABLE)?)\s+({_IDENT})", re.IGNORECASE)
_UPDATE = re.compile(rf"\bUPDATE\s+({_IDENT})\s+SET\b", re.IGNORECASE)
_DELETE = re.compile(rf"\bDELETE\s+FROM\s+({_IDENT})", re.IGNORECASE)
_MERGE = re.compile(rf"\bMERGE\s+INTO\s+({_IDENT})", re.IGNORECASE)
_TRUNCATE = re.compile(rf"\bTRUNCATE\s+TABLE\s+({_IDENT})", re.IGNORECASE)
_FROM = re.compile(rf"\b(?:FROM|JOIN)\s+({_IDENT})", re.IGNORECASE)
_CALL = re.compile(rf"\b(?:CALL|EXEC|EXECUTE|PERFORM)\s+({_IDENT})", re.IGNORECASE)

_KIND_BY_OBJECT = {
    "TABLE": "SQL_TABLE", "VIEW": "SQL_VIEW", "MATERIALIZED": "SQL_VIEW",
    "PROCEDURE": "SQL_PROC", "FUNCTION": "SQL_PROC", "PACKAGE": "SQL_PROC",
    "TRIGGER": "SQL_TRIGGER", "INDEX": "SQL_INDEX",
    "SEQUENCE": "SQL_SEQUENCE", "TYPE": "SQL_TYPE",
}

_SQL_KEYWORDS = {
    "select", "where", "dual", "values", "set", "table", "only", "lateral",
    "unnest", "generate_series", "using",
}

MAX_FACTS_PER_FILE = 400


def extract(tree: FileTree) -> tuple[list[Fact], list[str], list[str]]:
    """Факты, разобранные файлы, нечитаемые файлы."""
    facts: list[Fact] = []
    parsed: list[str] = []
    failed: list[str] = []
    for path in tree.files:
        if is_vendor(path) or not path.lower().endswith(SQL_SUFFIXES):
            continue
        text = tree.read(path)
        if text is None:
            failed.append(path)
            continue
        parsed.append(path)
        facts.extend(_scan(text, path))
    return facts, parsed, failed


def _clean(name: str) -> str:
    return name.strip().strip('"').strip(";").strip(",").strip()


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _scan(text: str, path: str) -> list[Fact]:
    out: list[Fact] = []
    body = _strip_comments(text)

    for match in _CREATE.finditer(body):
        obj = match.group(1).upper()
        name = _clean(match.group(2))
        if not name:
            continue
        out.append(Fact(
            kind=_KIND_BY_OBJECT.get(obj, "SQL_OBJECT"), key=name, value=obj.lower(),
            path=path, line=_line_of(body, match.start()), basis=name.split(".")[-1],
            category="data", action="define", mechanism="sql",
        ))

    for match in _COMMENT_ON.finditer(body):
        obj = match.group(1).upper()
        name = _clean(match.group(2))
        note = " ".join(match.group(4).split())[:200]
        out.append(Fact(
            kind="SQL_COLUMN" if obj == "COLUMN" else "SQL_DOC", key=name, value=note,
            path=path, line=_line_of(body, match.start()), basis=name.split(".")[-1],
            category="data", action="define", mechanism="sql_comment",
        ))

    for pattern, verb in (
        (_INSERT, "insert"), (_UPDATE, "update"), (_DELETE, "delete"),
        (_MERGE, "merge"), (_TRUNCATE, "truncate"),
    ):
        for match in pattern.finditer(body):
            name = _clean(match.group(1))
            if not name or name.lower() in _SQL_KEYWORDS:
                continue
            out.append(Fact(
                kind="SQL_WRITE", key=name, value=verb,
                path=path, line=_line_of(body, match.start()), basis=name.split(".")[-1],
                category="data", action="write", mechanism="sql",
            ))

    seen: set[str] = set()
    for match in _FROM.finditer(body):
        name = _clean(match.group(1))
        low = name.lower()
        if not name or low in _SQL_KEYWORDS or low in seen or name.startswith("("):
            continue
        seen.add(low)
        out.append(Fact(
            kind="SQL_READ", key=name, value="",
            path=path, line=_line_of(body, match.start()), basis=name.split(".")[-1],
            category="data", action="read", mechanism="sql",
        ))

    for match in _CALL.finditer(body):
        name = _clean(match.group(1))
        if not name or name.lower() in _SQL_KEYWORDS:
            continue
        out.append(Fact(
            kind="SQL_CALL", key=name, value="",
            path=path, line=_line_of(body, match.start()), basis=name.split(".")[-1],
            category="runtime", action="call", mechanism="sql",
        ))

    return out[:MAX_FACTS_PER_FILE]


def _strip_comments(text: str) -> str:
    """Убирает /* */ и -- комментарии, сохраняя нумерацию строк."""
    def blank(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    return re.sub(r"--[^\n]*", "", re.sub(r"/\*.*?\*/", blank, text, flags=re.DOTALL))
