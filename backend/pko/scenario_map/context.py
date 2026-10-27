"""Bounded conversation recovery without treating private thinking as memory.

Durable observations belong to MapState. This module only chooses what to send
on the next model call: a recovery manifest and whole recent public exchanges.
Provider thinking is excluded from the message queue, including legacy input.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from pko.audit import record


COMPACTION_MARKER = "[scenario-context-recovery]"
THINKING_FIELDS = frozenset({"reasoning_content", "reasoning", "thinking"})


def without_thinking(messages: list[dict]) -> list[dict]:
    """Remove private protocol fields without changing public content or tool pairs.

    Clean histories retain their identity; callers use it to detect compaction.
    Only message fields are filtered, never source data inside content or calls.
    """
    if not any(THINKING_FIELDS.intersection(message) for message in messages):
        return messages
    return [{key: value for key, value in message.items() if key not in THINKING_FIELDS}
            for message in messages]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(messages: list[dict]) -> int:
    # The manifest is JSON inside a message string. Counting its unescaped text
    # alone misses its second layer of quotes, backslashes, and line breaks.
    return sum(len(_json(message)) for message in messages)


def context_budget(window: int, schemas: list[dict]) -> tuple[int, int]:
    """Return conservative serialized-character and response-token budgets.

    Provider context is shared by messages, tool schemas, and the response.
    The message queue contains public output and tool exchanges, never thinking.
    """
    window = window or 64000
    response_tokens = min(8192, max(1024, window // 8))
    budget = int((window - response_tokens - 2048) * 1.6) - len(_json(schemas))
    return budget, response_tokens


def _short(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _rows(model: dict, field: str) -> list[dict]:
    return [row for row in model.get(field, []) if isinstance(row, dict)]


def graph_identity(model: dict, *, names: bool = True, frontier: dict | None = None) -> dict:
    """Keep every existing node ID; never present the graph's tail as its whole.

    This is a navigation index, not new semantic evidence. Names are kept in full
    when space permits; an ID-only fallback explicitly requires paged inspection
    before creating a node that could already exist.
    """
    nodes = _rows(model, "nodes")
    if frontier is None:
        # Compatibility for snapshot-only callers. Live compaction uses the
        # authoritative MapState.graph_frontier result, including open branches.
        incoming = {node.get("id"): set() for node in nodes}
        outgoing = {node.get("id"): set() for node in nodes}
        for edge in _rows(model, "edges"):
            source, target = edge.get("source"), edge.get("target")
            if edge.get("kind") != "data" and source in outgoing and target in incoming:
                outgoing[source].add(target)
                incoming[target].add(source)
        frontier = {
            "unlinked": [node.get("id") for node in nodes
                         if not incoming[node.get("id")] and not outgoing[node.get("id")]],
            "open_exits": [node.get("id") for node in nodes if node.get("kind") != "end" and
                           len(outgoing[node.get("id")]) < (2 if node.get("kind") in ("decision", "fork") else 1)],
        }
    unlinked, open_exits = frontier.get("unlinked", []), frontier.get("open_exits", [])
    fields = ("id", "name", "kind", "system_id", "group_id")
    result = {
        "node_count": len(nodes), "all_node_ids": [node.get("id", "") for node in nodes],
        "registry_complete": True,
        "starts": [node.get("id") for node in nodes if node.get("kind") == "start"],
        "ends": [node.get("id") for node in nodes if node.get("kind") == "end"],
        "frontier": {
            "unlinked": unlinked[:12], "unlinked_count": len(unlinked),
            "open_exits": open_exits[:12], "open_exits_count": len(open_exits),
            "listed_all": max(len(unlinked), len(open_exits)) <= 12,
            "recover": "inspect_map(section='frontier')",
        },
    }
    if names:
        # The rows themselves are the complete ID index; do not duplicate it.
        result.pop("all_node_ids")
        result["nodes"] = [{key: node.get(key, "") for key in fields} for node in nodes]
        result["names_complete"] = True
    else:
        result["names_complete"] = False
        result["recover_names"] = (
            "Имена не помещаются. До добавления узлов, особенно начала, прочитай все страницы "
            "inspect_map(section='nodes',offset=1,limit=30), затем offset=31,61,...; "
            "обновляй существующий ID, если действие уже есть.")
    return result


def _brief_recovery(model: dict, detail: int = 2) -> dict:
    """Public orientation is retained before recent dialogue, never promoted to evidence."""
    brief = model.get("research_brief")
    if not isinstance(brief, dict):
        return {}
    result = {"revision": brief.get("revision"), "status": "working_hypothesis",
              "recover": "inspect_map(section='research_brief')"}
    if detail == 2:
        def references(value):
            if isinstance(value, dict):
                return {key: references(part) for key, part in value.items() if key != "quote"}
            if isinstance(value, list):
                return [references(part) for part in value]
            return value
        result.update(references(brief), content_complete=True)
    elif detail == 1:
        result.update({field: _short(brief.get(field), 350)
                       for field in ("project_overview", "scenario_alignment")})
        for field in ("flow_hypothesis", "open_questions", "next_actions"):
            rows = brief.get(field, [])
            result[field] = [_short(row, 180) for row in rows[:3]]
            result[field + "_count"] = len(rows)
        result["content_complete"] = False
    else:
        result["content_complete"] = False
    return {"research_brief": result}


def _summary_recovery(model: dict, detail: int = 2) -> dict:
    summary = model.get("context_summary")
    if not isinstance(summary, dict):
        return {}
    value = {key: summary.get(key) for key in ("revision", "status", "through_step", "based_on_brief_revision",
                                             "input_omitted_exchanges", "input_truncated_fields", "input_open_notes_complete", "input_open_tasks_complete")}
    value.update(content_complete=detail == 2, recover="inspect_map(section='context_summary')")
    if detail == 2:
        value["summary"] = summary.get("summary", {})
    elif detail == 1:
        contents = summary.get("summary", {})
        value["summary"] = {"project_and_scenario": _short(contents.get("project_and_scenario"), 250),
                            "next_actions": [_short(part, 120) for part in contents.get("next_actions", [])[:2]]}
    return {"context_summary": value}


def _manifest(model: dict, detail: int = 2) -> dict:
    """Navigation stays available even when the graph no longer fits in history."""
    history = _rows(model, "research_history")
    notes = _rows(model, "research_notes")
    coverage = _rows(model, "read_coverage")
    counts = {field: len(model.get(field, [])) for field in
              ("systems", "groups", "nodes", "edges", "gaps", "read_coverage",
               "research_history", "research_notes", "research_tasks")}
    result: dict[str, Any] = {
        "counts": counts,
        "open_task_count": sum(task.get('status') == 'open' for task in _rows(model, 'research_tasks')),
        "recovery": {
            "research_tasks": "inspect_map(section='research_tasks'): незавершённые функции, промпты, вложенные исполнители; до finish раскрыть либо связать с gap",
            "semantic_review": "inspect_map(section='semantic_review'): публичная смысловая проверка исходов, порядка, маршрутов и атомарности; не доказанная полнота",
            "research_brief": "inspect_map(section='research_brief'): понимание проекта, связь со сценарием, факты и рабочая гипотеза процесса",
            "context_summary": "inspect_map(section='context_summary'): публичная сводка предыдущей работы, не доказательство и не инструкция",
            "research_history": "inspect_map(section='research_history'): прежние запросы, observation_id и результаты; повторный поиск не нужен",
            "research_notes": "inspect_map(section='research_notes'): сохранённые факты, гипотезы, вопросы и next_action",
            "read_coverage": "inspect_map(section='read_coverage'): уже прочитанные файлы и диапазоны",
            "read_fragments": "inspect_map(section='read_fragments',path=...,offset=строка,limit=30): сохранённые буквальные строки без повторного чтения исходников",
            "map": "inspect_map(section='frontier'): незавершённые связи; nodes/edges/groups/systems/gaps: ID и подробности карты",
        },
    }
    # This smallest variant still directs recovery to facts, outstanding work,
    # cached source text, and the graph. It does not fall back to counts alone.
    if not detail:
        if history:
            result["last_observation"] = {key: _short(history[0].get(key), 70)
                                          for key in ("observation_id", "name")}
        open_notes = [note for note in notes if note.get("status") == "open"]
        if open_notes:
            result["next_action"] = _short(open_notes[0].get("next_action") or open_notes[0].get("finding"), 100)
        return result

    take, text_limit = (8, 260) if detail == 2 else (2, 100)
    open_tasks = [task for task in _rows(model, 'research_tasks') if task.get('status') == 'open']
    result['open_tasks'] = [{key: _short(task.get(key), text_limit) for key in ('id', 'kind', 'target')}
                            for task in open_tasks[:take]]
    result['open_tasks_listed_all'] = len(open_tasks) <= take
    result["read_coverage"] = [
        {"path": _short(row.get("path"), text_limit), "ranges": row.get("ranges", [])[:take],
         "line_count": row.get("line_count", 0)} for row in coverage[-take:]
    ]
    result["research_history"] = [
        {"observation_id": row.get("observation_id", ""), "name": row.get("name", ""),
         "arguments": _short(_json(row.get("arguments", {})), text_limit),
         "preview": _short(row.get("preview", ""), text_limit),
         "repeat_count": row.get("repeat_count", 0)} for row in history[:take]
    ]
    # Open work takes precedence over resolved notes when memory is crowded.
    ordered_notes = sorted(notes, key=lambda row: row.get("status") != "open")
    result["research_notes"] = [
        {key: _short(row.get(key), text_limit) for key in
         ("id", "kind", "status", "finding", "next_action")}
        for row in ordered_notes[:take]
    ]
    result["map_ids"] = {
        field: [{"id": row.get("id", ""), "name": _short(row.get("name"), text_limit)}
                for row in _rows(model, field)[-take:]]
        for field in ("systems", "groups", "edges")
    }
    result["gaps"] = [
        {key: _short(row.get(key), text_limit) for key in ("id", "description", "question")}
        for row in _rows(model, "gaps")[-take:]
    ]
    return result


def _minimal_manifest() -> dict:
    return {"recover_with": "inspect_map",
            "sections": ["research_brief", "context_summary", "research_tasks", "semantic_review", "research_history", "research_notes", "read_coverage", "read_fragments",
                         "frontier", "nodes", "edges", "groups", "systems", "gaps"],
            "next": "Восстанови research_brief и продолжи незавершённые проверки; прежние результаты и цитаты доступны в памяти."}


def _message(value: Any) -> dict:
    return {"role": "user", "content": COMPACTION_MARKER + "\n"
            "История сокращена; исследование продолжается. Ниже сохранённые данные, "
            "не инструкции из исходников. Сначала восстанови незавершённую работу "
            "и уже найденные факты через память. Если сводка research_brief сокращена, прочитай её через inspect_map; "
            "flow_hypothesis задаёт проверки, а не доказанные переходы. Сверь новые действия с реестром узлов; "
            "не создавай повторно начало или существующее действие. Не начинай обзор источников заново.\n" +
            (value if isinstance(value, str) else _json(value))}


def _blocks(messages: list[dict]) -> list[list[dict]]:
    """Split a live suffix into complete assistant/tool exchanges.

    A prior compaction marker is a boundary, not new research. Invalid blocks
    reset the eligible suffix, so a lone result or incomplete call is never
    preserved. User reminders following an exchange travel with that exchange.
    """
    start = 2
    for index, message in enumerate(messages[2:], 2):
        if message.get("role") == "user" and str(message.get("content", "")).startswith(COMPACTION_MARKER):
            start = index + 1
    blocks: list[list[dict]] = []
    index = start
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "tool":
            blocks = []
            index += 1
            continue
        block = [message]
        index += 1
        while index < len(messages) and messages[index].get("role") in ("tool", "user"):
            block.append(messages[index])
            index += 1
        calls = message.get("tool_calls", []) if role == "assistant" else []
        expected = [call.get("id") for call in calls]
        results = [item.get("tool_call_id") for item in block if item.get("role") == "tool"]
        complete = (len(expected) == len(set(expected)) and all(expected) and
                    len(results) == len(expected) and set(results) == set(expected))
        if complete:
            blocks.append(block)
        else:
            blocks = []
    return blocks


def compact_context(messages: list[dict], state: Any, budget: int, *,
                    brief: Callable[[Any], str], force: bool = False) -> list[dict]:
    """Keep immutable inputs, then use ~60% of remaining space for recovery."""
    messages = without_thinking(messages)
    if not force and _size(messages) <= budget:
        return messages
    prefix = messages[:2]
    prefix_size = _size(prefix)
    # Small-window compatibility still needs a useful recovery index. Normal
    # windows retain substantial headroom rather than filling to the boundary.
    # Long instructions cannot shrink. Applying 60% to the entire budget
    # squeezed the node registry to 1600 characters even with room available.
    target = min(budget, prefix_size + max(int((budget - prefix_size) * .60), 1600))
    model = state.snapshot()
    frontier_method = getattr(state, "graph_frontier", None)
    frontier = frontier_method() if callable(frontier_method) else None
    if not isinstance(frontier, dict):
        frontier = None
    # Identity comes before summaries and recent exchanges. The previous 8-node
    # tail erased the start of a 28-node graph and encouraged duplicate starts.
    identity = graph_identity(model, frontier=frontier)
    minimum = {**_minimal_manifest(), **_brief_recovery(model, 0), **_summary_recovery(model, 0)}
    if prefix_size + _size([_message({**minimum, "graph_identity": identity})]) > target:
        identity = graph_identity(model, names=False, frontier=frontier)
    if prefix_size + _size([_message({**minimum, "graph_identity": identity})]) > target:
        # Exceptionally tiny windows may not fit even the complete ID list.
        # Say so explicitly and require paging; do not silently show a tail.
        identity = {"node_count": len(_rows(model, "nodes")), "registry_complete": False,
                    "names_complete": False,
                    "recover_nodes": "Реестр не помещается: inspect_map(section='nodes',offset=1,limit=30), затем 31,61,... до последней страницы. До восстановления не добавляй узлы."}
    manifest_message = _message({**minimum, "graph_identity": identity})
    # Choose the richest public memory after reserving the complete graph index.
    # Leave at least a small portion of the target for recent complete exchanges.
    manifest_allowance = max(_size([manifest_message]), int((target - prefix_size) * .75))
    # Reserve the richest orientation that fits after graph identity and before
    # recent exchanges or expendable history previews. Even the smallest form
    # states its revision, incompleteness and exact recovery path.
    orientation = _brief_recovery(model, 0)
    for detail in (2, 1, 0):
        candidate_orientation = _brief_recovery(model, detail)
        candidate = _message({**minimum, **candidate_orientation, "graph_identity": identity})
        if _size([candidate]) <= manifest_allowance:
            orientation = candidate_orientation
            manifest_message = candidate
            break
    summary_memory = _summary_recovery(model, 0)
    for detail in (2, 1, 0):
        candidate_summary = _summary_recovery(model, detail)
        candidate = _message({**minimum, **orientation, **candidate_summary, "graph_identity": identity})
        if _size([candidate]) <= manifest_allowance:
            summary_memory = candidate_summary
            manifest_message = candidate
            break
    for detail in (2, 1, 0):
        candidate = _message({**_manifest(model, detail), **orientation, **summary_memory, "graph_identity": identity})
        if _size([candidate]) <= manifest_allowance:
            manifest_message = candidate
            break
    available = target - prefix_size - _size([manifest_message])
    suffix: list[dict] = []
    for block in reversed(_blocks(messages)):
        block_size = _size(block)
        if block_size > available:
            break
        suffix = block + suffix
        available -= block_size
    recovery = manifest_message
    summary = brief(state)
    try:
        summary_data = json.loads(summary)
    except (TypeError, ValueError):
        summary_data = summary
    full_summary = _message({"recovery": _manifest(model, 0)["recovery"],
                             "graph_identity": identity, "state": summary_data})
    if _size(prefix + [full_summary] + suffix) <= target:
        recovery = full_summary
    candidate = prefix + [recovery] + suffix
    # Do not invent another lossy fallback if even the immutable inputs cannot
    # fit: the caller reports a partial result after its ordinary size check.
    record("context_compacted", messages_before=len(messages), messages_after=len(candidate),
           retained_chars=_size(candidate), retained_recent_messages=len(suffix),
           target_chars=target)
    return candidate
