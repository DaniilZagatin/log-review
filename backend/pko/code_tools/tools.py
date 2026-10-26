"""Инструменты единого агента: read-only доступ к коду репозитория.

Четыре инструмента — `list_files`, `read_file`, `search`, `show_facts` — и
ничего больше: нет ни bash, ни write-операций, поэтому набор инструментов
физически не может изменить снимок исходников, только прочитать его на
зафиксированном снимке. Секреты (`*_key`/`*_token`/`*_secret`/`*_password`)
маскируются в выдаче, `.env*`/`*.pem`/`*.key` не читаются вовсе.

`show_facts` отдаёт уже разобранные экстракторами факты (ROUTE, TOOL,
SQL_WRITE, TEST и т.п.) — агент не парсит AST заново, а берёт готовые
сигнатуры с line/kind/name для evidence. `search` поддерживает контекст строк
вокруг совпадения (before/after), чтобы видеть декоратор/сигнатуру рядом с
именем без второго вызова `read_file`.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from pko.extractors.base import FileTree, is_vendor
from pko.extractors.runner import Extraction

MAX_LIST_FILES = 400
MAX_READ_LINES = 400
SEARCH_MAX_MATCHES = 200
SEARCH_TIMEOUT_SECONDS = 5.0
MAX_SEARCH_PATTERN_CHARS = 10_000
MAX_SEARCH_LINE_CHARS = 2000
MAX_SEARCH_CONTEXT = 20
FACTS_MAX_ROWS = 200

_BLOCKED_NAME_PREFIXES = (".env",)
# Хранилища ключей рядом с `.pem`/`.key`: в целевых репозиториях Java они
# встречаются чаще, чем PEM, и содержат ровно то же самое — приватный ключ.
# Тот же список запрещён к упаковке в архив передачи; расходиться им незачем.
_BLOCKED_NAME_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b\w*(?:key|token|secret|password)\w*\s*[:=]\s*)([\"']?)([^\s\"']{4,})\2"
)

# Python's re cannot interrupt one pathological match from the same thread.
# User regex compilation and matching therefore run only in a disposable child.
# The parent kills/reaps it on timeout; no pattern is interpolated into code.
# JSONL input avoids retaining another complete copy of the source corpus.
_SEARCH_WORKER = r'''
import json
import re
import sys

def emit(value):
    print(json.dumps(value, ensure_ascii=True))

config = json.loads(sys.stdin.buffer.readline())
pattern = config["pattern"]
try:
    compiled = re.compile(pattern)
except (re.error, ValueError, OverflowError, RecursionError) as exc:
    emit({"error": "invalid_regex", "detail": str(exc)[:500]})
    sys.exit(0)
# This remains an early diagnostic only. The process timeout, not this
# incomplete heuristic, bounds every other backtracking shape as well.
if re.search(r"\([^()]*[+*?][^()]*\)[+*]", pattern):
    emit({"error": "nested_repeat"})
    sys.exit(0)
matches = []
for raw in sys.stdin.buffer:
    item = json.loads(raw)
    for number, line in enumerate(item["text"].splitlines(), 1):
        if len(line) <= config["max_line_chars"] and compiled.search(line):
            matches.append([item["index"], number])
            if len(matches) >= config["max_matches"]:
                emit({"matches": matches, "scanned": item["index"] + 1, "truncated": True})
                sys.exit(0)
emit({"matches": matches, "scanned": config["total_files"], "truncated": False})
'''

# Нативные OpenAI-совместимые схемы тулов (`tools=[...]` в запросе) — один в
# один повторяют сигнатуры и дефолты методов `ToolBox` ниже.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "Список файлов репозитория в источниках (vendor-каталоги скрыты). "
                "Если файлов больше страницы, первым идёт карта каталогов: где "
                "сколько файлов и какие расширения — по ней ориентироваться дешевле, "
                "чем листать плоский список."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "glob": {"type": "string", "description": "Шаблон имени файла или пути, например *.py", "default": "*"},
                    "offset": {"type": "integer", "description": "С какого файла продолжить (постранично)", "default": 1},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Содержимое файла построчно, с номерами строк. Секреты маскируются, .env/.pem/.key не читаются.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу, как он вернулся из list_files/search"},
                    "offset": {"type": "integer", "description": "Строка, с которой начать", "default": 1},
                    "limit": {"type": "integer", "description": f"Сколько строк вернуть (не больше {MAX_READ_LINES})", "default": MAX_READ_LINES},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Regex-поиск по файлам репозитория в источниках. "
                f"before/after показывают строки вокруг совпадения (до {MAX_SEARCH_CONTEXT}), "
                "чтобы видеть декоратор/сигнатуру рядом с именем без отдельного read_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Регулярное выражение для поиска"},
                    "glob": {"type": "string", "description": "Ограничить поиск файлами по шаблону", "default": "*"},
                    "before": {"type": "integer", "description": "Сколько строк показать до совпадения", "default": 0},
                    "after": {"type": "integer", "description": "Сколько строк показать после совпадения", "default": 0},
                    "offset": {"type": "integer", "description": "С какого совпадения продолжить (постранично)", "default": 1},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "who_calls",
            "description": (
                "Кто вызывает эту функцию или метод. Отвечает на главный вопрос "
                "шкалы: код написан — но задействован ли он? Пустой ответ значит "
                "«вызовов не найдено» — кандидат на статус «Код реализован, но не "
                "используется». Разбор приблизительный: динамическая "
                "диспетчеризация и реестры так не видны, поэтому пустой ответ — "
                "повод проверить руками, а не приговор."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя функции, метода или класса без скобок"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_facts",
            "description": (
                "Уже разобранные факты кода (ROUTE/TOOL/SQL_WRITE/TEST/GRAPH_NODE и др.) — "
                "готовые сигнатуры с kind, path, line, key/value. Не парсит AST заново: "
                "берёт результаты детерминированных экстракторов. Фильтр по path (glob) "
                "и/или kind. Полезно для evidence: факт уже содержит line+kind+name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Шаблон пути (glob), например backend/src/*.py", "default": "*"},
                    "kind": {"type": "string", "description": "Только факты этого вида (ROUTE, TOOL, SQL_WRITE, TEST, GRAPH_NODE, SETTING, LIMIT, EXTERNAL, LLM_CALL, MODULE, OWNER, TEST_REPORT и др.)"},
                    "offset": {"type": "integer", "description": "С какого факта продолжить (постранично)", "default": 1},
                },
            },
        },
    },
]


@dataclass
class ToolResult:
    ok: bool
    content: str
    meta: dict[str, Any]


def _search_timeout() -> ToolResult:
    return ToolResult(
        False,
        "Поиск остановлен по лимиту времени. Regex может требовать слишком долгого перебора. "
        "Упростите регулярное выражение или сузьте glob и повторите поиск; "
        "отсутствие совпадений этим запуском не установлено.",
        {"timed_out": True, "count": 0},
    )


class ToolBox:
    """Инструменты над `FileTree` и `Extraction` — снимком переданных исходников."""

    def __init__(self, tree: FileTree, extraction: Extraction | None = None):
        self.tree = tree
        self._files = [p for p in tree.files if not is_vendor(p)]
        self._extraction = extraction

    def call(self, name: str, args: dict[str, Any] | None) -> ToolResult:
        args = args if isinstance(args, dict) else {}
        if name == "list_files":
            return self.list_files(glob=str(args.get("glob") or "*"), offset=_as_int(args.get("offset"), 1))
        if name == "read_file":
            return self.read_file(
                path=str(args.get("path") or ""),
                offset=_as_int(args.get("offset"), 1),
                limit=_as_int(args.get("limit"), MAX_READ_LINES),
            )
        if name == "search":
            return self.search(
                pattern=str(args.get("pattern") or ""),
                glob=str(args.get("glob") or "*"),
                before=_as_int(args.get("before"), 0),
                after=_as_int(args.get("after"), 0),
                offset=_as_int(args.get("offset"), 1),
            )
        if name == "who_calls":
            return self.who_calls(name=str(args.get("name") or ""))
        if name == "show_facts":
            return self.show_facts(
                path=str(args.get("path") or "*"),
                kind=str(args.get("kind") or ""),
                offset=_as_int(args.get("offset"), 1),
            )
        return ToolResult(
            False,
            f"неизвестный инструмент: {name!r}. Доступны: list_files, read_file, "
            "search, who_calls, show_facts",
            {},
        )

    def list_files(self, glob: str = "*", offset: int = 1) -> ToolResult:
        matched = _glob_filter(self._files, glob)
        page, has_more = _paginate(matched, offset, MAX_LIST_FILES)
        content = "\n".join(page) if page else "(ничего не найдено)"
        if has_more:
            content += f"\n... есть ещё файлы, offset={offset + MAX_LIST_FILES} для продолжения"
        # Плоский список на пять тысяч файлов — это тринадцать страниц по
        # алфавиту: агент тратит шаги и контекст, а картины целого не получает.
        # Карта каталогов даёт её за один вызов; при мультизиповой загрузке
        # сразу видно и то, из скольких репозиториев собран снимок.
        if len(matched) > MAX_LIST_FILES and offset <= 1:
            content = _directory_digest(matched) + "\n\n" + content
        return ToolResult(True, content, {"count": len(page), "total": len(matched)})

    def read_file(self, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> ToolResult:
        path = path.strip()
        if path not in self._files:
            return ToolResult(False, f"файл не найден в источниках: {path}", {})
        if _is_blocked(path):
            return ToolResult(False, f"чтение файла запрещено (похоже на секрет): {path}", {})
        text = self.tree.read(path)
        if text is None:
            return ToolResult(False, f"файл не читается: {path}", {})

        lines = text.splitlines()
        limit = max(1, min(limit, MAX_READ_LINES))
        offset = max(1, offset)
        window = lines[offset - 1: offset - 1 + limit]
        if not window:
            return ToolResult(True, "(пусто или offset за пределами файла)", {"total_lines": len(lines)})
        numbered = "\n".join(
            f"{i}: {_mask_secrets(line)}" for i, line in enumerate(window, start=offset)
        )
        if offset - 1 + limit < len(lines):
            numbered += f"\n... есть ещё строки (всего {len(lines)}), offset={offset + limit} для продолжения"
        return ToolResult(True, numbered, {"total_lines": len(lines)})

    def search(
        self, pattern: str, glob: str = "*",
        before: int = 0, after: int = 0, offset: int = 1,
    ) -> ToolResult:
        """Regex-поиск с опциональным контекстом строк вокруг совпадения.

        `before`/`after` — сколько строк показать до/после совпадения. Без
        контекста (0/0) выдача — построчная `path:i: text`, как раньше. С
        контекстом — блочная: каждое совпадение группой с номерами строк,
        чтобы видеть декоратор/сигнатуру рядом без отдельного `read_file`.
        """
        pattern = pattern.strip()
        if not pattern:
            return ToolResult(False, "пустой паттерн поиска", {})
        if len(pattern) > MAX_SEARCH_PATTERN_CHARS:
            return ToolResult(False, "regex слишком длинный — сократите паттерн поиска", {})

        before = max(0, min(before, MAX_SEARCH_CONTEXT))
        after = max(0, min(after, MAX_SEARCH_CONTEXT))

        matched_files = _spread_by_directory(_glob_filter(self._files, glob))
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        try:
            with tempfile.TemporaryFile(mode="w+b") as source:
                header = {"pattern": pattern, "max_line_chars": MAX_SEARCH_LINE_CHARS,
                          "max_matches": SEARCH_MAX_MATCHES * 4, "total_files": len(matched_files)}
                source.write((json.dumps(header) + "\n").encode("utf-8"))
                for index, path in enumerate(matched_files):
                    if time.monotonic() >= deadline:
                        return _search_timeout()
                    text = self.tree.read(path)
                    if text is not None:
                        source.write((json.dumps({"index": index, "text": text}, ensure_ascii=True) + "\n").encode("utf-8"))
                source.seek(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _search_timeout()
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", _SEARCH_WORKER], stdin=source,
                    capture_output=True, encoding="utf-8", timeout=remaining,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            if completed.returncode != 0:
                return ToolResult(False, "Не удалось выполнить regex-поиск. Упростите паттерн и повторите поиск.", {})
            result = json.loads(completed.stdout)
        except subprocess.TimeoutExpired:
            return _search_timeout()
        except (OSError, ValueError) as exc:
            return ToolResult(False, "Не удалось запустить изолированный regex-поиск. Повторите поиск позже.",
                              {"error_type": type(exc).__name__})
        if result.get("error") == "invalid_regex":
            return ToolResult(False, f"некорректный regex: {result['detail']}", {})
        if result.get("error") == "nested_repeat":
            return ToolResult(False,
                              "паттерн похож на катастрофический backtracking (вложенный повтор внутри "
                              "повторяющейся группы) — упростите regex", {})

        blocks: list[str] = []
        previous_index, lines = None, []
        for index, number in result["matches"]:
            path = matched_files[index]
            if index != previous_index:
                lines = (self.tree.read(path) or "").splitlines()
                previous_index = index
            blocks.append(_format_match(path, number, lines, before, after))
        truncated, scanned = result["truncated"], result["scanned"]

        page, has_more = _paginate(blocks, offset, SEARCH_MAX_MATCHES)
        content = "\n\n".join(page) if page else "(совпадений нет)"
        if has_more:
            content += f"\n\n... есть ещё совпадения, offset={offset + SEARCH_MAX_MATCHES} для продолжения"
        if truncated:
            content += (
                f"\n(поиск остановлен раньше срока: просмотрено {scanned} файлов "
                f"из {len(matched_files)}. Файлы берутся вперемешку по каталогам, "
                "так что это срез по всему репозиторию, а не по его началу. "
                "Сузьте паттерн или glob, чтобы досмотреть остальное)"
            )
        return ToolResult(True, content, {"count": len(page), "scanned": scanned})

    def who_calls(self, name: str) -> ToolResult:
        """Вызовы объявления по фактам `CALLS` — и вызовы ли это вообще.

        Разница между «Работает» и «Код реализован, но не используется» стоит
        50 пунктов готовности прототипа и все 100 промышленной, а до сих пор
        это суждение модель делала вручную и ничем не подтверждала. Здесь оно
        опирается на детерминированный разбор — приблизительный, но общий для
        всех прогонов.
        """
        name = (name or "").strip().strip("()")
        if not name:
            return ToolResult(False, "нужно имя функции или класса", {})
        if self._extraction is None or not self._extraction.facts:
            return ToolResult(True, "(факты недоступны — экстракция не выполнялась)",
                              {"count": 0})

        # `CALLS` — граф вызовов Python. `SQL_CALL` — вызов процедуры из SQL:
        # у целевого стека это тот же вопрос «задействовано ли», и не учитывать
        # его значило бы объявлять живые процедуры неиспользуемыми.
        callers = [
            f for f in self._extraction.facts
            if (f.kind == "CALLS" and isinstance(f.value, dict)
                and f.value.get("callee") == name)
            or (f.kind == "SQL_CALL" and f.key == name)
        ]
        where_defined = [
            f for f in self._extraction.facts
            if f.kind in ("FUNCTION", "CLASS_DEF", "TOOL",
                          "SQL_PROC", "JAVA_METHOD", "JAVA_CLASS")
            and f.key == name
        ]

        lines: list[str] = []
        # Одно объявление — одна строка: тул из `tools.py` приходит и как TOOL,
        # и как FUNCTION с той же строки.
        shown: set[tuple[str, int | None]] = set()
        for fact in where_defined:
            spot = (fact.path, fact.line)
            if spot in shown:
                continue
            shown.add(spot)
            lines.append(f"объявлено: {fact.path}:{fact.line}")
            if len(shown) >= 5:
                break
        if not callers:
            lines.append(
                f"вызовов «{name}» в разобранном коде не найдено — это кандидат на "
                "«Код реализован, но не используется». Проверь руками: вызов мог "
                "уйти через реестр, строку конфига или динамическую диспетчеризацию."
            )
            return ToolResult(True, "\n".join(lines), {"count": 0})

        lines.append(f"вызывают «{name}» ({len(callers)}):")
        for fact in callers[:FACTS_MAX_ROWS]:
            caller = fact.value.get("caller", "?") if isinstance(fact.value, dict) else "?"
            where = f"- {fact.path}:{fact.line}"
            lines.append(where + (f" | из «{caller}»" if caller != "?" else ""))
        if len(callers) > FACTS_MAX_ROWS:
            lines.append(f"... и ещё {len(callers) - FACTS_MAX_ROWS}")
        return ToolResult(True, "\n".join(lines), {"count": len(callers)})

    def show_facts(self, path: str = "*", kind: str = "", offset: int = 1) -> ToolResult:
        """Готовые факты из экстракторов — без повторного парсинга кода агентом.

        Фильтр по `path` (glob) и `kind`. Выдача: `kind | path:line | key=value`
        построчно. Пагинация по `offset`. Без экстракции (ToolBox без extraction)
        возвращает понятное «нет данных», не ошибку.
        """
        if self._extraction is None or not self._extraction.facts:
            return ToolResult(True, "(факты недоступны — экстракция не выполнялась)", {"count": 0, "total": 0})

        facts = self._extraction.facts
        if kind:
            kind_norm = kind.strip().upper()
            facts = [f for f in facts if f.kind == kind_norm]
        if path and path != "*":
            facts = [f for f in facts if _glob_match(f.path, path)]

        rows: list[str] = []
        for f in facts:
            loc = f"{f.path}:{f.line}" if f.line else f.path
            value_str = _format_fact_value(f.value)
            row = f"{f.kind} | {loc} | {f.key}"
            if value_str:
                row += f" = {value_str}"
            rows.append(row)

        page, has_more = _paginate(rows, offset, FACTS_MAX_ROWS)
        content = "\n".join(page) if page else "(фактов по фильтру нет)"
        if has_more:
            content += f"\n... есть ещё факты, offset={offset + FACTS_MAX_ROWS} для продолжения"
        return ToolResult(True, content, {"count": len(page), "total": len(rows)})


def _format_match(path: str, line_no: int, lines: list[str], before: int, after: int) -> str:
    """Блок совпадения: при контексте — группа с номерами строк, иначе одна строка."""
    if before == 0 and after == 0:
        return f"{path}:{line_no}: {_mask_secrets(lines[line_no - 1].strip())[:200]}"

    start = max(1, line_no - before)
    end = min(len(lines), line_no + after)
    header = f"─── {path}:{start}-{end} (совпадение на {line_no}) ───"
    body = "\n".join(
        f"{i}: {_mask_secrets(lines[i - 1])}"
        for i in range(start, end + 1)
    )
    return header + "\n" + body


def _format_fact_value(value: Any) -> str:
    """Компактное строковое представление значения факта для выдачи."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)[:120]
    return str(value)[:120]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# Сколько каталогов показывать в карте и на какой глубине их складывать.
