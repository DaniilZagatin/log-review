"""Sequential report stages over a saved map; a failed stage never touches the map.

Each stage records its own execution and result status. A stage whose
dependency did not complete is blocked; other results remain. Model stages run
only with a configured report model and share one request and time budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pko.audit import record
from pko.llm.tracing import trace_span
from pko.observability.binding import review_bindings
from pko.observability.context import build_context
from pko.observability.contracts import REPORT_VERSION, STAGES, map_revision, stage_result, validate_report
from pko.observability.coverage import assess_coverage
from pko.observability.evidence import EvidenceRegistry
from pko.observability.facts import build_facts
from pko.observability.instrumentation import build_changes
from pko.observability.log_discovery import discover_logging, logging_config_paths
from pko.observability.mapping import link_sites, researched_paths
from pko.observability.metrics import build_metrics, readiness, register_missing_facts
from pko.observability.model_stage import BudgetExhausted, StageModel, methodology


@dataclass
class ReportOptions:
    """Report stage inputs and limits, separate from the research budget."""
    client: Any = None
    max_requests: int = 24
    max_seconds: float = 1800.0
    business: dict | None = None
    runtime: dict | None = None


def _completed(report: dict, stage_id: str) -> bool:
    return any(row["id"] == stage_id and row["execution_status"] == "completed" for row in report["stages"])


def _run_stage(report: dict, stage_id: str, requires: tuple[str, ...], body, *, model_stage: bool = False,
               stage_model: StageModel | None = None) -> bool:
    """Run ``body() -> (result_status, reasons, counts)`` with an isolated outcome."""
    blocked = [name for name in requires if not _completed(report, name)]
    with trace_span(stage_id) as span:
        if blocked:
            row = stage_result(stage_id, "blocked", reasons=[f"Не выполнен этап «{STAGES[name]}»." for name in blocked])
        elif model_stage and stage_model is None:
            row = stage_result(stage_id, "not_run", reasons=["Модель для этапов отчёта не настроена."])
        else:
            try:
                result_status, reasons, counts = body()
                row = stage_result(stage_id, "completed", result_status, reasons, counts)
                span.set_output(counts)
            except BudgetExhausted as exc:
                row = stage_result(stage_id, "interrupted", reasons=[str(exc)])
            except Exception as exc:  # A later stage must not cost the saved map or earlier results.
                span.record_error(exc)
                row = stage_result(stage_id, "failed", reasons=[f"{type(exc).__name__}: {exc}"[:500]])
    report["stages"].append(row)
    return row["execution_status"] == "completed"


class _Rejected(RuntimeError):
    """The model answer did not pass host checks even after the repair turn."""


def _accepted(result, errors: list[str]):
    if not result:
        raise _Rejected("Ответ модели не прошёл проверки: " + "; ".join(errors[:3]))
    return result


def run_observability(model: dict, tree, *, stage_model: StageModel | None = None,
                      options: ReportOptions | None = None, run_id: str | None = None) -> dict:
    """Build the process report for ``model`` while its source snapshot is alive."""
    options = options or ReportOptions()
    revision = map_revision(model)
    run = model.get("run") or {}
    map_complete = run.get("status") == "completed"
    partial_map = [] if map_complete else ["Карта частичная: выводы о полноте наблюдаемости процесса не делаются."]
    registry = EvidenceRegistry(tree)
    # A report rerun over a saved map is its own run; map_run_id names the research run.
    report = {"report_version": REPORT_VERSION, "run_id": run_id or run.get("id"), "map_run_id": run.get("id"),
              "map_revision": revision,
              "map_status": run.get("status"), "stages": [], "logging": {}, "methodology": {}}
    state: dict = {}

    def inventory():
        paths = researched_paths(model)
        state["inventory"] = found = discover_logging(tree, paths, config_paths=logging_config_paths(tree))
        scope = found["scope"]
        reasons = (["Каталог усечён лимитами."] if scope["truncated"] else []) + \
                  (["Часть файлов не удалось прочитать."] if scope["files_unreadable"] else []) + \
                  ([] if paths else ["Исследователь не прочитал ни одного файла."])
        report["logging"] = {"scope": scope, "limitations": found["limitations"],
                             "sites": [{**site, "link_status": None, "operation_ids": []} for site in found["sites"]],
                             "wrappers": found["wrappers"], "config_hints": found["config_hints"],
                             "bindings": [], "event_bindings": [], "counts": {}}
        return ("partial" if reasons else "complete"), reasons, {"files": scope["files_scanned"], "sites": scope["sites_returned"]}

    def mapping():
        linked = link_sites(model, state["inventory"], revision)
        report["logging"].update(sites=linked["sites"], bindings=linked["bindings"], counts=linked["counts"])
        return ("complete" if map_complete else "partial"), partial_map, linked["counts"]

    def binding_review():
        report["methodology"]["event_binding"] = methodology("event_binding")["sha256"]
        logging = report["logging"]
        reviewed = review_bindings(model, logging["sites"], logging["wrappers"], registry, stage_model, revision)
        for site in logging["sites"]:
            site["review_status"] = reviewed["review"].get(site["id"], "not_reviewed")
            if reviewed["reasons"].get(site["id"]):
                site["review_reasons"] = reviewed["reasons"][site["id"]]
        logging["event_bindings"] = reviewed["bindings"]
        counts = reviewed["counts"]
        reasons = list(partial_map)
        if reviewed["deferred"]:
            reasons.append(f"Не рассмотрено из-за лимитов: {len(reviewed['deferred'])}.")
        if counts["rejected"]:
            reasons.append(f"Ответ модели отклонён проверками для мест: {counts['rejected']}.")
        if counts["conflict"]:
            reasons.append(f"Противоречивые интерпретации: {counts['conflict']}.")
        reasons += [error for error in reviewed["errors"] if error.startswith("Исчерпан")]
        return ("partial" if reasons else "complete"), reasons, counts

    _run_stage(report, "logging_inventory", (), inventory)
    _run_stage(report, "event_operation_mapping", ("logging_inventory",), mapping)
    _run_stage(report, "event_binding_review", ("event_operation_mapping",), binding_review,
               model_stage=True, stage_model=stage_model)
    bindings_partial = [] if any(row["id"] == "event_binding_review" and row["result_status"] == "complete"
                                 for row in report["stages"]) else [
        "Смысл мест логирования установлен не полностью: наблюдаемость фактов может быть занижена."]
    if options.runtime is not None:
        report["runtime_profile"] = options.runtime

    def process_context():
        report["methodology"]["process_context"] = methodology("process_context")["sha256"]
        context, errors = build_context(model, report, stage_model, options.business)
        report["process_context"] = _accepted(context, errors)
        reasons = partial_map + ([f"Отклонено проверками: {len(errors)}."] if errors else [])
        return ("partial" if reasons else "complete"), reasons, {
            "questions": len(context["measurement_questions"]), "outcomes": len(context["outcomes"])}

    def fact_requirements():
        report["methodology"]["fact_requirements"] = methodology("fact_requirements")["sha256"]
        facts, errors = build_facts(model, report, stage_model)
        report["facts"] = _accepted(facts, errors)
        reasons = partial_map + bindings_partial + ([f"Отклонено проверками: {len(errors)}."] if errors else [])
        return ("partial" if reasons else "complete"), reasons, {
            "facts": len(facts), "observed": sum(bool(fact["observed_by"]) for fact in facts)}

    def coverage():
        report["coverage"] = assessed = assess_coverage(report["facts"], report, options.runtime)
        summary = assessed["summary"]
        reasons = partial_map + bindings_partial
        return ("partial" if reasons else "complete"), reasons, {
            key: summary[key] for key in ("facts", "observable_in_code", "with_gaps", "with_unknowns")}

    def instrumentation():
        report["methodology"]["instrumentation"] = methodology("instrumentation")["sha256"]
        changes, errors, pending = build_changes(model, report, stage_model, registry)
        report["instrumentation"] = changes
        reasons = partial_map + ([f"Отклонено проверками: {len(errors)}."] if errors else []) + \
            ([f"Факты без рассмотренных доработок из-за лимитов: {', '.join(pending)}."] if pending else [])
        return ("partial" if reasons else "complete"), reasons, {
            "changes": len(changes), "unresolved_locations": sum(c["location"]["status"] == "unresolved" for c in changes)}

    def metric_catalog():
        report["methodology"]["metric_catalog"] = methodology("metric_catalog")["sha256"]
        metrics, errors = build_metrics(model, report, stage_model)
        coverage_rows = {row["fact_id"]: row for row in report["coverage"]["facts"]}
        for metric in _accepted(metrics, errors):
            metric["readiness"] = readiness(metric, coverage_rows, options.runtime)
        report["metrics"] = metrics
        report["requested_facts"] = register_missing_facts(report)
        reasons = partial_map + ([f"Отклонено проверками: {len(errors)}."] if errors else []) + \
            ([] if _completed(report, "instrumentation_plan") else
             ["Доработки не построены: зависимости метрик от них не указаны."])
        return ("partial" if reasons else "complete"), reasons, {
            "metrics": len(metrics), "requested_facts": len(report["requested_facts"])}

    _run_stage(report, "process_context", (), process_context, model_stage=True, stage_model=stage_model)
    _run_stage(report, "fact_requirements", ("process_context",), fact_requirements,
               model_stage=True, stage_model=stage_model)
    _run_stage(report, "logging_coverage", ("fact_requirements",), coverage)
    _run_stage(report, "instrumentation_plan", ("logging_coverage",), instrumentation,
               model_stage=True, stage_model=stage_model)
    _run_stage(report, "metric_catalog", ("logging_coverage",), metric_catalog,
               model_stage=True, stage_model=stage_model)
    if stage_model is not None:
        report["budget"] = stage_model.budget.snapshot()
    validate_report(report, model)
    record("process_report", stages={row["id"]: row["execution_status"] for row in report["stages"]},
           sites=len(report["logging"].get("sites") or []),
           event_bindings=len(report["logging"].get("event_bindings") or []),
           facts=len(report.get("facts") or []), changes=len(report.get("instrumentation") or []),
           metrics=len(report.get("metrics") or []))
    return report
