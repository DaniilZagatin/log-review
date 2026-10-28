"""Hierarchy contract: business stages over one revision of the atomic map.

Membership ``operation_id → stage_id`` is stored once; inputs, continuations,
outcomes and gaps of a stage are always derived from the atomic map. Stages are
proposals: nothing here can claim they were reviewed or accepted.
"""
from __future__ import annotations

import re

from pko.observability.contracts import map_revision

HIERARCHY_VERSION = "1.0"
_STAGE_ID = re.compile(r"stage-[1-9]\d{0,2}")


class HierarchyError(ValueError):
    pass


def validate_hierarchy(hierarchy: dict, model: dict) -> None:
    """Structure and references only; the business meaning of a stage is not proven here."""
    if not isinstance(hierarchy, dict) or hierarchy.get("hierarchy_version") != HIERARCHY_VERSION:
        raise HierarchyError("unsupported hierarchy version")
    if hierarchy.get("map_revision") != map_revision(model):
        raise HierarchyError("hierarchy belongs to another map revision")
    if hierarchy.get("authority_status") != "proposed":
        raise HierarchyError("business stages stay a proposal")
    operations = {node["id"] for node in model.get("nodes") or []}
    stages = hierarchy.get("stages") or []
    stage_ids = [stage.get("id") for stage in stages]
    if len(set(stage_ids)) != len(stage_ids) or any(not _STAGE_ID.fullmatch(str(value)) for value in stage_ids):
        raise HierarchyError("stage ids must be unique stage-N")
    membership = hierarchy.get("membership") or {}
    unassigned = {row.get("operation_id") for row in hierarchy.get("unassigned") or []}
    if set(membership) - operations or unassigned - operations:
        raise HierarchyError("membership refers to operations outside the map")
    if set(membership.values()) - set(stage_ids):
        raise HierarchyError("membership refers to an unknown stage")
    if set(membership) & unassigned or (set(membership) | unassigned) != operations:
        raise HierarchyError("every operation is either in one stage or explicitly unassigned")
    for stage in stages:
        members = {operation for operation, owner in membership.items() if owner == stage["id"]}
        if not members:
            raise HierarchyError(f"{stage['id']}: a stage has at least one operation")
        if stage.get("collapsible") and stage.get("entry_operation_id") not in members:
            raise HierarchyError(f"{stage['id']}: a collapsible stage enters through one of its operations")
