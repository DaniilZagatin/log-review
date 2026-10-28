"""One bounded structured request per report packet, with one addressed repair.

The model answers through a single function tool. Code validates the answer;
when it is malformed or some items fail host checks, the model gets exactly one
addressed repair turn. Whatever is still invalid stays rejected. Transport
retries live in ``call_model``; the shared request budget and deadline are
enforced here so later stages cannot run unbounded. A packet that does not fit
the provider window is refused before any request, so the caller can split it.
"""
from __future__ import annotations

import hashlib
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

_METHODOLOGY = Path(__file__).with_name("methodology")
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class BudgetExhausted(RuntimeError):
    """No request or time is left for another report packet."""


class PacketTooLarge(ValueError):
    """The packet does not fit the provider window; split it or report it."""


@dataclass
class ReportBudget:
    max_requests: int
    deadline: float
    used: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)

    def check(self, reserve: int = 0) -> None:
        """Check admission without consuming a physical request."""
        if self.used >= self.max_requests - reserve:
            raise BudgetExhausted("Исчерпан лимит запросов к модели для этого этапа отчёта." if reserve else
                                  "Исчерпан лимит запросов к модели для этапов отчёта.")
        if time.monotonic() >= self.deadline:
            raise BudgetExhausted("Исчерпан лимит времени этапов отчёта.")

    def take(self, stage: str, reserve: int = 0) -> None:
        """Charge a physical dispatch; ``reserve`` stays available to later stages."""
        self.check(reserve)
        self.used += 1
        self.by_stage[stage] = self.by_stage.get(stage, 0) + 1

    def snapshot(self) -> dict:
        return {"max_requests": self.max_requests, "used_requests": self.used, "by_stage": dict(self.by_stage)}


def methodology(stage: str, folder: Path = _METHODOLOGY) -> dict:
    """Stage instructions: the folder's shared rules plus the stage file, with a content digest."""
    text = (folder / "common.md").read_text(encoding="utf-8") + "\n\n" + \
        (folder / f"{stage}.md").read_text(encoding="utf-8")
    return {"id": stage, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}


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
    """A bounded public cause shared by partial packets and deferred work."""
    detail = str(sanitize(getattr(exc, "hint", "") or str(exc)))[:400]
    return f"Запрос к модели не завершён ({type(exc).__name__}): {detail}"


def _cache_candidate(responses: list[tuple[dict, Any]], merged, tool: dict, key):
    """Rebuild provider arguments for accepted items, including items kept from the first answer.

    The host validates this candidate again before caching it. Normalised host
    fields are never sent through a provider-input validator as raw arguments.
    """
    if not responses:
        return None
    properties = tool.get("parameters", {}).get("properties", {})
    candidate = {name: value for name, value in responses[-1][0].items() if name in properties}
    if key is None or not isinstance(merged, list):
        return candidate
    fields = [name for name, schema in properties.items() if schema.get("type") == "array"]
    if len(fields) != 1:
        return None
    field = fields[0]
    groups = {}
    for arguments, accepted in responses:
        if not isinstance(accepted, list) or not isinstance(arguments.get(field), list):
            continue
        wanted = {key(item) for item in accepted}
        current = {}
        for item in arguments[field]:
            try:
                identity = key(item)
            except (KeyError, TypeError, AttributeError):
                continue
            if identity in wanted:
                current.setdefault(identity, []).append(item)
        groups.update(current)
    rows, seen = [], set()
    for item in merged:
        identity = key(item)
        if identity not in seen:
            if identity not in groups:
                return None
            rows.extend(groups[identity])
            seen.add(identity)
    candidate[field] = rows
    return candidate


class StageModel:
    def __init__(self, client, budget: ReportBudget, cache: dict | None = None, methodology_dir: Path = _METHODOLOGY):
        self.methodology_dir = methodology_dir
        self.client = client
        self.budget = budget
        # Accepted answers by packet digest; reused only after revalidation.
        self.cache = cache if cache is not None else {}
        self.cache_hits = 0

    def _limits(self, schema: dict) -> tuple[int, int]:
        window = getattr(getattr(self.client, "spec", None), "context_window", None) or DEFAULT_CONTEXT_WINDOW
        return context_budget(window, [schema])

    def _cache_key(self, rules: dict, schema: dict, payload: dict) -> str:
        model = getattr(getattr(self.client, "spec", None), "model", "")
        data = json.dumps([rules["sha256"], schema, payload, model], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def ask(self, stage: str, payload: dict, tool: dict,
            validate: Callable[[dict], tuple[Any, list[str]]], *, reserve: int = 0,
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
        cache_key = self._cache_key(rules, schema, payload)
        cached = self.cache.get(cache_key)
        if cached is not None:
            result, errors = validate(cached)
            if result is not None and not errors:
                self.cache_hits += 1
                record("report_cache_hit", stage=stage, key=cache_key)
                return result, errors
        budget, response_tokens = self._limits(schema)
        size = sum(len(json.dumps(message, ensure_ascii=False)) for message in messages)
        if size > budget:
            raise PacketTooLarge(f"Пакет этапа {stage} ({size} символов) не помещается в окно модели ({budget}).")
        result, errors, first, first_errors = None, [], None, []
        responses = []
        for attempt in (1, 2):
            if attempt == 2:
                if sum(len(json.dumps(message, ensure_ascii=False)) for message in messages) > budget:
                    errors.append("Исправление ответа не выполнено: пакет не помещается в окно модели.")
                    break
            with trace_span("report_model_request", stage=stage, attempt=attempt) as span:
                try:
                    reply = call_model(self.client, messages, deadline=self.budget.deadline,
                                       before_attempt=lambda: self.budget.take(stage, reserve),
                                       before_retry=lambda: self.budget.check(reserve),
                                       tools=[schema], temperature=0, max_tokens=response_tokens)
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
                    if answer is not None:
                        responses.append((arguments, answer))
                    if answer is not None or result is None:
                        result, errors = answer, answer_errors
                    else:
                        errors = errors + answer_errors
                else:
                    # A malformed repair keeps the first validated answer.
                    errors = errors + [problem]
                span.set_output({"errors": len(errors)})
            record("report_stage_answer", stage=stage, attempt=attempt, errors=errors[:20])
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
        merged, errors = _merge(first, first_errors, result, errors, key)
        if merged is not None and not errors:
            candidate = _cache_candidate(responses, merged, tool, key)
            if candidate is not None:
                candidate = sanitize(candidate)
                checked, problems = validate(candidate)
                if not problems and checked == merged:
                    self.cache[cache_key] = candidate
        return merged, errors
