"""Process report contract: a separate layer over one revision of the map.

``scenario_map.json`` stays canonical; operations gain no observability fields.
Every reference into the map is ``(map_revision, operation_id)``. Code can only
propose: no stage of this report may claim reviewed or accepted authority.
"""
from __future__ import annotations

import hashlib
import json

REPORT_VERSION = "0.1"
EXECUTION_STATUSES = ("not_run", "running", "completed", "failed", "blocked", "interrupted")
RESULT_STATUSES = ("none", "complete", "partial", "stale")
# None: linkage was not evaluated because the mapping stage failed.
LINK_STATUSES = ("linked", "ambiguous", "unlinked", None)
LINK_BASES = ("operation_evidence", "code_explanation", None)
STAGES = {"logging_inventory": "Каталог мест логирования",
          "event_operation_mapping": "Кандидаты связи с операциями"}


class ReportError(ValueError):
    pass


def map_revision(model: dict) -> str:
    """Digest of what the map asserts about execution; run metadata is excluded."""
    content = {key: model.get(key) for key in ("schema_version", "scenario", "nodes", "edges")}
    data = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def stage_result(stage_id: str, execution_status: str, result_status: str = "none",
                 reasons: list[str] | None = None, counts: dict | None = None) -> dict:
    if stage_id not in STAGES:
        raise ReportError(f"unknown stage: {stage_id}")
    if execution_status not in EXECUTION_STATUSES or result_status not in RESULT_STATUSES:
        raise ReportError(f"invalid status for {stage_id}: {execution_status}/{result_status}")
    if execution_status != "completed" and result_status == "complete":
        raise ReportError(f"{stage_id}: only a completed stage can have a complete result")
    return {"id": stage_id, "title": STAGES[stage_id], "execution_status": execution_status,
            "result_status": result_status, "reasons": list(reasons or []), "counts": dict(counts or {})}


def validate_report(report: dict, model: dict) -> None:
    """Structural checks only; they do not establish what any event means."""
    if not isinstance(report, dict) or report.get("report_version") != REPORT_VERSION:
        raise ReportError("unsupported process report version")
    revision = map_revision(model)
    if report.get("map_revision") != revision:
        raise ReportError("process report belongs to another map revision")
    stages = report.get("stages")
    if not isinstance(stages, list) or len({row.get("id") for row in stages}) != len(stages):
        raise ReportError("stage results must be a list with unique ids")
    for row in stages:
        stage_result(row.get("id"), row.get("execution_status"), row.get("result_status"))
    logging = report.get("logging") or {}
    sites = logging.get("sites") or []
    site_ids = [site.get("id") for site in sites]
    if len(set(site_ids)) != len(site_ids):
        raise ReportError("logging site ids must be unique")
    operations = {node.get("id") for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    for site in sites:
        if site.get("link_status") not in LINK_STATUSES or site.get("link_basis") not in LINK_BASES:
            raise ReportError(f"{site.get('id')}: unknown link status or basis")
        if any(operation_id not in operations for operation_id in site.get("operation_ids") or []):
            raise ReportError(f"{site.get('id')}: refers to an operation outside the map")
    for binding in logging.get("bindings") or []:
        if binding.get("map_revision") != revision or binding.get("operation_id") not in operations:
            raise ReportError(f"{binding.get('id')}: refers to another map revision or operation")
        if binding.get("site_id") not in site_ids:
            raise ReportError(f"{binding.get('id')}: refers to an unknown logging site")
        if binding.get("authority_status") != "proposed":
            raise ReportError(f"{binding.get('id')}: only a proposed binding can be produced by code")
