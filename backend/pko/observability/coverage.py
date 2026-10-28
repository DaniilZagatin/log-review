"""Deterministic, separately reported coverage of each required fact.

No axis hides another: a fact is observable in code only when one record
establishes its meaning, case, timing and needed fields together.
Unknown stays unknown, and no process-wide percentage is computed while the
list of facts is a proposal. A missing runtime file means "not provided",
never zero records.
"""
from __future__ import annotations

AXES = ("emit", "semantics", "fields", "correlation", "timing", "time_source", "runtime", "quality")
COMPATIBLE = {"request": {"request"}, "queued": {"queued"}, "start": {"start"}, "result": {"result"},
              "refusal": {"refusal"}, "error": {"error"}, "retry": {"retry"}, "cancellation": {"cancellation"},
              "compensation": {"compensation"}, "state_change": {"state_change", "result"}}
_AFTER = {"after_commit", "after_action", "on_response"}
_OUTCOME_LIFECYCLES = {"result", "refusal", "state_change", "cancellation", "compensation"}
GAP_VALUES = {"emit": {"not_found"}, "semantics": {"incompatible"}, "fields": {"missing", "partial"},
              "correlation": {"missing"}, "timing": {"premature"}, "time_source": {"missing"},
              "runtime": {"no_records"}, "quality": {"issues"}}
UNKNOWN_VALUES = {"unknown", "unexplained_candidates", "key_unknown"}
_OPEN_REVIEW = {"unresolved", "conflict", "rejected", "deferred", "not_reviewed"}
_LITERAL_KEYS = {"literal", "resolved_constant"}


def _timing(lifecycle: str, moments: set[str]) -> str:
    known = moments - {"unknown"}
    if not known:
        return "unknown"
    if lifecycle in _OUTCOME_LIFECYCLES:
        return "consistent" if known & _AFTER else "premature"
    if lifecycle == "error":
        return "consistent" if known & {"on_failure", "after_action"} else "premature"
    return "consistent"


def _runtime_quality(runtime: dict, event: str, wanted: set[str]) -> str:
    """Quality within this key's records; a union of field names proves no completeness."""
    by_event = runtime.get("quality_by_event")
    stats = by_event.get(event) if isinstance(by_event, dict) else None
    if not isinstance(stats, dict):
        # Old profiles have only file-wide errors and a union of field names.
        # Neither establishes quality for the records observing this fact.
        return "unknown"
    count = stats.get("record_count")
    fields = stats.get("field_counts")
    if (type(count) is not int or count <= 0 or count != runtime["event_counts"].get(event)
            or not isinstance(fields, dict)):
        return "unknown"
    uncertain = False
    for name in ("missing_case_count", "invalid_time_count", "missing_event_id_count", "conflicting_event_id_count"):
        amount = stats.get(name)
        if type(amount) is not int or amount < 0:
            uncertain = True
        elif amount:
            return "issues"
    for name in wanted:
        if name not in fields:
            if stats.get("field_names_truncated") is False:
                return "issues"
            uncertain = True
        elif type(fields[name]) is not int or not 0 <= fields[name] <= count:
            uncertain = True
        elif fields[name] < count:
            return "issues"
    return "unknown" if uncertain else "no_issues_found"


def _record_axes(fact: dict, binding: dict, site: dict, runtime: dict | None) -> dict:
    """Axes established by one record alone; nothing is borrowed from another record."""
    sources = [source for source in fact["field_sources"] if source["binding_id"] == binding["id"]]
    covered = {source["purpose"] for source in sources}
    axes = {"emit": "found",
            "semantics": "compatible" if binding["event_role"] in COMPATIBLE[fact["lifecycle"]] else "incompatible",
            "correlation": "identified" if binding["case_fields"] or "case_id" in covered else "missing",
            "timing": _timing(fact["lifecycle"], {binding["lifecycle_moment"]})}
    payload_needs = [need for need in fact["needed"] if need not in ("case_id", "timestamp")]
    if not payload_needs:
        axes["fields"] = "not_required"
    else:
        found = sum(need in covered for need in payload_needs)
        axes["fields"] = ("available" if found == len(payload_needs) else
                          "unknown" if site.get("fields_status") == "partial" else "partial" if found else "missing")
    if "timestamp" not in fact["needed"]:
        axes["time_source"] = "not_required"
    elif "timestamp" in covered:
        axes["time_source"] = "event_time_field"
    elif site.get("kind") == "log_call":
        # The logging framework stamps the write time, not the business event time.
        axes["time_source"] = "record_time"
    else:
        axes["time_source"] = "missing"
    if runtime is None:
        axes["runtime"] = axes["quality"] = "not_provided"
    elif site.get("key_status") not in _LITERAL_KEYS:
        axes["runtime"], axes["quality"] = "key_unknown", "not_assessed"
    elif site.get("event_key") in runtime["event_counts"]:
        wanted = {source["field"] for source in sources} | set(binding["case_fields"])
        axes["runtime"] = "records_present"
        axes["quality"] = _runtime_quality(runtime, site["event_key"], wanted)
    else:
        axes["runtime"], axes["quality"] = "no_records", "not_assessed"
    return axes


