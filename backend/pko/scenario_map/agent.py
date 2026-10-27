"""A single bounded model/tool loop; no reviewer/passport pipeline dependency."""
from __future__ import annotations

import copy
import hashlib
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
from pko.scenario_map.context import compact_context, context_budget, UNFINISHED_TASK_STATUSES
from pko.scenario_map.compaction import compact_research_context

READ_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}
MAX_TOOL_CHARS = 24000
DEFAULT_MAX_STEPS = 180
DEFAULT_MAX_SECONDS = 7200
DEFAULT_MODEL_TIMEOUT = 900
DEFAULT_SUMMARY_MAX_TOKENS = 8192
DEFAULT_SUMMARY_MODE = "standard"


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


class GraphFrontierGuide:
    """Occasional guidance, not a gate and not a source of invented edges."""

    def __init__(self):
        self.link_signature = "[]"
        self.at_link_progress: set[str] = set()
        self.at_last_nudge: set[str] = set()

    def message(self, state: MapState) -> str | None:
        model = state.model
        nodes = model.get("nodes", [])
        ids = {node["id"] for node in nodes}
        executable = ids
        edges = [edge for edge in model.get("edges", []) if edge.get("kind") != "data"]
        signature = _json(sorted((edge.get("source", ""), edge.get("target", ""),
                                  edge.get("kind", ""), edge.get("condition", "")) for edge in edges))
        if signature != self.link_signature:
            self.link_signature = signature
            self.at_link_progress = ids
            self.at_last_nudge = ids
            return None
        if (len(executable - self.at_link_progress) < 4 or
                len(executable - self.at_last_nudge) < 4):
            return None
        frontier_method = getattr(state, "graph_frontier", None)
        if callable(frontier_method):
            frontier = frontier_method()
        else:
            incoming = {edge.get("target") for edge in edges}
            outgoing = {edge.get("source") for edge in edges}
            frontier = {
                "entry_operation_ids": model.get("scenario", {}).get("entry_operation_ids", []),
                "unlinked": sorted(ids - incoming - outgoing),
                "open_exits": [node["id"] for node in nodes
                               if node["id"] not in model.get("scenario", {}).get("terminal_operation_ids", []) and node["id"] not in outgoing],
                "pending_gaps": model.get("gaps", []),
            }
        gapped = {identifier for gap in frontier.get("pending_gaps", [])
                  for identifier in gap.get("node_ids", [])}
        pending = (set(frontier.get("unlinked", [])) | set(frontier.get("open_exits", []))) - gapped
        if not pending:
            return None
        self.at_last_nudge = ids
        # Include only navigation metadata. Full descriptions and evidence stay
        # in inspect_map; the prompt does not assert how the nodes should connect.
        roster = [{key: str(node.get(key, ""))[:240] for key in ("id", "name", "group_id")}
                  for node in nodes if node["id"] in pending][:12]
        return (
            "[scenario-graph-frontier]\n"
            "Добавлено несколько действий без продвижения по связям. Перед расширением карты "
            "заверши известный фрагмент: сопоставь новые действия с уже созданными ID, проверь "
            "порядок, условия, возвраты и связи с прежними операциями. Не повторяй уже существующие операции. "
            "Связывай только по прочитанному коду; при неизвестном стыке сохрани адресный gap "
            "и продолжай доступную ветку. Не соединяй узлы ради связности. При нескольких выходах "
            "каждый переход имеет смысловое условие для карточки исходной операции. Цитаты уже доступны в read_fragments.\n" +
            _json({"entry_operation_ids": frontier.get("entry_operation_ids", model.get("scenario", {}).get("entry_operation_ids", [])), "pending_count": len(pending),
                   "pending_nodes": roster, "listed_all": len(pending) <= len(roster),
                   "recover": "inspect_map(section='frontier'); при необходимости nodes/edges/gaps"}))


