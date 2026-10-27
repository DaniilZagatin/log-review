"""Proposed instrumentation changes for facts with coverage gaps.

Code checks references, keeps an unknown place of change as ``unresolved``
with a search task, verifies anchored places against issued lines, sets the
status of every event key itself and rejects dependency cycles. Changes are a
draft for developers; nothing is applied to the analysed sources.
"""
from __future__ import annotations

import re

from pko.observability.context import confirmed_bindings, text
from pko.observability.evidence import EvidenceRegistry
from pko.observability.facts import LIFECYCLES
from pko.observability.model_stage import BudgetExhausted, PacketTooLarge, StageModel

STAGE = "instrumentation"
ACTIONS = ("map_existing", "join", "enrich", "add_field", "add_event", "move_emit", "fix_delivery")
TIMINGS = ("before_commit", "after_commit", "after_response", "on_failure", "unknown")
MAX_CHANGES = 40
FACTS_PER_PACKET = 10
CONTEXT_LINES = 8
_CHANGE_ID = re.compile(r"c-[1-9]\d{0,2}")
_STRINGS = {"type": "array", "items": {"type": "string"}}
_FIELD = {"type": "object", "additionalProperties": False, "required": ["name", "purpose"], "properties": {
    "name": {"type": "string"}, "type": {"type": "string"}, "source": {"type": "string"}, "purpose": {"type": "string"}}}

TOOL = {
    "name": "submit_instrumentation_changes",
    "description": "Вернуть минимальные доработки логирования для фактов с пробелами.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["changes"], "properties": {
        "changes": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["id", "action", "fact_ids", "question", "location", "change", "event", "validation"],
                    "properties": {
                        "id": {"type": "string"}, "action": {"type": "string", "enum": list(ACTIONS)},
                        "fact_ids": _STRINGS, "operation_ids": _STRINGS, "question": {"type": "string"},
                        "existing_binding_id": {"type": "string"},
                        "why_existing_insufficient": {"type": "string"},
                        "location": {"type": "object", "additionalProperties": False, "required": ["status"], "properties": {
                            "status": {"type": "string", "enum": ["anchored", "unresolved"]},
                            "path": {"type": "string"}, "line_start": {"type": "integer"}, "line_end": {"type": "integer"},
                            "search_task": {"type": "string"}}},
                        "change": {"type": "string"},
                        "event": {"type": "object", "additionalProperties": False, "properties": {
                            "key": {"type": "string"}, "fields": {"type": "array", "items": _FIELD},
                            "lifecycle": {"type": "string", "enum": list(LIFECYCLES)},
                            "timing": {"type": "string", "enum": list(TIMINGS)},
                            "identity": {"type": "string"}, "dedup": {"type": "string"}}},
                        "depends_on": _STRINGS, "validation": {"type": "string"}}}}}},
}


def find_cycle(changes: list[dict]) -> list[str]:
    """IDs on dependency cycles (iterative Kahn; unknown references are checked separately)."""
    ids = {change["id"] for change in changes}
    waiting = {change["id"]: len(set(change["depends_on"]) & ids) for change in changes}
    dependants: dict[str, list[str]] = {}
    for change in changes:
        for dependency in set(change["depends_on"]) & ids:
            dependants.setdefault(dependency, []).append(change["id"])
    ready = [name for name, count in waiting.items() if count == 0]
    while ready:
        for dependant in dependants.get(ready.pop(), ()):
            waiting[dependant] -= 1
            if waiting[dependant] == 0:
                ready.append(dependant)
    return sorted(name for name, count in waiting.items() if count)


def settle_dependencies(changes: list[dict], earlier: list[dict] = ()) -> tuple[list[dict], list[str]]:
    """Keep only changes whose dependencies resolve, transitively and without cycles."""
    kept, problems = list(changes), []
    while True:
        known = {change["id"] for change in [*earlier, *kept]}
        broken = [change for change in kept if any(ref not in known for ref in change["depends_on"])]
        for change in broken:
            missing = [ref for ref in change["depends_on"] if ref not in known]
            problems.append(f"{change['id']}: depends_on ссылается на отсутствующие изменения {', '.join(missing)}")
        cycle = set(find_cycle([*earlier, *kept])) - {change["id"] for change in earlier}
        if cycle:
            problems.append(f"Зависимости образуют цикл: {', '.join(sorted(cycle))}")
        if not broken and not cycle:
            return kept, problems
        kept = [change for change in kept if change not in broken and change["id"] not in cycle]


