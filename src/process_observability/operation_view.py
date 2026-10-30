"""Deterministic inspector views of one saved bundle (R08): operations, addressed questions and detail levels.

operation_views(bundle) -> {operation id: view}       in process-map order
card_links(bundle) -> {card id: links}                 exact owners through facts, otherwise labelled candidates
related_operations(transitions, operation_id) -> {upstream, downstream, transition_ids}
question_index(bundle) -> [question]                   every unknown kept, structured items addressed
detail_view(bundle, level) -> {level, items, transitions, unknowns, open_units, totals}

Read-only adapters of saved artifacts: nothing here reads source files, calls a model or creates a finding. A link is
exact only where the atomic record names the operation, the emit site and the fact IDs; a map without that record
keeps event-key links explicitly as candidates. A verified quote shows that source text exists, never that the code
ran, a record was delivered or a receiver committed a result. Detail levels regroup the same records for display;
they never drop one and never derive an execution order from stages. Atomicity hints are heuristics of the map's
shape, not a semantic check, and never change saved artifacts, accounting or status.
"""

import re

LEVELS = ("process", "stages", "operations")
PREVIEW_LINES = 4
# Host support states and reconciled site states under which a record cannot show the operation's own result.
_UNSUPPORTED = frozenset({"emit_outside_operation_sources", "no_static_emit"})
_TROUBLED = frozenset({"contradicted", "unsupported"})
_CONJUNCTION = re.compile(r"\s(?:и|или|а\s+также)\s", re.IGNORECASE)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _unique(values):
    return list(dict.fromkeys(values))


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _range(anchor):
    """(path, first line, last line) of a source anchor dict, or None."""
    anchor = _dict(anchor)
    path, first, last = anchor.get("path"), anchor.get("start_line"), anchor.get("end_line")
    if isinstance(path, str) and type(first) is int and type(last) is int:
        return path, first, last
    return None


def _overlaps(left, right):
    return left[0] == right[0] and left[1] <= right[2] and right[1] <= left[2]


def preview(quote, lines=PREVIEW_LINES):
    """The first lines of a cited fragment; one more line marks that the full text continues."""
    rows = quote.splitlines() if isinstance(quote, str) else []
    return "\n".join(rows if len(rows) <= lines + 1 else [*rows[:lines], "…"])


