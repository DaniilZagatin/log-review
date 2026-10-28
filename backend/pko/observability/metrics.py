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
READINESS = ("methodology", "definition", "facts", "semantics", "correlation", "data", "calculator")

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
                        "profile_direction": {"type": "string"},
                        "open_questions": _STRINGS}}}}},
}


# Why a fact cannot yet feed a metric, by coverage axis and value.
GAP_TEXT = {
    ("emit", "not_found"): "запись не найдена в прочитанном коде",
    ("emit", "unexplained_candidates"): "есть места логирования, смысл которых не установлен",
    ("semantics", "incompatible"): "смысл найденной записи не соответствует нужной границе",
    ("semantics", "unknown"): "смысл записи не установлен",
    ("fields", "missing"): "в записи нет нужных полей",
    ("fields", "partial"): "в записи есть не все нужные поля",
    ("fields", "unknown"): "полнота полей записи не установлена",
    ("correlation", "missing"): "запись не связана с экземпляром процесса",
    ("correlation", "unknown"): "связь записи с экземпляром не установлена",
    ("timing", "premature"): "запись делается раньше, чем результат установлен",
    ("timing", "unknown"): "момент записи не установлен",
    ("time_source", "missing"): "нет времени события",
    ("time_source", "unknown"): "источник времени не установлен",
    ("runtime", "no_records"): "в переданных логах нет записей с этим ключом",
    ("runtime", "key_unknown"): "ключ записи не установлен, наличие в логах не проверено",
    ("quality", "issues"): "в переданных логах есть нарушения качества",
    ("quality", "unknown"): "качество записей этого события не установлено",
}
_AXIS_COMPONENTS = {"semantics": ("semantics",), "correlation": ("correlation",), "timing": ("timing", "time_source"),
                    "fields": ("fields",)}


def readiness(metric: dict, coverage: dict, runtime: dict | None, facts: dict | None = None) -> dict:
    """Each component is a reason; every coverage gap of every fact is named.

    No component can be satisfied by the model; the calculator does not exist,
    so a metric is never ready in this version.
    """
    facts = facts or {}
    rows = [coverage[fact_id] for fact_id in metric["fact_ids"] if fact_id in coverage]
    missing = [fact["meaning"] for fact in metric["missing_facts"]]
    undefined = not metric["fact_ids"] and not missing
    components = {"methodology": "proposed",
                  "facts": "available" if rows and not missing and all(row["observable_in_code"] for row in rows)
                  else "incomplete"}
    for component, axes in _AXIS_COMPONENTS.items():
        components[component] = "gaps" if any(
            (axis, row["axes"].get(axis)) in GAP_TEXT for row in rows for axis in axes) or not rows else "established"
    components.update(data="not_provided" if runtime is None else "sample_only", calculator="not_implemented")
    blockers = ["Методика не согласована уполномоченным специалистом."]
    definition_gaps = []
    rule_names = {"open_cases": "незавершённых экземпляров", "late_events": "поздних записей",
                  "retries": "повторных попыток", "duplicates": "повторных доставок"}
    for rule, label in rule_names.items():
        if not text((metric.get("rules") or {}).get(rule)):
            definition_gaps.append(f"Не задано правило учёта {label}; требуется решение владельца методики.")
    if metric.get("requires_interval"):
        for key, label in (("start_fact_id", "начала"), ("finish_fact_id", "окончания")):
            if not metric.get(key):
                definition_gaps.append(f"Для длительности не определён факт {label} интервала.")
        if metric.get("start_fact_id") and metric.get("start_fact_id") == metric.get("finish_fact_id"):
            definition_gaps.append("Границы длительности ссылаются на один факт; различение начала и окончания не определено.")
    components["definition"] = "incomplete" if definition_gaps else "specified"
    blockers.extend(definition_gaps)
    if undefined:
        blockers.append("Факты для этого направления не определены.")
    if missing:
        blockers.append("Нужны факты, которых нет в перечне: " + "; ".join(missing) + ".")
    for row in rows:
        reasons = [GAP_TEXT[(axis, row["axes"][axis])] for axis in row["axes"] if (axis, row["axes"][axis]) in GAP_TEXT
                   and (axis not in ("runtime", "quality") or runtime is not None)]
        if reasons:
            meaning = facts.get(row["fact_id"], {}).get("meaning")
            blockers.append(f"{row['fact_id']}" + (f" ({meaning})" if meaning else "") + ": " + "; ".join(reasons) + ".")
    blockers.append("Данные выполнения не переданы." if runtime is None else
                    "Передана только выборка записей; полнота за период не установлена.")
    blockers.append("Вычисление метрик не реализовано; значения не рассчитываются.")
    return {"status": "not_ready", "components": components, "blockers": blockers}


