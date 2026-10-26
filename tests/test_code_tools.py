"""Инструменты единого агента (`list_files`/`read_file`/`search`/`show_facts`) —
read-only над локальным снимком и готовыми фактами экстракторов, без Git.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.extractors.base import Fact
from pko.extractors.runner import extract_all, Extraction
from pko.code_tools.local_tree import LocalTree
from pko.code_tools.tools import (
    MAX_LIST_FILES,
    MAX_SEARCH_PATTERN_CHARS,
    TOOL_SCHEMAS,
    ToolBox,
    _directory_digest,
    _glob_filter,
    _is_blocked,
    _mask_secrets,
    _paginate,
)

ROUTER_PATH = "backend/src/api/v1/router.py"


def fixture_tree():
    files = {
        ROUTER_PATH: "from fastapi import APIRouter\nrouter = APIRouter()\n@router.post('/requests')\ndef submit():\n    return {'ok': True}\n",
        "backend/src/tools.py": "from langchain_core.tools import tool\n@tool\ndef search_schema():\n    return []\n",
        "backend/src/storage.py": "def save_observation(value):\n    return value\n",
    }
    return LocalTree(Path("."), sorted(files), files)


class ToolSchemasTest(unittest.TestCase):
    def test_schemas_match_toolbox_dispatch(self):
        names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        self.assertEqual(names, {
            "list_files", "read_file", "search", "who_calls", "show_facts",
        })
        for schema in TOOL_SCHEMAS:
            self.assertEqual(schema["type"], "function")
            self.assertIn("properties", schema["function"]["parameters"])


class ToolBoxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = fixture_tree()
        cls.extraction = extract_all(cls.tree)

    def setUp(self):
        self.tools = ToolBox(self.tree, extraction=self.extraction)

    def test_list_files_finds_known_path(self):
        result = self.tools.list_files()
        self.assertTrue(result.ok)
        self.assertIn(ROUTER_PATH, result.content)

    def test_list_files_respects_glob(self):
        result = self.tools.list_files(glob="*.py")
        self.assertTrue(result.ok)
        for line in result.content.splitlines():
            if line.startswith("..."):
                continue
            self.assertTrue(line.endswith(".py"), msg=line)

    def test_read_file_returns_numbered_lines(self):
        result = self.tools.read_file(ROUTER_PATH)
        self.assertTrue(result.ok)
        self.assertIn("1: from fastapi import APIRouter", result.content)

    def test_read_file_offset_and_limit(self):
        windowed = self.tools.read_file(ROUTER_PATH, offset=2, limit=1)
        self.assertTrue(windowed.ok)
        first_line = windowed.content.splitlines()[0]
        self.assertTrue(first_line.startswith("2:"), msg=first_line)
        # Роутер длиннее одной строки — должна появиться пометка о продолжении.
        self.assertIn("есть ещё строки", windowed.content)

    def test_read_file_missing_path_is_a_clean_failure(self):
        result = self.tools.read_file("backend/src/does_not_exist.py")
        self.assertFalse(result.ok)
        self.assertIn("не найден", result.content)

    def test_read_file_blocks_env_and_key_files(self):
        for path in (".env", ".env.local", "secrets.pem", "id.key"):
            self.assertTrue(_is_blocked(path), msg=path)
        self.assertFalse(_is_blocked(ROUTER_PATH))

    def test_search_finds_known_symbol(self):
        result = self.tools.search(pattern="APIRouter")
        self.assertTrue(result.ok)
        self.assertIn(f"{ROUTER_PATH}:", result.content)

    def test_search_no_matches_is_not_an_error(self):
        result = self.tools.search(pattern="totally_absent_symbol_xyz")
        self.assertTrue(result.ok)
        self.assertIn("совпадений нет", result.content)

    def test_search_rejects_invalid_regex(self):
        result = self.tools.search(pattern="(unclosed")
        self.assertFalse(result.ok)
        self.assertIn("regex", result.content)

    def test_search_rejects_catastrophic_shape(self):
        result = self.tools.search(pattern=r"(a+)+$")
        self.assertFalse(result.ok)
        self.assertIn("backtracking", result.content)

    def test_search_rejects_oversized_pattern_before_starting_worker(self):
        with patch("pko.code_tools.tools.subprocess.run") as run:
            result = self.tools.search("x" * (MAX_SEARCH_PATTERN_CHARS + 1))
        self.assertFalse(result.ok)
        self.assertIn("слишком длинный", result.content)
        run.assert_not_called()

    def test_ambiguous_alternation_cannot_hang_the_agent(self):
        # An outer timeout protects the entire test process if the production
        # isolation regresses. This pattern bypasses the nested-repeat heuristic.
        script = r'''
import json
import time
from pathlib import Path
from pko.code_tools.local_tree import LocalTree
from pko.code_tools import tools
tools.SEARCH_TIMEOUT_SECONDS = 0.3
tree = LocalTree(Path("."), ["source-1/a.py"], {"source-1/a.py": "a" * 40 + "b"})
box = tools.ToolBox(tree)
started = time.monotonic()
result = box.search(r"(a|aa)+$")
elapsed = time.monotonic() - started
tools.SEARCH_TIMEOUT_SECONDS = 5.0
followup = box.search("b$")
print(json.dumps({"ok": result.ok, "meta": result.meta, "content": result.content,
                  "elapsed": elapsed, "followup_ok": followup.ok, "followup": followup.content}))
'''
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "backend"), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run([sys.executable, "-B", "-c", script], cwd=root, env=env,
                                   capture_output=True, encoding="utf-8", timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"]["timed_out"])
        self.assertLess(result["elapsed"], 3.0)
        self.assertIn("лимиту времени", result["content"])
        self.assertTrue(result["followup_ok"])
        self.assertIn("source-1/a.py:1:", result["followup"])

    def test_search_context_shows_surrounding_lines(self):
        result = self.tools.search(pattern="APIRouter", before=1, after=1)
        self.assertTrue(result.ok)
        # Блочный формат: заголовок с диапазоном строк + строки с номерами
        self.assertIn(ROUTER_PATH, result.content)
        self.assertIn("совпадение на", result.content)
        # Контекстная строка (import) должна быть видна рядом с совпадением
        lines = result.content.splitlines()
        numbered = [l for l in lines if l.strip().startswith("1:")]
        self.assertTrue(any("APIRouter" in l for l in numbered), msg=lines)

    def test_search_without_context_is_one_line_per_match(self):
        result = self.tools.search(pattern="APIRouter")
        self.assertTrue(result.ok)
        # Без контекста — старый формат `path:i: text`, без блочного заголовка
        self.assertNotIn("совпадение на", result.content)
        self.assertIn(f"{ROUTER_PATH}:", result.content)

    def test_show_facts_returns_known_route(self):
        result = self.tools.show_facts(kind="ROUTE")
        self.assertTrue(result.ok)
        self.assertIn(ROUTER_PATH, result.content)
        self.assertIn("ROUTE", result.content)

    def test_show_facts_filters_by_path_glob(self):
        result = self.tools.show_facts(path="*.py")
        self.assertTrue(result.ok)
        for line in result.content.splitlines():
            if line.startswith("...") or line.startswith("("):
                continue
            self.assertIn(".py", line, msg=line)

    def test_show_facts_pagination_reports_has_more(self):
        result = self.tools.show_facts(offset=1)
        self.assertTrue(result.ok)
        # На фикстуре 37 фактов, лимит 200 — всё влезает, has_more=False
        self.assertNotIn("есть ещё факты", result.content)

    def test_show_facts_without_extraction_is_graceful(self):
        empty = ToolBox(self.tree)
        result = empty.show_facts()
        self.assertTrue(result.ok)
        self.assertIn("недоступны", result.content)

    def test_unknown_tool_is_a_clean_failure(self):
        result = self.tools.call("delete_everything", {})
        self.assertFalse(result.ok)
        self.assertIn("неизвестный инструмент", result.content)


class MaskSecretsTest(unittest.TestCase):
    def test_key_value_assignment_is_masked(self):
        masked = _mask_secrets('api_key = "sk-abc123def456"')
        self.assertNotIn("sk-abc123def456", masked)
        self.assertIn("***", masked)

    def test_ordinary_code_is_untouched(self):
        line = "def read_file(path, offset=1):"
        self.assertEqual(_mask_secrets(line), line)


class PaginateAndGlobTest(unittest.TestCase):
    def test_paginate_reports_has_more(self):
        items = [str(i) for i in range(10)]
        page, has_more = _paginate(items, offset=1, limit=4)
        self.assertEqual(page, ["0", "1", "2", "3"])
        self.assertTrue(has_more)
        page2, has_more2 = _paginate(items, offset=9, limit=4)
        self.assertEqual(page2, ["8", "9"])
        self.assertFalse(has_more2)

    def test_glob_filter_matches_suffix_pattern(self):
        paths = ["a/b.py", "a/b.md", "c/d.py"]
        self.assertEqual(_glob_filter(paths, "*.py"), ["a/b.py", "c/d.py"])
        self.assertEqual(_glob_filter(paths, "*"), paths)


class DirectoryDigestTest(unittest.TestCase):
    """Плоский список на пять тысяч файлов — тринадцать страниц по алфавиту.

    Агент тратит шаги и контекст, а картины целого не получает. Карта
    каталогов даёт её за один вызов.
    """

    def test_digest_counts_files_and_extensions(self) -> None:
        files = ([f"backend/src/f{i}.py" for i in range(40)]
                 + [f"backend/db/f{i}.sql" for i in range(25)])
        digest = _directory_digest(files)
        self.assertIn("65 файлов", digest)
        self.assertIn("backend/src/ — 40", digest)
        self.assertIn(".sql 25", digest)

    def test_digest_shows_separate_repositories(self) -> None:
        """При мультизиповой загрузке видно, из скольких репозиториев снимок."""
        files = ([f"agent-a/src/f{i}.py" for i in range(5)]
                 + [f"agent-b/src/f{i}.py" for i in range(5)])
        digest = _directory_digest(files)
        self.assertIn("agent-a/src/", digest)
        self.assertIn("agent-b/src/", digest)

    def test_digest_precedes_the_page_only_on_a_big_listing(self) -> None:
        many = {f"src/f{i}.py": "x" for i in range(MAX_LIST_FILES + 10)}
        tools = ToolBox(_ManyFileTree(many))
        out = tools.call("list_files", {"glob": "*"})
        self.assertIn("Карта каталогов", out.content)

        few = ToolBox(_ManyFileTree({"src/a.py": "x"}))
        self.assertNotIn("Карта каталогов", few.call("list_files", {}).content)


class _ManyFileTree:
    """Дерево из словаря путь→содержимое."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = dict(files)
        self.files = sorted(files)

    @property
    def files_set(self) -> set[str]:
        return set(self.files)

    def read(self, path: str) -> str | None:
        return self._files.get(path)

    def match(self, suffixes=(), names=()) -> list[str]:
        return [p for p in self.files if suffixes and p.endswith(tuple(suffixes))]


