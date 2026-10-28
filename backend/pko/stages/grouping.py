"""One bounded model request groups map operations into business stages.

The packet is compact: operation names, results and outcomes, transitions with
conditions and each operation's dominator as a hint for single-entry regions.
Code checks the partition and computes, per stage, whether it can be shown as
one block. The atomic map is never changed.
"""
from __future__ import annotations

import re

from pko.audit import record
from pko.observability.contracts import map_revision
from pko.observability.model_stage import PacketTooLarge, StageModel, methodology
from pko.scenario_map.quality import QualityError, ResearchQuality
from pko.stages.contracts import HIERARCHY_VERSION, validate_hierarchy
from pko.stages.inputs import METHODOLOGY, STAGE, build_payload, input_fingerprint, payload_fingerprint
from pko.stages.regions import collapse_check, operation_graph

MAX_STAGES = 60
_STAGE_ID = re.compile(r"stage-[1-9]\d{0,2}")
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
        members = item.get("operation_ids")
        if not isinstance(members, list) or not members or any(not isinstance(value, str) for value in members):
            problems.append(f"{label}: operation_ids — непустой список строк с id операций")
            members = []
        unknown = [value for value in members if value not in operations]
        repeated = [value for value in members if value in owner]
        if unknown:
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
    rows = arguments.get("unassigned")
    if not isinstance(rows, list):
        errors.append("unassigned: ожидается список объектов operation_id/reason")
        rows = []
    for row in rows:
        operation = row.get("operation_id") if isinstance(row, dict) else None
        if not isinstance(operation, str):
            errors.append("unassigned: operation_id должен быть строкой с id операции")
        elif operation not in operations or operation in owner:
            errors.append(f"unassigned: {operation} — операция карты, не включённая в этапы")
        elif _short(row.get("reason"), 500):
            unassigned[operation] = _short(row["reason"], 500)
        else:
            errors.append(f"unassigned: {operation} — укажите непустую текстовую причину")
    missing = sorted(operations - set(owner) - set(unassigned))
    if missing:
        errors.append(f"{_UNPLACED}: " + ", ".join(missing[:30]) + "; включите их в этапы или в unassigned с причиной")
    return accepted, unassigned, errors


