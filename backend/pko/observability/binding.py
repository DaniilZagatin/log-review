"""Model-reviewed meaning of logging sites, checked against issued code lines.

Only sites with location candidates are reviewed. The model chooses one of the
site's candidate operations and the role of the record; code checks that the
citation covers the emit (and the wrapper, for wrapper calls), that case fields
exist in the call, and that several interpretations of one site have distinct
conditions. A confirmed binding is still a proposal: its business meaning is
not accepted until a specialist reviews it.
"""
from __future__ import annotations

from pko.observability.evidence import EvidenceRegistry, overlaps
from pko.observability.model_stage import BudgetExhausted, PacketTooLarge, StageModel

STAGE = "event_binding"
ROLES = ("request", "queued", "start", "result", "refusal", "error", "retry", "duplicate",
         "cancellation", "compensation", "state_change", "diagnostic", "unknown")
MOMENTS = ("before_action", "after_action", "before_commit", "after_commit", "on_response", "on_failure", "unknown")
REPEATS = ("once_per_execution", "per_attempt", "per_delivery", "unknown")
DECISIONS = ("bind", "no_operation", "unresolved")
BINDING_STATUSES = ("confirmed", "no_operation", "unresolved", "conflict")
REVIEW_STATUSES = BINDING_STATUSES + ("rejected", "deferred", "not_reviewed")
BATCH_SIZE = 6
MAX_BATCHES = 10
CONTEXT_LINES = 10

_REF = {"type": "object", "additionalProperties": False, "required": ["path", "line_start", "line_end"],
        "properties": {"path": {"type": "string"}, "line_start": {"type": "integer"}, "line_end": {"type": "integer"}}}
TOOL = {
    "name": "submit_event_bindings",
    "description": "Вернуть смысл каждого места логирования: операцию, роль записи и основания в выданных строках.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["bindings"], "properties": {
        "bindings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["site_id", "decision", "event_role", "lifecycle_moment", "repeat_semantics",
                         "business_meaning", "evidence"],
            "properties": {
                "site_id": {"type": "string"},
                "decision": {"type": "string", "enum": list(DECISIONS)},
                "operation_id": {"type": "string"},
                "event_role": {"type": "string", "enum": list(ROLES)},
                "lifecycle_moment": {"type": "string", "enum": list(MOMENTS)},
                "repeat_semantics": {"type": "string", "enum": list(REPEATS)},
                "condition": {"type": "string"},
                "business_meaning": {"type": "string"},
                "case_fields": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": _REF},
                "question": {"type": "string"}}}}}},
}


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _ranges(node: dict, key: str) -> list[dict]:
    if key == "evidence":
        refs = node.get("evidence") or []
    else:
        refs = [ref for row in node.get("code_explanations") or [] for ref in row.get("evidence") or []]
    return [ref for ref in refs if isinstance(ref, dict) and isinstance(ref.get("line_start"), int)]


def _operation_view(node: dict) -> dict:
    return {"operation_id": node["id"], "name": node.get("name"), "description": node.get("description"),
            "input": node.get("input"), "output": node.get("output"), "outcome": node.get("outcome"),
            "effect_kind": node.get("effect_kind"),
            "evidence": [{k: ref[k] for k in ("path", "line_start", "line_end")} for ref in _ranges(node, "evidence")]}