class _OneFileTree:
    """Минимальное дерево из одного модуля — для проверок графа вызовов."""

    def __init__(self, source: str, path: str = "app.py") -> None:
        self.files = [path]
        self._path = path
        self._source = source

    @property
    def files_set(self) -> set[str]:
        return set(self.files)

    def read(self, path: str) -> str | None:
        return self._source if path == self._path else None

    def match(self, suffixes=(), names=()) -> list[str]:
        if suffixes and self._path.endswith(tuple(suffixes)):
            return [self._path]
        if names and self._path in set(names):
            return [self._path]
        return []


class WhoCallsTest(unittest.TestCase):
    """Кто вызывает объявление — на этом держится «работает» против «не используется»."""

    def setUp(self):
        tree = fixture_tree()
        self.tools = ToolBox(tree, extraction=extract_all(tree))

    def test_uncalled_function_is_reported_as_a_candidate(self):
        out = self.tools.call("who_calls", {"name": "save_observation"})
        self.assertTrue(out.ok)
        self.assertIn("объявлено:", out.content)
        self.assertIn("не найдено", out.content)
        self.assertEqual(out.meta["count"], 0)

    def test_declaration_is_listed_once(self):
        """Тул из `tools.py` приходит и как TOOL, и как FUNCTION — строка одна."""
        out = self.tools.call("who_calls", {"name": "search_schema"})
        self.assertEqual(out.content.count("объявлено:"), 1)

    def test_called_function_lists_its_callers(self):
        """Вызов своей функции виден как ребро графа — весь путь целиком.

        Дерево тут синтетическое: в `mini_repo` межмодульных вызовов нет, и
        привязывать проверку инструмента к содержимому фикстуры значило бы
        проверять фикстуру, а не инструмент.
        """
        source = (
            "def helper(x):\n"
            "    \"\"\"Вспомогательный расчёт.\"\"\"\n"
            "    return x\n"
            "\n"
            "def main():\n"
            "    \"\"\"Точка входа.\"\"\"\n"
            "    return helper(1)\n"
        )
        tools = ToolBox(_OneFileTree(source),
                        extraction=extract_all(_OneFileTree(source)))
        out = tools.call("who_calls", {"name": "helper"})
        self.assertEqual(out.meta["count"], 1)
        self.assertIn("вызывают", out.content)
        self.assertIn("из «main»", out.content)

    def test_library_calls_are_not_edges(self):
        """Вопрос графа — задействован ли НАШ код, а не вызывался ли `len`."""
        source = "def main():\n    return len([1, 2])\n"
        facts = extract_all(_OneFileTree(source)).facts
        self.assertEqual([f for f in facts if f.kind == "CALLS"], [])

    def test_call_belongs_to_the_nearest_function(self):
        """Вызов из вложенной функции — не вызов из внешней.

        Фантомное ребро опаснее отсутствующего: оно показывает мёртвый код
        достижимым, а ради этого суждения граф и строится.
        """
        source = (
            "def helper():\n    return 1\n"
            "\n"
            "def outer():\n"
            "    def inner():\n"
            "        return helper()\n"
            "    return inner()\n"
        )
        edges = {
            f.key for f in extract_all(_OneFileTree(source)).facts if f.kind == "CALLS"
        }
        self.assertIn("inner -> helper", edges)
        self.assertIn("outer -> inner", edges)
        self.assertNotIn("outer -> helper", edges)

    def test_sql_procedure_calls_are_counted(self):
        """У целевого стека «задействовано ли» — это вызов процедуры."""
        facts = [
            Fact(kind="SQL_PROC", key="calc_score", value="procedure",
                 path="db/a.sql", line=2),
            Fact(kind="SQL_CALL", key="calc_score", value="", path="db/b.sql", line=7),
        ]
        tools = ToolBox(_OneFileTree("--\n"),
                        extraction=Extraction(facts=facts))
        out = tools.call("who_calls", {"name": "calc_score"})
        self.assertEqual(out.meta["count"], 1)
        self.assertIn("db/b.sql:7", out.content)
        self.assertIn("объявлено: db/a.sql:2", out.content)

    def test_empty_name_is_a_clean_error(self):
        self.assertFalse(self.tools.call("who_calls", {"name": "  "}).ok)


if __name__ == "__main__":
    unittest.main()