class _Saved:
    """Indexes of one saved bundle; unknown or malformed parts become empty, never invented."""

    def __init__(self, bundle):
        bundle = bundle if isinstance(bundle, dict) else {}
        process = _dict(bundle.get("process_map"))
        self.bundle, self.process = bundle, process
        self.stages = [item for item in _records(process.get("stages")) if isinstance(item.get("id"), str)]
        self.stage_names = {item["id"]: _text(item.get("name")) for item in self.stages}
        self.operations, seen = [], set()
        for op in _records(process.get("operations")):
            if isinstance(op.get("id"), str) and op["id"] not in seen:
                seen.add(op["id"])
                self.operations.append(op)
        self.op_index = {op["id"]: op for op in self.operations}
        self.mappings = _records(process.get("event_mappings"))
        self.transitions = [item for item in _records(process.get("operation_transitions"))
                            if item.get("from_operation") in self.op_index and item.get("to_operation") in self.op_index]
        self.unknowns = _strings(process.get("unknowns"))
        self.facts = [item for item in _records(_dict(bundle.get("log_coverage")).get("facts"))
                      if isinstance(item.get("id"), str)]
        self.fact_index = {fact["id"]: fact for fact in self.facts}
        self.cards = [item for item in _records(_dict(bundle.get("instrumentation_plan")).get("cards"))
                      if isinstance(item.get("id"), str)]
        self.metrics = [item for item in _records(_dict(bundle.get("metric_catalog")).get("metrics"))
                        if isinstance(item.get("id"), str)]
        self.evidence = {item["id"]: item for item in _records(bundle.get("evidence")) if isinstance(item.get("id"), str)}
        atomic = bundle.get("atomic_map")
        self.atomic = atomic if isinstance(atomic, dict) else None
        record = self.atomic or {}
        self.atomic_ops = {op.get("id"): op for op in _records(record.get("operations"))}
        self.atomic_edges = {item.get("id"): item for item in _records(record.get("transitions"))}
        self.units = {key: value for key, value in _dict(record.get("unit_states")).items() if isinstance(value, dict)}
        lifecycle = _dict(bundle.get("coverage_lifecycle"))
        self.sites = {(site.get("path"), site.get("start_line"), site.get("end_line"), site.get("event_key")): site
                      for site in _records(lifecycle.get("sites"))}
        self.logs = self._logs()

    def exact(self, operation_id):
        """Whether the atomic record names this operation: its links use exact site and fact IDs."""
        return self.atomic is not None and operation_id in self.atomic_ops

    def code_ranges(self, operation):
        found = []
        for key in _strings(operation.get("evidence_ids")):
            span = _range(_dict(self.evidence.get(key)).get("source_anchor"))
            if span is not None:
                found.append(span)
        return found

    def _logs(self):
        """One entry per process-map event mapping with its owners; exact only through the atomic record."""
        pending = {}
        for item in _records((self.atomic or {}).get("event_mappings")):
            key = (item.get("operation_id"), item.get("event_key"), tuple(_strings(item.get("evidence_ids"))))
            pending.setdefault(key, []).append(item)
        result = []
        for mapping in self.mappings:
            key, role = mapping.get("event_key"), mapping.get("lifecycle_role")
            operation_id = mapping.get("operation_id")
            record = None
            if self.atomic is not None and isinstance(operation_id, str):
                queue = pending.get((operation_id, key, tuple(_strings(mapping.get("evidence_ids")))))
                record = queue.pop(0) if queue else None
            if record is not None:
                span = (record.get("path"), record.get("start_line"), record.get("end_line"))
                facts = [identifier for identifier in _strings(record.get("fact_ids"))
                         if _dict(self.fact_index.get(identifier)).get("stage_id") == mapping.get("stage_id")]
                owners, linkage, site_id, ambiguity = [operation_id], "exact", record.get("site_id"), record.get("ambiguity")
            else:
                spans = [_range(_dict(self.evidence.get(ref)).get("source_anchor"))
                         for ref in _strings(mapping.get("evidence_ids"))]
                span = next((item for item in spans if item is not None), (None, None, None))
                facts = [fact["id"] for fact in self.facts if fact.get("stage_id") == mapping.get("stage_id")
                         and key in _strings(fact.get("event_keys"))]
                if isinstance(operation_id, str) and operation_id in self.op_index:
                    owners = [operation_id]
                else:
                    owners = [op["id"] for op in self.operations if op.get("stage_id") == mapping.get("stage_id")
                              and key in _strings(op.get("event_keys"))]
                linkage, site_id, ambiguity = "event_key", None, None
            site = self.sites.get((*span, key)) if record is not None else None
            support = mapping.get("role_support") or "not_assessed"
            shows = (linkage == "exact" and role == "result" and support == "emit_within_operation_sources"
                     and _dict(site).get("status") not in _TROUBLED
                     and mapping.get("reconciliation") not in _TROUBLED)
            result.append({"owners": owners, "entry": {
                "event_key": key, "site_id": site_id, "path": span[0], "start_line": span[1], "end_line": span[2],
                "lifecycle_role": role, "occurrence_rule": mapping.get("occurrence_rule"),
                "role_basis": mapping.get("role_basis"), "role_support": support,
                "reconciliation": mapping.get("reconciliation"), "site_status": _dict(site).get("status"),
                "fact_ids": facts, "ambiguity": _text(ambiguity), "linkage": linkage,
                "evidence_ids": _strings(mapping.get("evidence_ids")), "shows_new_result": shows}})
        return result

    def operation_facts(self, operation):
        """Explicit atomic owners, including facts without telemetry; legacy event-key links remain candidates."""
        if self.exact(operation["id"]):
            ids = _unique(identifier for log in self.logs if operation["id"] in log["owners"]
                          and log["entry"]["linkage"] == "exact"
                          for identifier in log["entry"]["fact_ids"])
            if self.atomic.get("schema") == "atomic-map/2":
                for fact in _records(_dict(self.atomic.get("code_led")).get("facts")):
                    identifier = fact.get("id")
                    if (operation["id"] in _strings(fact.get("operation_ids"))
                            and identifier in self.fact_index
                            and fact.get("stage_id") == operation.get("stage_id")
                            and self.fact_index[identifier].get("stage_id") == operation.get("stage_id")):
                        ids.append(identifier)
                ids = _unique(ids)
            linkage = "exact"
        else:
            keys = set(_strings(operation.get("event_keys")))
            ids = [fact["id"] for fact in self.facts if fact.get("stage_id") == operation.get("stage_id")
                   and keys & set(_strings(fact.get("event_keys")))]
            linkage = "event_key"
        return [{"id": identifier, "meaning": self.fact_index[identifier].get("meaning"),
                 "stage_id": self.fact_index[identifier].get("stage_id"),
                 "instrumentation": self.fact_index[identifier].get("instrumentation"),
                 "lifecycle_check": self.fact_index[identifier].get("lifecycle_check"),
                 "lifecycle_roles": _strings(self.fact_index[identifier].get("lifecycle_roles")),
                 "linkage": linkage} for identifier in ids if identifier in self.fact_index]