_DIGEST_DIRS = 30
_DIGEST_DEPTH = 2


def _directory_digest(paths: list[str]) -> str:
    """Карта каталогов: где сколько файлов и какие расширения преобладают."""
    buckets: dict[str, list[str]] = {}
    for path in paths:
        parts = path.split("/")
        key = "/".join(parts[:_DIGEST_DEPTH]) if len(parts) > 1 else "."
        buckets.setdefault(key, []).append(path)

    rows = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = [f"Карта каталогов ({len(paths)} файлов, {len(buckets)} каталогов):"]
    for key, group in rows[:_DIGEST_DIRS]:
        exts: dict[str, int] = {}
        for path in group:
            name = path.rsplit("/", 1)[-1]
            ext = "." + name.rsplit(".", 1)[-1] if "." in name else "(без расширения)"
            exts[ext] = exts.get(ext, 0) + 1
        top = ", ".join(
            f"{ext} {count}"
            for ext, count in sorted(exts.items(), key=lambda kv: -kv[1])[:4]
        )
        lines.append(f"  {key}/ — {len(group)}: {top}")
    if len(rows) > _DIGEST_DIRS:
        lines.append(f"  … и ещё {len(rows) - _DIGEST_DIRS} каталогов")
    lines.append("Сужайте выдачу глобом: list_files с glob=\"backend/**\" или \"*.py\".")
    return "\n".join(lines)


