"""Deterministic report-stage client for tests and the offline demo.

It answers each report tool from the payload it receives, the way a careful
model would, so the stages, host checks and renderer run without a provider.
It is not an evaluation of a real model.
"""
from __future__ import annotations

import json

from pko.llm.client import ChatResult
from scenario_script import SPEC


def _role(key: str | None) -> str:
    key = (key or "").lower()
    if "reject" in key or "unsupported" in key:
        return "refusal"
    if "error" in key or "fail" in key:
        return "error"
    return "result"


class ScriptedReportClient:
    spec = SPEC

    def __init__(self, overrides=None):
        # overrides: tool name -> callable(payload) -> arguments, or a list of such for successive calls
        self.overrides = dict(overrides or {})
        self.requests: list[tuple[str, dict]] = []

    def chat(self, messages, **kwargs):
        tool = kwargs["tools"][0]["function"]["name"]
        payload = json.loads(messages[1]["content"])
        self.requests.append((tool, payload))
        handler = self.overrides.get(tool)
        if isinstance(handler, list):
            handler = handler.pop(0) if handler else None
        arguments = handler(payload, messages) if handler else getattr(self, tool)(payload)
        return ChatResult(text="", usage={}, tool_calls=[{
            "id": f"call-{len(self.requests)}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(arguments, ensure_ascii=False)}}])

    def submit_event_bindings(self, payload):
        bindings = []
        for site in payload["sites"]:
            lines = {"path": site["path"], "line_start": site["line_start"], "line_end": site["line_end"]}
            if site["kind"] == "wrapper_call":
                bindings.append({"site_id": site["site_id"], "decision": "unresolved", "event_role": "unknown",
                                 "lifecycle_moment": "unknown", "repeat_semantics": "unknown",
                                 "business_meaning": "", "evidence": [lines],
                                 "question": "Какие ключ и поля передаёт обёртка в запись?"})
                continue
            bindings.append({"site_id": site["site_id"], "decision": "bind",
                             "operation_id": site["candidate_operation_ids"][0],
                             "event_role": _role(site["key_candidate"]), "lifecycle_moment": "after_action",
                             "repeat_semantics": "once_per_execution",
                             "business_meaning": f"Фиксирует исход операции: {site['key_candidate'] or 'ключ не установлен'}",
                             "case_fields": [name for name in site["fields"] if name.endswith("_id")],
                             "evidence": [lines]})
        return {"bindings": bindings}

    def submit_process_context(self, payload):
        operations = payload["operations"]
        identifiers = sorted({name for row in payload["event_bindings"] for name in row["case_fields"]})
        return {"goal": f"Довести вариант «{payload['scenario']['title']}» до результата",
                "start": payload["scenario"].get("start") or "Получение заявки",
                "finish": payload["scenario"].get("end") or "Результат или отказ",
                "outcomes": [{"name": "Успешный результат", "kind": "success",
                              "operation_ids": [row["operation_id"] for row in operations if row["outcome"] == "success"]},
                             {"name": "Отказ", "kind": "negative",
                              "operation_ids": [row["operation_id"] for row in operations if row["outcome"] == "refusal"]}],
                "case": {"subject": "Заявка", "identifiers": [{"name": name, "source": "code", "where": "поля записи"}
                                                            for name in identifiers]
                         or [{"name": "request_id", "source": "hypothesis", "where": "предположение"}]},
                "glossary": [{"term": "Заявка", "meaning": "Обращение клиента за услугой", "source": "scenario"}],
                "measurement_questions": [
                    {"id": "q-1", "question": "Какая доля заявок завершается каждым исходом?", "family": "outcome",
                     "source": "proposal"},
                    {"id": "q-2", "question": "Сколько времени проходит от приёма заявки до результата?",
                     "family": "duration", "source": "proposal"}],
                "hypotheses": [], "open_questions": ["Какие отказы считаются ошибкой процесса?"]}

    def submit_fact_requirements(self, payload):
        operations = payload["operations"]
        bindings = payload["event_bindings"]
        lifecycle = {"success": "result", "refusal": "refusal"}
        chosen = [(operations[0]["operation_id"], "start", "Заявка принята в обработку", ["q-2"])] if operations else []
        chosen += [(row["operation_id"], lifecycle[row["outcome"]], f"Исход: {row['name']}",
                    ["q-1", "q-2"] if row["outcome"] == "success" else ["q-1"])
                   for row in operations if row["outcome"] in lifecycle]
        chosen += [(row["operation_id"], {"refusal": "refusal", "error": "error"}.get(row["event_role"], "result"),
                    row["business_meaning"], ["q-1"]) for row in bindings
                   if row["operation_id"] not in {item[0] for item in chosen}]
        facts = []
        for number, (operation_id, stage, meaning, questions) in enumerate(chosen, 1):
            observed = [row for row in bindings if row["operation_id"] == operation_id]
            facts.append({"id": f"f-{number}", "meaning": meaning, "question_ids": questions, "lifecycle": stage,
                          "operation_ids": [operation_id], "applicability": "applicable",
                          "needed": ["case_id", "timestamp"], "observed_by": [row["binding_id"] for row in observed],
                          "field_sources": [{"purpose": "case_id", "binding_id": row["binding_id"], "field": name}
                                            for row in observed for name in row["case_fields"][:1]],
                          "rationale": "Без этого факта нельзя ответить на вопрос измерения."})
        return {"facts": facts}

    def submit_instrumentation_changes(self, payload):
        changes = []
        for number, fact in enumerate(payload["facts_with_gaps"], 1):
            fragment = next((row for row in payload["fragments"]
                             if row["about"].get("operation_id") in fact["operation_ids"]), None)
            axes = fact["coverage"]["axes"]
            action = "add_event" if axes["emit"] == "not_found" else "add_field" if axes["correlation"] == "missing" else "enrich"
            location = ({"status": "anchored", "path": fragment["path"], "line_start": fragment["about"]["cited_lines"][0],
                         "line_end": fragment["about"]["cited_lines"][1]} if fragment else
                        {"status": "unresolved", "search_task": "Найти место фиксации результата операции"})
            changes.append({"id": f"c-{number}", "action": action, "fact_ids": [fact["id"]],
                            "operation_ids": fact["operation_ids"], "question": "Сколько заявок дошло до этого шага?",
                            "why_existing_insufficient": "Существующих записей для этого факта нет.",
                            "location": location, "change": f"Зафиксировать факт: {fact['meaning']}",
                            "event": {"key": f"process.{fact['id']}" if action == "add_event" else "",
                                      "fields": [{"name": "request_id", "type": "string", "source": "заявка",
                                                  "purpose": "идентификатор экземпляра"}],
                                      "lifecycle": fact["lifecycle"], "timing": "after_commit",
                                      "identity": "request_id определяет заявку", "dedup": "по request_id и ключу"},
                            "depends_on": [],
                            "validation": "Провести заявку по сценарию и проверить одну запись с request_id."})
        return {"changes": changes}

    def submit_metric_definitions(self, payload):
        facts = payload["facts"]
        outcome_facts = [fact["id"] for fact in facts if fact["lifecycle"] in ("result", "refusal")]
        start = next((fact["id"] for fact in facts if fact["lifecycle"] == "start"), None)
        finish = next((fact["id"] for fact in facts if fact["lifecycle"] == "result"), None)
        changes = [change["id"] for change in payload["changes"]]
        metrics = [{"id": "m-1", "name": "Доля заявок по исходам", "purpose": "Видеть долю успешных заявок и отказов",
                    "question_ids": ["q-1"], "counting_unit": "case", "measure_unit": "доля",
                    "definition": "Число заявок с исходом / число завершённых заявок за период",
                    "population": "Заявки выбранного варианта", "window": "Календарный месяц по дате завершения",
                    "rules": {"open_cases": "Незавершённые заявки не входят в знаменатель"},
                    "fact_ids": outcome_facts, "change_ids": changes, "open_questions": []}]
        if start and finish:
            metrics.append({"id": "m-2", "name": "Длительность обработки заявки", "purpose": "Видеть время до результата",
                            "question_ids": ["q-2"], "counting_unit": "case", "measure_unit": "минуты",
                            "definition": "Время от приёма заявки до результата", "population": "Успешные заявки",
                            "window": "Календарный месяц по дате результата", "start_fact_id": start,
                            "finish_fact_id": finish, "fact_ids": [start, finish], "change_ids": changes,
                            "missing_facts": [], "open_questions": ["Учитывать ли нерабочее время?"]})
        return {"metrics": metrics}
