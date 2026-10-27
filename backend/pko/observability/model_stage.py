"""One bounded structured request per report packet, with one addressed repair.

The model answers through a single function tool. Code validates the answer;
when it is malformed or some items fail host checks, the model gets exactly one
addressed repair turn. Whatever is still invalid stays rejected. Transport
retries live in ``call_model``; the shared request budget and deadline are
enforced here so later stages cannot run unbounded.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pko.audit import record
from pko.llm.tracing import trace_span
from pko.scenario_map.model_retry import ModelDeadlineExceeded, call_model

_METHODOLOGY = Path(__file__).with_name("methodology")
RESPONSE_TOKENS = 8192


class BudgetExhausted(RuntimeError):
    """No request or time is left for another report packet."""


@dataclass
class ReportBudget:
    max_requests: int
    deadline: float
    used: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    def take(self, stage: str) -> None:
        if self.used >= self.max_requests:
            raise BudgetExhausted("Исчерпан лимит запросов к модели для этапов отчёта.")
        if time.monotonic() >= self.deadline:
            raise BudgetExhausted("Исчерпан лимит времени этапов отчёта.")
        self.used += 1
        self.by_stage[stage] = self.by_stage.get(stage, 0) + 1

    def snapshot(self) -> dict:
        return {"max_requests": self.max_requests, "used_requests": self.used, "by_stage": dict(self.by_stage)}


def methodology(stage: str) -> dict:
    """Stage instructions: shared rules plus the stage file, with a content digest."""
    text = (_METHODOLOGY / "common.md").read_text(encoding="utf-8") + "\n\n" + \
        (_METHODOLOGY / f"{stage}.md").read_text(encoding="utf-8")
    return {"id": stage, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}


def _arguments(reply, tool_name: str) -> tuple[Any, str | None, dict | None]:
    calls = [call for call in reply.tool_calls or [] if call.get("function", {}).get("name") == tool_name]
    raw = calls[0]["function"].get("arguments") if calls else reply.text
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None, "Ответ не является JSON-объектом аргументов инструмента.", calls[0] if calls else None
    if not isinstance(value, dict):
        return None, f"Ответ должен быть вызовом инструмента {tool_name} с JSON-объектом.", calls[0] if calls else None
    return value, None, calls[0] if calls else None


class StageModel:
    def __init__(self, client, budget: ReportBudget):
        self.client = client
        self.budget = budget

    def ask(self, stage: str, payload: dict, tool: dict,
            validate: Callable[[dict], tuple[Any, list[str]]]) -> tuple[Any, list[str]]:
        """Return ``(result, remaining_errors)``; ``result`` is None if nothing valid came back.

        ``validate`` returns the accepted result and addressed errors; items it
        rejects are excluded from the result, never repaired silently.
        """
        rules = methodology(stage)
        schema = {"type": "function", "function": tool}
        messages = [{"role": "system", "content": rules["text"]},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]
        result, errors = None, []
        for attempt in (1, 2):
            if attempt == 2:
                try:
                    self.budget.take(stage)
                except BudgetExhausted:
                    break
            else:
                self.budget.take(stage)
            with trace_span("report_model_request", stage=stage, attempt=attempt) as span:
                try:
                    reply = call_model(self.client, messages, deadline=self.budget.deadline,
                                       tools=[schema], temperature=0, max_tokens=RESPONSE_TOKENS)
                except ModelDeadlineExceeded as exc:
                    if result is not None:
                        break
                    raise BudgetExhausted(str(exc)) from exc
                arguments, problem, call = _arguments(reply, tool["name"])
                if problem is None:
                    result, errors = validate(arguments)
                elif result is None:
                    errors = [problem]
                else:
                    # A malformed repair keeps the first validated answer.
                    errors = errors + [problem]
                    break
                span.set_output({"errors": len(errors)})
            record("report_stage_answer", stage=stage, attempt=attempt, errors=errors[:20])
            if not errors or attempt == 2:
                break
            # One addressed repair: the model sees exactly what was rejected.
            feedback = {"ok": False, "errors": errors[:40],
                        "instruction": "Исправь только отклонённые элементы и повтори вызов целиком; принятые элементы повтори без изменений."}
            assistant = {"role": "assistant", "content": reply.text or ""}
            if call is not None:
                assistant["tool_calls"] = [call]
                messages += [assistant, {"role": "tool", "tool_call_id": call.get("id", "call"),
                                         "content": json.dumps(feedback, ensure_ascii=False)}]
            else:
                messages += [assistant, {"role": "user", "content": json.dumps(feedback, ensure_ascii=False)}]
        return result, errors
