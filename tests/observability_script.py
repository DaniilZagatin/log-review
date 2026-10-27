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
