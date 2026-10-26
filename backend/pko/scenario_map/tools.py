"""Provider-compatible tools for the single scenario investigator.

Read-only repository tools are supplied by the runner. These tools change the
in-memory map only and cannot write to the inspected sources.
"""
from __future__ import annotations

from typing import Any

from .schema import EDGE_KINDS, EVIDENCE_STATUSES, EXECUTION_CHECKS, INSPECT_SECTIONS, OPERATION_KINDS, STEP_KINDS


def _string(description: str = "") -> dict[str, Any]:
    return {"type": "string", "description": description}


def _list(description: str = "") -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


EVIDENCE = {
    "type": "array", "description": "Буквальные цитаты из уже полученных read_file строк; без номеров строк в quote. Весь заявленный диапазон должен совпадать с quote, включая маскировку секретов.",
    "maxItems": 12,
    "items": {"type": "object", "additionalProperties": False,
              "properties": {"path": _string("Путь source-N/..."),
                             "line_start": {"type": "integer", "minimum": 1},
                             "line_end": {"type": "integer", "minimum": 1},
                             "quote": _string("Полный буквальный текст указанного диапазона строк")},
              "required": ["path", "line_start", "line_end", "quote"]},
}
SUPPORT = {"evidence_status": {"type": "string", "enum": list(EVIDENCE_STATUSES),
                               "description": "observed: непосредственно видно в коде; inferred: вывод из цитат; unknown: данных недостаточно, необходим адресный gap"},
           "evidence": EVIDENCE}
EDIT_ID = _string("Для изменения передайте существующий id; для нового объекта опустите. ID назначает код. Все остальные поля передавайте целиком.")
OPERATION = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "node_id": _string("Отдельный узел именно этой операции; две операции не могут ссылаться на один крупный шаг"),
        "name": _string("Краткое название одного действия"),
        "kind": {"type": "string", "enum": list(OPERATION_KINDS)},
        "action": _string("Одно минимальное исполняемое действие, объект и причина отдельного контроля"),
        "effect": _string("Один проверяемый результат или изменение состояния; отделяйте отправку запроса от эффекта в получателе"),
        "failure": _string("Самостоятельная точка отказа и наблюдаемая обработка; если не установлено, скажите это и используйте unknown в checks.failures с gap"),
        "condition": _string("Условие выполнения; не смешивайте с успешным эффектом"),
        "evidence": EVIDENCE,
    }, "required": ["node_id", "name", "kind", "action", "effect", "failure", "evidence"],
}
EXECUTION_CHECK = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["present", "not_found", "unknown"],
                   "description": "present — найдено и показано на карте; not_found — не найдено только в обследованном участке, не доказанное отсутствие; unknown — недостаточно данных"},
        "explanation": _string("Что именно проверено в прочитанном исполнении и какой вывод сделан"),
        "evidence": EVIDENCE,
        "node_ids": _list("Узлы, показывающие найденное поведение в данной АС"),
        "edge_ids": _list("Переходы, относящиеся к обследованным операциям"),
        "gap_ids": _list("Обязательно для unknown; пробел должен адресовать эти операции или АС"),
    }, "required": ["status", "explanation", "evidence"],
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


