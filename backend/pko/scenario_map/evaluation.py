"""Offline comparison with a human-authored rubric; never execute source code.

Matching names and citation anchors does not verify the truth of a claim or
discover omitted source behavior. The rubric's authorship and limits are part
of every result. This evaluator is intentionally outside production gates.
"""
from __future__ import annotations

import re
from collections import defaultdict


LIMITATION = ("Сравнение с контрольной разметкой проверяет только перечисленные случаи. "
              "Исходники и модель не запускаются; совпадение названий и ссылок не доказывает "
              "семантическую полноту или выполнение процесса.")


def _pattern(pattern, value):
    if not isinstance(pattern, str) or len(pattern) > 1000:
        raise ValueError("Шаблон должен быть строкой до 1000 символов")
    try:
        return re.search(pattern, str(value or ""), re.IGNORECASE) is not None
    except re.error as exc:
        raise ValueError(f"Некорректный шаблон разметки: {exc}") from exc


def evaluate_map(model: dict, rubric: dict) -> dict:
    if not isinstance(model, dict) or not isinstance(rubric, dict):
        raise ValueError("Карта и разметка должны быть объектами JSON")
    nodes, edges = model.get("nodes", []), model.get("edges", [])
    expected = rubric.get("expected_operations", [])
    if not isinstance(expected, list) or not expected:
        raise ValueError("Разметка должна содержать expected_operations")
    digest = rubric.get("source_snapshot_digest")
    if digest is not None:
        if not isinstance(digest, str) or not digest:
            raise ValueError("source_snapshot_digest требует непустую строку")
        actual = [source.get("digest") for source in model.get("sources", []) if isinstance(source, dict)]
        if digest not in actual:
            # Citation line numbers refer to this exact source snapshot. Do not
            # grade another revision as though its anchors were still valid.
            return {"rubric_id": rubric.get("id", ""), "rubric_status": rubric.get("status", "unreviewed"),
                    "semantic_verification": False, "limitation": LIMITATION, "source_digest_verified": False,
                    "passed": False, "matched_operations": 0, "expected_operations": len(expected), "matches": {},
                    "findings": [{"code": "rubric_source_mismatch", "expected_digest": digest, "actual_digests": actual}]}
    node_index = {n["id"]: n for n in nodes}
    if len(node_index) != len(nodes):
        raise ValueError("В карте повторяются идентификаторы операций")
    aliases, owners, findings = {}, defaultdict(list), []

    def report(code, **details):
        findings.append({"code": code, **details})

    for operation in expected:
        if not isinstance(operation, dict):
            raise ValueError("expected_operations содержит только объекты")
        alias = operation.get("id")
        if not isinstance(alias, str) or not alias or alias in aliases:
            raise ValueError("Операции разметки требуют уникальных непустых id")
        if not isinstance(operation.get("label"), str) or not operation["label"]:
            raise ValueError("Операция разметки требует label")
        anchor, pattern = operation.get("evidence"), operation.get("name_pattern")
        if pattern is not None:
            _pattern(pattern, "")  # Validate even when the map contains no nodes.
        if anchor is not None and (not isinstance(anchor, dict) or
                not isinstance(anchor.get("path_suffix"), str) or not anchor["path_suffix"] or
                type(anchor.get("line")) is not int or anchor["line"] < 1):
            raise ValueError("evidence требует path_suffix и положительный номер line")
        matched = []
        for node in nodes:
            if pattern is not None and not _pattern(pattern, node.get("name")):
                continue
            if anchor is not None and not any(
                    str(ref.get("path", "")).replace("\\", "/").endswith(anchor["path_suffix"].replace("\\", "/")) and
                    ref.get("line_start", 0) <= anchor["line"] <= ref.get("line_end", 0)
                    for ref in node.get("evidence", [])):
                continue
            if pattern is None and anchor is None and node.get("name", "").casefold() != operation["label"].casefold():
                continue
            matched.append(node["id"])
        aliases[alias] = matched[0] if len(matched) == 1 else None
        if len(matched) != 1:
            report("missing_operation" if not matched else "ambiguous_operation", operation=alias,
                   label=operation["label"], candidates=matched)
        else:
            owners[matched[0]].append(alias)
    for node_id, labels in owners.items():
        if len(labels) > 1:
            report("collapsed_operations", node_id=node_id, operations=labels)

    def resolve(alias):
        if alias not in aliases:
            raise ValueError(f"Неизвестный alias разметки: {alias}")
        return aliases[alias]

    control = [edge for edge in edges if edge.get("kind") != "data"]
    for collection, forbidden in (("expected_transitions", False), ("forbidden_transitions", True)):
        for rule in rubric.get(collection, []):
            source, target = resolve(rule["source"]), resolve(rule["target"])
            if source is None or target is None:
                continue  # The unresolved operation already has an explicit finding.
            candidates = [edge for edge in control if edge["source"] == source and edge["target"] == target]
            matching = [edge for edge in candidates if "condition_pattern" not in rule or
                        _pattern(rule["condition_pattern"], edge.get("condition"))]
            if (forbidden and matching) or (not forbidden and not matching):
                report("forbidden_transition" if forbidden else "missing_transition", rule=rule,
                       edge_ids=[edge.get("id") for edge in (matching if forbidden else candidates)])

    outgoing = defaultdict(set)
    for edge in control:
        outgoing[edge["source"]].add(edge["target"])

    def reachable(source, target):
        visited, todo = {source}, [source]
        while todo:
            current = todo.pop()
            for nxt in outgoing[current]:
                if nxt == target:
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    todo.append(nxt)
        return source == target

    for rule in rubric.get("expected_paths", []):
        path = rule.get("path", [])
        if not isinstance(path, list) or len(path) < 2:
            raise ValueError("expected_paths.path требует минимум два alias")
        actual = [resolve(alias) for alias in path]
        if any(node is None for node in actual):
            continue
        check = reachable if rule.get("allow_intermediate", False) else lambda a, b: b in outgoing[a]
        if not all(check(a, b) for a, b in zip(actual, actual[1:])):
            report("missing_path", rule=rule, node_ids=actual)
    for rule in rubric.get("expected_properties", []):
        if not isinstance(rule.get("values"), list) or not rule["values"]:
            raise ValueError("expected_properties.values требует непустой список")
        node_id = resolve(rule["operation"])
        if node_id is not None and node_index[node_id].get(rule["field"]) not in rule["values"]:
            report("property_mismatch", rule=rule, node_id=node_id,
                   actual=node_index[node_id].get(rule["field"]))
    return {"rubric_id": rubric.get("id", ""), "rubric_status": rubric.get("status", "unreviewed"),
            "semantic_verification": False, "limitation": LIMITATION,
            "source_digest_verified": True if digest is not None else None,
            "passed": not findings, "matched_operations": sum(value is not None for value in aliases.values()),
            "expected_operations": len(expected), "matches": aliases, "findings": findings}
