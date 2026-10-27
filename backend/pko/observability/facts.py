"""Required business facts: a versioned list of proposed measurement obligations.

Facts reference measurement questions, map operations and confirmed logging
records of those operations. Field sources must name fields the record's call
actually passes. Every fact stays a proposal until a specialist accepts the list.
"""
from __future__ import annotations

import re

from pko.observability.context import confirmed_bindings, operations_view, text
from pko.observability.model_stage import StageModel

STAGE = "fact_requirements"
LIFECYCLES = ("request", "queued", "start", "result", "refusal", "error", "retry",
              "cancellation", "compensation", "state_change")
APPLICABILITY = ("applicable", "conditional", "unknown")
NEEDS = ("case_id", "timestamp", "result", "reason", "actor", "channel", "amount", "other")
MAX_FACTS = 30
_FACT_ID = re.compile(r"f-[1-9]\d{0,2}")
_STRINGS = {"type": "array", "items": {"type": "string"}}

TOOL = {
    "name": "submit_fact_requirements",
    "description": "Вернуть бизнес-факты, необходимые для вопросов измерения, и записи, которые их уже фиксируют.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["facts"], "properties": {
        "facts": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                  "required": ["id", "meaning", "question_ids", "lifecycle", "operation_ids", "applicability",
                               "needed", "observed_by", "rationale"],
                  "properties": {
                      "id": {"type": "string"}, "meaning": {"type": "string"}, "question_ids": _STRINGS,
                      "lifecycle": {"type": "string", "enum": list(LIFECYCLES)}, "operation_ids": _STRINGS,
                      "outside_application": {"type": "boolean"},
                      "applicability": {"type": "string", "enum": list(APPLICABILITY)},
                      "condition": {"type": "string"},
                      "needed": {"type": "array", "items": {"type": "string", "enum": list(NEEDS)}},
                      "observed_by": _STRINGS,
                      "field_sources": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                        "required": ["purpose", "binding_id", "field"], "properties": {
                                            "purpose": {"type": "string", "enum": list(NEEDS)},
                                            "binding_id": {"type": "string"}, "field": {"type": "string"}}}},
                      "rationale": {"type": "string"}}}}}},
}


def _validate_fact(item, questions: set, operations: set, bindings: dict) -> tuple[dict | None, list[str]]:
    if not isinstance(item, dict) or not _FACT_ID.fullmatch(str(item.get("id"))):
        return None, ["facts: у каждого факта id вида f-N"]
    label, errors = item["id"], []
    question_ids, operation_ids = item.get("question_ids") or [], item.get("operation_ids") or []
    observed, needed = item.get("observed_by") or [], item.get("needed") or []
    outside = item.get("outside_application") is True
    if not text(item.get("meaning")) or not text(item.get("rationale")):
        errors.append(f"{label}: нужны meaning и rationale")
    if not question_ids or any(ref not in questions for ref in question_ids):
        errors.append(f"{label}: question_ids — существующие вопросы измерения")
    if item.get("lifecycle") not in LIFECYCLES or item.get("applicability") not in APPLICABILITY:
        errors.append(f"{label}: lifecycle и applicability — значения из перечней")
    if item.get("applicability") == "conditional" and not text(item.get("condition")):
        errors.append(f"{label}: для conditional укажи condition")
    if any(ref not in operations for ref in operation_ids):
        errors.append(f"{label}: operation_ids только из карты")
    if outside and (operation_ids or observed):
        errors.append(f"{label}: факт вне приложения не ссылается на операции и записи карты")
    if not outside and not operation_ids:
        errors.append(f"{label}: укажи операции карты или outside_application=true")
    if any(need not in NEEDS for need in needed):
        errors.append(f"{label}: needed — значения из перечня")
    for ref in observed:
        binding = bindings.get(ref)
        if binding is None:
            errors.append(f"{label}: observed_by {ref!r} — только подтверждённые записи из event_bindings")
        elif binding["operation_id"] not in operation_ids:
            errors.append(f"{label}: запись {ref} относится к операции {binding['operation_id']}, которой нет в operation_ids")
    sources = []
    for source in item.get("field_sources") or []:
        binding = bindings.get(source.get("binding_id")) if isinstance(source, dict) else None
        if (binding is None or source.get("binding_id") not in observed or source.get("purpose") not in needed
                or source.get("field") not in binding["fields"]):
            errors.append(f"{label}: field_sources — purpose из needed, binding_id из observed_by и field из полей этой записи")
        else:
            sources.append({"purpose": source["purpose"], "binding_id": source["binding_id"], "field": source["field"]})
    if errors:
        return None, errors
    return {"id": label, "meaning": text(item["meaning"], 500), "question_ids": list(dict.fromkeys(question_ids)),
            "lifecycle": item["lifecycle"], "operation_ids": list(dict.fromkeys(operation_ids)),
            "outside_application": outside, "applicability": item["applicability"],
            "condition": text(item.get("condition"), 500), "needed": list(dict.fromkeys(needed)),
            "observed_by": list(dict.fromkeys(observed)), "field_sources": sources,
            "rationale": text(item["rationale"], 1000), "provenance": "model_proposal",
            "authority_status": "proposed"}, []


def build_facts(model: dict, report: dict, stage_model: StageModel) -> tuple[list | None, list[str]]:
    context = report["process_context"]
    questions = {question["id"] for question in context["measurement_questions"]}
    operations = {node["id"] for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    bindings = {row["binding_id"]: row for row in confirmed_bindings(report)}
    payload = {"process_context": {key: context[key] for key in ("goal", "start", "finish", "outcomes", "case",
                                                                  "measurement_questions")},
               "operations": operations_view(model), "event_bindings": list(bindings.values())}

    def validate(arguments):
        items = arguments.get("facts")
        if not isinstance(items, list):
            return None, ["facts: ожидается список"]
        accepted, errors, seen = [], [], set()
        for item in items[:MAX_FACTS]:
            fact, problems = _validate_fact(item, questions, operations, bindings)
            if fact is not None and fact["id"] in seen:
                fact, problems = None, [f"{fact['id']}: повторяющийся идентификатор"]
            errors += problems
            if fact is not None:
                seen.add(fact["id"])
                accepted.append(fact)
        if len(items) > MAX_FACTS:
            errors.append(f"facts: не больше {MAX_FACTS} фактов")
        return accepted, errors

    return stage_model.ask(STAGE, payload, TOOL, validate)