def _card_links(saved, facts_of):
    """Per logging change: exact owners through the change's fact, else labelled candidates (never an owner)."""
    links = {}
    for card in saved.cards:
        fact_id = card.get("fact_id")
        exact = [op["id"] for op in saved.operations if saved.exact(op["id"])
                 and any(item["id"] == fact_id for item in facts_of[op["id"]])]
        candidates = []
        if not exact:
            for op in saved.operations:
                if not saved.exact(op["id"]) and any(item["id"] == fact_id for item in facts_of[op["id"]]):
                    candidates.append((op["id"], "event_key"))
            anchor = _range(card.get("source_anchor")) if card.get("location") == "anchored" else None
            if anchor is not None:
                for op in saved.operations:
                    if (op["id"] not in {item for item, _ in candidates}
                            and any(_overlaps(anchor, span) for span in saved.code_ranges(op))):
                        candidates.append((op["id"], "source_overlap"))
        links[card["id"]] = {"card_id": card["id"], "fact_id": fact_id, "operation_ids": exact,
                             "candidates": [{"operation_id": identifier, "linkage": linkage}
                                            for identifier, linkage in candidates]}
    return links


def card_links(bundle):
    saved = _Saved(bundle)
    facts_of = {op["id"]: saved.operation_facts(op) for op in saved.operations}
    return _card_links(saved, facts_of)


def related_operations(transitions, operation_id):
    """Structural reachability over map transitions, cycles included; never an observed trace of one case.

    upstream: operations from which operation_id can be reached; downstream: operations reachable from it; the
    operation itself is in neither. transition_ids: transitions with both ends on the upstream side (the operation
    included) or both on the downstream side, so a bypass from an ancestor straight to a descendant is not part of
    the scope. Iterative and linear in the size of the map.
    """
    forward, backward, rows = {}, {}, []
    for item in transitions if isinstance(transitions, list) else []:
        if not isinstance(item, dict):
            continue
        source, target = item.get("from_operation"), item.get("to_operation")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        forward.setdefault(source, []).append(target)
        backward.setdefault(target, []).append(source)
        rows.append((item.get("id"), source, target))

    def reach(adjacency):
        seen, todo = {operation_id}, [operation_id]
        while todo:
            for following in adjacency.get(todo.pop(), ()):
                if following not in seen:
                    seen.add(following)
                    todo.append(following)
        return seen
    upstream, downstream = reach(backward), reach(forward)
    edges = [identifier for identifier, source, target in rows
             if isinstance(identifier, str) and ((source in upstream and target in upstream)
                                                 or (source in downstream and target in downstream))]
    return {"upstream": sorted(upstream - {operation_id}), "downstream": sorted(downstream - {operation_id}),
            "transition_ids": _unique(edges)}


# Addressed questions.


def _unit_reason(state):
    from .direct_atomic import _REASONS_RU
    reason = state.get("reason")
    if state.get("state") == "request_failed":
        return "решение не получено, запрос не принят: " + str(reason)
    return "решение не получено: " + _REASONS_RU.get(reason, str(reason))


