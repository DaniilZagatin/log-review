"""Запуск всех экстракторов и подсчёт покрытия анализа."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pko.extractors import java_code
from pko.extractors import sql_code
from pko.extractors import contracts as contracts_ex
from pko.extractors import deps as deps_ex
from pko.extractors import ownership as ownership_ex
from pko.extractors import policy_specs as policy_ex
from pko.extractors import prompts as prompts_ex
from pko.extractors import python_code
from pko.extractors import test_reports as tests_ex
from pko.extractors.base import Fact, FileTree, is_vendor
from pko.model.schema import Coverage
from pko.model.taxonomy import Facets

# Что PKO умеет разбирать в первой версии.
ANALYZED_GLOBS = ["*.py", "pyproject.toml", "requirements*.txt", "package.json", "*.json",
                  "*.yaml", "*.yml", "*.toml", "*.ini", "*.cfg", ".env*", "CODEOWNERS"]

# Периметр первой версии — backend на Python и сопутствующая конфигурация.
# Фронтенд статически не разбирается, поэтому держать его в знаменателе
# покрытия нечестно: 42% «непокрытого» складывались из `.tsx`/`.ts`/`.css`, и
# проверка CHK-COV-001 падала не из-за пробела в анализируемой системе, а
# из-за границы самого инструмента. Файлы никуда не исчезают: они названы
# отдельной неблокирующей строкой и остаются видны агенту.
OUT_OF_PERIMETER_SUFFIXES = (
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".css", ".scss", ".less", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2",
)
PERIMETER_NAME = "backend-периметр (Python и конфигурация)"


def is_out_of_perimeter(path: str) -> bool:
    """Файл вне периметра первой версии: не ошибка анализа, а его граница."""
    return path.lower().endswith(OUT_OF_PERIMETER_SUFFIXES)


@dataclass
class Extraction:
    """Результат разбора одной версии."""

    facts: list[Fact] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    parse_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Fact]:
        return [f for f in self.facts if f.kind == kind]

    def select(
        self,
        predicate: Callable[[Facets], bool] | None = None,
        action: str | None = None,
        mechanism: str | None = None,
    ) -> list[Fact]:
        """Факты по смыслу, а не по имени вида.

        Так находка на незнакомом стеке попадает в модель наравне с той, что
        нашёл статический разбор: `SQL_WRITE` и универсальный
        `EFFECT/write/orm` отвечают одному и тому же вопросу.
        """
        out = []
        for fact in self.facts:
            facets = fact.facets
            if predicate is not None and not predicate(facets):
                continue
            if action is not None and facets.action != action:
                continue
            if mechanism is not None and facets.mechanism != mechanism:
                continue
            out.append(fact)
        return out


def extract_all(tree: FileTree, junit_path: str | Path | None = None) -> Extraction:
    """Собрать факты по снимку репозитория."""
    facts, parsed, failed = python_code.extract(tree)

    # Целевой стек инициатив — SQL-процедуры и Java-сервисы, а не Python.
    # Без этих двух экстракторов show_facts на таком репозитории почти пуст
    # и агент вынужден работать вслепую (задача T4).
    sql_facts, sql_parsed, sql_failed = sql_code.extract(tree)
    java_facts, java_parsed, java_failed = java_code.extract(tree)
    facts.extend(sql_facts)
    facts.extend(java_facts)
    parsed.extend(sql_parsed)
    parsed.extend(java_parsed)
    failed.extend(sql_failed)
    failed.extend(java_failed)

    facts.extend(deps_ex.extract(tree))
    # Объявленные контракты и политики: интерфейс, схема, инструмент или лимит
    # часто вынесены из кода, и без них система выглядит безинтерфейсной.
    contract_facts = contracts_ex.extract(tree)
    policy_facts = policy_ex.extract(tree)
    facts.extend(contract_facts)
    facts.extend(policy_facts)
    facts.extend(prompts_ex.extract(tree))
    facts.extend(ownership_ex.extract(tree))
    facts.extend(tests_ex.extract(tree))

    notes: list[str] = []
    if junit_path:
        junit = tests_ex.read_junit(junit_path)
        if junit.facts:
            facts.extend(junit.facts)
        else:
            notes.append(f"Отчёт о тестах не прочитан: {junit.source}")

    # В отличие от YAML/TOML, произвольный JSON не считается разобранным только
    # по расширению: экстракторы поддерживают лишь JSON Schema, OpenAPI/tool
    # manifests и известные policy/eval-поля. Путь засчитывается, только если
    # один из структурных экстракторов действительно распознал содержимое и
    # выпустил по нему наблюдение. Поэтому битый JSON и посторонний data dump не
    # улучшают coverage, а поддерживаемая схема или конфигурация — улучшают.
    structured_paths = {
        fact.path for fact in (*contract_facts, *policy_facts) if fact.path
    }
    coverage = _coverage(tree, parsed, structured_paths)
    if failed:
        notes.append(f"Не удалось прочитать файлов: {len(failed)}")
    skipped_kinds = _skipped_summary(tree, parsed)
    if skipped_kinds:
        notes.append("Не анализировались: " + ", ".join(skipped_kinds))
    outside = [p for p in tree.files if not is_vendor(p) and is_out_of_perimeter(p)]
    if outside:
        groups = sorted({_ext_group(p) for p in outside})
        notes.append(
            f"Вне периметра первой версии — {len(outside)} файл(ов) "
            f"({', '.join(groups[:6])}): покрытие считается по backend-периметру, "
            f"фронтенд статически не разбирается."
        )

    return Extraction(facts=facts, coverage=coverage, parse_failures=failed, notes=notes)


def _coverage(
    tree: FileTree,
    parsed: list[str],
    structured_paths: set[str] | None = None,
) -> Coverage:
    meaningful = [
        p for p in tree.files
        if not is_vendor(p) and not is_out_of_perimeter(p)
    ]
    analyzed = set(parsed) | set(structured_paths or ())
    for p in meaningful:
        base = p.rsplit("/", 1)[-1].lower()
        if (
            base in {"pyproject.toml", "package.json", "codeowners"}
            or base.startswith("requirements")
            or base.startswith(".env")
            or p.lower().endswith((".yaml", ".yml", ".toml", ".ini", ".cfg"))
        ):
            analyzed.add(p)

    # В числитель попадают только файлы периметра: `parsed` их и так не
    # содержит, но конфигурация добавлялась выше без проверки.
    analyzed = {p for p in analyzed if p in set(meaningful)}
    skipped = sorted({_ext_group(p) for p in meaningful if p not in analyzed})
    return Coverage(
        files_total=len(meaningful),
        files_analyzed=len(analyzed),
        analyzed_globs=list(ANALYZED_GLOBS),
        skipped_globs=skipped[:20],
    )


def _skipped_summary(tree: FileTree, parsed: list[str]) -> list[str]:
    groups: dict[str, int] = {}
    for p in tree.files:
        if is_vendor(p) or is_out_of_perimeter(p) or p in set(parsed):
            continue
        ext = _ext_group(p)
        if ext in {"*.py", "*.toml", "*.yaml", "*.yml", "*.json"}:
            continue
        groups[ext] = groups.get(ext, 0) + 1
    top = sorted(groups.items(), key=lambda kv: -kv[1])[:5]
    return [f"{ext} ({n})" for ext, n in top if n >= 3]


def _ext_group(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return "без расширения"
    return "*." + base.rsplit(".", 1)[-1].lower()
