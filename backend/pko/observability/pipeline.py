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
from pko.observability.contracts import REPORT_VERSION, STAGES, map_revision, stage_result, validate_report
from pko.observability.evidence import EvidenceRegistry
from pko.observability.log_discovery import discover_logging, logging_config_paths
from pko.observability.mapping import link_sites, researched_paths
from pko.observability.model_stage import StageModel, methodology


@dataclass
class ReportOptions:
    """Report stage inputs and limits, separate from the research budget."""
    client: Any = None
    max_requests: int = 24
    max_seconds: float = 1800.0


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
            except Exception as exc:  # A later stage must not cost the saved map or earlier results.
                span.record_error(exc)
                row = stage_result(stage_id, "failed", reasons=[f"{type(exc).__name__}: {exc}"[:500]])
    report["stages"].append(row)
    return row["execution_status"] == "completed"


def run_observability(model: dict, tree, *, stage_model: StageModel | None = None) -> dict:
    """Build the process report for ``model`` while its source snapshot is alive."""
    revision = map_revision(model)
    run = model.get("run") or {}
    map_complete = run.get("status") == "completed"
    partial_map = [] if map_complete else ["Карта частичная: выводы о полноте наблюдаемости процесса не делаются."]
    registry = EvidenceRegistry(tree)
    report = {"report_version": REPORT_VERSION, "run_id": run.get("id"), "map_revision": revision,
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
    if stage_model is not None:
        report["budget"] = stage_model.budget.snapshot()
    validate_report(report, model)
    record("process_report", stages={row["id"]: row["execution_status"] for row in report["stages"]},
           sites=len(report["logging"].get("sites") or []),
           event_bindings=len(report["logging"].get("event_bindings") or []))
    return report
