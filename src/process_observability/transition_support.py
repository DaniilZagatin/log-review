"""Structural support of proposed operation transitions (transition-support/1, direct-atomic-validator/10).

classify(basis, contract, evidence, from_units, to_units, supplied, links) -> (state, reason, support)
still_open(open_links, operations, unit_states, active) -> {operation ID: [open link rows]}
unsupported_pairs(proposals) -> [((from, to), [inactive proposals])]
gap_id(kind, operation_ids, unit_ids) -> the host ID of one code-led gap

A transition that a map-atomic page proposes is active only when the host recomputes one of three supports from the
source that same page supplied, all host data:
- same_unit: one supplied unit realizes or serves both actions and a cited range lies inside its supplied source;
- call_chain: at most MAX_LINKS control links between supplied units connect a unit of one action to a unit of the
  other, in either direction (a return into the caller counts), and every link's source unit has a cited range inside
  its supplied source that covers the call line whenever one is known;
- contract_both_sides (source_handoff only): one cited range with the contract literal inside the supplied source of
  a sending unit and another inside the supplied source of a different, receiving unit.
A unit's supplied source here is its owned source: the ranges its page supplied, clipped to the unit's own lines (a
finding ref or site of other code supplied with it never counts). Every support records the owned source of its units
(unit_spans), so that the export can check each witness against the exact ranges of the unit it is claimed for.
Control links are host navigation (resolved same-file calls, catalog callee anchors), host static calls and, kept
apart as model provenance, resolved code-model relations of a control kind. Shared data, objects, fields and
identifiers are never a link. Everything else stays an inactive proposal with a generic reason; nothing is deleted.

This is a structural eligibility boundary, never semantic certification: a real chain with a wrong condition, branch
versus sequence in a common caller, a wrong registry mapping and what an outcome means all stay model text. No source
is executed or imported and no model is called here.
"""

from .code_review import stable_id

POLICY = "transition-support/1"
MAX_LINKS = 2
# Resolved code-model relations of these kinds are control links (model provenance); data_handoff and branch are not.
CONTROL_KINDS = frozenset({"call", "dispatch", "callback", "return", "control_handoff"})
# An unresolved code-model relation of these kinds leaves the realizing action's continuation open.
OPEN_KINDS = frozenset({"dispatch", "callback", "control_handoff"})
HOST_ORIGINS = frozenset({"navigation", "host_static"})
ORIGINS = ("host_static", "navigation", "worker_proposal")
SUPPORT_KINDS = ("same_unit", "call_chain", "contract_both_sides")
REASONS = ("endpoint_source_not_supplied", "no_source_chain", "chain_without_cited_call", "handoff_one_side")
OPEN_LINK_KINDS = ("unresolved_relation", "dependency_not_supplied")
# Decisions that make a callee no step of its own: an open link to it closes.
_NO_STEP = frozenset({"supporting", "excluded", "outside_process"})
_PENDING = frozenset({"deferred", "request_failed"})


def inside(anchor, spans):
    """Every line of anchor (path, first, last) lies in the union of spans [(path, first, last)]."""
    path, first, last = anchor
    lines = {line for other, start, end in spans if other == path
             for line in range(max(start, first), min(end, last) + 1)}
    return len(lines) == last - first + 1


def covers(anchor, calls):
    """anchor overlaps one of the known call lines [(path, first, last)]."""
    path, first, last = anchor
    return any(other == path and start <= last and first <= end for other, start, end in calls)


def _witness(evidence, spans, calls):
    for identifier, anchor, _ in evidence:
        if inside(anchor, spans) and (not calls or covers(anchor, calls)):
            return identifier
    return None


def _support(kind, units, links, witnesses, provenance, supplied):
    """One support record; unit_spans is the owned source of each of its units on the proposing page."""
    return {"kind": kind, "unit_ids": list(units), "links": links, "witness_evidence_ids": list(witnesses),
            "provenance": provenance, "unit_spans": {unit: [list(span) for span in supplied[unit]] for unit in units}}


def _link_row(key, link, witness):
    return {"from_unit": key[0], "to_unit": key[1], "origins": sorted(link["origins"]),
            "relation_ids": sorted(link["relation_ids"]), "call_lines": [list(item) for item in sorted(link["lines"])],
            "witness_evidence_id": witness}


def _search(from_units, to_units, order, graph):
    """The shortest path of at most MAX_LINKS links from a unit of one side to another unit of the other side.

    graph: unit -> [(neighbour, link key)] in supplied order. Returns ([units], [link keys]) or None; among equally
    short paths the first start and the first neighbours in supplied order win.
    """
    targets, starts = set(to_units), set(from_units)
    for depth in range(1, MAX_LINKS + 1):
        for start in [unit for unit in order if unit in starts]:
            frontier = [([start], [])]
            for _ in range(depth):
                frontier = [(units + [following], keys + [key]) for units, keys in frontier
                            for following, key in graph.get(units[-1], ()) if following not in units]
            for units, keys in frontier:
                if units[-1] in targets:
                    return units, keys
    return None