def _observable(axes: dict) -> bool:
    return (axes["emit"] == "found" and axes["semantics"] == "compatible" and axes["correlation"] == "identified"
            and axes["timing"] == "consistent" and axes["fields"] in ("available", "not_required")
            and axes["time_source"] in ("event_time_field", "record_time", "not_required"))


def _score(axes: dict) -> tuple:
    return (_observable(axes), axes["semantics"] == "compatible",
            sum(axes[axis] not in GAP_VALUES[axis] and axes[axis] not in UNKNOWN_VALUES for axis in AXES))


def assess_fact(fact: dict, bindings: dict, sites: dict, runtime: dict | None) -> dict:
    """Coverage of one fact by its best single record.

    Meaning, case, moment and fields must come from the same record: two
    insufficient records do not add up to an observable fact without an
    established rule that joins them.
    """
    axes = dict.fromkeys(AXES, "unknown")
    if fact["outside_application"]:
        axes = dict.fromkeys(AXES, "not_applicable")
        axes["emit"] = "outside_application"
        return {"fact_id": fact["id"], "axes": axes, "gaps": [], "unknowns": [], "observable_in_code": False,
                "record_id": None, "records": []}
    observed = [bindings[ref] for ref in fact["observed_by"] if ref in bindings]
    records = [{"binding_id": row["id"], "axes": _record_axes(fact, row, sites.get(row["site_id"], {}), runtime)}
               for row in observed]
    best = max(records, key=lambda record: _score(record["axes"]), default=None)
    if best is not None:
        axes = dict(best["axes"])
    else:
        open_candidates = any(site.get("review_status") in _OPEN_REVIEW and site.get("link_status") in ("linked", "ambiguous")
                              and set(site.get("operation_ids") or []) & set(fact["operation_ids"])
                              for site in sites.values())
        axes["emit"] = "unexplained_candidates" if open_candidates else "not_found"
        if not [need for need in fact["needed"] if need not in ("case_id", "timestamp")]:
            axes["fields"] = "not_required"
        if "timestamp" not in fact["needed"]:
            axes["time_source"] = "not_required"
        if runtime is None:
            axes["runtime"] = axes["quality"] = "not_provided"
    gaps = [axis for axis in AXES if axes[axis] in GAP_VALUES[axis]]
    unknowns = [axis for axis in AXES if axes[axis] in UNKNOWN_VALUES]
    return {"fact_id": fact["id"], "axes": axes, "gaps": gaps, "unknowns": unknowns,
            "observable_in_code": best is not None and _observable(axes),
            "record_id": best["binding_id"] if best else None, "records": records}


def assess_coverage(facts: list[dict], report: dict, runtime: dict | None) -> dict:
    logging = report.get("logging") or {}
    bindings = {row["id"]: row for row in logging.get("event_bindings") or [] if row["status"] == "confirmed"}
    sites = {site["id"]: site for site in logging.get("sites") or []}
    rows = [assess_fact(fact, bindings, sites, runtime) for fact in facts]
    summary = {"facts": len(rows), "obligations_status": "proposed",
               "observable_in_code": sum(row["observable_in_code"] for row in rows),
               "with_gaps": sum(bool(row["gaps"]) for row in rows),
               "with_unknowns": sum(bool(row["unknowns"]) for row in rows),
               "outside_application": sum(row["axes"]["emit"] == "outside_application" for row in rows),
               "by_axis": {axis: {} for axis in AXES}}
    for row in rows:
        for axis, value in row["axes"].items():
            summary["by_axis"][axis][value] = summary["by_axis"][axis].get(value, 0) + 1
    return {"facts": rows, "summary": summary}