def profile_gaps(metrics: list[dict], report: dict, profile: dict | None) -> list[dict]:
    """Profile directions the model left out stay in the catalog with their known definition."""
    if not profile:
        return []
    covered = {metric.get("profile_direction") for metric in metrics}
    questions = {row.get("profile_question_id"): row["id"] for row in report["process_context"]["measurement_questions"]}
    number = max((int(metric["id"][2:]) for metric in metrics if metric["id"][2:].isdigit()), default=0)
    added = []
    for direction in profile["metric_directions"]:
        if direction["id"] in covered:
            continue
        number += 1
        added.append({"id": f"m-{number}", "name": direction["name"], "purpose": direction["known"],
                      "question_ids": [questions[direction["question_id"]]] if direction["question_id"] in questions else [],
                      "counting_unit": "other", "measure_unit": "", "definition": direction["known"],
                      "population": "", "window": "", "start_fact_id": None, "finish_fact_id": None,
                      "rules": dict.fromkeys(RULES, ""), "fact_ids": [], "change_ids": [],
                      "requires_interval": any(q.get("profile_question_id") == direction["question_id"]
                                               and q.get("family") == "duration" for q in report["process_context"]["measurement_questions"]),
                      "missing_facts": [], "open_questions": list(direction["open_points"]),
                      "profile_direction": direction["id"], "source": "profile",
                      "value": None, "value_reason": "Значения метрик в этой версии не вычисляются.",
                      "provenance": "domain_profile", "authority_status": "proposed"})
    return added


def build_metrics(model: dict, report: dict, stage_model: StageModel, profile: dict | None = None) -> tuple[list | None, list[str]]:
    context = report["process_context"]
    questions = {question["id"] for question in context["measurement_questions"]}
    facts = {fact["id"]: fact for fact in report["facts"]}
    changes = {change["id"] for change in report.get("instrumentation") or []}
    operations = {node["id"] for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    coverage = {row["fact_id"]: row for row in report["coverage"]["facts"]}
    payload = {"process_context": {key: context[key] for key in ("goal", "outcomes", "case", "measurement_questions")},
               "facts": [{**fact, "coverage": coverage.get(fact["id"], {}).get("axes")} for fact in facts.values()],
               "changes": [{"id": change["id"], "action": change["action"], "fact_ids": change["fact_ids"],
                            "change": change["change"]} for change in report.get("instrumentation") or []],
               "domain_metric_directions": profile["metric_directions"] if profile else []}
    directions = {row["id"] for row in profile["metric_directions"]} if profile else set()
    duration_questions = {row["id"] for row in context["measurement_questions"] if row.get("family") == "duration"}
    omitted = 0

    def validate(arguments):
        nonlocal omitted
        items = arguments.get("metrics")
        if not isinstance(items, list):
            return None, ["metrics: ожидается список"]
        accepted, errors, seen = [], [], set()
        omitted = max(omitted, max(0, len(items) - MAX_METRICS))
        if omitted:
            errors.append(f"metrics: лимит {MAX_METRICS}; не рассмотрено определений: {omitted}.")
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
            if item.get("profile_direction") and item["profile_direction"] not in directions:
                problems.append(f"{label}: profile_direction — идентификатор направления из domain_metric_directions")
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
                "requires_interval": bool(duration_questions.intersection(item["question_ids"]) or
                                          item.get("start_fact_id") or item.get("finish_fact_id")),
                "fact_ids": list(dict.fromkeys(fact_ids)), "change_ids": list(dict.fromkeys(item.get("change_ids") or [])),
                "missing_facts": [{"meaning": text(fact["meaning"], 500), "lifecycle": fact["lifecycle"],
                                   "operation_ids": list(dict.fromkeys(fact.get("operation_ids") or []))} for fact in missing],
                "open_questions": [text(value, 500) for value in item.get("open_questions") or [] if text(value)][:10],
                "profile_direction": item.get("profile_direction") or None,
                "value": None, "value_reason": "Значения метрик в этой версии не вычисляются.",
                "provenance": "model_proposal", "authority_status": "proposed"})
        return accepted, errors

    result, errors = stage_model.ask(STAGE, payload, TOOL, validate, key=lambda metric: metric["id"])
    if result and len(result) > MAX_METRICS:
        omitted = max(omitted, len(result) - MAX_METRICS)
        result = result[:MAX_METRICS]
    if omitted:
        errors = [*errors, f"Каталог ограничен {MAX_METRICS} определениями; не рассмотрено из ответа: {omitted}."]
    return result, errors


def register_missing_facts(report: dict) -> list[dict]:
    """The single reconciliation round: requested facts join the list as unassessed."""
    requested = []
    for metric in report.get("metrics") or []:
        for index, fact in enumerate(metric["missing_facts"], 1):
            requested.append({"id": f"{metric['id']}-need-{index}", "requested_by": metric["id"], **fact,
                              "status": "unassessed", "provenance": "model_proposal", "authority_status": "proposed"})
    return requested
