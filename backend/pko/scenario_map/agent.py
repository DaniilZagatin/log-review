"""A single bounded model/tool loop; no reviewer/passport pipeline dependency."""
from __future__ import annotations

import copy
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Callable

from pko.audit import AuditedClient, record, sanitize
from pko.errors import LlmError
from pko.llm.client import ChatClient
from pko.llm.tracing import trace_client, trace_span, trace_tool_call
from pko.code_tools.tools import TOOL_SCHEMAS, ToolBox
from pko.scenario_map.schema import MapState
from pko.scenario_map.tools import TOOLS
from pko.scenario_map.model_retry import ModelDeadlineExceeded, call_model
from pko.scenario_map.context import compact_context, context_budget

READ_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}
MAX_TOOL_CHARS = 24000
DEFAULT_MAX_STEPS = 180
DEFAULT_MAX_SECONDS = 7200
DEFAULT_MODEL_TIMEOUT = 900


class DeadlineClient(ChatClient):
    """Reuse response serialization; cap I/O waits by the remaining soft budget.

    SDK retries stay disabled; the agent retries transient model errors once
    through call_model, within the same run deadline.
    Remaining time is rechecked before every dispatch. HTTPX timeouts bound each
    I/O wait, not total request duration; a continuously trickling response can
    exceed the soft budget. Provider timeouts preserve a partial result.
    """
    deadline: float = 0

    def _create(self, payload, extra_body=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Достигнут лимит времени исследования.")
        options = dict(payload)
        if extra_body:
            options["extra_body"] = extra_body
        try:
            bounded = self._client.with_options(timeout=min(self.timeout, remaining), max_retries=0)
            return bounded.chat.completions.create(**options)
        except Exception as exc:
            raise LlmError("Не удалось получить ответ модели исследователя.",
                           str(sanitize(str(exc)))[:500]) from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _cycle(signatures: list[str]) -> bool:
    return any(len(signatures) >= size * 3 and
               signatures[-size:] * 3 == signatures[-size * 3:]
               for size in (1, 2, 3))


def _brief(state: MapState) -> str:
    """Retain graph identity and outstanding work; full evidence remains in state."""
    model = state.snapshot()
    model.pop("evidence_materials", None)
    def without_quotes(value):
        if isinstance(value, dict):
            return {key: without_quotes(item) for key, item in value.items()
                    if key not in ("quote", "node_fingerprints", "edge_fingerprints")}
        if isinstance(value, list):
            return [without_quotes(item) for item in value]
        return value
    return _json(without_quotes(model))


def _compact(messages: list[dict], state: MapState, budget: int) -> list[dict]:
    return compact_context(messages, state, budget, brief=_brief)


def _progress(state: MapState) -> str:
    """Observation repetition/recency is bookkeeping, not research progress."""
    snapshot = state.snapshot()
    observations = snapshot.pop("research_history", [])
    snapshot.pop("research_history_evicted", None)
    snapshot["observation_ids"] = sorted(row["observation_id"] for row in observations)
    return _json(snapshot)


class ResearchTools:
    def __init__(self, state: MapState, workspace):
        self.state = state
        self.box = ToolBox(workspace.tree, workspace.extraction)

    def answer(self, call: dict) -> dict:
        function = call.get("function") or {}
        name = function.get("name", "")
        try:
            raw = function.get("arguments", "{}")
            args = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(args, dict):
                raise ValueError("аргументы должны быть объектом")
            if name in READ_NAMES:
                result = self.box.call(name, args)
                content = result.content
                if len(content) > MAX_TOOL_CHARS:
                    prefix = content[:MAX_TOOL_CHARS]
                    boundary = prefix.rfind("\n")
                    content = prefix[:boundary] if boundary >= 0 else ""
                    content += "\n[Выдача ограничена; продолжи чтение с последней полной строки.]"
                if name == "read_file" and result.ok:
                    lines = [int(n) for n in re.findall(r"(?m)^(\d+):", content)]
                    if lines:
                        self.state.observe_read(str(args.get("path", "")), min(lines),
                                                max(lines) - min(lines) + 1)
                payload = {"ok": result.ok, "content": content, "meta": result.meta}
                payload["memory"] = self.state.research_memory.observe(name, args, payload)
            else:
                payload = self.state.dispatch(name, args)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            payload = {"ok": False, "error": f"Некорректный вызов инструмента: {exc}"}
        record("scenario_tool", name=name, arguments=function.get("arguments"), result=payload)
        return {"role": "tool", "tool_call_id": call.get("id", ""), "content": _json(payload)}


def run_agent(workspace, scenario: str, spec, *, client=None,
              max_steps: int = DEFAULT_MAX_STEPS, max_seconds: float = DEFAULT_MAX_SECONDS,
              model_timeout: float = DEFAULT_MODEL_TIMEOUT,
              checkpoint: Callable[[dict], None] | None = None,
              on_event: Callable[[str, dict], None] | None = None) -> dict:
    if max_steps < 1 or not all(math.isfinite(value) and value > 0 for value in (max_seconds, model_timeout)):
        raise ValueError("Лимиты исследования и ожидания модели должны быть конечными положительными числами")
    state = MapState(workspace, scenario)
    state.model["run"].update({"status": "running", "steps": 0, "model": spec.model,
                               "model_timeout_seconds": model_timeout, "max_seconds": max_seconds})
    toolkit = ResearchTools(state, workspace)
    schemas = copy.deepcopy(TOOL_SCHEMAS) + copy.deepcopy(TOOLS)
    for tool in schemas:
        if tool["function"]["name"] == "who_calls":
            tool["function"]["description"] = (
                "Найти возможные места вызова функции. Приблизительный статический поиск; "
                "отсутствие совпадения не доказывает отсутствие вызова.")
    system = Path(__file__).with_name("system.md").read_text(encoding="utf-8")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _json({
        "scenario_description": scenario, "sources": workspace.sources,
        "source_notes": workspace.notes, "source_omissions": getattr(workspace, "omissions", []),
        "instruction": "Восстанови карту именно этого варианта. Начни с ориентации в источниках."})}]
    started = time.monotonic()
    signatures: list[str] = []
    idle, empty = 0, 0
    loop_recovery_used = False
    window = spec.context_window or 64000
    message_budget, response_tokens = context_budget(window, schemas)
    state.model["run"]["context_budget_chars"] = message_budget
    audited_client = None

    def save():
        if checkpoint:
            checkpoint(state.snapshot())

    try:
        save()
        if sum(len(_json(m)) for m in messages) + 1600 > message_budget:
            raise ValueError("Окно модели слишком мало для инструментов и описания сценария; используйте модель с большим контекстом или сократите описание.")
        if client is None:
            client = DeadlineClient(spec=spec, timeout=model_timeout)
            client.deadline = started + max_seconds
        client = trace_client(client, spec)
        audited_client = client if isinstance(client, AuditedClient) else AuditedClient(client)
        with trace_span("scenario_research", model=spec.model):
            for step in range(1, max_steps + 1):
                if time.monotonic() - started >= max_seconds:
                    state.model["run"].update(status="partial", reason="Достигнут лимит времени исследования.")
                    break
                state.model["run"]["steps"] = step
                if sum(len(_json(m)) for m in messages) > message_budget:
                    with trace_span("compact_context", messages_before=len(messages)) as compact_span:
                        messages = _compact(messages, state, message_budget)
                        state.model["run"]["context_compactions"] = state.model["run"].get("context_compactions", 0) + 1
                        compact_span.set_attribute("messages_after", len(messages))
                if sum(len(_json(m)) for m in messages) > message_budget:
                    raise ValueError("Сохранённый контекст не помещается в окно модели; карта сохранена частично.")
                with trace_span("scenario_step", step=step):
                    reply = call_model(audited_client, messages, deadline=started + max_seconds,
                                       before_retry=save, on_event=on_event,
                                       tools=schemas, temperature=0, max_tokens=response_tokens)
                    calls = reply.tool_calls or []
                    assistant = {"role": "assistant", "content": reply.text or ""}
                    if calls:
                        assistant["tool_calls"] = calls
                    # Retained only in the live provider conversation, never in audit/model.
                    if getattr(reply, "reasoning_content", ""):
                        assistant["reasoning_content"] = reply.reasoning_content
                    messages.append(assistant)
                    if not calls:
                        empty += 1
                        messages.append({"role": "user", "content":
                            "Сохрани выводы инструментами карты и вызови finish. Текст ответа не завершает исследование."})
                        if empty >= 3:
                            state.model["run"].update(status="partial", reason="Модель не сохранила результат инструментами.")
                            break
                        continue
                    empty = 0
                    before = _progress(state)
                    accepted_finish = False
                    for call in calls:
                        if accepted_finish or time.monotonic() - started >= max_seconds:
                            answer = trace_tool_call(lambda owner, c: {
                                "role": "tool", "tool_call_id": c.get("id", ""),
                                "content": _json({"ok": False, "error": "Исследование уже остановлено."})}, toolkit, call)
                        else:
                            answer = trace_tool_call(lambda owner, c: owner.answer(c), toolkit, call)
                        messages.append(answer)
                        function = call.get("function") or {}
                        payload = json.loads(answer["content"])
                        name = function.get("name", "")
                        signature = _json(function)
                        signatures.append(signature)
                        if name == "finish" and payload.get("ok"):
                            accepted_finish = True
                        if on_event:
                            on_event("tool", {"name": name, "ok": payload.get("ok"), "step": step,
                                              "nodes": len(state.model.get("nodes", [])),
                                              "edges": len(state.model.get("edges", []))})
                    # New observations/lines/notes/maps count; looking at the same
                    # material again does not reset the no-progress guard.
                    changed = before != _progress(state)
                    idle = 0 if changed else idle + 1
                    if changed:
                        signatures.clear()
                    save()
                    if accepted_finish:
                        break
                    if _cycle(signatures) or idle >= 24:
                        if not loop_recovery_used:
                            loop_recovery_used = True
                            state.model["run"]["loop_recovery_attempted"] = True
                            messages.append({"role": "user", "content":
                                "Повторяются действия без новых данных. Ранее полученные результаты сохранены: "
                                "inspect_map(section='research_notes') — выводы и следующие действия; "
                                "research_history — прошлые запросы и ответы; read_coverage и read_fragments — "
                                "прочитанные строки. Восстанови нужное состояние и продолжи незавершённый участок. "
                                "Не начинай обзор заново. Если продолжение невозможно, сохрани пробелы и заверши через finish."})
                            record("research_loop_recovery", step=step, idle=idle)
                            signatures.clear()
                            idle = 0
                            save()
                            continue
                        state.model["run"].update(status="partial", reason="Исследование остановлено: повторение действий без нового результата.")
                        break
                    if idle == 12 or step >= max_steps - 3:
                        messages.append({"role": "user", "content":
                            "Сохрани найденные шаги и переходы, перечисли оставшиеся пробелы и заверши через finish."})
            else:
                state.model["run"].update(status="partial", reason="Достигнут лимит шагов исследования.")
    except ModelDeadlineExceeded as exc:
        state.model["run"].update(status="partial", reason=str(exc))
    except KeyboardInterrupt:
        state.model["run"].update(status="partial", reason="Исследование прервано пользователем.", interrupted=True)
    except Exception as exc:
        hint = str(sanitize(getattr(exc, "hint", "")))[:500]
        record("scenario_error", error_type=type(exc).__name__, error_message=str(exc), hint=hint)
        state.model["run"].update(status="partial" if state.model.get("nodes") else "failed",
                                  reason=str(sanitize(f"Ошибка исследования: {exc}")) + (f" {hint}" if hint else ""))
    finally:
        if audited_client is not None:
            audited_client.flush()
    state.model["run"]["duration_seconds"] = round(time.monotonic() - started, 3)
    if state.model["run"].get("status") == "running":
        state.model["run"].update(status="partial", reason="Исследование остановлено без принятого завершения.")
    save()
    return state.snapshot()