TOOLS = [
    _tool("set_scope", "Зафиксировать границы выбранного варианта сценария по входному описанию и коду. Укажите допущения явно; не подменяйте исходный сценарий.",
          {"title": _string(), "start": _string("Событие начала"), "end": _string("Допустимые результаты завершения"),
           "exclusions": _list("Соседние сценарии вне исследования"), "assumptions": _list("Неустановленные границы и допущения")},
          ["title", "start", "end"]),
    _tool("upsert_system", "Добавить или полностью обновить АС. Директория не равна АС: source_ids может содержать несколько источников, источник может относиться к нескольким АС.",
          {"id": EDIT_ID, "name": _string(), "source_ids": _list(), "description": _string(), **SUPPORT},
          ["name", "source_ids", "evidence_status", "evidence"]),
    _tool("upsert_step", "Добавить или полностью обновить отдельную атомарную операцию либо управляющий узел. Отдельные проверки, чтения, расчёты, записи, отправки, приём и обработка отказа должны быть отдельными узлами, если имеют свой эффект/точку отказа. Не сворачивайте несколько действий в бизнес-блок и не делите автоматически по функциям. parent_id — только группировка. Для неизвестной АС допустим system_id='unknown'.",
          {"id": EDIT_ID, "name": _string(), "kind": {"type": "string", "enum": list(STEP_KINDS)},
           "system_id": _string(), "description": _string(), "input": _string(), "output": _string(),
           "condition": _string(), "parent_id": _string("Существующий шаг, который этот шаг детализирует; иначе опустите"), **SUPPORT},
          ["name", "kind", "system_id", "description", "evidence_status", "evidence"]),
    _tool("upsert_transition", "Добавить/обновить переход. data — передача данных без утверждения порядка исполнения. Циклы разрешены. Любой observed межсистемный переход, независимо от kind, требует цитат отправителя и получателя, адресного channel и содержательного contract; совпадение имён само по себе связь не доказывает. Без этого используйте inferred/unknown.",
          {"id": EDIT_ID, "source": _string(), "target": _string(), "kind": {"type": "string", "enum": list(EDGE_KINDS)},
           "description": _string(), "condition": _string("Обязательно для branch"), "data": _string("Передаваемые данные"),
           "correlation": _string("Как сохраняется идентификатор экземпляра сценария"),
           "channel": _string("Маршрут/API или канал доставки сообщения и механизм сопоставления сторон"),
           "contract": _string("Проверенный формат сообщения/запроса, вызываемая операция и соответствие обработчику"),
           "sender_evidence": EVIDENCE, "receiver_evidence": EVIDENCE, **SUPPORT},
          ["source", "target", "kind", "description", "evidence_status", "evidence"]),
    _tool("add_gap", "Зафиксировать недостающие материалы, неизвестное поведение или неподтверждённый переход. Для неизвестного перехода укажите оба шага. Для неизвестной АС, включая АС без шагов, укажите system_ids. После разрешения удалите gap через remove_element.",
          {"description": _string(), "node_ids": _list(), "system_ids": _list()}, ["description"]),
    _tool("review_source", "Зафиксировать исследование источника либо мотивированное исключение. reviewed требует чтения кода и означает рассмотренный материал, а не доказательство полного покрытия. out_of_scope без чтения допускается только как допущение и делает результат partial; после изучения обновите review_source.",
          {"source_id": _string(), "disposition": {"type": "string", "enum": ["reviewed", "out_of_scope", "unavailable"]},
           "reason": _string("Что исследовано или почему источник исключён/недоступен")}, ["source_id", "disposition", "reason"]),
    _tool("review_execution", "Сохранить или полностью обновить обследование исполнения и перечень атомарных операций. Все action/decision/wait/external с известной реализацией должны иметь отдельную запись и node_id. Нельзя покрыть две операции одним крупным узлом. Один обзор может охватывать несколько функций одной АС; цитаты операций и проверок входят в его прочитанную область evidence. Обязательны проверки ветвей, отказов, интеграций, изменений состояния и вычислений. После правок узлов/связей обновите обзор. Код проверяет покрытие перечня, не семантическую полноту обнаружения.",
          {"id": EDIT_ID, "name": _string("Обследованный участок исполнения выбранного сценария"), "system_id": _string(),
           "evidence": EVIDENCE,
           "operations": {"type": "array", "items": OPERATION, "minItems": 1, "maxItems": 100},
           "checks": {"type": "object", "additionalProperties": False,
                      "properties": {name: EXECUTION_CHECK for name in EXECUTION_CHECKS}, "required": list(EXECUTION_CHECKS)}},
          ["name", "system_id", "evidence", "operations", "checks"]),
    _tool("remove_element", "Удалить ошибочный элемент карты. Если на него ссылаются другие элементы, сначала обновите или удалите эти ссылки. Удаление не каскадное.",
          {"kind": {"type": "string", "enum": ["system", "step", "transition", "gap", "execution_review"]}, "id": _string()}, ["kind", "id"]),
    _tool("inspect_map", "Получить сводку или раздел карты постранично. По умолчанию summary с количеством объектов и замечаниями. Для восстановления ID после сокращения контекста читайте systems/nodes/edges. Длинные поля сокращены, цитаты показаны ссылками; полный результат сохранён.",
          {"section": {"type": "string", "enum": list(INSPECT_SECTIONS), "default": "summary"},
           "offset": {"type": "integer", "minimum": 1, "default": 1},
           "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}}, []),
    _tool("finish", "Завершить исследование после проверки карты. Нужны границы, начальные/конечные шаги, рассмотренные источники, связи либо адресные gaps и актуальный review_execution с покрытием всех выявленных операций. Крупные шаги без отдельного перечня операций не завершают исследование. Частичный результат допустим, неизвестное не маскируется под доказанное.",
          {"summary": _string("Что восстановлено, основные границы и ограничения; без процентов готовности")}, ["summary"]),
]