def classify(basis, contract, evidence, from_units, to_units, supplied, links):
    """(state, reason, support) of one proposed transition, from host data of its own page only.

    evidence: [(evidence ID, (path, first, last), frozen quote)] in citation order; from_units / to_units: the units
    that realize or serve each action (host accounting); supplied: {unit ID: [(path, first, last)]} of every unit
    this page supplied (page units, then shared rows); links: {(source unit, target unit): {"origins", "relation_ids",
    "lines"}} of control links between supplied units. Deterministic; it never raises for an unsupported claim.
    """
    order = list(supplied)
    sending = [unit for unit in from_units if unit in supplied]
    receiving = [unit for unit in to_units if unit in supplied]
    if not sending or not receiving:
        return "inactive", "endpoint_source_not_supplied", None
    if basis == "source_handoff":
        bearing = [(identifier, anchor) for identifier, anchor, quote in evidence
                   if type(contract) is str and contract.strip() and contract in (quote or "")]

        def owned(units):
            return [(identifier, next(unit for unit in units if inside(anchor, supplied[unit])))
                    for identifier, anchor in bearing if any(inside(anchor, supplied[unit]) for unit in units)]
        for sender, sender_unit in owned(sending):
            for receiver, receiver_unit in owned(receiving):
                if receiver != sender and receiver_unit != sender_unit:
                    return "active", None, _support("contract_both_sides", [sender_unit, receiver_unit], [],
                                                    [sender, receiver], "host", supplied)
        return "inactive", "handoff_one_side", None
    for unit in order:
        if unit in sending and unit in receiving:
            witness = _witness(evidence, supplied[unit], ())
            if witness is not None:
                return "active", None, _support("same_unit", [unit], [], [witness], "host", supplied)
    position = {unit: index for index, unit in enumerate(order)}
    witnesses = {key: _witness(evidence, supplied[key[0]], sorted(link["lines"])) for key, link in links.items()}

    def graph(witnessed):
        adjacent = {}
        for key in sorted(links, key=lambda pair: (position[pair[0]], position[pair[1]])):
            if witnessed and witnesses[key] is None:
                continue
            for here, there in ((key[0], key[1]), (key[1], key[0])):
                rows = adjacent.setdefault(here, [])
                if all(other != there for other, _ in rows):  # one link per neighbour: the first in supplied order
                    rows.append((there, key))
        for rows in adjacent.values():
            rows.sort(key=lambda row: position[row[0]])
        return adjacent
    found = _search(sending, receiving, order, graph(True))
    if found is not None:
        units, keys = found
        rows = [_link_row(key, links[key], witnesses[key]) for key in keys]
        model = any(not set(row["origins"]) & HOST_ORIGINS for row in rows)
        return "active", None, _support("call_chain", units, rows,
                                        list(dict.fromkeys(row["witness_evidence_id"] for row in rows)),
                                        "model_relation" if model else "host", supplied)
    if _search(sending, receiving, order, graph(False)) is not None:
        return "inactive", "chain_without_cited_call", None
    return "inactive", "no_source_chain", None


def still_open(open_links, operations, unit_states, active):
    """{operation ID: [open link rows]} of the recorded open control links that stay open after all pages, in record
    order; the gap of an operation names the distinct unit IDs of its rows.

    A link to a known unit closes when that unit is pending (deferred or failed: pending local work names it), was
    decided as no step of its own (supporting, excluded, outside_process), realizes the same operation, or realizes
    an operation joined to this one by an active transition. An unresolved target never closes. The producer and the
    export derive the host gaps with this one function.
    """
    joined = {}
    for edge in active:
        joined.setdefault(edge["from"], set()).add(edge["to"])
        joined.setdefault(edge["to"], set()).add(edge["from"])
    result = {}
    for link in open_links:
        key, target = link["operation_id"], link["target_unit_id"]
        if key not in operations:
            continue
        if target is not None:
            state = unit_states.get(target) or {}
            reached = set(state.get("operation_ids") or [])
            if (state.get("state") in _PENDING or (state.get("state") in ("accounted", "triaged")
                                                   and state.get("disposition") in _NO_STEP)
                    or key in reached or reached & joined.get(key, set())):
                continue
        result.setdefault(key, []).append(link)
    return result


def unsupported_pairs(proposals):
    """[((from, to), [inactive proposals])] of ordered pairs without an active proposal, in proposal order."""
    active = {(item["from"], item["to"]) for item in proposals if item["state"] == "active"}
    pairs = {}
    for item in proposals:
        if item["state"] == "inactive" and (item["from"], item["to"]) not in active:
            pairs.setdefault((item["from"], item["to"]), []).append(item)
    return list(pairs.items())


def gap_id(kind, operation_ids, unit_ids):
    """A gap ID from its kind, operations and units; an unsupported transition keeps its direction."""
    ordered = list(operation_ids) if kind == "unsupported_transition" else sorted(operation_ids)
    return stable_id("agp", kind, ordered, sorted(unit_ids))
