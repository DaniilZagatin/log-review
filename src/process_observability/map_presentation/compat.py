"""Producer-compatible helpers for the vendored map renderer, without the producer runtime.

``map_revision`` is an unchanged copy of producer ``backend/pko/stages/inputs.py::map_revision``.
``validate_hierarchy`` is the structural part of producer
``backend/pko/stages/contracts.py::validate_hierarchy``: this side cannot recompute the
producer's grouping-input and methodology digests, so it checks the exact map revision and
every reference instead. A hierarchy remains a model proposal either way.
"""
from __future__ import annotations

import hashlib
import json


class HierarchyError(ValueError):
    pass


def map_revision(model: dict) -> str:
    """Digest of what the map asserts about execution; run metadata is excluded."""
    content = {key: model.get(key) for key in ("schema_version", "scenario", "nodes", "edges")}
    data = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate_hierarchy(hierarchy: dict, model: dict) -> None:
    """Structure and references only; the business meaning of a stage is not proven here."""
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
