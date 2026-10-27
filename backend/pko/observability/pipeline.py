"""Sequential report stages over a saved map; a failed stage never touches the map."""
from __future__ import annotations

from pko.audit import record
from pko.llm.tracing import trace_span
from pko.observability.contracts import REPORT_VERSION, map_revision, stage_result, validate_report
from pko.observability.log_discovery import discover_logging, logging_config_paths
from pko.observability.mapping import link_sites, researched_paths


def run_observability(model: dict, tree) -> dict:
    """Build the process report for ``model`` while its source snapshot is alive.

    Each stage records its own execution and result status. A dependency that
    failed blocks only its dependants; the map and earlier results remain.
    """
    revision = map_revision(model)
    map_complete = (model.get("run") or {}).get("status") == "completed"
    report = {"report_version": REPORT_VERSION, "run_id": (model.get("run") or {}).get("id"),
              "map_revision": revision, "map_status": (model.get("run") or {}).get("status"),
              "stages": [], "logging": {}}

    inventory = None
    with trace_span("logging_inventory") as span:
        try:
            paths = researched_paths(model)
            inventory = discover_logging(tree, paths, config_paths=logging_config_paths(tree))
            scope = inventory["scope"]
            reasons = []
            if scope["truncated"]:
                reasons.append("Каталог усечён лимитами.")
            if scope["files_unreadable"]:
                reasons.append("Часть файлов не удалось прочитать.")
            if not paths:
                reasons.append("Исследователь не прочитал ни одного файла.")
            result = stage_result("logging_inventory", "completed", "partial" if reasons else "complete", reasons,
                                  {"files": scope["files_scanned"], "sites": scope["sites_returned"]})
            span.set_output(result["counts"])
        except Exception as exc:  # A later stage must not cost the saved map.
            span.record_error(exc)
            result = stage_result("logging_inventory", "failed", reasons=[f"{type(exc).__name__}: {exc}"])
    report["stages"].append(result)

    with trace_span("event_operation_mapping") as span:
        if inventory is None:
            result = stage_result("event_operation_mapping", "blocked",
                                  reasons=["Каталог мест логирования не построен."])
            linked = {"sites": [], "bindings": [], "counts": {}}
        else:
            try:
                linked = link_sites(model, inventory, revision)
                reasons = [] if map_complete else [
                    "Карта частичная: выводы о полноте наблюдаемости процесса не делаются."]
                result = stage_result("event_operation_mapping", "completed",
                                      "complete" if map_complete else "partial", reasons, linked["counts"])
                span.set_output(linked["counts"])
            except Exception as exc:
                span.record_error(exc)
                linked = {"sites": [{**site, "link_status": None, "operation_ids": []} for site in inventory["sites"]],
                          "bindings": [], "counts": {}}
                result = stage_result("event_operation_mapping", "failed", reasons=[f"{type(exc).__name__}: {exc}"])
    report["stages"].append(result)

    if inventory is not None:
        report["logging"] = {"scope": inventory["scope"], "limitations": inventory["limitations"],
                             "sites": linked["sites"],
                             "wrappers": inventory["wrappers"], "config_hints": inventory["config_hints"],
                             "bindings": linked["bindings"], "counts": linked["counts"]}
    validate_report(report, model)
    record("process_report", stages={row["id"]: row["execution_status"] for row in report["stages"]},
           sites=len(report["logging"].get("sites") or []), bindings=len(report["logging"].get("bindings") or []))
    return report
