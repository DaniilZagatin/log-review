"""Conservative offline comparison and observable revisions, never a semantic proof."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json

from pko.audit import record, sanitize

LIMITATION = ("Сопоставление по основаниям — ориентир для ревью, не доказательство одинакового смысла. "
              "Неоднозначные соответствия не выбираются автоматически. Исходники и модель не запускаются.")
MUTATIONS = frozenset({"set_scope", "upsert_step", "upsert_transition", "patch_fragment",
                      "review_execution", "review_semantics", "add_gap", "save_research_task", "save_research_brief"})
NODE_FIELDS = ("name", "description", "input", "output", "condition", "outcome", "effect_kind",
               "continuation_mode", "join_mode", "completion_condition", "external_interactions", "evidence")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _anchors(node):
    return tuple(sorted(set((str(ref.get("path", "")).replace("\\", "/"),
                             ref.get("line_start", 0), ref.get("line_end", 0))
                            for ref in node.get("evidence", []) if isinstance(ref, dict))))


def _overlap(left, right):
    return any(p == q and a <= d and c <= b for p, a, b in _anchors(left)
               for q, c, d in _anchors(right))


def _key(node):
    # A shared function range can support several actions. Contract/call context
    # disambiguates only exact anchors, never by an invented similarity score.
    calls = tuple(sorted((x.get("name", ""), x.get("contract", ""), x.get("direction", ""))
                         for x in node.get("external_interactions", [])))
    return _anchors(node), calls


def _identity(model):
    sources = model.get("sources", [])
    known = bool(sources) and all(s.get("digest") for s in sources)
    run = model.get("run", {})
    configuration = run.get("reproducibility")
    if configuration:
        configuration = {**configuration, **{key: run.get(key) for key in
                         ("max_steps", "max_seconds", "model_timeout_seconds", "model_mode")},
                         "code_sha256": (run.get("build") or {}).get("code_sha256")}
        if any(configuration.get(key) is None for key in ("max_steps", "max_seconds", "model_timeout_seconds", "model_mode", "code_sha256")):
            configuration = None
    return {"sources": sorted((s.get("id", ""), s.get("digest", "")) for s in sources) if known else None,
            "description": digest(model.get("scenario", {}).get("description"))
                           if model.get("scenario", {}).get("description") else None,
            "configuration": configuration}


def compare_maps(left, right, left_hierarchy=None, right_hierarchy=None):
    """Return explicit uncertainty; a match is an anchor match, not correctness."""
    identities = [_identity(m) for m in (left, right)]
    identity = {key: ("unknown" if any(i[key] is None for i in identities) else
                      "same" if identities[0][key] == identities[1][key] else "different")
                for key in identities[0]}
    result = {"schema_version": "1.0", "semantic_verification": False, "limitation": LIMITATION,
              "identity": identity, "counts": [{k: len(m.get(k, [])) for k in ("nodes", "edges", "gaps")}
                                                   for m in (left, right)]}
    if identity["sources"] != "same":
        return {**result, "comparison_status": "incomparable_sources", "matches": [],
                "reason": "Для сопоставления строк нужны подтверждённые одинаковые снимки."}
    nodes = [{n["id"]: n for n in m.get("nodes", [])} for m in (left, right)]
    if any(len(idx) != len(m.get("nodes", [])) for idx, m in zip(nodes, (left, right))):
        raise ValueError("Повторяющиеся ID действий")
    groups = [defaultdict(list), defaultdict(list)]
    for g, idx in zip(groups, nodes):
        for node_id, node in idx.items():
            if _anchors(node):
                g[_key(node)].append(node_id)
    matches = {ids[0]: groups[1][key][0] for key, ids in groups[0].items()
               if len(ids) == 1 and len(groups[1].get(key, [])) == 1}
    # If only the address changed, an otherwise unique evidence anchor is still
    # useful for showing that change, rather than reporting two missing actions.
    remaining = [{k: n for k, n in idx.items() if k not in (matches if side == 0 else matches.values())}
                 for side, idx in enumerate(nodes)]
    for a, node in remaining[0].items():
        candidates = [b for b, other in remaining[1].items() if _anchors(node) and _anchors(node) == _anchors(other)]
        if len(candidates) == 1:
            b = candidates[0]
            reverse = [x for x, other in remaining[0].items() if _anchors(other) == _anchors(remaining[1][b])]
            if len(reverse) == 1:
                matches[a] = b
    unresolved = []
    for side, idx in enumerate(nodes):
        used = matches if side == 0 else set(matches.values())
        for node_id, node in idx.items():
            if node_id not in used:
                candidates = sorted(k for k, other in nodes[1-side].items() if _overlap(node, other))
                unresolved.append({"side": "left" if side == 0 else "right", "id": node_id,
                                   "status": "ambiguous_or_split" if candidates else "no_anchor_match",
                                   "candidates": candidates})
    changed = []
    for a, b in sorted(matches.items()):
        fields = [key for key in NODE_FIELDS if nodes[0][a].get(key) != nodes[1][b].get(key)]
        if fields:
            changed.append({"left_id": a, "right_id": b, "fields": fields,
                            "left": {k: nodes[0][a].get(k) for k in fields if k != "evidence"},
                            "right": {k: nodes[1][b].get(k) for k in fields if k != "evidence"}})
    reverse = {b: a for a, b in matches.items()}
    edge_sets, unresolved_edges = [], []
    for side, model in enumerate((left, right)):
        edges = Counter()
        mapping = {a: a for a in matches} if side == 0 else reverse
        for edge in model.get("edges", []):
            if edge.get("source") not in mapping or edge.get("target") not in mapping:
                unresolved_edges.append({"side": "left" if side == 0 else "right", "id": edge.get("id")})
                continue
            key = (mapping[edge["source"]], mapping[edge["target"]], edge.get("kind", "sequence"), edge.get("condition", ""))
            edges[key] += 1
        edge_sets.append(edges)
    edge_delta = lambda counts: [{"source_left_id": a, "target_left_id": b, "kind": kind, "condition": cond, "count": count}
                                for (a, b, kind, cond), count in sorted(counts.items())]
    scope_changes = {}
    ls, rs = (m.get("scenario", {}) for m in (left, right))
    for field in ("instance_unit", "variant_context", "variant_factors", "start", "end", "exclusions", "assumptions"):
        if ls.get(field) != rs.get(field):
            scope_changes[field] = {"left": ls.get(field), "right": rs.get(field)}
    for field in ("entry_operation_ids", "terminal_operation_ids"):
        a, b = set(ls.get(field, [])), set(rs.get(field, []))
        if a - matches.keys() or b - reverse.keys() or a != {reverse[x] for x in b if x in reverse}:
            scope_changes[field] = {"left": sorted(a), "right": sorted(b)}
    result.update(comparison_status="anchor_comparison", matches=[{"left_id": a, "right_id": b} for a, b in sorted(matches.items())],
                  unresolved_operations=unresolved, changed_operations=changed, scope_changes=scope_changes,
                  transitions={"only_left": edge_delta(edge_sets[0]-edge_sets[1]),
                               "only_right": edge_delta(edge_sets[1]-edge_sets[0]), "unresolved": unresolved_edges},
                  runs=[{key: m.get("run", {}).get(key) for key in ("id", "root_run_id", "attempt", "status", "stop_kind", "reason")}
                        for m in (left, right)])
    if left_hierarchy is not None and right_hierarchy is not None:
        from pko.stages.contracts import validate_hierarchy
        try:
            for hierarchy, model in ((left_hierarchy, left), (right_hierarchy, right)):
                validate_hierarchy(hierarchy, model)
        except (ValueError, KeyError, TypeError) as exc:
            result["grouping"] = {"status": "unverified_input", "reason": str(exc)}
            return sanitize(result)
        memberships = [h.get("membership", {}) for h in (left_hierarchy, right_hierarchy)]
        pairs, differences = 0, []
        ids = sorted(matches)
        for pos, a in enumerate(ids):
            for b in ids[pos+1:]:
                values = [memberships[0].get(a), memberships[0].get(b), memberships[1].get(matches[a]), memberships[1].get(matches[b])]
                if not all(isinstance(v, str) and v for v in values):
                    continue
                pairs += 1
                if (values[0] == values[1]) != (values[2] == values[3]):
                    differences.append([a, b])
        result["grouping"] = {"matched_assigned_pairs": pairs, "different_pairs_left_ids": differences,
                              "limitation": "Только однозначно сопоставленные и распределённые действия; корректность групп не доказана."}
    return sanitize(result)


def mutation_snapshot(model):
    """Bounded public state only; no hidden model reasoning or source bodies."""
    return deepcopy({"scenario": model.get("scenario", {}),
                     "nodes": [{k: n.get(k) for k in ("id", *NODE_FIELDS)} for n in model.get("nodes", [])],
                     "edges": model.get("edges", []),
                     "reviews": model.get("execution_reviews", []), "gaps": model.get("gaps", []),
                     "tasks": model.get("research_tasks", []), "semantic_review": model.get("semantic_review"),
                     "brief": model.get("research_brief")})


def record_mutation(before, model, tool, result):
    after = mutation_snapshot(model)
    if before == after and result.get("ok"):
        return
    changes = {}
    for key in ("nodes", "edges", "reviews", "gaps", "tasks"):
        old = {v["id"]: v for v in before[key]}
        new = {v["id"]: v for v in after[key]}
        changes[key] = {"added": sorted(new.keys()-old.keys()), "removed": sorted(old.keys()-new.keys()),
                        "changed": [{"id": i, "fields": sorted(k for k in old[i].keys() | new[i].keys()
                                                            if old[i].get(k) != new[i].get(k))}
                                    for i in sorted(old.keys() & new.keys()) if old[i] != new[i]]}
    scope = {k: {"before": before["scenario"].get(k), "after": after["scenario"].get(k)}
             for k in before["scenario"].keys() | after["scenario"].keys()
             if before["scenario"].get(k) != after["scenario"].get(k)}
    record("map_revision", tool=tool, accepted=bool(result.get("ok")),
           revision_before=digest(before), revision_after=digest(after), changes=changes,
           scope_changes=scope, semantic_review_changed=before["semantic_review"] != after["semantic_review"],
           brief_changed=before["brief"] != after["brief"], stale_review_ids=result.get("stale_review_ids", []),
           semantic_review_stale=result.get("semantic_review_stale", False),
           diagnostic={k: result[k] for k in ("code", "error", "issues", "details") if k in result})


def audit_statistics(paths):
    """Count recorded tool events once, not their duplicated span/log wrappers."""
    total = rejected = relevant = relevant_rejected = streak = longest = repeated = 0
    by_tool = defaultdict(lambda: {"calls": 0, "rejected": 0})
    attempts, partial_events = set(), 0
    def events(path):
        nonlocal partial_events
        chunks = []
        with open(path, encoding="utf-8-sig") as stream:
            for line in stream:
                if line.startswith("=== "):
                    if chunks:
                        try:
                            yield json.loads("".join(chunks))
                        except ValueError:
                            partial_events += 1
                    chunks = []
                elif line.strip() or chunks:
                    chunks.append(line)
        if chunks:
            try:
                yield json.loads("".join(chunks))
            except ValueError:
                partial_events += 1
    for path in paths:
        streak = 0
        for event in events(path):
            if event.get("run_id"):
                attempts.add(event["run_id"])
            if event.get("event") == "addressed_rejection_repeated":
                repeated = max(repeated, event.get("count", 0))
            if event.get("event") != "scenario_tool":
                continue
            name = event.get("name", "unknown")
            failed = event.get("result", {}).get("ok") is False
            total += 1
            rejected += int(failed)
            by_tool[name]["calls"] += 1
            by_tool[name]["rejected"] += int(failed)
            is_relevant = name in MUTATIONS or name in ("finish", "review_source")
            relevant += int(is_relevant)
            relevant_rejected += int(is_relevant and failed)
            streak = streak + 1 if failed else 0
            longest = max(longest, streak)
    return {"attempt_count": len(attempts), "tool_calls": total, "rejected_calls": rejected,
            "mutation_review_calls": relevant, "mutation_review_rejected": relevant_rejected,
            "mutation_review_rejection_rate": relevant_rejected/relevant if relevant else None,
            "longest_consecutive_rejections": longest, "max_recorded_addressed_repeats": repeated,
            "incomplete_events": partial_events, "by_tool": dict(sorted(by_tool.items()))}
