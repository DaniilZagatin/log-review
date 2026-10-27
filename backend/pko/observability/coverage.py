"""Deterministic, separately reported coverage of each required fact.

No axis hides another: a fact is observable in code only when the emit,
its meaning, correlation, timing and needed fields are all established.
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


def assess_fact(fact: dict, bindings: dict, sites: dict, runtime: dict | None) -> dict:
    observed = [bindings[ref] for ref in fact["observed_by"] if ref in bindings]
    observed_sites = [sites[row["site_id"]] for row in observed if row["site_id"] in sites]
    axes = dict.fromkeys(AXES, "unknown")
    if fact["outside_application"]:
        axes.update(emit="outside_application", semantics="not_applicable", fields="not_applicable",
                    correlation="not_applicable", timing="not_applicable", time_source="not_applicable",
                    runtime="not_applicable", quality="not_applicable")
        return {"fact_id": fact["id"], "axes": axes, "gaps": [], "unknowns": [], "observable_in_code": False}
    open_candidates = any(site.get("review_status") in _OPEN_REVIEW and site.get("link_status") in ("linked", "ambiguous")
                          and set(site.get("operation_ids") or []) & set(fact["operation_ids"])
                          for site in sites.values())
    if observed:
        axes["emit"] = "found"
        axes["semantics"] = "compatible" if any(row["event_role"] in COMPATIBLE[fact["lifecycle"]] for row in observed) \
            else "incompatible"
        axes["correlation"] = "identified" if any(row["case_fields"] for row in observed) or any(
            source["purpose"] == "case_id" for source in fact["field_sources"]) else "missing"
        axes["timing"] = _timing(fact["lifecycle"], {row["lifecycle_moment"] for row in observed})
    else:
        axes["emit"] = "unexplained_candidates" if open_candidates else "not_found"
    covered = {source["purpose"] for source in fact["field_sources"]}
    payload_needs = [need for need in fact["needed"] if need not in ("case_id", "timestamp")]
    if not payload_needs:
        axes["fields"] = "not_required"
    elif observed:
        found = sum(need in covered for need in payload_needs)
        partial_lists = any(site.get("fields_status") == "partial" for site in observed_sites)
        axes["fields"] = ("available" if found == len(payload_needs) else
                          "unknown" if partial_lists else "partial" if found else "missing")
    if "timestamp" not in fact["needed"]:
        axes["time_source"] = "not_required"
    elif "timestamp" in covered:
        axes["time_source"] = "event_time_field"
    elif any(site.get("kind") == "log_call" for site in observed_sites):
        # The logging framework stamps the write time, not the business event time.
        axes["time_source"] = "record_time"
    elif observed:
        axes["time_source"] = "missing"
    if runtime is None:
        axes["runtime"] = axes["quality"] = "not_provided"
    elif observed:
        keys = [site.get("event_key") for site in observed_sites if site.get("key_status") in _LITERAL_KEYS]
        present = [key for key in keys if key in runtime["event_counts"]]
        axes["runtime"] = "records_present" if present else "no_records" if keys else "key_unknown"
        if present:
            delivered = set().union(*(runtime["fields_by_event"].get(key, []) for key in present))
            wanted = {source["field"] for source in fact["field_sources"]}
            issues = runtime["missing_case_count"] or runtime["invalid_time_count"] or (wanted - delivered)
            axes["quality"] = "issues" if issues else "no_issues_found"
        else:
            axes["quality"] = "not_assessed"
    gaps = [axis for axis in AXES if axes[axis] in GAP_VALUES[axis]]
    unknowns = [axis for axis in AXES if axes[axis] in UNKNOWN_VALUES]
    observable = (axes["emit"] == "found" and axes["semantics"] == "compatible" and axes["correlation"] == "identified"
                  and axes["timing"] == "consistent" and axes["fields"] in ("available", "not_required")
                  and axes["time_source"] in ("event_time_field", "record_time", "not_required"))
    return {"fact_id": fact["id"], "axes": axes, "gaps": gaps, "unknowns": unknowns, "observable_in_code": observable}


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
