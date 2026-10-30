"""The grouping input and its reproducible fingerprint, independent of the runner."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from pko.stages.regions import immediate_dominators, operation_graph

STAGE = "business_stages"
OPERATIONS_STAGE = "business_operations"
ACTION_PROFILE = "variant-actions-v1"
METHODOLOGY = Path(__file__).with_name("methodology")
INPUT_MODES = ("full", "compact")


def grouping_stage(model: dict) -> str:
    """Select semantics explicitly; an old map never acquires the new meaning."""
    profile = model.get("semantic_profile")
    if profile not in (None, "", ACTION_PROFILE):
        raise ValueError("unsupported map semantic profile for grouping")
    return OPERATIONS_STAGE if profile == ACTION_PROFILE else STAGE


def hierarchy_kind(model: dict) -> str:
    return grouping_stage(model)


def _short(value, limit: int) -> str:
    value = value.strip() if isinstance(value, str) else ""
    return value if len(value) <= limit else value[:limit - 1] + "…"


def build_payload(model: dict, compact: bool = False) -> dict:
    """Keep execution semantics intact; compact mode omits only optional context.

    Conditions, results and operation descriptions are not shortened: removing
    their end can turn an exception into an unconditional result. If even the
    compact packet does not fit, the caller keeps the atomic map instead.
    """
    grouping_stage(model)
    dominators = immediate_dominators(operation_graph(model))
    scenario = model.get("scenario") or {}
    terminals = set(scenario.get("terminal_operation_ids") or [])
    operations = []
    for node in model.get("nodes") or []:
        row = {key: deepcopy(node.get(key, "")) for key in (
            "id", "name", "description", "input", "condition", "effect_kind", "continuation_mode",
            "join_mode", "outcome", "completion_condition", "evidence_status")}
        row.update(result=deepcopy(node.get("output", "")), dominator=dominators.get(node["id"]),
                   terminal=node["id"] in terminals)
        row["external_interactions"] = [{key: deepcopy(item[key]) for key in (
            "name", "purpose", "direction", "endpoint", "contract") if key in item}
            for item in node.get("external_interactions") or []]
        operations.append(row)
    transitions = [{"id": edge.get("id"), "from": edge["source"], "to": edge["target"],
                    "kind": edge.get("kind", "sequence"), "condition": deepcopy(edge.get("condition", "")),
                    "evidence_status": edge.get("evidence_status", "unknown")}
                   for edge in model.get("edges") or []]
    payload = {"scenario": {key: deepcopy(scenario.get(key)) for key in (
        "title", "start", "end", "exclusions", "assumptions", "entry_operation_ids", "terminal_operation_ids")},
        "operations": operations, "transitions": transitions,
        "gaps": [{key: deepcopy(value) for key, value in gap.items() if key not in ("evidence", "code_explanations")}
                 for gap in model.get("gaps") or []]}
    if model.get("semantic_profile") == ACTION_PROFILE:
        payload["semantic_profile"] = ACTION_PROFILE
        payload["hierarchy_kind"] = OPERATIONS_STAGE
        # Keep the variant and its instance distinct, in both packet modes.
        for key in ("process_name", "coverage", "variant_context", "instance_unit", "variant_factors"):
            if key in scenario:
                payload["scenario"][key] = deepcopy(scenario[key])
        if "research_coverage" in model:
            payload["research_coverage"] = deepcopy(model["research_coverage"])
        payload["map_status"] = {
            "run_status": (model.get("run") or {}).get("status"),
            "reason": (model.get("run") or {}).get("reason"),
            "validation": deepcopy(model.get("validation") or {}),
        }
    if not compact:
        brief = model.get("research_brief") or {}
        payload["research_brief"] = {key: _short(brief.get(key), 1500) for key in ("project_overview", "scenario_alignment")}
        payload["process_description"] = _short(scenario.get("description"), 8000)
        groups = model.get("groups") or []
        if groups:
            # Research-time groups supply vocabulary, never a required partition.
            payload["research_groups"] = [{"name": deepcopy(group.get("name")), "operation_ids": [
                node["id"] for node in model.get("nodes") or [] if node.get("group_id") == group.get("id")]} for group in groups]
    return payload


def input_fingerprint(model: dict, mode: str) -> str:
    if mode not in INPUT_MODES:
        raise ValueError("unknown business-stage input mode")
    return payload_fingerprint(build_payload(model, compact=mode == "compact"))


def payload_fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def map_revision(model: dict) -> str:
    """Digest of what the map asserts about execution; run metadata is excluded."""
    content = {key: model.get(key) for key in ("schema_version", "scenario", "nodes", "edges")}
    if "semantic_profile" in model:
        content["semantic_profile"] = model["semantic_profile"]
    data = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def methodology(stage: str = STAGE, folder: Path = METHODOLOGY) -> dict:
    """Request instructions: the folder's shared rules plus the stage file, with a content digest."""
    common = "business_operations_common.md" if stage == OPERATIONS_STAGE else "common.md"
    text = (folder / common).read_text(encoding="utf-8") + "\n\n" + \
        (folder / f"{stage}.md").read_text(encoding="utf-8")
    return {"id": stage, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}