def _fragments(model: dict, report: dict, facts: list[dict], registry: EvidenceRegistry) -> list[dict]:
    """Issue the emit sites observing these facts and the cited code of their operations."""
    nodes = {node["id"]: node for node in model.get("nodes") or []}
    sites = {site["id"]: site for site in report["logging"].get("sites") or []}
    bindings = {row["id"]: row for row in report["logging"].get("event_bindings") or []}
    ranges: list[tuple[str, int, int, dict]] = []
    for fact in facts:
        for ref in fact["observed_by"]:
            site = sites.get(bindings.get(ref, {}).get("site_id"))
            if site:
                ranges.append((site["path"], site["line_start"] - CONTEXT_LINES, site["line_end"] + CONTEXT_LINES,
                               {"binding_id": ref}))
        for operation_id in fact["operation_ids"]:
            for ref in (nodes.get(operation_id) or {}).get("evidence") or []:
                ranges.append((ref["path"], ref["line_start"] - CONTEXT_LINES, ref["line_end"] + CONTEXT_LINES,
                               {"operation_id": operation_id, "cited_lines": [ref["line_start"], ref["line_end"]]}))
    fragments, seen = [], set()
    for path, start, end, about in ranges:
        if (path, start, end) in seen:
            continue
        seen.add((path, start, end))
        fragment = registry.fragment(path, start, end)
        if fragment:
            fragments.append({"id": f"fragment-{len(fragments) + 1}", "about": about, **fragment})
    return fragments


