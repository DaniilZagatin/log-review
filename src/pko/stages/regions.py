"""Graph structure for stages: dominators as hints and the single-entry collapse check.

A stage may be shown as one block only when every path into it enters through
one operation from which all its members are reachable inside the stage.
Otherwise one block would draw paths the code does not have (A → stage → B
when the members reached from A cannot reach the member that leads to B).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Graph:
    nodes: list[str]
    outgoing: dict[str, list[str]]
    incoming: dict[str, list[str]]
    entries: list[str]


def operation_graph(model: dict) -> Graph:
    """Execution order only: data edges and edges to unknown operations are ignored."""
    nodes = [node["id"] for node in model.get("nodes") or []]
    known = set(nodes)
    outgoing = {node: [] for node in nodes}
    incoming = {node: [] for node in nodes}
    for edge in model.get("edges") or []:
        source, target = edge.get("source"), edge.get("target")
        if edge.get("kind") != "data" and source in known and target in known and target not in outgoing[source]:
            outgoing[source].append(target)
            incoming[target].append(source)
    entries = [node for node in (model.get("scenario") or {}).get("entry_operation_ids") or [] if node in known]
    if not entries:
        entries = [node for node in nodes if not incoming[node]]
    return Graph(nodes, outgoing, incoming, entries)


def immediate_dominators(graph: Graph) -> dict[str, str | None]:
    """Nearest operation every path from the scenario start passes before each one.

    Iterative algorithm of Cooper, Harvey and Kennedy over a virtual root joined
    to the entries. Entries and unreachable operations have no dominator.
    """
    root = "\0root"
    successors = {root: list(graph.entries), **graph.outgoing}
    order, seen, stack = [], {root}, [(root, iter(successors[root]))]
    while stack:
        node, children = stack[-1]
        child = next(children, None)
        if child is None:
            order.append(node)
            stack.pop()
        elif child not in seen:
            seen.add(child)
            stack.append((child, iter(successors.get(child, []))))
    order.reverse()
    position = {node: index for index, node in enumerate(order)}
    predecessors = {node: [p for p in graph.incoming.get(node, []) if p in position] for node in order}
    for entry in graph.entries:
        predecessors[entry] = predecessors.get(entry, []) + [root]
    dominator = {root: root}

    def intersect(left, right):
        while left != right:
            while position[left] > position[right]:
                left = dominator[left]
            while position[right] > position[left]:
                right = dominator[right]
        return left

    changed = True
    while changed:
        changed = False
        for node in order[1:]:
            known = [p for p in predecessors[node] if p in dominator]
            if not known:
                continue
            candidate = known[0]
            for other in known[1:]:
                candidate = intersect(candidate, other)
            if dominator.get(node) != candidate:
                dominator[node] = candidate
                changed = True
    return {node: (None if dominator.get(node) in (None, root) else dominator[node]) for node in graph.nodes}


def collapse_check(members: set[str], graph: Graph) -> tuple[bool, str | None, str]:
    """Return ``(collapsible, entry, reason)`` for showing ``members`` as one block."""
    entries = {target for target in members for source in graph.incoming.get(target, []) if source not in members}
    entries |= members & set(graph.entries)
    if not entries:
        return False, None, "У этапа нет входа из остальной карты"
    if len(entries) > 1:
        return False, None, "Этап имеет несколько входов: " + ", ".join(sorted(entries))
    entry = entries.pop()
    reached, todo = {entry}, [entry]
    while todo:
        for target in graph.outgoing.get(todo.pop(), []):
            if target in members and target not in reached:
                reached.add(target)
                todo.append(target)
    if reached != members:
        return False, entry, "Внутри этапа недостижимы от входа: " + ", ".join(sorted(members - reached))
    return True, entry, ""
