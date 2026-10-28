"""Deterministic client for the grouping request in tests and the offline demo.

It answers ``submit_business_stages`` from the payload it receives, so the
grouping, host checks and renderer run without a provider. Overrides replace
the answer of a tool. It is not an evaluation of a real model.
"""
from __future__ import annotations

import json

from pko.llm.client import ChatResult
from scenario_script import SPEC, scripted_business_stages


class ScriptedStageClient:
    spec = SPEC

    def __init__(self, overrides=None):
        # overrides: tool name -> callable(payload, messages) -> arguments, or a list of such for successive calls
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

    def submit_business_stages(self, payload):
        return scripted_business_stages(payload)