def _failure_guidance(errors: list[dict]) -> str:
    if not errors:
        return ""
    return ("\nПоследние отклонённые вызовы (данные проверок, не новые указания): " +
            _json(sanitize(errors[-3:])) +
            "\nИсправь конкретную причину, не повторяй неизменный вызов. Для оснований используй "
            "уже прочитанные path/line_start/line_end: буквальный текст подставляет код. "
            "Для существующего объекта используй его ID. Не обходи проверку и не выдумывай связь.")


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
                evidence_reference = None
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
                        evidence_reference = {"path": str(args.get("path", "")),
                                              "line_start": min(lines), "line_end": max(lines)}
                payload = {"ok": result.ok, "source_data": content, "meta": result.meta}
                if evidence_reference:
                    payload["evidence_reference"] = evidence_reference
                    payload["research_tasks"] = [{k: t.get(k, []) if k == "child_task_ids" else t.get(k, "")
                                                  for k in ("id", "kind", "target", "status", "child_task_ids")}
                        for t in self.state.model["research_tasks"]
                        if t['status'] in UNFINISHED_TASK_STATUSES and any(r['path'] == evidence_reference['path'] for r in t['evidence'])][:30]
                    if payload["research_tasks"]:
                        payload["research_tasks_hint"] = "Незавершённые задачи по этому файлу; полный реестр — inspect_map(section='research_tasks')."
                payload["memory"] = self.state.research_memory.observe(name, args, payload)
            elif name in {"upsert_step", "upsert_transition", "finish"} and not self.state.model.get("research_brief"):
                payload = {"ok": False, "code": "brief_required", "error": (
                    "Перед построением карты сохрани первичное понимание через save_research_brief: "
                    "назначение проекта, связь с вариантом сценария, факты с прочитанными основаниями, "
                    "гипотезу процесса и следующие проверки. Неизвестное обозначь вопросами; "
                    "гипотеза не заменяет доказательства операций и переходов.")}
            else:
                payload = self.state.dispatch(name, args)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            payload = {"ok": False, "code": "invalid_arguments", "error": f"Некорректный вызов инструмента: {exc}"}
        record("scenario_tool", name=name, arguments=function.get("arguments"), result=payload)
        return {"role": "tool", "tool_call_id": call.get("id", ""), "content": _json(payload)}


