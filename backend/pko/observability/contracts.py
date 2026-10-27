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
          "event_operation_mapping": "Кандидаты связи с операциями",
          "event_binding_review": "Смысл мест логирования",
          "process_context": "Контекст процесса и вопросы измерения",
          "fact_requirements": "Необходимые бизнес-факты",
          "logging_coverage": "Покрытие фактов логированием",
          "instrumentation_plan": "Доработки логирования",
          "metric_catalog": "Определения метрик",
          "requirement_reconciliation": "Факты, запрошенные метриками"}
BINDING_STATUSES = ("confirmed", "no_operation", "unresolved", "conflict")
EVIDENCE_DECISIONS = ("bind", "no_operation")


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
    binding_ids = set()
    for binding in logging.get("event_bindings") or []:
        name = binding.get("id")
        if name in binding_ids or binding.get("site_id") not in site_ids:
            raise ReportError(f"{name}: duplicate event binding or unknown logging site")
        binding_ids.add(name)
        if binding.get("map_revision") != revision or binding.get("status") not in BINDING_STATUSES:
            raise ReportError(f"{name}: refers to another map revision or has an unknown status")
        if binding.get("authority_status") != "proposed":
            raise ReportError(f"{name}: a model interpretation stays a proposal")
        if binding.get("decision") == "bind" and binding.get("operation_id") not in operations:
            raise ReportError(f"{name}: refers to an operation outside the map")
        if binding.get("decision") in EVIDENCE_DECISIONS and not binding.get("evidence"):
            raise ReportError(f"{name}: an interpretation of a logging site needs cited lines")

    _validate_downstream(report, operations, binding_ids, {
        row["id"] for row in logging.get("event_bindings") or [] if row.get("status") == "confirmed"})


def _proposal(row: dict, label: str) -> None:
    if row.get("authority_status") != "proposed":
        raise ReportError(f"{label}: only a specialist can accept a proposal")


def _validate_downstream(report: dict, operations: set, binding_ids: set, confirmed: set) -> None:
    from pko.observability.instrumentation import find_cycle
    context = report.get("process_context")
    questions = set()
    if context is not None:
        _proposal(context, "process_context")
        questions = {row["id"] for row in context.get("measurement_questions") or []}
        if any(ref not in operations for row in context.get("outcomes") or [] for ref in row.get("operation_ids") or []):
            raise ReportError("process_context: outcome refers to an operation outside the map")
    facts = {}
    for fact in report.get("facts") or []:
        label = fact.get("id")
        _proposal(fact, label)
        if label in facts or any(ref not in questions for ref in fact.get("question_ids") or []):
            raise ReportError(f"{label}: duplicate fact or unknown measurement question")
        if any(ref not in operations for ref in fact.get("operation_ids") or []):
            raise ReportError(f"{label}: refers to an operation outside the map")
        if any(ref not in confirmed for ref in fact.get("observed_by") or []):
            raise ReportError(f"{label}: observed only by confirmed event bindings")
        facts[label] = fact
    coverage = report.get("coverage")
    if coverage is not None:
        if {row["fact_id"] for row in coverage.get("facts") or []} != set(facts):
            raise ReportError("coverage must assess exactly the listed facts")
        if coverage.get("summary", {}).get("obligations_status") != "proposed":
            raise ReportError("coverage obligations are proposed until a specialist accepts them")
    changes = report.get("instrumentation") or []
    change_ids = {change.get("id") for change in changes}
    for change in changes:
        label = change.get("id")
        _proposal(change, label)
        if any(ref not in facts for ref in change.get("fact_ids") or []) or                 any(ref not in change_ids for ref in change.get("depends_on") or []):
            raise ReportError(f"{label}: refers to unknown facts or changes")
        if any(ref not in operations for ref in change.get("operation_ids") or []):
            raise ReportError(f"{label}: refers to an operation outside the map")
        if change.get("event", {}).get("key_status") not in ("not_set", "proposed", "existing"):
            raise ReportError(f"{label}: an event key is never agreed by code or model")
        location = change.get("location") or {}
        if location.get("status") == "anchored" and "quote" not in location:
            raise ReportError(f"{label}: an anchored change needs cited lines")
    if find_cycle(changes):
        raise ReportError("instrumentation dependencies form a cycle")
    for metric in report.get("metrics") or []:
        label = metric.get("id")
        _proposal(metric, label)
        if metric.get("value") is not None or metric.get("readiness", {}).get("status") != "not_ready":
            raise ReportError(f"{label}: metric values are not computed in this version")
        if any(ref not in facts for ref in metric.get("fact_ids") or []) or                 any(ref not in change_ids for ref in metric.get("change_ids") or []):
            raise ReportError(f"{label}: refers to unknown facts or changes")
