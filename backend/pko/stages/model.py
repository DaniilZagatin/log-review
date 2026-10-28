"""One bounded structured request over the published map, with one addressed repair.

The model answers through a single function tool. Code validates the answer;
when it is malformed or some items fail host checks, the model gets exactly one
addressed repair turn. Whatever is still invalid stays rejected. Transport
retries live in ``call_model``; the request budget and deadline are enforced
here. A packet that does not fit the provider window is refused before any
request, so the caller can send a compact one instead.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pko.audit import record, sanitize
from pko.llm.registry import DEFAULT_CONTEXT_WINDOW
from pko.llm.tracing import trace_span
from pko.scenario_map.context import context_budget
from pko.scenario_map.model_retry import ModelDeadlineExceeded, call_model
from pko.stages.inputs import METHODOLOGY, methodology

RESPONSE_CAP = 32768

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class BudgetExhausted(RuntimeError):
    """No request or time is left for another grouping request."""


class PacketTooLarge(ValueError):
    """The packet does not fit the provider window."""


@dataclass
class RequestBudget:
    max_requests: int
    deadline: float
    used: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    def check(self) -> None:
        """Check admission without consuming a physical request."""
        if self.used >= self.max_requests:
            raise BudgetExhausted("Исчерпан лимит запросов к модели для бизнес-этапов.")
        if time.monotonic() >= self.deadline:
            raise BudgetExhausted("Исчерпан лимит времени построения бизнес-этапов.")

    def take(self, stage: str) -> None:
        """Charge a physical dispatch."""
        self.check()
        self.used += 1
        self.by_stage[stage] = self.by_stage.get(stage, 0) + 1

    def snapshot(self) -> dict:
        return {"max_requests": self.max_requests, "used_requests": self.used, "by_stage": dict(self.by_stage)}


def _arguments(reply, tool_name: str) -> tuple[Any, str | None, dict | None]:
    calls = [call for call in reply.tool_calls or [] if call.get("function", {}).get("name") == tool_name]
    raw = calls[0]["function"].get("arguments") if calls else reply.text
    if not calls and isinstance(raw, str) and _FENCE.match(raw):
        raw = _FENCE.match(raw).group(1)
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None, "Ответ не является JSON-объектом аргументов инструмента.", calls[0] if calls else None
    if not isinstance(value, dict):
        return None, f"Ответ должен быть вызовом инструмента {tool_name} с JSON-объектом.", calls[0] if calls else None
    return value, None, calls[0] if calls else None


def _label(error: str) -> str:
    return error.split(":", 1)[0].strip()


def _merge(first, first_errors, result, errors, key):
    """Accepted items of the first answer survive a repair that omits them."""
    if key is None or not isinstance(first, list) or not isinstance(result, list) or result is first:
        return result, errors
    fresh = {key(item) for item in result}
    merged = result + [item for item in first if key(item) not in fresh]
    accepted = {key(item) for item in merged}
    mentioned = {_label(error) for error in errors}
    kept = [error for error in errors if _label(error) not in accepted]
    # An item rejected first and silently dropped by the repair stays reported.
    kept += [error for error in first_errors if _label(error) not in accepted | mentioned]
    return merged, kept


def request_failure(exc: Exception) -> str:
    """A bounded public cause of a request that did not complete."""
    detail = str(sanitize(getattr(exc, "hint", "") or str(exc)))[:400]
    return f"Запрос к модели не завершён ({type(exc).__name__}): {detail}"


class StageModel:
    def __init__(self, client, budget: RequestBudget, methodology_dir: Path = METHODOLOGY):
        self.methodology_dir = methodology_dir
        self.client = client
        self.budget = budget

    def _limits(self, schema: dict) -> tuple[int, int]:
        window = getattr(getattr(self.client, "spec", None), "context_window", None) or DEFAULT_CONTEXT_WINDOW
        # One answer lists every operation of the map with a goal, result and
        # boundary per stage; the research limit of 8192 tokens is too small.
        return context_budget(window, [schema], response_cap=RESPONSE_CAP)

    def ask(self, stage: str, payload: dict, tool: dict,
            validate: Callable[[dict], tuple[Any, list[str]]], *,
            key: Callable[[Any], str] | None = None) -> tuple[Any, list[str]]:
        """Return ``(result, remaining_errors)``; ``result`` is None if nothing valid came back.

        ``validate`` returns the accepted result and addressed errors; items it
        rejects are excluded from the result, never repaired silently. With
        ``key``, items accepted in the first answer are kept when the repair
        omits them.
        """
        rules = methodology(stage, self.methodology_dir)
        schema = {"type": "function", "function": tool}
        messages = [{"role": "system", "content": rules["text"]},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]
        budget, response_tokens = self._limits(schema)
        size = sum(len(json.dumps(message, ensure_ascii=False)) for message in messages)
        if size > budget:
            raise PacketTooLarge(f"Пакет {stage} ({size} символов) не помещается в окно модели ({budget}).")
        result, errors, first, first_errors = None, [], None, []
        for attempt in (1, 2):
            if attempt == 2:
                if sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) > budget:
                    errors.append("Исправление ответа не выполнено: пакет не помещается в окно модели.")
                    break
            with trace_span("stage_model_request", stage=stage, attempt=attempt) as span:
                try:
                    # Without reasoning: the answer is a structured tool call that the
                    # host checks, and reasoning used up the whole answer budget twice.
                    reply = call_model(self.client, messages, deadline=self.budget.deadline,
                                       before_attempt=lambda: self.budget.take(stage),
                                       before_retry=self.budget.check,
                                       tools=[schema], temperature=0, max_tokens=response_tokens,
                                       enable_thinking=False)
                except (ModelDeadlineExceeded, BudgetExhausted) as exc:
                    if result is not None:
                        errors.append(str(exc))
                        span.set_status("interrupted", str(exc))
                        break
                    raise BudgetExhausted(str(exc)) from exc
                except Exception as exc:
                    if result is None:
                        raise
                    # A failed repair cannot erase items accepted in its first answer.
                    errors.append(request_failure(exc))
                    span.record_error(exc)
                    break
                arguments, problem, call = _arguments(reply, tool["name"])
                if problem is None:
                    answer, answer_errors = validate(arguments)
                    if answer is not None or result is None:
                        result, errors = answer, answer_errors
                    else:
                        errors = errors + answer_errors
                else:
                    # A malformed repair keeps the first validated answer.
                    errors = errors + [problem]
                span.set_output({"errors": len(errors)})
            record("stage_model_answer", stage=stage, attempt=attempt, errors=errors[:20])
            if attempt == 1:
                first, first_errors = result, list(errors)
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
        return _merge(first, first_errors, result, errors, key)
