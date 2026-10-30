"""Publication facts derived from the final map, separate from model commentary.

This projection describes stored fields. It does not infer terminality, external
execution or semantic completeness, and it is not part of resumable research.
"""
from __future__ import annotations

from copy import deepcopy


COMMENTARY_STATUS = "model_commentary_not_verified"
OUTCOMES = {"success": "успех", "refusal": "отказ", "failure": "ошибка",
            "cancelled": "отмена", "deferred": "отложено",
            "no_decision": "без решения", "unknown": "не установлен",
            "shared": "общее завершение разных ветвей"}


def build_public_summary(model: dict) -> dict:
    """Recompute even if a saved map contains stale derived fields."""
    nodes = model.get("nodes") or []
    scenario = model.get("scenario") or {}
    by_id = {node.get("id"): node for node in nodes}
    terminal_ids = deepcopy(scenario.get("terminal_operation_ids") or [])
    terminals = []
    for identifier in terminal_ids:
        node = by_id.get(identifier)
        if node is not None:
            terminals.append({"id": identifier, "name": node.get("name", ""),
                              "outcome": node.get("outcome") or "unknown",
                              "outcome_scope": node.get("outcome_scope") or "branch",
                              "effect_kind": node.get("effect_kind") or "unknown",
                              "completion_condition": node.get("completion_condition") or ""})
    external = [{"operation_id": node.get("id"), "operation_name": node.get("name", ""),
                 "interaction_index": index,
                 **{key: deepcopy(item.get(key, "")) for key in
                    ("name", "purpose", "direction", "endpoint", "contract")}}
                for node in nodes for index, item in enumerate(node.get("external_interactions") or [], 1)]
    counts = {"nodes": len(nodes), "edges": len(model.get("edges") or []),
              "gaps": len(model.get("gaps") or []), "terminal_operations": len(terminals),
              "external_operations": sum(bool(node.get("external_interactions")) for node in nodes),
              "external_interactions": len(external)}
    outcomes = {}
    for terminal in terminals:
        outcome = "shared" if terminal["outcome_scope"] == "shared" else terminal["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    facts = {"version": "1", "status": "derived_from_map_fields",
             "semantic_completeness_verified": False,
             "run_status": (model.get("run") or {}).get("status", "unknown"),
             "counts": counts,
             "operations": [{"id": node.get("id"), "name": node.get("name", "")} for node in nodes],
             "entry_operation_ids": deepcopy(scenario.get("entry_operation_ids") or []),
             "terminal_operation_ids": terminal_ids,
             "terminal_operations": terminals,
             "unresolved_terminal_ids": [identifier for identifier in terminal_ids if identifier not in by_id],
             "terminal_outcomes": outcomes, "external_interactions": external}
    noun = ("действий" if model.get("semantic_profile") == "variant-actions-v1" else
            "операций" if str(model.get("schema_version", "")).startswith("2.") else "узлов")
    summary = (f"На карте: {noun} — {counts['nodes']}; переходов — {counts['edges']}; "
               f"пробелов — {counts['gaps']}. "
               f"Явно заданных конечных {noun} — {counts['terminal_operations']}. "
               f"С внешними взаимодействиями — {counts['external_operations']} "
               f"(обращений — {counts['external_interactions']}).")
    if outcomes:
        labels = [f"{OUTCOMES.get(key, key)} — {outcomes[key]}"
                  for key in list(OUTCOMES) + sorted(set(outcomes) - set(OUTCOMES)) if key in outcomes]
        summary += f" Завершения, указанные у конечных {noun}: " + "; ".join(labels) + "."
    if facts["unresolved_terminal_ids"]:
        summary += f" Не разрешены ссылки на конечные действия: {len(facts['unresolved_terminal_ids'])}."
    previous = model.get("model_commentary")
    commentary = (previous.get("text", "") if isinstance(previous, dict)
                  and previous.get("status") == COMMENTARY_STATUS else model.get("summary", ""))
    return {"summary": summary, "factual_summary": facts,
            "model_commentary": {"status": COMMENTARY_STATUS, "text": str(commentary or "")}}


def apply_public_summary(model: dict) -> None:
    """Update publication-only fields, preserving the original public comment."""
    model.update(build_public_summary(model))