def build_changes(model: dict, report: dict, stage_model: StageModel, registry: EvidenceRegistry,
                  profile: dict | None = None, reserve: int = 0,
                  fact_ids: set[str] | None = None) -> tuple[list, list[str], list[str]]:
    """Return accepted changes, errors and IDs of gap facts left without a packet.

    A packet too large for the provider window is split; a single fact that
    still does not fit is left pending with the others the budget did not reach.
    """
    coverage = {row["fact_id"]: row for row in report["coverage"]["facts"]}
    facts = {fact["id"]: fact for fact in report["facts"]}
    gap_facts = [facts[fact_id] for fact_id, row in coverage.items()
                 if (row["gaps"] or row["unknowns"]) and (fact_ids is None or fact_id in fact_ids)]
    operations = {node["id"] for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    bindings = {row["binding_id"]: row for row in confirmed_bindings(report)}
    context = report["process_context"]
    # Changes already in the report stay referable; new IDs never collide with them.
    accepted: list[dict] = list(report.get("instrumentation") or []) if fact_ids is not None else []
    earlier = len(accepted)
    errors: list[str] = []
    pending: list[str] = []
    queue = [gap_facts[index:index + FACTS_PER_PACKET] for index in range(0, len(gap_facts), FACTS_PER_PACKET)]
    while queue:
        packet = queue.pop(0)
        packet_ids = {fact["id"] for fact in packet}
        payload = {"process_context": {key: context[key] for key in ("goal", "case", "measurement_questions")},
                   "facts_with_gaps": [{**fact, "coverage": coverage[fact["id"]]} for fact in packet],
                   "event_bindings": [row for row in bindings.values()
                                      if any(row["binding_id"] in fact["observed_by"] or row["operation_id"] in fact["operation_ids"]
                                             for fact in packet)],
                   "earlier_changes": [{"id": change["id"], "action": change["action"], "fact_ids": change["fact_ids"]}
                                       for change in accepted],
                   "fragments": _fragments(model, report, packet, registry),
                   "domain_field_catalog": profile["event_field_catalog"] if profile else []}
        taken = {change["id"] for change in accepted}

        def validate(arguments, packet_ids=packet_ids, taken=taken):
            items = arguments.get("changes")
            if not isinstance(items, list):
                return None, ["changes: ожидается список"]
            result, problems, seen = [], [], set(taken)
            for item in items[:MAX_CHANGES]:
                change, item_errors = _validate_change(item, packet_ids, operations, bindings, registry, seen)
                problems += item_errors
                if change is not None:
                    seen.add(change["id"])
                    result.append(change)
            result, dependency_problems = settle_dependencies(result, accepted)
            return result, problems + dependency_problems

        try:
            result, problems = stage_model.ask(STAGE, payload, TOOL, validate, reserve=reserve,
                                               key=lambda change: change["id"])
        except PacketTooLarge as exc:
            if len(packet) > 1:
                middle = len(packet) // 2
                queue[:0] = [packet[:middle], packet[middle:]]
            else:
                pending.append(packet[0]["id"])
                errors.append(str(exc))
            continue
        except BudgetExhausted as exc:
            errors.append(str(exc))
            return accepted[earlier:], errors, pending + [fact["id"] for rest in [packet, *queue] for fact in rest]
        # Items kept across a repair are settled again against everything accepted so far.
        settled, dependency_problems = settle_dependencies(result or [], accepted)
        accepted += settled
        errors += problems + [problem for problem in dependency_problems if problem not in problems]
    return accepted[earlier:], errors, pending


def _validate_change(item, packet_ids, operations, bindings, registry: EvidenceRegistry, seen) -> tuple[dict | None, list[str]]:
    if not isinstance(item, dict) or not _CHANGE_ID.fullmatch(str(item.get("id"))) or item.get("id") in seen:
        return None, ["changes: у каждого изменения уникальный id вида c-N"]
    label, errors = item["id"], []
    action, fact_ids = item.get("action"), item.get("fact_ids") or []
    operation_ids = item.get("operation_ids") or []
    if action not in ACTIONS:
        errors.append(f"{label}: action — значение из перечня")
    if not fact_ids or any(ref not in packet_ids for ref in fact_ids):
        errors.append(f"{label}: fact_ids — факты из facts_with_gaps")
    if any(ref not in operations for ref in operation_ids):
        errors.append(f"{label}: operation_ids только из карты")
    for name in ("question", "change", "validation"):
        if not text(item.get(name)):
            errors.append(f"{label}: заполни {name}")
    event = item.get("event") if isinstance(item.get("event"), dict) else {}
    existing = item.get("existing_binding_id")
    if action == "map_existing" and existing not in bindings:
        errors.append(f"{label}: для map_existing укажи existing_binding_id подтверждённой записи")
    if action == "add_event" and not text(item.get("why_existing_insufficient")):
        errors.append(f"{label}: для add_event объясни why_existing_insufficient")
    if event.get("timing", "unknown") not in TIMINGS or event.get("lifecycle", "result") not in LIFECYCLES:
        errors.append(f"{label}: event.timing и event.lifecycle — значения из перечней")
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    anchor = None
    if location.get("status") == "anchored":
        refs, problems = registry.resolve([{key: location.get(key) for key in ("path", "line_start", "line_end")}],
                                          f"{label}: location")
        errors += problems
        anchor = refs[0] if refs else None
    elif location.get("status") == "unresolved":
        if not text(location.get("search_task")):
            errors.append(f"{label}: для unresolved опиши search_task — что и где найти")
    else:
        errors.append(f"{label}: location.status — anchored или unresolved")
    if errors:
        return None, errors
    key = text(event.get("key"), 200)
    existing_keys = {row["key_candidate"] for row in bindings.values() if row["key_status"] in ("literal", "resolved_constant")}
    # The model never sets agreement: code labels the key by where it comes from.
    key_status = ("not_set" if not key else "proposed" if action == "add_event" else
                  "existing" if key in existing_keys else "proposed")
    fields = [{name: text(field.get(name), 300) for name in ("name", "type", "source", "purpose")}
              for field in event.get("fields") or [] if isinstance(field, dict) and text(field.get("name"))][:24]
    return {"id": label, "action": action, "fact_ids": list(dict.fromkeys(fact_ids)),
            "operation_ids": list(dict.fromkeys(operation_ids)), "question": text(item["question"], 500),
            "existing_binding_id": existing if action == "map_existing" else None,
            "why_existing_insufficient": text(item.get("why_existing_insufficient"), 1000),
            "location": ({"status": "anchored", **anchor} if anchor else
                         {"status": "unresolved", "search_task": text(location.get("search_task"), 500)}),
            "change": text(item["change"], 1500),
            "event": {"key": key or None, "key_status": key_status, "fields": fields,
                      "lifecycle": event.get("lifecycle"), "timing": event.get("timing", "unknown"),
                      "identity": text(event.get("identity"), 500), "dedup": text(event.get("dedup"), 500)},
            "depends_on": list(dict.fromkeys(ref for ref in item.get("depends_on") or [] if isinstance(ref, str))),
            "validation": text(item["validation"], 1000), "provenance": "model_proposal",
            "authority_status": "proposed"}, []
