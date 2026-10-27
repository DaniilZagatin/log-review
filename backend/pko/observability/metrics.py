"""Metric definitions with readiness decomposed into explicit reasons.

Definitions and formulas stay data; nothing is evaluated. Readiness is set by
code from the definition's dependencies, never by the model. A metric that needs
a fact missing from the list registers it as an unassessed requirement; there
is one such reconciliation round, so metrics, facts and changes cannot loop.
"""
from __future__ import annotations

import re

from pko.observability.context import text
from pko.observability.facts import LIFECYCLES
from pko.observability.model_stage import StageModel

STAGE = "metric_catalog"
COUNTING_UNITS = ("case", "operation_execution", "attempt", "event", "other")
MAX_METRICS = 15
_METRIC_ID = re.compile(r"m-[1-9]\d{0,2}")
_STRINGS = {"type": "array", "items": {"type": "string"}}
RULES = ("open_cases", "late_events", "retries", "duplicates")
READINESS = ("methodology", "facts", "semantics", "correlation", "data", "calculator")

TOOL = {
    "name": "submit_metric_definitions",
    "description": "Вернуть определения метрик для вопросов измерения, без значений.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["metrics"], "properties": {
        "metrics": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["id", "name", "purpose", "question_ids", "counting_unit", "measure_unit",
                                 "definition", "population", "window", "fact_ids"],
                    "properties": {
                        "id": {"type": "string"}, "name": {"type": "string"}, "purpose": {"type": "string"},
                        "question_ids": _STRINGS,
                        "counting_unit": {"type": "string", "enum": list(COUNTING_UNITS)},
                        "measure_unit": {"type": "string"}, "definition": {"type": "string"},
                        "population": {"type": "string"}, "window": {"type": "string"},
                        "start_fact_id": {"type": "string"}, "finish_fact_id": {"type": "string"},
                        "rules": {"type": "object", "additionalProperties": False,
                                  "properties": {name: {"type": "string"} for name in RULES}},
                        "fact_ids": _STRINGS, "change_ids": _STRINGS,
                        "missing_facts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                          "required": ["meaning", "lifecycle"], "properties": {
                                              "meaning": {"type": "string"},
                                              "lifecycle": {"type": "string", "enum": list(LIFECYCLES)},
                                              "operation_ids": _STRINGS}}},
                        "open_questions": _STRINGS}}}}},
}


def readiness(metric: dict, coverage: dict, runtime: dict | None) -> dict:
    """Each component is a reason; no component can be satisfied by the model."""
    rows = [coverage[fact_id] for fact_id in metric["fact_ids"] if fact_id in coverage]
    missing = [fact["meaning"] for fact in metric["missing_facts"]]
    unobservable = [row["fact_id"] for row in rows if row["axes"]["emit"] != "found"]
    meaning = [row["fact_id"] for row in rows if row["axes"]["emit"] == "found" and row["axes"]["semantics"] != "compatible"]
    correlation = [row["fact_id"] for row in rows if row["axes"]["emit"] == "found" and row["axes"]["correlation"] != "identified"]
    components = {
        "methodology": "proposed",
        "facts": "missing" if missing or unobservable else "available",
        "semantics": "unconfirmed" if meaning or unobservable else "confirmed_in_code",
        "correlation": "unestablished" if correlation or unobservable else "identified",
        "data": "not_provided" if runtime is None else "sample_only",
        "calculator": "not_implemented"}
    blockers = ["Методика не согласована уполномоченным специалистом."]
    if missing:
        blockers.append("Нужны факты, которых нет в перечне: " + "; ".join(missing) + ".")
    if unobservable:
        blockers.append("Факты не наблюдаются в коде: " + ", ".join(unobservable) + ".")
    if meaning:
        blockers.append("Смысл записи не соответствует нужной границе: " + ", ".join(meaning) + ".")
    if correlation:
        blockers.append("Не установлена связь записи с экземпляром процесса: " + ", ".join(correlation) + ".")
    blockers.append("Данные выполнения не переданы." if runtime is None else
                    "Передана только выборка записей; полнота за период не установлена.")
    blockers.append("Вычисление метрик не реализовано; значения не рассчитываются.")
    return {"status": "not_ready", "components": components, "blockers": blockers}