class _Packet:
    """Sites of one request with the fragments issued for them."""

    def __init__(self, sites, operations, wrappers, registry: EvidenceRegistry):
        self.sites = {site["id"]: site for site in sites}
        self.wrapper_ranges: dict[str, list[dict]] = {}
        self.fragments, self.operation_ids = [], []
        for site in sites:
            path, start, end = site["path"], site["line_start"], site["line_end"]
            # The enclosing function cited by a candidate operation is the natural reading unit.
            functions = [ref for operation_id in site["operation_ids"] for ref in _ranges(operations[operation_id], "code_explanations")
                         if overlaps(ref, path, start, end)]
            function = min(functions, key=lambda ref: ref["line_end"] - ref["line_start"], default=None)
            first, last = ((function["line_start"], function["line_end"]) if function else
                           (start - CONTEXT_LINES, end + CONTEXT_LINES))
            self._issue(registry, path, min(first, start), max(last, end), start)
            if site.get("kind") == "wrapper_call":
                for wrapper in wrappers:
                    if wrapper.get("path") == path and wrapper.get("name") == site.get("callee"):
                        self._issue(registry, path, wrapper["line_start"], wrapper["line_end"], wrapper["line_start"])
                        self.wrapper_ranges.setdefault(site["id"], []).append(wrapper)
            for operation_id in site["operation_ids"]:
                if operation_id not in self.operation_ids:
                    self.operation_ids.append(operation_id)

    def _issue(self, registry, path, start, end, focus):
        fragment = registry.fragment(path, start, end, focus=focus)
        if fragment is not None:
            self.fragments.append({"id": f"fragment-{len(self.fragments) + 1}", **fragment})


def _validate_item(item, packet: _Packet, registry: EvidenceRegistry):
    if not isinstance(item, dict):
        return None, ["bindings: каждый элемент — объект"]
    site = packet.sites.get(item.get("site_id"))
    if site is None:
        return None, [f"site_id {item.get('site_id')!r} не входит в этот пакет"]
    label, errors = site["id"], []
    decision, role = item.get("decision"), item.get("event_role")
    moment, repeat = item.get("lifecycle_moment", "unknown"), item.get("repeat_semantics", "unknown")
    if decision not in DECISIONS:
        errors.append(f"{label}: decision — одно из {', '.join(DECISIONS)}")
    if role not in ROLES or moment not in MOMENTS or repeat not in REPEATS:
        errors.append(f"{label}: event_role, lifecycle_moment и repeat_semantics — значения из перечней")
    case_fields = item.get("case_fields") or []
    if not isinstance(case_fields, list) or any(name not in site.get("fields", []) for name in case_fields):
        errors.append(f"{label}: case_fields — только имена из fields места: {', '.join(site.get('fields', [])) or 'полей нет'}")
    evidence, problems = registry.resolve(item.get("evidence"), f"{label}: evidence")
    errors += problems
    meaning, question = _text(item.get("business_meaning"), 1000), _text(item.get("question"), 500)
    operation_id = item.get("operation_id")
    if decision in ("bind", "no_operation"):
        if not any(overlaps(ref, site["path"], site["line_start"], site["line_end"]) for ref in evidence):
            errors.append(f"{label}: evidence должно включать строки вызова {site['path']}:{site['line_start']}-{site['line_end']}")
        if not meaning:
            errors.append(f"{label}: опиши business_meaning")
    if decision == "bind":
        if operation_id not in site["operation_ids"]:
            errors.append(f"{label}: operation_id — одна из операций-кандидатов {', '.join(site['operation_ids'])}")
        if role == "unknown":
            errors.append(f"{label}: для bind укажи роль записи или выбери unresolved")
        if site.get("kind") == "wrapper_call" and not any(
                overlaps(ref, wrapper["path"], wrapper["line_start"], wrapper["line_end"])
                for ref in evidence for wrapper in packet.wrapper_ranges.get(label, [])):
            errors.append(f"{label}: для вызова обёртки сошлись на строки обёртки, где она пишет запись, или выбери unresolved")
    elif decision == "unresolved" and not question:
        errors.append(f"{label}: для unresolved задай конкретный question")
    if errors:
        return None, errors
    return {"site_id": label, "decision": decision, "operation_id": operation_id if decision == "bind" else None,
            "event_role": role, "lifecycle_moment": moment, "repeat_semantics": repeat,
            "condition": _text(item.get("condition"), 500), "business_meaning": meaning,
            "case_fields": sorted(set(case_fields)), "evidence": evidence, "question": question}, []


def _reconcile(items: list[dict]) -> str:
    """Status shared by all interpretations of one site."""
    decisions = {item["decision"] for item in items}
    if len(decisions) > 1:
        return "conflict"
    decision = decisions.pop()
    if decision == "bind" and len(items) > 1:
        conditions = [item["condition"] for item in items]
        if not all(conditions) or len(set(conditions)) != len(conditions):
            return "conflict"
    return {"bind": "confirmed", "no_operation": "no_operation", "unresolved": "unresolved"}[decision]