def _questions(saved, facts_of):
    """Structured, addressed items first; then every remaining process unknown, so nothing is lost."""
    items = []
    names = {op["id"]: _text(op.get("name")) or op["id"] for op in saved.operations}

    def add(kind, subject, text, operations, source):
        items.append({"subject_kind": kind, "subject_id": subject, "text": text,
                      "operation_ids": [item for item in operations if item in saved.op_index], "source": source})
    for log in saved.logs:
        entry = log["entry"]
        if entry["linkage"] != "exact":
            continue
        [owner] = log["owners"]
        where = f"Событие {entry['event_key']} ({entry['path']}:{entry['start_line']})"
        if entry["role_support"] != "emit_within_operation_sources":
            add("operation", owner, f"{where}: место вызова не входит в источники операции «{names[owner]}»; "
                                    "привязка не подтверждена.", [owner], "event_mapping")
        if entry["ambiguity"]:
            add("operation", owner, f"{where}: {entry['ambiguity']}", [owner], "event_mapping")
    for op in saved.operations:
        for question in _strings(_dict(op.get("details")).get("questions")):
            add("operation", op["id"], f"Операция «{names[op['id']]}»: {question}", [op["id"]], "details")
    outgoing = {}
    for item in saved.transitions:
        outgoing.setdefault(item["from_operation"], []).append(item)
    for source, rows in outgoing.items():
        if len(rows) < 2:
            continue
        for item in rows:
            if _text(item.get("condition")) is None:
                add("transition", item.get("id"), f"Переход «{names[source]}» → «{names[item['to_operation']]}»: "
                                                  "условие не установлено; ветвь не считается успешной или "
                                                  "последовательной по умолчанию.", [source], "transition")
    for identifier, state in saved.units.items():
        symbol, path = state.get("symbol"), state.get("path")
        head = f"Единица {symbol} ({path}"
        if state.get("disposition") == "unresolved" and _text(state.get("question")):
            question = state["question"]
            text = next((line for line in saved.unknowns if line.startswith(head) and line.endswith(question)),
                        f"{head}): {question}")
            add("unit", identifier, text, _strings(state.get("operation_ids")), "unit")
        elif state.get("state") in ("deferred", "request_failed"):
            add("unit", identifier, f"{head}): {_unit_reason(state)}", [], "unit")
    for fact in saved.facts:
        if fact.get("lifecycle_check") == "ambiguous":
            owners = [op["id"] for op in saved.operations if saved.exact(op["id"])
                      and any(item["id"] == fact["id"] for item in facts_of[op["id"]])]
            add("fact", fact["id"], f"Факт «{fact.get('meaning')}» ({fact['id']}): роль или операция связанных записей "
                                    "неоднозначна; факт не подтверждён до уточнения.", owners, "fact")
    # /2 already stores exact gap owners. Keep that address in the existing inspector instead of reducing every
    # new gap to an unowned process unknown. /1 has no such contract and must not infer owners from labels.
    if (saved.atomic or {}).get("schema") == "atomic-map/2":
        for gap in _records(_dict(saved.atomic.get("code_led")).get("gaps")):
            identifier, kind, question = (_text(gap.get(key)) for key in ("id", "kind", "question"))
            if not identifier or not kind or not question:
                continue
            owners = [key for key in _strings(gap.get("operation_ids")) if key in saved.op_index]
            unit_ids = [key for key in _strings(gap.get("unit_ids")) if key in saved.units]
            subject_kind = "operation" if owners else "unit" if unit_ids else "process"
            subject_id = owners[0] if owners else unit_ids[0] if unit_ids else None
            add(subject_kind, subject_id, f"Разрыв {identifier} ({kind}): {question}", owners, "code_led_gap")
    covered = {item["text"] for item in items}
    for line in saved.unknowns:
        if line not in covered:
            add("process", None, line, [], "process")
            covered.add(line)
    return items


def question_index(bundle):
    saved = _Saved(bundle)
    facts_of = {op["id"]: saved.operation_facts(op) for op in saved.operations}
    return _questions(saved, facts_of)


# Operation views.


def _hints(name, logs):
    hints = []
    if isinstance(name, str) and _CONJUNCTION.search(name):
        hints.append({"kind": "heuristic", "signal": "conjunction_in_name", "semantic_verification": False,
                      "text": "Название соединяет несколько действий союзом: возможно, это несколько самостоятельных "
                              "операций."})
    results = {(item["path"], item["start_line"], item["end_line"]) for item in logs
               if item["lifecycle_role"] == "result"}
    if len(results) > 1:
        hints.append({"kind": "heuristic", "signal": "several_result_sites", "semantic_verification": False,
                      "text": f"Записей с ролью «результат» в разных местах кода: {len(results)}. Возможно, в "
                              "операции объединены несколько самостоятельных результатов."})
    return hints