def _spread_by_directory(paths: list[str]) -> list[str]:
    """Разложить файлы вперемешку по каталогам, сохранив порядок внутри каждого.

    Поиск ограничен таймаутом, и при обходе подряд бюджет съедает первый по
    алфавиту каталог: на большом репозитории агент систематически не видит
    вторую половину кода и не знает об этом. Круговой обход даёт срез по
    всему дереву.
    """
    buckets: dict[str, list[str]] = {}
    for path in paths:
        head = path.rsplit("/", 1)[0] if "/" in path else ""
        buckets.setdefault(head, []).append(path)
    if len(buckets) <= 1:
        return list(paths)

    order = sorted(buckets)
    out: list[str] = []
    index = 0
    while len(out) < len(paths):
        for key in order:
            bucket = buckets[key]
            if index < len(bucket):
                out.append(bucket[index])
        index += 1
    return out


def _glob_filter(paths: list[str], pattern: str) -> list[str]:
    if pattern in ("", "*"):
        return paths
    return [p for p in paths if _glob_match(p, pattern)]


def _glob_match(path: str, pattern: str) -> bool:
    if pattern in ("", "*"):
        return True
    return (
        fnmatch.fnmatch(path, pattern)
        or fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    )


def _paginate(items: list[Any], offset: int, limit: int) -> tuple[list[Any], bool]:
    offset = max(1, offset)
    start = offset - 1
    page = items[start: start + limit]
    return page, start + limit < len(items)


def _is_blocked(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return base.startswith(_BLOCKED_NAME_PREFIXES) or base.endswith(_BLOCKED_NAME_SUFFIXES)


def _mask_secrets(text: str) -> str:
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}***{m.group(2)}", text)
