"""Hierarchy contract: business stages over one revision of the atomic map.

Membership ``operation_id → stage_id`` is stored once; inputs, continuations,
outcomes and gaps of a stage are always derived from the atomic map. Stages are
proposals: nothing here can claim they were reviewed or accepted.
"""
from __future__ import annotations

import re

from pko.stages.inputs import (ACTION_PROFILE, INPUT_MODES, METHODOLOGY, STAGE, grouping_stage,
                               hierarchy_kind, input_fingerprint, map_revision, methodology)

HIERARCHY_VERSION = "1.1"
OPERATIONS_HIERARCHY_VERSION = "1.2"
_STAGE_ID = re.compile(r"stage-[1-9]\d{0,2}")


class HierarchyError(ValueError):
    pass


def validate_hierarchy(hierarchy: dict, model: dict) -> None:
    """Structure and references only; the business meaning of a stage is not proven here."""
    action_profile = model.get("semantic_profile") == ACTION_PROFILE
    if model.get("semantic_profile") not in (None, "", ACTION_PROFILE):
        raise HierarchyError("unsupported map semantic profile")
    version = OPERATIONS_HIERARCHY_VERSION if action_profile else HIERARCHY_VERSION
    if not isinstance(hierarchy, dict) or hierarchy.get("hierarchy_version") != version:
        raise HierarchyError("unsupported hierarchy version")
    if action_profile:
        if hierarchy.get("semantic_profile") != ACTION_PROFILE or hierarchy.get("hierarchy_kind") != hierarchy_kind(model):
            raise HierarchyError("hierarchy uses another semantic profile")
    elif hierarchy.get("hierarchy_kind", STAGE) != STAGE or hierarchy.get("semantic_profile"):
        raise HierarchyError("legacy map requires business-stage semantics")
    if hierarchy.get("map_revision") != map_revision(model):
        raise HierarchyError("hierarchy belongs to another map revision")
    # Only a hierarchy that proposes nothing may lack an input: building that input is what failed.
    unbuilt = (hierarchy.get("input_mode") is None and hierarchy.get("input_sha256") is None
               and not hierarchy.get("stages") and not hierarchy.get("membership")
               and (hierarchy.get("status") or {}).get("result") == "none")
    if not unbuilt and hierarchy.get("input_mode") not in INPUT_MODES:
        raise HierarchyError("unknown business-stage input mode")
    if not unbuilt and hierarchy.get("input_sha256") != input_fingerprint(model, hierarchy["input_mode"]):
        raise HierarchyError("hierarchy belongs to another grouping input")
    if hierarchy.get("methodology_sha256") != methodology(grouping_stage(model), METHODOLOGY)["sha256"]:
        raise HierarchyError("hierarchy uses another grouping methodology")
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
        if action_profile and (not isinstance(stage.get("boundary"), str) or not stage["boundary"].strip()):
            raise HierarchyError(f"{stage['id']}: a business operation needs a completion boundary")
        members = {operation for operation, owner in membership.items() if owner == stage["id"]}
        if not members:
            raise HierarchyError(f"{stage['id']}: a stage has at least one operation")
        if stage.get("collapsible") and stage.get("entry_operation_id") not in members:
            raise HierarchyError(f"{stage['id']}: a collapsible stage enters through one of its operations")
