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
from uuid import uuid4

from pko.audit import AuditedClient, record, sanitize
from pko.errors import LlmError
from pko.llm.client import ChatClient
from pko.llm.registry import DEFAULT_CONTEXT_WINDOW
from pko.llm.tracing import trace_client, trace_span, trace_tool_call
from pko.code_tools.tools import TOOL_SCHEMAS, ToolBox
from pko.scenario_map.schema import MapState
from pko.scenario_map.tools import TOOLS
from pko.scenario_map.model_retry import ModelDeadlineExceeded, call_model
from pko.scenario_map.context import compact_context, context_budget, UNFINISHED_TASK_STATUSES
from pko.scenario_map.compaction import compact_research_context
from pko.scenario_map.stability import MUTATIONS, mutation_snapshot, record_mutation
from pko.scenario_map.checkpoint import (CheckpointError, compatibility, make_checkpoint,
                                         validate_compatibility, checkpoint_scenario)

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


def _rejection_key(function: dict, payload: dict) -> str:
    """Identify the failed invariant, independently from cosmetic argument edits."""
    try:
        args = json.loads(function.get('arguments') or '{}')
    except (TypeError, ValueError):
        args = {}
    details = payload.get('details') or {}
    address = {key: args[key] for key in ('id', 'node_id', 'edge_id', 'task_id') if isinstance(args, dict) and key in args}
    if isinstance(details, dict):
        address.update({key: details[key] for key in ('node_id', 'edge_id', 'task_id', 'field') if key in details})
    # Changing the unread line number or quoted text is still the same citation
    # authority failure. A newly delivered required range changes its context.
    invariant = ('read_evidence_required' if payload.get('code') == 'invalid_evidence' else
                 payload.get('error', payload.get('errors', '')))
    return _json({'tool': function.get('name'), 'code': payload.get('code'),
                  'address': address, 'invariant': invariant})


