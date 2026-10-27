"""Process context and measurement questions proposed over the saved map.

The context separates sources: scenario text, business description, code of
the map and the model's own proposals. A case identifier attributed to code
must be a name that actually appears in the map's cited code or in fields of
confirmed logging records.
"""
from __future__ import annotations

import re

from pko.observability.model_stage import StageModel

STAGE = "process_context"
FAMILIES = ("volume", "duration", "outcome", "quality", "cost", "other")
OUTCOME_KINDS = ("success", "negative", "cancellation", "open")
QUESTION_SOURCES = ("scenario", "business", "proposal")
ALL_QUESTION_SOURCES = QUESTION_SOURCES + ("profile",)
IDENTIFIER_SOURCES = ("code", "business", "hypothesis")
TERM_SOURCES = ("scenario", "business", "code")
MAX_QUESTIONS = 12
_QUESTION_ID = re.compile(r"q-[1-9]\d{0,2}")
_STRINGS = {"type": "array", "items": {"type": "string"}}

TOOL = {
    "name": "submit_process_context",
    "description": "Вернуть цель, границы, исходы, единицу учёта, словарь и вопросы измерения выбранного варианта.",
    "parameters": {"type": "object", "additionalProperties": False,
                   "required": ["goal", "start", "finish", "outcomes", "case", "measurement_questions"],
                   "properties": {
        "goal": {"type": "string"}, "start": {"type": "string"}, "finish": {"type": "string"},
        "outcomes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["name", "kind"], "properties": {
                         "name": {"type": "string"}, "kind": {"type": "string", "enum": list(OUTCOME_KINDS)},
                         "operation_ids": _STRINGS}}},
        "case": {"type": "object", "additionalProperties": False, "required": ["subject"], "properties": {
            "subject": {"type": "string"},
            "identifiers": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                            "required": ["name", "source"], "properties": {
                                "name": {"type": "string"}, "where": {"type": "string"},
                                "source": {"type": "string", "enum": list(IDENTIFIER_SOURCES)}}}}}},
        "glossary": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["term", "meaning", "source"], "properties": {
                         "term": {"type": "string"}, "meaning": {"type": "string"},
                         "source": {"type": "string", "enum": list(TERM_SOURCES)}}}},
        "measurement_questions": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                  "required": ["id", "question", "family", "source"], "properties": {
                                      "id": {"type": "string"}, "question": {"type": "string"},
                                      "family": {"type": "string", "enum": list(FAMILIES)},
                                      "source": {"type": "string", "enum": list(QUESTION_SOURCES)},
                                      "rationale": {"type": "string"}}}},
        "hypotheses": _STRINGS, "open_questions": _STRINGS}},
}


def text(value, limit: int = 1000) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def operations_view(model: dict) -> list[dict]:
    return [{"operation_id": node["id"], "name": node.get("name"), "description": node.get("description"),
             "output": node.get("output"), "outcome": node.get("outcome"), "effect_kind": node.get("effect_kind")}
            for node in model.get("nodes") or [] if node.get("kind") == "operation"]


def confirmed_bindings(report: dict) -> list[dict]:
    sites = {site["id"]: site for site in report["logging"].get("sites") or []}
    return [{"binding_id": row["id"], "operation_id": row["operation_id"], "event_role": row["event_role"],
             "lifecycle_moment": row["lifecycle_moment"], "business_meaning": row["business_meaning"],
             "key_candidate": sites[row["site_id"]].get("event_key"), "key_status": sites[row["site_id"]].get("key_status"),
             "fields": sites[row["site_id"]].get("fields", []), "case_fields": row["case_fields"]}
            for row in report["logging"].get("event_bindings") or []
            if row["status"] == "confirmed" and row["site_id"] in sites]


def _code_names(model: dict, report: dict) -> str:
    """Text in which a code-sourced identifier must literally appear."""
    quotes = [ref.get("quote") or "" for node in model.get("nodes") or [] for ref in node.get("evidence") or []]
    fields = [name for row in confirmed_bindings(report) for name in row["fields"]]
    return "\n".join(quotes + fields)


def _strings(value, limit: int, count: int) -> list[str]:
    return [text(item, limit) for item in value[:count] if text(item, limit)] if isinstance(value, list) else []


def add_profile_questions(context: dict, profile: dict | None) -> None:
    """Profile questions are trusted product data: code adds them, the model does not echo them."""
    if not profile:
        return
    questions = context["measurement_questions"]
    number = max((int(row["id"][2:]) for row in questions), default=0)
    for row in profile["measurement_questions"]:
        number += 1
        questions.append({"id": f"q-{number}", "question": row["question"], "family": row["family"],
                          "source": "profile", "profile_question_id": row["id"],
                          "rationale": f"Профиль «{profile['title']}», версия {profile['version']}"})