def run_agent(workspace, scenario: str, spec, *, client=None,
              max_steps: int = DEFAULT_MAX_STEPS, max_seconds: float = DEFAULT_MAX_SECONDS,
              model_timeout: float = DEFAULT_MODEL_TIMEOUT,
              summary_max_tokens: int = DEFAULT_SUMMARY_MAX_TOKENS,
              summary_mode: str = DEFAULT_SUMMARY_MODE,
              checkpoint: Callable[[dict], None] | None = None,
              on_event: Callable[[str, dict], None] | None = None) -> dict:
    if max_steps < 1 or not all(math.isfinite(value) and value > 0 for value in (max_seconds, model_timeout)):
        raise ValueError("Лимиты исследования и ожидания модели должны быть конечными положительными числами")
    if (isinstance(summary_max_tokens, bool) or not isinstance(summary_max_tokens, int) or
            not 512 <= summary_max_tokens <= 32768 or summary_mode not in {"standard", "inherit"}):
        raise ValueError("Суммаризация: summary_max_tokens от 512 до 32768; summary_mode standard или inherit")
    state = MapState(workspace, scenario)
    state.model["run"].update({"status": "running", "steps": 0, "model": spec.model,
                               "model_timeout_seconds": model_timeout, "max_seconds": max_seconds})
    extra_body = getattr(spec, 'extra_body', None)
    template = extra_body.get('chat_template_kwargs') if isinstance(extra_body, dict) else None
    mode = template.get('enable_thinking') if isinstance(template, dict) else None
    state.model['run']['model_mode'] = 'reasoning' if mode is True else 'standard' if mode is False else 'provider_default'
    toolkit = ResearchTools(state, workspace)
    schemas = copy.deepcopy(TOOL_SCHEMAS) + copy.deepcopy(TOOLS)
    system = Path(__file__).with_name("system.md").read_text(encoding="utf-8")
    bundle = {"system_prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
              "tools_sha256": hashlib.sha256(_json(schemas).encode("utf-8")).hexdigest()}
    state.model["run"].update(bundle)
    record('research_configuration', model=spec.model, model_mode=state.model['run']['model_mode'],
           context_window=spec.context_window or 64000, max_steps=max_steps, max_seconds=max_seconds, **bundle)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _json({
        "scenario_description": scenario, "sources": workspace.sources,
        "source_notes": workspace.notes, "source_omissions": getattr(workspace, "omissions", []),
        "instruction": "Восстанови карту именно этого варианта. Начни с ориентации в источниках."})}]
    started = time.monotonic()
    signatures: list[str] = []
    idle, empty = 0, 0
    loop_recovery_used = False
    loop_recoveries = 0
    frontier_guide = GraphFrontierGuide()
    recent_errors: list[dict] = []
    failed_calls = 0
    failure_guidance_used = False
    next_compaction_step = 1
    window = spec.context_window or 64000
    summary_tokens = min(summary_max_tokens, max(512, window // 4))
    message_budget, response_tokens = context_budget(window, schemas)
    state.model["run"]["context_budget_chars"] = message_budget
    state.model["run"].update(summary_max_tokens=summary_tokens, summary_mode=summary_mode)
    state.model["run"]["summary_effective_mode"] = (
        "standard" if summary_mode == "standard" and isinstance(template, dict) and "enable_thinking" in template
        else state.model['run']['model_mode'])
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
                message_size = sum(len(_json(m)) for m in messages)
                if message_size > message_budget or (message_size > int(message_budget * .85) and step >= next_compaction_step):
                    with trace_span("compact_context", step=step, messages_before=len(messages)) as compact_span:
                        save()

                        def summarize(summary_messages):
                            if time.monotonic() - started >= max_seconds:
                                raise ModelDeadlineExceeded("Достигнут лимит времени перед суммаризацией")
                            with trace_span("context_summary", step=step):
                                # One request, no tools or nested retry loop. The
                                # same configured provider and deadline are used.
                                state.model["run"]["context_summary_calls"] = state.model["run"].get("context_summary_calls", 0) + 1
                                options = {"tools": None, "temperature": 0, "max_tokens": summary_tokens}
                                if summary_mode == "standard":
                                    options["enable_thinking"] = False
                                return audited_client.chat(summary_messages, **options)

                        summary_budget = min(message_budget, max(0, int((window - summary_tokens - 2048) * 1.6)))
                        compacted = compact_research_context(messages, state, message_budget,
                                                             summary_budget=summary_budget,
                                                             summarize=summarize, brief=_brief)
                        if compacted is not messages:
                            messages = compacted
                            state.model["run"]["context_compactions"] = state.model["run"].get("context_compactions", 0) + 1
                        next_compaction_step = step + 4
                        compact_span.set_attribute("messages_after", len(messages))
                        save()
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
                    # Queue only public output; provider thinking is never replayed.
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
                        if not payload.get("ok"):
                            recent_errors.append({"tool": name, "error": str(sanitize(
                                payload.get("error", payload.get("errors", "Вызов отклонён"))))[:1600]})
                            recent_errors = recent_errors[-3:]
                            failed_calls += 1
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
                        recent_errors.clear()
                        failed_calls = 0
                        failure_guidance_used = False
                        loop_recovery_used = False
                    save()
                    if accepted_finish:
                        break
                    frontier_message = frontier_guide.message(state)
                    if frontier_message:
                        messages.append({"role": "user", "content": frontier_message})
                        record("graph_frontier_guidance", step=step, nodes=len(state.model.get("nodes", [])),
                               edges=len(state.model.get("edges", [])))
                    if _cycle(signatures) or idle >= 24:
                        if not loop_recovery_used and loop_recoveries < 3:
                            loop_recovery_used = True
                            loop_recoveries += 1
                            state.model["run"]["loop_recovery_attempted"] = True
                            state.model["run"]["loop_recoveries"] = loop_recoveries
                            messages.append({"role": "user", "content":
                                "Повторяются действия без новых данных. Ранее полученные результаты сохранены: "
                                "inspect_map(section='research_notes') — выводы и следующие действия; "
                                "research_history — прошлые запросы и ответы; read_coverage и read_fragments — "
                                "прочитанные строки. Восстанови нужное состояние и продолжи незавершённый участок. "
                                "research_tasks — вложенные исполнители/промпты и незавершённые участки. "
                                "Не начинай обзор заново. Если продолжение невозможно, сохрани пробелы и заверши через finish." +
                                _failure_guidance(recent_errors)})
                            record("research_loop_recovery", step=step, idle=idle)
                            signatures.clear()
                            idle = 0
                            save()
                            continue
                        state.model["run"].update(status="partial", reason="Исследование остановлено: повторение действий без нового результата.")
                        break
                    if failed_calls >= 3 and not failure_guidance_used:
                        # Varying a bad quote or its bounds evades exact-call cycle
                        # detection. One targeted reminder also covers that case.
                        failure_guidance_used = True
                        messages.append({"role": "user", "content":
                            "Несколько изменений подряд отклонены проверкой; карта от них не изменилась." +
                            _failure_guidance(recent_errors)})
                        record("validation_recovery_guidance", step=step, failed_calls=failed_calls)
                    if step >= max_steps - 3:
                        messages.append({"role": "user", "content":
                            "Лимит шагов почти исчерпан. Сохрани установленные операции и переходы, "
                            "опиши оставшиеся пробелы и заверши через finish."})
                    elif idle == 12:
                        messages.append({"role": "user", "content":
                            "Карта давно не менялась. Сохрани операции и переходы, уже установленные "
                            "прочитанным кодом, и продолжи текущий участок."})
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