class AddressedRejections:
    """Keep one problem's history across unrelated accepted edits.

    This feeds the existing recovery/stop guard; it has no retry loop or budget
    of its own. A new read or meaningful change to the addressed object earns a
    fresh attempt. Runtime counters are per attempt, as is the existing guard.
    """

    def __init__(self):
        self.problems: dict[str, dict] = {}
        self.current: str | None = None
        self.active: set[str] = set()

    def begin_step(self):
        self.current = None
        self.active.clear()

    def observe(self, function: dict, payload: dict, state: MapState):
        if payload.get('ok'):
            return
        try:
            arguments = json.loads(function.get('arguments') or '{}')
        except (TypeError, ValueError):
            return
        if not isinstance(arguments, dict):
            return
        details = payload.get('details') or {}
        if function.get('name') == 'patch_fragment' and isinstance(details, dict):
            # Track the addressed producer, not the whole changing patch. Its
            # index can move when independent edits are added to a later reply.
            handled = False
            for issue in details.get('issues', []):
                if not isinstance(issue, dict):
                    continue
                match = re.fullmatch(r'(steps|transitions)\[(\d+)\](?:\..*)?', str(issue.get('field', '')))
                if not match:
                    continue
                collection, index = match[1], int(match[2])
                rows = arguments.get(collection, [])
                if not isinstance(rows, list) or index >= len(rows) or not isinstance(rows[index], dict):
                    continue
                self.observe({'name': 'upsert_step' if collection == 'steps' else 'upsert_transition',
                              'arguments': _json(rows[index])},
                             {'ok': False, 'code': issue.get('code'), 'error': issue.get('problem', ''),
                              'details': {k: v for k, v in issue.items() if k not in ('field', 'code', 'problem')}}, state)
                handled = True
            if handled:
                return
        if function.get('name') == 'review_semantics' and isinstance(details, dict):
            handled = False
            issues = list(details.get('issues', [])) + list(details.get('cases', []))
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                nested_cases = issue.get('cases', [])
                if isinstance(nested_cases, list):
                    issues.extend(part for part in nested_cases if isinstance(part, dict))
                field = str(issue.get('field', ''))
                ids = {issue[k] for k in ('node_id', 'edge_id') if isinstance(issue.get(k), str)}
                for k in ('missing_node_ids', 'missing_edge_ids', 'unknown_ids'):
                    ids.update(v for v in issue.get(k, []) if isinstance(v, str))
                case_index = issue.get('case_index')
                case = None
                cases = arguments.get('route_cases', [])
                if type(case_index) is int and isinstance(cases, list) and 1 <= case_index <= len(cases):
                    case = cases[case_index-1]
                    if isinstance(case, dict):
                        ids.update(v for v in case.get('path', []) if isinstance(v, str))
                if not ids and field.startswith('checks.'):
                    check = arguments.get('checks', {}).get(field.split('.')[1], {})
                    if isinstance(check, dict):
                        for k in ('node_ids', 'edge_ids'):
                            ids.update(v for v in check.get(k, []) if isinstance(v, str))
                for identifier in sorted(ids):
                    self.observe({'name': 'review_semantics.' + field,
                                  'arguments': _json({'id': identifier})},
                                 {'ok': False, 'code': issue.get('code', payload.get('code')),
                                  'error': issue.get('problem', ''), 'details': {'field': field}}, state)
                    handled = True
            if handled:
                return
        identifiers = {str(value) for owner in (arguments, details) if isinstance(owner, dict)
                       for key, value in owner.items() if key in ('id', 'node_id', 'edge_id', 'task_id') and isinstance(value, str)}
        if not identifiers:
            return
        evidence_only = payload.get('code') == 'invalid_evidence'
        references = []
        def collect_refs(value):
            if isinstance(value, dict):
                if isinstance(value.get('path'), str):
                    references.append({key: value[key] for key in ('path', 'line_start', 'line_end') if key in value})
                for item in value.values():
                    collect_refs(item)
            elif isinstance(value, list):
                for item in value:
                    collect_refs(item)
        if evidence_only:
            collect_refs(arguments)
            collect_refs(details)
        context = self.context(state, identifiers, evidence_only, references)
        key = _rejection_key(function, payload)
        previous = self.problems.get(key)
        if not previous or previous['context'] != context:
            previous = {'context': context, 'count': 0, 'recovered': False, 'identifiers': identifiers,
                        'evidence_only': evidence_only, 'references': references}
            self.problems[key] = previous
        previous['count'] += 1
        self.active.add(key)
        self.current = max(self.active, key=lambda item: self.problems[item]['count'])
        # Bounded metadata only, not a second source of research facts.
        if len(self.problems) > 32:
            evicted = next(iter(self.problems))
            del self.problems[evicted]
            self.active.discard(evicted)
            self.current = max(self.active, key=lambda item: self.problems[item]['count'], default=None)
        if previous['count'] >= 3:
            record('addressed_rejection_repeated', tool=function.get('name'),
                   code=payload.get('code'), object_ids=sorted(identifiers),
                   count=previous['count'], recovery_used=previous['recovered'])

    @staticmethod
    def context(state: MapState, identifiers: set[str], evidence_only=False, references=()) -> str:
        if evidence_only:
            # Editing output/conditions cannot repair a citation to unread lines.
            # Only newly delivered requested source lines earn another attempt.
            coverage = []
            for ref in references:
                lines = state._reads.get(ref['path'], {})
                low, high = ref.get('line_start'), ref.get('line_end')
                visible = sorted(n for n in lines if not (type(low) is int and type(high) is int) or low <= n <= high)
                coverage.append((ref['path'], visible))
            if not references:
                coverage = [(path, sorted(lines)) for path, lines in sorted(state._reads.items())]
            # Evidence order and duplicate references do not deliver new lines.
            coverage = sorted({(path, tuple(lines)) for path, lines in coverage})
            return hashlib.sha256(_json(coverage).encode()).hexdigest()
        model = state.model
        related = {}
        for field in ('nodes', 'edges', 'research_tasks', 'execution_reviews'):
            related[field] = [state._fingerprint(row) for row in model.get(field, [])
                              if row.get('id') in identifiers]
        # A transition repair can change a node's valid continuation without
        # changing the node itself. Treat that as relevant progress too.
        related['incident_edges'] = [state._fingerprint(row) for row in model.get('edges', [])
                                     if row.get('source') in identifiers or row.get('target') in identifiers]
        related['read_lines'] = sum(len(lines) for lines in state._reads.values())
        return hashlib.sha256(_json(related).encode()).hexdigest()

    def refresh(self, state: MapState):
        # A later tool in the same reply may have actually repaired the object.
        for key in tuple(self.active):
            row = self.problems[key]
            if row['context'] != self.context(state, row['identifiers'], row['evidence_only'], row['references']):
                del self.problems[key]
                self.active.remove(key)
        self.current = max(self.active, key=lambda item: self.problems[item]['count'], default=None)

    def stalled(self) -> bool:
        return bool(self.current and self.problems[self.current]['count'] >= 3)

    def recovered(self) -> bool:
        return bool(self.current and self.problems[self.current]['recovered'])

    def mark_recovery(self):
        if self.current:
            self.problems[self.current].update(count=0, recovered=True)


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
    """Track accepted research state, not request identities or presentation edits."""
    snapshot = state.snapshot()
    snapshot.pop("research_history", None)
    snapshot.pop("research_history_evicted", None)
    # A new rejection is a diagnostic, not progress: counting it would let a
    # stream of failing calls defeat the loop guard.
    snapshot.pop("run", None)
    snapshot.pop("validation", None)
    snapshot.pop("research_coverage", None)
    # Registering more unexamined work is not progress in understanding it.
    snapshot['research_tasks'] = [task for task in snapshot.get('research_tasks', [])
                                  if task.get('status') != 'open']
    for node in snapshot.get('nodes', []):
        for key in ('name', 'basis', 'code_explanations'):
            node.pop(key, None)
    for edge in snapshot.get('edges', []):
        for key in ('basis', 'code_explanations', 'description'):
            edge.pop(key, None)
    return _json(snapshot)