def build_context(model: dict, report: dict, stage_model: StageModel, business: dict | None,
                  profile: dict | None = None) -> tuple[dict | None, list[str]]:
    operations = {node["id"] for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    scenario, brief = model.get("scenario") or {}, model.get("research_brief") or {}
    payload = {
        "scenario": {key: scenario.get(key) for key in ("title", "description", "start", "end",
                                                         "entry_operation_ids", "terminal_operation_ids")},
        "research_brief": {key: brief.get(key) for key in ("project_overview", "scenario_alignment",
                                                           "flow_hypothesis", "open_questions")},
        "operations": operations_view(model),
        "gaps": [gap.get("description") for gap in model.get("gaps") or []][:30],
        "event_bindings": confirmed_bindings(report),
        "business_description": business["text"] if business else None,
        "domain_profile_questions": [row["question"] for row in profile["measurement_questions"]] if profile else []}
    names = _code_names(model, report)

    def validate(arguments):
        errors = []
        goal = text(arguments.get("goal"))
        if not goal:
            errors.append("goal: опиши бизнес-результат процесса")
        if not business and any(item.get("source") == "business"
                                for key in ("measurement_questions", "glossary")
                                for item in arguments.get(key) or [] if isinstance(item, dict)):
            errors.append("Бизнес-описание не передано: источник business недопустим")
        outcomes = []
        for item in arguments.get("outcomes") or []:
            refs = (item.get("operation_ids") or []) if isinstance(item, dict) else []
            if not isinstance(item, dict) or item.get("kind") not in OUTCOME_KINDS or not text(item.get("name")):
                errors.append("outcomes: у исхода нужны name и kind из перечня")
            elif any(ref not in operations for ref in refs):
                errors.append(f"outcomes «{text(item.get('name'), 80)}»: operation_ids только из карты")
            else:
                outcomes.append({"name": text(item["name"], 300), "kind": item["kind"], "operation_ids": list(dict.fromkeys(refs))})
        case = arguments.get("case") if isinstance(arguments.get("case"), dict) else {}
        identifiers = []
        for item in case.get("identifiers") or []:
            name = text(item.get("name"), 100) if isinstance(item, dict) else ""
            if not name or item.get("source") not in IDENTIFIER_SOURCES:
                errors.append("case.identifiers: нужны name и source из перечня")
            elif item["source"] == "code" and not re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", names):
                errors.append(f"case.identifiers: имя {name!r} не встречается в коде карты и полях записей; укажи source=hypothesis")
            elif item["source"] == "business" and not business:
                errors.append("case.identifiers: бизнес-описание не передано")
            else:
                identifiers.append({"name": name, "source": item["source"], "where": text(item.get("where"), 300)})
        questions, seen = [], set()
        for item in (arguments.get("measurement_questions") or [])[:MAX_QUESTIONS]:
            if (not isinstance(item, dict) or not _QUESTION_ID.fullmatch(str(item.get("id"))) or item["id"] in seen
                    or item.get("family") not in FAMILIES or item.get("source") not in QUESTION_SOURCES
                    or not text(item.get("question"))):
                errors.append(f"measurement_questions {item.get('id') if isinstance(item, dict) else ''}: нужны уникальный id q-N, question, family и source из перечней")
                continue
            seen.add(item["id"])
            questions.append({"id": item["id"], "question": text(item["question"], 500), "family": item["family"],
                              "source": item["source"], "rationale": text(item.get("rationale"), 500)})
        if not questions:
            errors.append("measurement_questions: нужен хотя бы один вопрос измерения")
        glossary = [{"term": text(item.get("term"), 100), "meaning": text(item.get("meaning"), 500), "source": item.get("source")}
                    for item in (arguments.get("glossary") or [])[:30]
                    if isinstance(item, dict) and text(item.get("term")) and item.get("source") in TERM_SOURCES]
        if errors and not (goal and questions):
            return None, errors
        return {"goal": goal, "start": text(arguments.get("start")), "finish": text(arguments.get("finish")),
                "outcomes": outcomes, "case": {"subject": text(case.get("subject"), 300), "identifiers": identifiers},
                "glossary": glossary, "measurement_questions": questions,
                "hypotheses": _strings(arguments.get("hypotheses"), 500, 20),
                "open_questions": _strings(arguments.get("open_questions"), 500, 20),
                "business_source": {k: business[k] for k in ("name", "sha256", "truncated")} if business else None,
                "provenance": "model_proposal", "authority_status": "proposed"}, errors

    context, errors = stage_model.ask(STAGE, payload, TOOL, validate)
    if context is not None:
        add_profile_questions(context, profile)
    return context, errors