def build_metrics(model: dict, report: dict, stage_model: StageModel) -> tuple[list | None, list[str]]:
    context = report["process_context"]
    questions = {question["id"] for question in context["measurement_questions"]}
    facts = {fact["id"]: fact for fact in report["facts"]}
    changes = {change["id"] for change in report.get("instrumentation") or []}
    operations = {node["id"] for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    coverage = {row["fact_id"]: row for row in report["coverage"]["facts"]}
    payload = {"process_context": {key: context[key] for key in ("goal", "outcomes", "case", "measurement_questions")},
               "facts": [{**fact, "coverage": coverage.get(fact["id"], {}).get("axes")} for fact in facts.values()],
               "changes": [{"id": change["id"], "action": change["action"], "fact_ids": change["fact_ids"],
                            "change": change["change"]} for change in report.get("instrumentation") or []]}

    def validate(arguments):
        items = arguments.get("metrics")
        if not isinstance(items, list):
            return None, ["metrics: ожидается список"]
        accepted, errors, seen = [], [], set()
        for item in items[:MAX_METRICS]:
            if not isinstance(item, dict) or not _METRIC_ID.fullmatch(str(item.get("id"))) or item["id"] in seen:
                errors.append("metrics: у каждой метрики уникальный id вида m-N")
                continue
            label, problems = item["id"], []
            if "value" in item:
                problems.append(f"{label}: значение метрики не задаётся")
            for name in ("name", "purpose", "measure_unit", "definition", "population", "window"):
                if not text(item.get(name)):
                    problems.append(f"{label}: заполни {name}")
            if item.get("counting_unit") not in COUNTING_UNITS:
                problems.append(f"{label}: counting_unit — значение из перечня")
            if not item.get("question_ids") or any(ref not in questions for ref in item["question_ids"]):
                problems.append(f"{label}: question_ids — существующие вопросы измерения")
            fact_ids = item.get("fact_ids") or []
            missing = item.get("missing_facts") or []
            if any(ref not in facts for ref in fact_ids) or (not fact_ids and not missing):
                problems.append(f"{label}: fact_ids — существующие факты; недостающие опиши в missing_facts")
            for name in ("start_fact_id", "finish_fact_id"):
                if item.get(name) and item[name] not in fact_ids:
                    problems.append(f"{label}: {name} должен входить в fact_ids")
            if any(ref not in changes for ref in item.get("change_ids") or []):
                problems.append(f"{label}: change_ids — существующие доработки")
            for fact in missing:
                if (not isinstance(fact, dict) or not text(fact.get("meaning")) or fact.get("lifecycle") not in LIFECYCLES
                        or any(ref not in operations for ref in fact.get("operation_ids") or [])):
                    problems.append(f"{label}: missing_facts — meaning, lifecycle из перечня и операции карты")
            errors += problems
            if problems:
                continue
            seen.add(label)
            rules = item.get("rules") if isinstance(item.get("rules"), dict) else {}
            accepted.append({
                "id": label, "name": text(item["name"], 200), "purpose": text(item["purpose"], 1000),
                "question_ids": list(dict.fromkeys(item["question_ids"])), "counting_unit": item["counting_unit"],
                "measure_unit": text(item["measure_unit"], 100), "definition": text(item["definition"], 1500),
                "population": text(item["population"], 1000), "window": text(item["window"], 500),
                "start_fact_id": item.get("start_fact_id") or None, "finish_fact_id": item.get("finish_fact_id") or None,
                "rules": {name: text(rules.get(name), 500) for name in RULES},
                "fact_ids": list(dict.fromkeys(fact_ids)), "change_ids": list(dict.fromkeys(item.get("change_ids") or [])),
                "missing_facts": [{"meaning": text(fact["meaning"], 500), "lifecycle": fact["lifecycle"],
                                   "operation_ids": list(dict.fromkeys(fact.get("operation_ids") or []))} for fact in missing],
                "open_questions": [text(value, 500) for value in item.get("open_questions") or [] if text(value)][:10],
                "value": None, "value_reason": "Значения метрик в этой версии не вычисляются.",
                "provenance": "model_proposal", "authority_status": "proposed"})
        return accepted, errors

    return stage_model.ask(STAGE, payload, TOOL, validate)


def register_missing_facts(report: dict) -> list[dict]:
    """The single reconciliation round: requested facts join the list as unassessed."""
    requested = []
    for metric in report.get("metrics") or []:
        for index, fact in enumerate(metric["missing_facts"], 1):
            requested.append({"id": f"{metric['id']}-need-{index}", "requested_by": metric["id"], **fact,
                              "status": "unassessed", "provenance": "model_proposal", "authority_status": "proposed"})
    return requested