def _tool_signature(function: dict, payload: dict) -> str:
    """Changing read bounds/query spelling cannot disguise the same returned data."""
    name = function.get('name')
    if name in READ_NAMES and payload.get('ok'):
        return _json({'tool': name, 'source_data': payload.get('source_data'),
                      'evidence_reference': payload.get('evidence_reference')})
    return _json(function)


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
        roster = [{key: str(node.get(key, ""))[:240] for key in ("id", "name")}
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
            "\nИсправь именно указанные в details случаи, объекты и диапазоны; остальное отправляй без изменений. "
            "Не повторяй неизменный вызов. Для оснований используй "
            "уже прочитанные path/line_start/line_end: буквальный текст подставляет код. "
            "Для существующего объекта используй его ID. Не обходи проверку и не выдумывай связь.")


class ResearchTools:
    def __init__(self, state: MapState, workspace):
        self.state = state
        self.box = ToolBox(workspace.tree, workspace.extraction)
        self.discovery_results: set[str] = set()
        # A resumed search already present in the bounded public cache is not
        # new progress. Missing/evicted payloads are never reconstructed here.
        offset = 1
        while True:
            page = state.research_memory.recall(offset=offset, limit=30)
            for row in page['items']:
                self._remember_discovery(row['name'], row.get('payload', {}))
            if not page['has_more']:
                break
            offset += len(page['items'])

    def _remember_discovery(self, name: str, payload: dict) -> None:
        # Reads advance only through the authoritative line ledger. Errors and
        # empty search results remain in memory but cannot reset a stuck loop.
        count = (payload.get('meta') or {}).get('count', 0)
        if (name not in READ_NAMES or name == 'read_file' or not payload.get('ok')
                or not isinstance(count, int) or count <= 0 or not payload.get('source_data')):
            return
        content = _json([name, payload['source_data']])
        self.discovery_results.add(hashlib.sha256(content.encode('utf-8')).hexdigest())

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
                    payload["research_tasks"] = [{k: t.get(k, []) if k in ("child_task_ids", "implementation") else t.get(k, "")
                                                  for k in ("id", "kind", "target", "status", "child_task_ids", "implementation")}
                        for t in self.state.model["research_tasks"]
                        if t['status'] in UNFINISHED_TASK_STATUSES and any(r['path'] == evidence_reference['path'] for r in t['evidence'])][:30]
                    if payload["research_tasks"]:
                        payload["research_tasks_hint"] = "Незавершённые задачи по этому файлу; полный реестр — inspect_map(section='research_tasks')."
                self._remember_discovery(name, payload)
                payload["memory"] = self.state.research_memory.observe(name, args, payload)
            elif name in {"upsert_step", "upsert_transition", "patch_fragment", "finish"} and not self.state.model.get("research_brief"):
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
              on_event: Callable[[str, dict], None] | None = None,
              resume_checkpoint: dict | None = None,
              run_metadata: dict | None = None) -> dict:
    if max_steps < 1 or not all(math.isfinite(value) and value > 0 for value in (max_seconds, model_timeout)):
        raise ValueError("Лимиты исследования и ожидания модели должны быть конечными положительными числами")
    if (isinstance(summary_max_tokens, bool) or not isinstance(summary_max_tokens, int) or
            not 512 <= summary_max_tokens <= 32768 or summary_mode not in {"standard", "inherit"}):
        raise ValueError("Суммаризация: summary_max_tokens от 512 до 32768; summary_mode standard или inherit")
    # Compatibility is checked before any checkpoint overwrite or model call.
    schemas = copy.deepcopy(TOOL_SCHEMAS) + copy.deepcopy(TOOLS)
    system = Path(__file__).with_name("system.md").read_text(encoding="utf-8")
    bundle = {"system_prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
              "tools_sha256": hashlib.sha256(_json(schemas).encode("utf-8")).hexdigest()}
    contract = compatibility(spec, bundle['system_prompt_sha256'], bundle['tools_sha256'],
                             summary_mode, summary_max_tokens)
    previous = {}
    if resume_checkpoint is not None:
        validate_compatibility(resume_checkpoint, spec, bundle['system_prompt_sha256'], bundle['tools_sha256'],
                               summary_mode, summary_max_tokens)
        if scenario != checkpoint_scenario(resume_checkpoint):
            raise CheckpointError("Описание варианта отличается от сохранённого checkpoint.")
        from pko.scenario_map.checkpoint_state import restore_state
        try:
            state = restore_state(workspace, resume_checkpoint['_resume']['state'])
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise CheckpointError('Нельзя восстановить checkpoint: ' + str(sanitize(str(exc))),
                                  'Проверьте исходные файлы, их порядок и целостность checkpoint. Сохранённый файл не изменён.') from exc
        previous = copy.deepcopy(state.model['run'])
    else:
        state = MapState(workspace, scenario)
    completion = previous.get('research_completion') or {}
    finished_research = (completion.get('status') in ('completed', 'partial') or
                         (previous.get('stop_kind') == 'finish' and previous.get('status') in ('completed', 'partial')))
    if finished_research and not completion:
        completion = {key: previous.get(key) for key in ('status', 'reason')}
    previous_steps = previous.get('total_steps', previous.get('steps', 0))
    previous_seconds = previous.get('total_duration_seconds', previous.get('duration_seconds', 0))
    if previous:
        history = copy.deepcopy(previous.get('attempt_history', []))
        history.append({key: previous.get(key) for key in
                        ('id', 'trace_id', 'attempt', 'status', 'stop_kind', 'reason', 'steps', 'duration_seconds')})
        state.model['run']['attempt_history'] = history[-100:]
        state.model['run']['attempt_history_omitted'] = previous.get('attempt_history_omitted', 0) + max(0, len(history)-100)
        state.model['run']['parent_run_id'] = previous.get('id')
        state.model['run']['root_run_id'] = previous.get('root_run_id') or previous.get('id')
        state.model['run']['attempt'] = previous.get('attempt', 1) + 1
        state.model['run']['resume_mode'] = 'publication' if finished_research else 'research'
    else:
        state.model['run']['attempt'] = 1
    for key in ('stop_kind', 'interrupted', 'termination_kind', 'grouping_status', 'trace_export', 'local_audit',
                'generated_at', 'duration_seconds', 'loop_recoveries', 'loop_recovery_attempted', 'checkpoint_persistence'):
        state.model['run'].pop(key, None)
    state.model["run"].update({"status": "running", "reason": "", "steps": 0, "model": spec.model,
                               "max_steps": max_steps, "model_timeout_seconds": model_timeout, "max_seconds": max_seconds})
    metadata = {'id': uuid4().hex, **(run_metadata or {})}
    state.model['run']['root_run_id'] = state.model['run'].get('root_run_id') or metadata['id']
    if metadata:
        state.model['run'].update({key: value for key, value in metadata.items()
                                  if key in ('id', 'trace_id', 'root_run_id', 'parent_run_id', 'attempt')})
    if finished_research:
        state.model['run'].update(status=completion['status'], stop_kind='finish', reason=completion.get('reason', ''))
    extra_body = getattr(spec, 'extra_body', None)
    template = extra_body.get('chat_template_kwargs') if isinstance(extra_body, dict) else None
    mode = template.get('enable_thinking') if isinstance(template, dict) else None
    state.model['run']['model_mode'] = 'reasoning' if mode is True else 'standard' if mode is False else 'provider_default'
    toolkit = ResearchTools(state, workspace)
    state.model["run"].update(bundle)
    state.model["run"]["reproducibility"] = copy.deepcopy(contract)
    record('research_configuration', model=spec.model, model_mode=state.model['run']['model_mode'],
           context_window=spec.context_window or DEFAULT_CONTEXT_WINDOW, max_steps=max_steps, max_seconds=max_seconds,
           semantic_profile=state.model.get('semantic_profile'), **bundle)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _json({
        "scenario_description": scenario, "sources": workspace.sources,
        "source_notes": workspace.notes, "source_omissions": getattr(workspace, "omissions", []),
        "instruction": ("Продолжи сохранённое исследование этого варианта. Принятые изменения и их ID сохранены; восстанови текущий участок из памяти." if previous else
                        "Восстанови карту именно этого варианта. Начни с ориентации в источниках.")})}]
    started = time.monotonic()
    signatures: list[str] = []
    rejected_signatures: list[str] = []
    idle, empty = 0, 0
    loop_recovery_used = False
    loop_recoveries = 0
    frontier_guide = GraphFrontierGuide()
    recent_errors: list[dict] = copy.deepcopy(previous.get('recent_rejections', []))[-3:]
    failed_calls = 0
    failure_guidance_used = False
    addressed_rejections = AddressedRejections()
    next_compaction_step = 1
    window = spec.context_window or DEFAULT_CONTEXT_WINDOW
    summary_tokens = min(summary_max_tokens, max(512, window // 4))
    message_budget, response_tokens = context_budget(window, schemas)
    state.model["run"]["context_budget_chars"] = message_budget
    state.model["run"].update(summary_max_tokens=summary_tokens, summary_mode=summary_mode)
    state.model["run"]["summary_effective_mode"] = (
        "standard" if summary_mode == "standard" and isinstance(template, dict) and "enable_thinking" in template
        else state.model['run']['model_mode'])
    audited_client = None
    checkpoint_failed = False
    if previous:
        # No old transcript, pending tool batch or provider reasoning is replayed.
        messages = compact_context(messages, state, message_budget, brief=_brief, force=True)
        frontier_guide.message(state)
        frontier_guide.at_link_progress = {node['id'] for node in state.model['nodes']}
        frontier_guide.at_last_nudge = set(frontier_guide.at_link_progress)
        record('research_resumed', parent_run_id=previous.get('id'), attempt=state.model['run']['attempt'],
               mode=state.model['run']['resume_mode'], checkpoint_sha256=resume_checkpoint['_resume']['state_sha256'],
               previous_steps=previous_steps, new_max_steps=max_steps, new_max_seconds=max_seconds)

    last_state_event = None

    def save():
        nonlocal last_state_event, checkpoint_failed
        state.model['run']['duration_seconds'] = round(time.monotonic() - started, 3)
        state.model['run']['total_duration_seconds'] = round(previous_seconds + state.model['run']['duration_seconds'], 3)
        state.model['run']['total_steps'] = previous_steps + state.model['run']['steps']
        snapshot = state.snapshot()
        coverage = snapshot.get('research_coverage') or {}
        validation = snapshot.get('validation') or {}
        scope = snapshot.get('scenario') or {}
        outgoing = {edge['source'] for edge in snapshot.get('edges', []) if edge.get('kind') != 'data'}
        terminals = set(scope.get('terminal_operation_ids', []))
        graph = {key: snapshot.get(key) for key in ('semantic_profile', 'scenario', 'nodes', 'edges')}
        event = {
            'semantic_profile': snapshot.get('semantic_profile'),
            'map_state_sha256': hashlib.sha256(_json(graph).encode('utf-8')).hexdigest(),
            'scope': {key: scope.get(key) for key in ('variant_context', 'instance_unit', 'variant_factors',
                                                     'start', 'end', 'exclusions')},
            'nodes': len(snapshot.get('nodes', [])), 'edges': len(snapshot.get('edges', [])),
            'gaps': len(snapshot.get('gaps', [])),
            'open_continuations': sum(n['id'] not in outgoing and n['id'] not in terminals
                                      for n in snapshot.get('nodes', [])),
            'coverage_status': coverage.get('status', 'unknown'),
            'partial_tasks': sum(row.get('status') == 'partial' for row in coverage.get('tasks', [])),
            'unmapped_actions': len(snapshot.get('detail_review', {}).get('unmapped_action_ids', [])),
            'structural_issues': len(validation.get('structural_issues', [])),
            'coverage_issues': len(validation.get('coverage_issues', [])),
            'status': snapshot['run'].get('status'), 'stop_kind': snapshot['run'].get('stop_kind'),
        }
        signature = _json(event)
        if signature != last_state_event:
            record('research_state', step=snapshot['run'].get('steps', 0), **event)
            last_state_event = signature
        if checkpoint and not checkpoint_failed:
            try:
                checkpoint(make_checkpoint(state, contract, snapshot))
            except Exception as exc:
                checkpoint_failed = True
                state.model['run']['checkpoint_persistence'] = 'failed'
                raise CheckpointError("Не удалось сохранить checkpoint; исследование остановлено.",
                                      str(sanitize(str(exc)))[:300]) from exc

    try:
        save()
        if finished_research:
            # A crash after accepted finish may precede HTML/group publication.
            # Restore those outputs without asking the researcher to run again.
            return state.snapshot()
        # The opening stays in context after every compaction, so it must fit the window itself.
        opening_size = sum(len(_json(m)) for m in messages)
        if opening_size + 1600 > message_budget:
            raise ValueError(f"Описание процесса и инструкции занимают {opening_size} символов и не помещаются в окно "
                             f"модели ({message_budget}). Сократите описание или используйте модель с большим контекстом.")
        if client is None:
            client = DeadlineClient(spec=spec, timeout=model_timeout)
            client.deadline = started + max_seconds
        client = trace_client(client, spec)
        audited_client = client if isinstance(client, AuditedClient) else AuditedClient(client)
        with trace_span("scenario_research", model=spec.model):
            for step in range(1, max_steps + 1):
                if time.monotonic() - started >= max_seconds:
                    state.model["run"].update(status="partial", stop_kind="time_limit", reason="Достигнут лимит времени исследования.")
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
                                       tools=schemas, temperature=0, max_tokens=response_tokens,
                                       recovery_max_tokens=min(32768, max(response_tokens, int(window -
                                           (sum(len(_json(m)) for m in messages) + len(_json(schemas))) / 1.6 - 2048))),
                                       context_window=window)
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
                            state.model["run"].update(status="partial", stop_kind="no_result", reason="Модель не сохранила результат инструментами.")
                            break
                        save()
                        continue
                    empty = 0
                    before = (_progress(state), len(toolkit.discovery_results))
                    accepted_finish = False
                    addressed_rejections.begin_step()
                    for call in calls:
                        proposed_name = (call.get("function") or {}).get("name", "")
                        mutation_before = mutation_snapshot(state.model) if proposed_name in MUTATIONS else None
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
                        if mutation_before is not None:
                            record_mutation(mutation_before, state.model, name, payload)
                        if not payload.get("ok"):
                            addressed_rejections.observe(function, payload, state)
                            # Keep the address of the problem (code, case, object,
                            # needed ranges): a bare message made the model resend
                            # the same rejected arguments.
                            rejection = {"tool": name, "code": payload.get("code", ""), "error": str(sanitize(
                                payload.get("error", payload.get("errors", payload.get("source_data", "Вызов отклонён")))))[:1600]}
                            if payload.get("details"):
                                details = sanitize(payload["details"])
                                rejection["details"] = details if len(_json(details)) <= 3000 else {"truncated": _json(details)[:3000]}
                            recent_errors.append(rejection)
                            recent_errors = recent_errors[-3:]
                            state.model["run"]["recent_rejections"] = copy.deepcopy(recent_errors)
                            failed_calls += 1
                            rejected_signatures.append(_rejection_key(function, payload))
                            rejected_signatures = rejected_signatures[-12:]
                        signature = _tool_signature(function, payload)
                        signatures.append(signature)
                        signatures = signatures[-12:]
                        if name == "finish" and payload.get("ok"):
                            accepted_finish = True
                            state.model["run"]["stop_kind"] = "finish"
                            state.model["run"]["research_completion"] = {
                                "status": payload['status'], "reason": payload.get('reason', ''), "step": step}
                        # Commit each tool before callbacks or the next call in
                        # this batch; resume never replays the discarded suffix.
                        save()
                        if on_event:
                            on_event("tool", {"name": name, "ok": payload.get("ok"), "step": step,
                                              "nodes": len(state.model.get("nodes", [])),
                                              "edges": len(state.model.get("edges", []))})
                    # Only new successful discovery content, read lines or
                    # accepted research changes can clear the failure history.
                    addressed_rejections.refresh(state)
                    changed = before != (_progress(state), len(toolkit.discovery_results))
                    idle = 0 if changed else idle + 1
                    cleanup_changed = bool(recent_errors or failed_calls or failure_guidance_used or loop_recovery_used)
                    if changed:
                        signatures.clear()
                        rejected_signatures.clear()
                        if addressed_rejections.current is None:
                            recent_errors.clear()
                            state.model["run"]["recent_rejections"] = []
                        failed_calls = 0
                        failure_guidance_used = False
                        loop_recovery_used = False
                    if changed and cleanup_changed:
                        save()
                    if accepted_finish:
                        break
                    frontier_message = frontier_guide.message(state)
                    if frontier_message:
                        messages.append({"role": "user", "content": frontier_message})
                        record("graph_frontier_guidance", step=step, nodes=len(state.model.get("nodes", [])),
                               edges=len(state.model.get("edges", [])))
                    addressed_stall = addressed_rejections.stalled()
                    if _cycle(signatures) or _cycle(rejected_signatures) or idle >= 24 or failed_calls >= 8 or addressed_stall:
                        if not loop_recovery_used and loop_recoveries < 3 and not (addressed_stall and addressed_rejections.recovered()):
                            loop_recovery_used = True
                            loop_recoveries += 1
                            if addressed_stall:
                                addressed_rejections.mark_recovery()
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
                            record("research_loop_recovery", step=step, idle=idle, failed_calls=failed_calls)
                            signatures.clear()
                            rejected_signatures.clear()
                            idle = 0
                            failed_calls = 0
                            save()
                            continue
                        state.model["run"].update(status="partial", stop_kind="no_progress", reason="Исследование остановлено: повторение действий без нового результата.")
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
                state.model["run"].update(status="partial", stop_kind="step_limit", reason="Достигнут лимит шагов исследования.")
    except CheckpointError as exc:
        state.model['run'].update(status='partial' if state.model.get('nodes') else 'failed',
                                  stop_kind='checkpoint_error', reason=exc.render())
        record('checkpoint_failed', reason=exc.render())
    except ModelDeadlineExceeded as exc:
        state.model["run"].update(status="partial", stop_kind="time_limit", reason=str(exc))
    except KeyboardInterrupt:
        state.model["run"].update(status="partial", stop_kind="cancelled", reason="Исследование прервано пользователем.", interrupted=True)
    except Exception as exc:
        hint = str(sanitize(getattr(exc, "hint", "")))[:500]
        record("scenario_error", error_type=type(exc).__name__, error_message=str(exc), hint=hint)
        state.model["run"].update(status="partial" if state.model.get("nodes") else "failed", stop_kind="error",
                                  reason=str(sanitize(f"Ошибка исследования: {exc}")) + (f" {hint}" if hint else ""))
    finally:
        if audited_client is not None:
            audited_client.flush()
    state.model["run"]["duration_seconds"] = round(time.monotonic() - started, 3)
    if state.model["run"].get("status") == "running":
        state.model["run"].update(status="partial", stop_kind="no_finish", reason="Исследование остановлено без принятого завершения.")
    if not checkpoint_failed:
        try:
            save()
        except CheckpointError as exc:
            state.model['run'].update(status='partial' if state.model.get('nodes') else 'failed',
                                      stop_kind='checkpoint_error', reason=exc.render())
            record('checkpoint_failed', reason=exc.render())
    return state.snapshot()