def operation_views(bundle):
    """One inspector view per operation of the saved map, in map order (see the module docstring)."""
    saved = _Saved(bundle)
    facts_of = {op["id"]: saved.operation_facts(op) for op in saved.operations}
    links = _card_links(saved, facts_of)
    questions = _questions(saved, facts_of)
    cards = {card["id"]: card for card in saved.cards}
    views = {}
    for op in saved.operations:
        identifier = op["id"]
        atomic = saved.atomic_ops.get(identifier) if saved.exact(identifier) else None
        details = op.get("details") if isinstance(op.get("details"), dict) else None
        logs = [log["entry"] for log in saved.logs if identifier in log["owners"]]
        model = set(_strings((atomic or {}).get("model_evidence_ids")))
        unit = set(_strings((atomic or {}).get("unit_evidence_ids")))
        code = []
        for key in _strings(op.get("evidence_ids")):
            item = _dict(saved.evidence.get(key))
            anchor = _dict(item.get("source_anchor"))
            span = _range(anchor)
            if span is None:
                continue
            cited_by = ("model" if key in model or atomic is None else "host_unit" if key in unit else "unknown")
            code.append({"evidence_id": key, "path": span[0], "symbol": anchor.get("symbol"), "start_line": span[1],
                         "end_line": span[2], "cited_by": cited_by, "preview": preview(item.get("safe_quote")),
                         "text_validation": item.get("text_validation")})
        improvements, candidates = [], []
        for link in links.values():
            card = cards[link["card_id"]]
            summary = {"card_id": card["id"], "fact_id": card.get("fact_id"), "action": card.get("action"),
                       "semantics": card.get("semantics"), "rationale": card.get("rationale"),
                       "fields": card.get("fields") if isinstance(card.get("fields"), dict) else {},
                       "transaction_timing": card.get("transaction_timing"), "location": card.get("location"),
                       "anchor": card.get("source_anchor") if isinstance(card.get("source_anchor"), dict) else None}
            if identifier in link["operation_ids"]:
                improvements.append({**summary, "linkage": "fact"})
            for candidate in link["candidates"]:
                if candidate["operation_id"] == identifier:
                    candidates.append({**summary, "linkage": candidate["linkage"],
                                       "shared_with": [other["operation_id"] for other in link["candidates"]
                                                       if other["operation_id"] != identifier]})

        def route(item, peer):
            edge = saved.atomic_edges.get(item.get("id"), {})
            return {"operation_id": item[peer], "transition_id": item.get("id"), "condition": _text(item.get("condition")),
                    "basis": edge.get("basis"), "basis_note": _text(edge.get("basis_note")),
                    "evidence_ids": _strings(item.get("evidence_ids"))}
        views[identifier] = {
            "id": identifier, "name": op.get("name"), "description": op.get("description"), "kind": op.get("kind"),
            "stage_id": op.get("stage_id"), "stage_name": saved.stage_names.get(op.get("stage_id")),
            "actor": op.get("actor"), "initiation": (atomic or {}).get("initiation"),
            "initiation_basis": (atomic or {}).get("initiation_basis"),
            "linkage": "exact" if atomic is not None else "event_key",
            "event_keys": _strings(op.get("event_keys")), "logs": logs, "facts": facts_of[identifier],
            "improvements": improvements, "improvement_candidates": candidates,
            "previous": [route(item, "from_operation") for item in saved.transitions
                         if item["to_operation"] == identifier],
            "next": [route(item, "to_operation") for item in saved.transitions if item["from_operation"] == identifier],
            "continuation": (details or {}).get("continuation") or "unknown",
            "join": (details or {}).get("join") or "unknown",
            "questions": [item for item in questions if identifier in item["operation_ids"]],
            "code": code, "details": details, "hints": _hints(op.get("name"), logs),
            "unit_ids": _strings((atomic or {}).get("unit_ids")), "request_id": (atomic or {}).get("request_id"),
            "evidence_ids": _strings(op.get("evidence_ids"))}
    return views


# Detail levels: the same records regrouped for display.


