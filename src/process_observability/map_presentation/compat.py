"""Use the current producer contract for action maps and explicit legacy checks.

New-profile map revisions and grouping validation have one implementation in pko.
Maps with no profile keep the older presentation contract without being relabeled.
"""
from __future__ import annotations


class HierarchyError(ValueError):
    pass


from pko.stages.inputs import ACTION_PROFILE, map_revision
from pko.stages.contracts import validate_hierarchy as validate_current_hierarchy


def validate_hierarchy(hierarchy: dict, model: dict) -> None:
    """Structure and references only; the business meaning of a stage is not proven here."""
    profile = model.get("semantic_profile")
    if profile not in (None, "", ACTION_PROFILE):
        raise HierarchyError("unsupported map semantic profile")
    if profile == ACTION_PROFILE:
        validate_current_hierarchy(hierarchy, model)
        return
    if not isinstance(hierarchy, dict):
        raise HierarchyError("hierarchy must be an object")
    if hierarchy.get("map_revision") != map_revision(model):
        raise HierarchyError("hierarchy belongs to another map revision")
    operations = {node.get("id") for node in model.get("nodes") or [] if isinstance(node, dict)}
    stages = hierarchy.get("stages") or []
    if not isinstance(stages, list) or any(not isinstance(stage, dict) for stage in stages):
        raise HierarchyError("stages must be a list of objects")
    stage_ids = [stage.get("id") for stage in stages]
    if len(set(stage_ids)) != len(stage_ids) or any(not isinstance(value, str) or not value for value in stage_ids):
        raise HierarchyError("stage ids must be unique nonempty strings")
    membership = hierarchy.get("membership") or {}
    rows = hierarchy.get("unassigned") or []
    if not isinstance(membership, dict) or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HierarchyError("membership must be an object and unassigned a list of objects")
    unassigned = {row.get("operation_id") for row in rows}
    if set(membership) - operations or unassigned - operations:
        raise HierarchyError("membership refers to operations outside the map")
    if set(membership.values()) - set(stage_ids):
        raise HierarchyError("membership refers to an unknown stage")
    if set(membership) & unassigned:
        raise HierarchyError("an operation is both in a stage and unassigned")
    for stage in stages:
        members = {operation for operation, owner in membership.items() if owner == stage["id"]}
        if not members:
            raise HierarchyError(f"{stage['id']}: a stage has at least one operation")
        if stage.get("collapsible") and stage.get("entry_operation_id") not in members:
            raise HierarchyError(f"{stage['id']}: a collapsible stage enters through one of its operations")