def review_bindings(model: dict, sites: list[dict], wrappers: list[dict], registry: EvidenceRegistry,
                    stage_model: StageModel, map_revision: str, reserve: int = 0) -> dict:
    """Review eligible sites in bounded packets; unreviewed sites stay explicit.

    ``reserve`` requests are left for later stages. A packet too large for the
    provider window is split; a single site that still does not fit is deferred.
    """
    operations = {node["id"]: node for node in model.get("nodes") or [] if node.get("kind") == "operation"}
    eligible = [site for site in sites if site.get("link_status") in ("linked", "ambiguous")]
    review = {site["id"]: "not_reviewed" for site in sites}
    reasons: dict[str, list[str]] = {}
    bindings, errors, deferred = [], [], []
    scenario = model.get("scenario") or {}
    batches = [eligible[index:index + BATCH_SIZE] for index in range(0, len(eligible), BATCH_SIZE)]
    deferred += [site["id"] for batch in batches[MAX_BATCHES:] for site in batch]
    queue = batches[:MAX_BATCHES]
    while queue:
        batch = queue.pop(0)
        packet = _Packet(batch, operations, wrappers, registry)
        payload = {
            "scenario": {"title": scenario.get("title"), "description": scenario.get("description")},
            "operations": [_operation_view(operations[operation_id]) for operation_id in packet.operation_ids],
            "sites": [{"site_id": site["id"], "path": site["path"], "line_start": site["line_start"],
                       "line_end": site["line_end"], "call": site.get("callee"), "kind": site.get("kind"),
                       "key_candidate": site.get("event_key"), "key_status": site.get("key_status"),
                       "fields": site.get("fields", []), "candidate_operation_ids": site["operation_ids"]}
                      for site in batch],
            "fragments": packet.fragments}

        def validate(arguments, packet=packet):
            items = arguments.get("bindings")
            if not isinstance(items, list):
                return None, ["bindings: ожидается список"]
            accepted, problems = [], []
            for item in items:
                value, item_errors = _validate_item(item, packet, registry)
                problems += item_errors
                if value is not None:
                    accepted.append(value)
            answered = {value["site_id"] for value in accepted} | {
                problem.split(":", 1)[0] for problem in problems}
            problems += [f"{site_id}: нет ответа для этого места" for site_id in packet.sites if site_id not in answered]
            return accepted, problems

        try:
            accepted, problems = stage_model.ask(STAGE, payload, TOOL, validate, reserve=reserve,
                                                 key=lambda item: item["site_id"])
        except PacketTooLarge as exc:
            if len(batch) > 1:
                middle = len(batch) // 2
                queue[:0] = [batch[:middle], batch[middle:]]
            else:
                deferred.append(batch[0]["id"])
                reasons[batch[0]["id"]] = [str(exc)]
            continue
        except BudgetExhausted as exc:
            deferred += [site["id"] for rest in [batch, *queue] for site in rest]
            errors.append(str(exc))
            break
        errors += problems
        by_site: dict[str, list[dict]] = {}
        for item in accepted or []:
            by_site.setdefault(item["site_id"], []).append(item)
        for site in batch:
            items = by_site.get(site["id"])
            if not items:
                review[site["id"]] = "rejected"
                reasons[site["id"]] = [problem for problem in problems if problem.startswith(site["id"] + ":")][:5]
                continue
            status = _reconcile(items)
            review[site["id"]] = status
            for index, item in enumerate(items, 1):
                bindings.append({"id": f"evb-{site['id'].removeprefix('log-')}-{index}", "map_revision": map_revision,
                                 "status": status, **item, "provenance": "model_interpretation",
                                 "authority_status": "proposed"})
    for site_id in deferred:
        review[site_id] = "deferred"
    counts = {status: sum(value == status for value in review.values()) for status in REVIEW_STATUSES}
    counts["eligible"] = len(eligible)
    return {"bindings": bindings, "review": review, "reasons": reasons, "errors": errors,
            "deferred": deferred, "counts": counts}