def detail_view(bundle, level):
    """Process, stage or operation grouping of every saved record; totals are identical at every level."""
    if level not in LEVELS:
        raise ValueError("detail level must be process, stages or operations")
    saved = _Saved(bundle)
    views = operation_views(bundle)
    questions = question_index(bundle)
    op_ids = [op["id"] for op in saved.operations]
    fact_ids = [fact["id"] for fact in saved.facts]
    card_ids = [card["id"] for card in saved.cards]
    metric_ids = [metric["id"] for metric in saved.metrics]
    fact_stage = {fact["id"]: fact.get("stage_id") for fact in saved.facts}

    def metrics_of(facts):
        chosen = set(facts)
        listed = {metric for fact in facts for metric in _strings(saved.fact_index[fact].get("metric_ids"))}
        return [metric["id"] for metric in saved.metrics
                if chosen & set(_strings(metric.get("dependencies"))) or metric["id"] in listed]

    def counted(items_ops, items_facts):
        return sum(1 for item in questions if set(item["operation_ids"]) & set(items_ops)
                   or (item["subject_kind"] == "fact" and item["subject_id"] in items_facts))

    def item(kind, identifier, name, ops, facts, cards, metrics, mappings, count):
        return {"kind": kind, "id": identifier, "name": name, "operation_ids": list(ops), "fact_ids": list(facts),
                "card_ids": list(cards), "metric_ids": list(metrics), "event_mapping_count": mappings,
                "question_count": count}
    items = []
    if level == "process":
        items.append(item("process", None, _text(saved.process.get("purpose")), op_ids, fact_ids, card_ids,
                          metric_ids, len(saved.mappings), len(questions)))
        transitions = []
    elif level == "stages":
        known = set(saved.stage_names)
        for stage in saved.stages:
            ops = [op["id"] for op in saved.operations if op.get("stage_id") == stage["id"]]
            facts = [fact for fact in fact_ids if fact_stage[fact] == stage["id"]]
            cards = [card["id"] for card in saved.cards if fact_stage.get(card.get("fact_id")) == stage["id"]]
            items.append(item("stage", stage["id"], saved.stage_names[stage["id"]], ops, facts, cards,
                              metrics_of(facts), sum(mapping.get("stage_id") == stage["id"] for mapping in saved.mappings),
                              counted(ops, facts)))
        transitions = [dict(row) for row in _records(saved.process.get("transitions"))]
        rest = _rest(items, op_ids, fact_ids, card_ids, metric_ids,
                     sum(mapping.get("stage_id") not in known for mapping in saved.mappings))
        if rest is not None:
            items.append(item("unassigned", None, None, *rest[:5], 0))
    else:
        for op in saved.operations:
            view = views[op["id"]]
            facts = [fact["id"] for fact in view["facts"]]
            items.append(item("operation", op["id"], op.get("name"), [op["id"]], facts,
                              [change["card_id"] for change in view["improvements"]], metrics_of(facts),
                              sum(mapping.get("operation_id") == op["id"] for mapping in saved.mappings),
                              len(view["questions"])))
        transitions = [dict(row) for row in _records(saved.process.get("operation_transitions"))]
        rest = _rest(items, op_ids, fact_ids, card_ids, metric_ids,
                     sum(mapping.get("operation_id") not in saved.op_index for mapping in saved.mappings))
        if rest is not None:
            items.append(item("unassigned", None, None, *rest[:5], 0))
    open_units = {key: {"state": state.get("state"), "reason": state.get("reason"), "symbol": state.get("symbol"),
                        "path": state.get("path")}
                  for key, state in saved.units.items() if state.get("state") in ("deferred", "request_failed")}
    totals = {"operations": len(op_ids), "event_mappings": len(saved.mappings), "facts": len(fact_ids),
              "cards": len(card_ids), "metrics": len(metric_ids), "questions": len(questions),
              "unknowns": len(saved.unknowns), "open_units": len(open_units), "evidence": len(saved.evidence)}
    return {"level": level, "items": items, "transitions": transitions, "unknowns": list(saved.unknowns),
            "open_units": open_units, "totals": totals}


def _rest(items, op_ids, fact_ids, card_ids, metric_ids, mappings):
    """Records no group holds, so that every level keeps all of them; None when nothing is left."""
    rest = []
    for key, identifiers in (("operation_ids", op_ids), ("fact_ids", fact_ids), ("card_ids", card_ids),
                             ("metric_ids", metric_ids)):
        held = {value for entry in items for value in entry[key]}
        rest.append([value for value in identifiers if value not in held])
    if not any(rest) and not mappings:
        return None
    return (*rest, mappings)
