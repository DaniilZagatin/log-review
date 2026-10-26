"""Объявленные контракты: OpenAPI, JSON Schema, манифесты инструментов.

Точка входа существует не только как декоратор в коде. В половине систем она
описана раньше кода — в спецификации OpenAPI, в манифесте инструментов агента,
в схеме события. Пока PKO читал только Python, такие системы выглядели
безинтерфейсными: проверка «траектория восстанавливается» падала не потому, что
траектории нет, а потому, что она объявлена не в том файле.

Что здесь важно и чего здесь нет: объявление — не доказательство исполнения.
Путь `POST /orders` в OpenAPI доказывает, что интерфейс объявлен, и ничего не
говорит о том, реализован ли он. Поэтому все факты отсюда идут с
`gate_eligible=False`: они попадают в паспорта, в машинный срез и в пробелы, но
допуск на них не выдаётся. Иначе репозиторий с одной спецификацией и без кода
проходил бы проверку «траектория восстанавливается из реализации» — ровно тот
случай, ради которого проверка и существует.

Разбор намеренно поверхностный: YAML читается подмножеством `pko.util.yamlmini`,
JSON — стандартным модулем. Ни один файл не исполняется.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from pko.extractors.base import Fact, FileTree, is_vendor
from pko.model import taxonomy
from pko.util.yamlmini import YamlSubsetError, loads

# Методы HTTP, которые считаются объявлением точки входа.
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# Манифесты инструментов агента: имя файла — не доказательство, содержимое —
# да, поэтому проверяется структура, а не только имя.
TOOL_HINTS = ("tools", "toolset", "functions", "manifest", "plugins")

MAX_ITEMS = 60


def extract(tree: FileTree) -> list[Fact]:
    facts: list[Fact] = []
    for path in tree.files:
        if is_vendor(path):
            continue
        lower = path.lower()
        if not lower.endswith((".yaml", ".yml", ".json")):
            continue
        data = _load(tree, path)
        if not isinstance(data, dict):
            continue
        # Один документ может одновременно иметь собственную `$schema` и быть
        # tool manifest. Распознаватели независимы: формат валидации документа
        # не отменяет его прикладной смысл.
        if _is_openapi(data):
            facts.extend(_openapi_facts(path, data))
        if _is_json_schema(data):
            facts.extend(_schema_facts(path, data))
        if _is_tool_manifest(path, data):
            facts.extend(_tool_facts(path, data))
    return facts


# --- OpenAPI ---------------------------------------------------------------
def _is_openapi(data: dict[str, Any]) -> bool:
    """Спецификацию опознаём по структуре, а не по имени файла."""
    return bool(data.get("openapi") or data.get("swagger")) and isinstance(
        data.get("paths"), dict
    )


def _openapi_facts(path: str, data: dict[str, Any]) -> Iterator[Fact]:
    paths = data.get("paths")
    if not isinstance(paths, dict):
        return
    title = _title(data)
    count = 0
    for route, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() not in HTTP_METHODS:
                continue
            count += 1
            if count > MAX_ITEMS:
                return
            summary = ""
            if isinstance(operation, dict):
                summary = str(operation.get("summary") or operation.get("operationId") or "")
            yield Fact(
                kind=taxonomy.ENTRYPOINT,
                key=f"{method.upper()} {route}",
                value=summary or route,
                path=path,
                line=1,
                basis=f"объявлен в спецификации {title}: {method.upper()} {route}",
                category=taxonomy.ENTRYPOINT,
                action="serve",
                mechanism="http_server",
                gate_eligible=False,
            )


def _title(data: dict[str, Any]) -> str:
    info = data.get("info")
    if isinstance(info, dict) and info.get("title"):
        return str(info["title"])
    return "OpenAPI"


# --- JSON Schema -----------------------------------------------------------
def _is_json_schema(data: dict[str, Any]) -> bool:
    # `$schema` указывает, по какой meta-schema валидируется *этот документ*;
    # поле встречается и в манифестах. Схемой данных считаем только документ,
    # который действительно описывает свойства объекта.
    properties = data.get("properties")
    return isinstance(properties, dict) and bool(
        data.get("$schema") or data.get("type") == "object"
    )


def _schema_facts(path: str, data: dict[str, Any]) -> Iterator[Fact]:
    """Схема данных — объявленный артефакт, а не эффект и не состояние.

    Она отвечает на вопрос «какими данными оперирует процесс» и ничего не
    говорит о том, кто их читает и меняет. Числить её состоянием значило бы
    утверждать переход, которого никто не наблюдал.
    """
    properties = data.get("properties")
    if not isinstance(properties, dict):
        return
    name = str(data.get("title") or path.rsplit("/", 1)[-1])
    required = data.get("required")
    required_count = len(required) if isinstance(required, list) else 0
    yield Fact(
        kind=taxonomy.ARTIFACT,
        key=f"schema:{name}",
        value=sorted(str(k) for k in properties)[:MAX_ITEMS],
        path=path,
        line=1,
        basis=(f"объявлена схема данных «{name}»: полей {len(properties)}, "
               f"обязательных {required_count}"),
        category=taxonomy.ARTIFACT,
        action="declare",
        mechanism="config",
        gate_eligible=False,
    )


# --- манифесты инструментов ------------------------------------------------
def _is_tool_manifest(path: str, data: dict[str, Any]) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if not any(hint in base for hint in TOOL_HINTS):
        return False
    return isinstance(_tool_list(data), list)


def _tool_list(data: dict[str, Any]) -> Any:
    for key in ("tools", "functions", "plugins"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return None


def _tool_facts(path: str, data: dict[str, Any]) -> Iterator[Fact]:
    """Инструмент агента — объявленный шаг траектории.

    Именно из манифеста видно, что система вообще умеет делать: без него
    остаётся только «модель что-то вызывает». Действие `call` здесь означает
    объявленную возможность вызова, не сам вызов.
    """
    tools = _tool_list(data) or []
    for entry in tools[:MAX_ITEMS]:
        name, description = _tool_name(entry)
        if not name:
            continue
        yield Fact(
            kind="TOOL",
            key=name,
            value=description or name,
            path=path,
            line=1,
            basis=f"объявлен инструмент агента «{name}» в манифесте",
            category=taxonomy.STEP,
            action="call",
            mechanism="agent_tool",
            gate_eligible=False,
        )


def _tool_name(entry: Any) -> tuple[str, str]:
    if isinstance(entry, str):
        return entry, ""
    if not isinstance(entry, dict):
        return "", ""
    # Формат OpenAI-совместимых манифестов: вложенный объект `function`.
    inner = entry.get("function") if isinstance(entry.get("function"), dict) else entry
    name = str(inner.get("name") or inner.get("id") or "")
    return name, str(inner.get("description") or "")


def _load(tree: FileTree, path: str) -> Any:
    """Прочитать файл. Нечитаемый файл — не ошибка прогона: он просто не даёт фактов."""
    text = tree.read(path)
    if text is None:
        return None
    try:
        if path.lower().endswith(".json"):
            return json.loads(text)
        return loads(text)
    except (json.JSONDecodeError, YamlSubsetError, ValueError):
        return None