def build_hierarchy(model: dict, stage_model: StageModel, *, run_id: str | None = None) -> dict:
    """Ask once (plus one repair), then check membership and collapsibility."""
    operations = {node["id"] for node in model.get("nodes") or []}
    graph = operation_graph(model)
    latest: dict = {}
    first_accepted: dict | None = None
    first_unassigned: dict = {}

    def validate(arguments):
        nonlocal first_accepted, first_unassigned
        stages, unassigned, errors = _validate(arguments, operations)
        if stages is not None:
            conflicts = []
            if first_accepted is None:
                first_accepted = {stage["id"]: stage for stage in stages}
                first_unassigned = dict(unassigned)
            else:
                unassigned = {**first_unassigned, **unassigned}
                owners = {operation: stage["id"] for stage in first_accepted.values()
                          for operation in stage["operation_ids"]}
                accepted = []
                for stage in stages:
                    previous = first_accepted.get(stage["id"])
                    stolen = [operation for operation in stage["operation_ids"]
                              if operation in owners and owners[operation] != stage["id"]]
                    if previous and set(previous["operation_ids"]) != set(stage["operation_ids"]):
                        conflicts.append(f"{stage['id']}: состав ранее принятого этапа нельзя менять при исправлении")
                    elif stolen:
                        conflicts.append(f"{stage['id']}: операции уже приняты в других этапах: " + ", ".join(
                            f"{operation} ({owners[operation]})" for operation in stolen[:8]))
                    else:
                        accepted.append(stage)
                stages = accepted
                for operation in set(unassigned) & set(owners):
                    conflicts.append(f"unassigned: {operation} уже принят в {owners[operation]}")
                    del unassigned[operation]
            errors += conflicts
            latest["unassigned"] = unassigned
            # Malformed stage lists of the latest answer; partition errors are recomputed below.
            latest["generic"] = [error for error in errors if error.startswith(("stages:", "unassigned:"))]
            # StageModel retains accepted first-answer items; conflict diagnostics
            # must survive even when a retained item has the same stage id.
            latest["conflicts"] = conflicts
        return stages, errors

    input_mode = "full"
    payload = build_payload(model)
    try:
        stages, errors = stage_model.ask(STAGE, payload, TOOL, validate, key=lambda stage: stage["id"])
    except PacketTooLarge:
        input_mode = "compact"
        payload = build_payload(model, compact=True)
        stages, errors = stage_model.ask(STAGE, payload, TOOL, validate, key=lambda stage: stage["id"])
    # Keep each accepted semantic contract with its complete proposed membership.
    # Never manufacture a new stage by trimming conflicting operations.
    membership, kept = {}, []
    for stage in stages or []:
        members = stage["operation_ids"]
        if set(members) & set(membership):
            errors.append(f"{stage['id']}: конфликт состава этапов; этап отклонён целиком")
            continue
        membership.update(dict.fromkeys(members, stage["id"]))
        kept.append(stage)
    stages = kept
    reasons = {operation: reason for operation, reason in latest.get("unassigned", {}).items() if operation not in membership}
    unplaced = sorted(operations - set(membership) - set(reasons))
    for operation in unplaced:
        reasons[operation] = "Модель не отнесла операцию к этапу"
    # The outcome describes the final partition: errors of the first answer that
    # the repair resolved are gone, rejected stages stay reported.
    errors = [error for error in errors if not error.startswith(("stages:", "unassigned:", _UNPLACED))]
    errors = list(dict.fromkeys(errors + latest.get("generic", []) + latest.get("conflicts", [])))
    if unplaced:
        errors.append(f"{_UNPLACED}: " + ", ".join(unplaced[:30]))
    records = []
    for stage in stages:
        collapsible, entry, why = collapse_check(set(stage["operation_ids"]), graph)
        records.append({key: stage[key] for key in ("id", "name", "goal", "result", "note")} |
                       {"collapsible": collapsible, "entry_operation_id": entry if collapsible else None,
                        "collapse_reason": why})
    status = "complete" if stages and not errors else "partial" if stages else "none"
    hierarchy = _hierarchy(model, records, membership, reasons, {"result": status, "reasons": errors[:20]},
                           run_id=run_id, input_sha256=payload_fingerprint(payload), input_mode=input_mode,
                           budget=stage_model.budget.snapshot())
    record("business_stages", stages=len(records), collapsible=sum(row["collapsible"] for row in records),
           unassigned=len(reasons), status=status)
    return hierarchy


def unavailable_hierarchy(model: dict, reason: str, *, run_id: str | None = None, budget: dict | None = None) -> dict:
    """Stages that could not be built: every operation stays unassigned with the reason."""
    operations = [node["id"] for node in model.get("nodes") or []]
    # The failure may be in building the input itself; the fallback must not fail again.
    try:
        mode, digest = "full", input_fingerprint(model, "full")
    except Exception:
        mode = digest = None
    return _hierarchy(model, [], {}, dict.fromkeys(operations, "Этапы не построены"),
                      {"result": "none", "reasons": [reason]}, run_id=run_id,
                      input_sha256=digest, input_mode=mode, budget=budget)


def _hierarchy(model: dict, records: list, membership: dict, reasons: dict, status: dict, *,
               run_id, input_sha256, input_mode, budget) -> dict:
    hierarchy = {"hierarchy_version": HIERARCHY_VERSION, "map_revision": map_revision(model), "run_id": run_id,
                 "map_run_id": (model.get("run") or {}).get("id"), "input_sha256": input_sha256,
                 "input_mode": input_mode,
                 "methodology_sha256": methodology(STAGE, METHODOLOGY)["sha256"],
                 "stages": records, "membership": membership,
                 "unassigned": [{"operation_id": operation, "reason": reason} for operation, reason in sorted(reasons.items())],
                 "status": status, "provenance": "model_proposal", "authority_status": "proposed", "budget": budget}
    validate_hierarchy(hierarchy, model)
    return hierarchy
