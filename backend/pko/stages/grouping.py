"""One bounded model request groups map operations into business stages.

The packet is compact: operation names, results and outcomes, transitions with
conditions and each operation's dominator as a hint for single-entry regions.
Code checks the partition and computes, per stage, whether it can be shown as
one block. The atomic map is never changed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pko.audit import record
from pko.observability.contracts import map_revision
from pko.observability.model_stage import PacketTooLarge, StageModel, methodology
from pko.scenario_map.quality import QualityError, ResearchQuality
from pko.stages.contracts import HIERARCHY_VERSION, validate_hierarchy
from pko.stages.regions import collapse_check, immediate_dominators, operation_graph

STAGE = "business_stages"
METHODOLOGY = Path(__file__).with_name("methodology")
MAX_STAGES = 60
_STAGE_ID = re.compile(r"stage-[1-9]\d{0,2}")
_STAGE_ERROR = re.compile(r"stage-[1-9]\d{0,2}:")
_UNPLACED = "Операции не распределены"
_STRINGS = {"type": "array", "items": {"type": "string"}}

TOOL = {
    "name": "submit_business_stages",
    "description": "Вернуть бизнес-этапы карты: цель, результат и операции каждого этапа; остальные операции — в unassigned с причиной.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["stages", "unassigned"], "properties": {
        "stages": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                   "required": ["id", "name", "goal", "result", "operation_ids"], "properties": {
                       "id": {"type": "string"}, "name": {"type": "string"}, "goal": {"type": "string"},
                       "result": {"type": "string"}, "operation_ids": _STRINGS, "note": {"type": "string"}}}},
        "unassigned": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "required": ["operation_id", "reason"], "properties": {
                           "operation_id": {"type": "string"}, "reason": {"type": "string"}}}}}},
}


def _short(value, limit: int) -> str:
    value = (value or "").strip() if isinstance(value, str) else ""
    return value if len(value) <= limit else value[:limit - 1] + "…"


def build_payload(model: dict, compact: bool = False) -> dict:
    """The map as the grouping sees it; ``compact`` drops prose when the window is tight."""
    graph = operation_graph(model)
    dominators = immediate_dominators(graph)
    scenario = model.get("scenario") or {}
    terminals = set(scenario.get("terminal_operation_ids") or [])
    operations = []
    for node in model.get("nodes") or []:
        row = {"id": node["id"], "name": _short(node.get("name"), 200), "dominator": dominators.get(node["id"])}
        if not compact:
            row["result"] = _short(node.get("output"), 200)
        if node.get("outcome") not in (None, "", "unknown"):
            row["outcome"] = node["outcome"]
        if node["id"] in terminals:
            row["terminal"] = True
            if node.get("completion_condition"):
                row["completion_condition"] = _short(node["completion_condition"], 200)
        operations.append(row)
    transitions = [{"from": edge["source"], "to": edge["target"],
                    **({"condition": _short(edge.get("condition"), 160)} if edge.get("condition") and not compact else {})}
                   for edge in model.get("edges") or [] if edge.get("kind") != "data"]
    brief = model.get("research_brief") or {}
    payload = {"scenario": {key: scenario.get(key) for key in ("title", "start", "end", "exclusions")},
               "operations": operations, "transitions": transitions}
    if not compact:
        payload["research_brief"] = {key: _short(brief.get(key), 1500) for key in ("project_overview", "scenario_alignment")}
        payload["process_description"] = _short(scenario.get("description"), 8000)
        groups = model.get("groups") or []
        if groups:
            # Older maps carry research-time groups; they are vocabulary, not a required partition.
            payload["research_groups"] = [{"name": group.get("name"), "operation_ids": [
                node["id"] for node in model["nodes"] if node.get("group_id") == group.get("id")]} for group in groups]
    return payload


def _validate(arguments: dict, operations: set[str]) -> tuple[list | None, dict, list[str]]:
    """Accepted stages, reasons for unassigned operations and addressed errors."""
    items = arguments.get("stages")
    if not isinstance(items, list):
        return None, {}, ["stages: ожидается список"]
    accepted, errors, owner = [], [], {}
    for item in items[:MAX_STAGES]:
        if not isinstance(item, dict) or not _STAGE_ID.fullmatch(str(item.get("id"))) or item.get("id") in {s["id"] for s in accepted}:
            errors.append("stages: у каждого этапа уникальный id вида stage-N")
            continue
        label, problems = item["id"], []
        fields = {}
        for name, limit in (("name", 300), ("goal", 1500), ("result", 1500)):
            try:
                fields[name] = ResearchQuality.business_text(item.get(name), name, limit)
            except QualityError as exc:
                problems.append(f"{label}: {exc}")
        members = item.get("operation_ids") or []
        unknown = [value for value in members if value not in operations]
        repeated = [value for value in members if value in owner]
        if not members or unknown:
            problems.append(f"{label}: operation_ids — существующие операции карты" + (f"; неизвестны: {', '.join(unknown[:8])}" if unknown else ""))
        if repeated:
            problems.append(f"{label}: операции уже входят в другие этапы: " + ", ".join(
                f"{value} ({owner[value]})" for value in repeated[:8]))
        errors += problems
        if problems:
            continue
        for value in members:
            owner[value] = label
        accepted.append({"id": label, **fields, "note": _short(item.get("note"), 1000),
                         "operation_ids": list(dict.fromkeys(members))})
    unassigned = {}
    for row in arguments.get("unassigned") or []:
        operation = row.get("operation_id") if isinstance(row, dict) else None
        if operation not in operations or operation in owner:
            errors.append(f"unassigned: {operation} — операция карты, не включённая в этапы")
        elif _short(row.get("reason"), 500):
            unassigned[operation] = _short(row["reason"], 500)
    missing = sorted(operations - set(owner) - set(unassigned))
    if missing:
        errors.append(f"{_UNPLACED}: " + ", ".join(missing[:30]) + "; включите их в этапы или в unassigned с причиной")
    return accepted, unassigned, errors


def build_hierarchy(model: dict, stage_model: StageModel, *, run_id: str | None = None) -> dict:
    """Ask once (plus one repair), then check membership and collapsibility."""
    operations = {node["id"] for node in model.get("nodes") or []}
    graph = operation_graph(model)
    latest: dict = {}

    def validate(arguments):
        stages, unassigned, errors = _validate(arguments, operations)
        if stages is not None:
            latest["unassigned"] = unassigned
            # Malformed stage lists of the latest answer; partition errors are recomputed below.
            latest["generic"] = [error for error in errors if error.startswith("stages:")]
        return stages, errors

    payload = build_payload(model)
    try:
        stages, errors = stage_model.ask(STAGE, payload, TOOL, validate, key=lambda stage: stage["id"])
    except PacketTooLarge:
        payload = build_payload(model, compact=True)
        stages, errors = stage_model.ask(STAGE, payload, TOOL, validate, key=lambda stage: stage["id"])
    # A repair answer comes first in the merged list; a stage kept from the first
    # answer cannot claim operations the repair assigned elsewhere.
    membership, kept = {}, []
    for stage in stages or []:
        members = [operation for operation in stage["operation_ids"] if operation not in membership]
        if members:
            membership.update(dict.fromkeys(members, stage["id"]))
            kept.append({**stage, "operation_ids": members})
    stages = kept
    reasons = {operation: reason for operation, reason in latest.get("unassigned", {}).items() if operation not in membership}
    unplaced = sorted(operations - set(membership) - set(reasons))
    for operation in unplaced:
        reasons[operation] = "Модель не отнесла операцию к этапу"
    # The outcome describes the final partition: errors of the first answer that
    # the repair resolved are gone, rejected stages stay reported.
    errors = [error for error in errors if _STAGE_ERROR.match(error)] + latest.get("generic", [])
    if unplaced:
        errors.append(f"{_UNPLACED}: " + ", ".join(unplaced[:30]))
    records = []
    for stage in stages:
        collapsible, entry, why = collapse_check(set(stage["operation_ids"]), graph)
        records.append({key: stage[key] for key in ("id", "name", "goal", "result", "note")} |
                       {"collapsible": collapsible, "entry_operation_id": entry if collapsible else None,
                        "collapse_reason": why})
    status = "complete" if stages and not errors else "partial" if stages else "none"
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    hierarchy = _hierarchy(model, records, membership, reasons, {"result": status, "reasons": errors[:20]},
                           run_id=run_id, input_sha256=digest, budget=stage_model.budget.snapshot())
    record("business_stages", stages=len(records), collapsible=sum(row["collapsible"] for row in records),
           unassigned=len(reasons), status=status)
    return hierarchy


def unavailable_hierarchy(model: dict, reason: str, *, run_id: str | None = None, budget: dict | None = None) -> dict:
    """Stages that could not be built: every operation stays unassigned with the reason."""
    operations = [node["id"] for node in model.get("nodes") or []]
    return _hierarchy(model, [], {}, dict.fromkeys(operations, "Этапы не построены"),
                      {"result": "none", "reasons": [reason]}, run_id=run_id, input_sha256=None, budget=budget)


def _hierarchy(model: dict, records: list, membership: dict, reasons: dict, status: dict, *,
               run_id, input_sha256, budget) -> dict:
    hierarchy = {"hierarchy_version": HIERARCHY_VERSION, "map_revision": map_revision(model), "run_id": run_id,
                 "map_run_id": (model.get("run") or {}).get("id"), "input_sha256": input_sha256,
                 "methodology_sha256": methodology(STAGE, METHODOLOGY)["sha256"],
                 "stages": records, "membership": membership,
                 "unassigned": [{"operation_id": operation, "reason": reason} for operation, reason in sorted(reasons.items())],
                 "status": status, "provenance": "model_proposal", "authority_status": "proposed", "budget": budget}
    validate_hierarchy(hierarchy, model)
    return hierarchy
