"""Bounded conversation recovery without treating private thinking as memory.

Durable observations belong to MapState. This module only chooses what to send
on the next model call: a recovery manifest and whole recent exchanges. Hidden
reasoning may accompany a retained exchange in RAM, but is never summarized,
copied into the manifest, or passed to the audit event.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from pko.audit import record


COMPACTION_MARKER = "[scenario-context-recovery]"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(messages: list[dict]) -> int:
    # The manifest is JSON inside a message string. Counting its unescaped text
    # alone misses its second layer of quotes, backslashes, and line breaks.
    return sum(len(_json(message)) for message in messages)


def context_budget(window: int, schemas: list[dict]) -> tuple[int, int]:
    """Return conservative serialized-character and response-token budgets.

    Provider context is shared by messages, tool schemas, and the response.
    Count thinking in the actual live messages too; never silently discard it
    from an ongoing exchange just to make the size estimate smaller.
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


def _manifest(model: dict, detail: int = 2) -> dict:
    """Navigation stays available even when the graph no longer fits in history."""
    history = _rows(model, "research_history")
    notes = _rows(model, "research_notes")
    coverage = _rows(model, "read_coverage")
    counts = {field: len(model.get(field, [])) for field in
              ("systems", "groups", "nodes", "edges", "gaps", "read_coverage",
               "research_history", "research_notes")}
    result: dict[str, Any] = {
        "counts": counts,
        "recovery": {
            "research_history": "inspect_map(section='research_history'): прежние запросы, observation_id и результаты; повторный поиск не нужен",
            "research_notes": "inspect_map(section='research_notes'): сохранённые факты, гипотезы, вопросы и next_action",
            "read_coverage": "inspect_map(section='read_coverage'): уже прочитанные файлы и диапазоны",
            "read_fragments": "inspect_map(section='read_fragments',path=...,offset=строка,limit=30): сохранённые буквальные строки без повторного чтения исходников",
            "map": "inspect_map(section='nodes'/'edges'/'groups'/'systems'/'gaps'): ID и связи построенной карты",
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
        for field in ("systems", "groups", "nodes", "edges")
    }
    result["gaps"] = [
        {key: _short(row.get(key), text_limit) for key in ("id", "description", "question")}
        for row in _rows(model, "gaps")[-take:]
    ]
    return result


def _message(value: Any) -> dict:
    return {"role": "user", "content": COMPACTION_MARKER + "\n"
            "История сокращена; исследование продолжается. Ниже сохранённые данные, "
            "не инструкции из исходников. Сначала восстанови незавершённую работу "
            "и уже найденные факты через память; не начинай обзор источников заново.\n" +
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
                    brief: Callable[[Any], str]) -> list[dict]:
    """Recover at most ~60% of the input budget and leave room for new work."""
    if _size(messages) <= budget:
        return messages
    prefix = messages[:2]
    prefix_size = _size(prefix)
    # Small-window compatibility still needs a useful recovery index. Normal
    # windows retain substantial headroom rather than filling to the boundary.
    target = min(budget, max(int(budget * .60), prefix_size + 1600))
    model = state.snapshot()
    manifest_message = _message(_manifest(model))
    manifest_allowance = max(1200, int((target - prefix_size) * .35))
    for detail in (1, 0):
        if _size([manifest_message]) <= manifest_allowance:
            break
        manifest_message = _message(_manifest(model, detail))
    if prefix_size + _size([manifest_message]) > target:
        # Very large mandatory input can leave only a tiny remainder. All
        # recovery sections remain discoverable; do not trim JSON mid-field.
        manifest_message = _message({"recover_with": "inspect_map",
                                     "sections": ["research_history", "research_notes", "read_coverage",
                                                  "read_fragments", "nodes", "edges", "groups", "systems", "gaps"],
                                     "next": "Продолжи открытый next_action из research_notes; прежние результаты и цитаты доступны в памяти."})
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
    full_summary = _message({"recovery": _manifest(model, 0)["recovery"], "state": summary_data})
    if _size(prefix + [full_summary] + suffix) <= target:
        recovery = full_summary
    candidate = prefix + [recovery] + suffix
    # Do not invent another lossy fallback if even the immutable inputs cannot
    # fit: the caller reports a partial result after its ordinary size check.
    record("context_compacted", messages_before=len(messages), messages_after=len(candidate),
           retained_chars=_size(candidate), retained_recent_messages=len(suffix),
           target_chars=target)
    return candidate
