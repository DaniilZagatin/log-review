"""Bound registry of technical relations, so business analysis reuses them without rediscovery.

build_registry(review, graph) -> registry          (technical_relations.json of a review-mode run)
check_registry(registry, *, snapshot_id, review_run_fingerprint) -> registry
binding(registry) -> compact identity and counts for records that use it

Two origins are kept apart and never merged:
- worker_proposal: relations the code worker proposed under the relation contract (ReviewOptions.relations), already
  checked by review_execution against the frozen text that request supplied. A model proposal, not a verified edge.
- host_static: call sites of the static code graph, one row per call record (its call_id and column are part of the
  row identity, so two calls on one line stay two rows). Navigation only: a static call is not execution,
  reachability or a business step, and it never becomes a business transition by itself.
When the technical review did not collect relations (an original-identity or legacy cache), status is
not_collected and only host_static rows exist; nothing is synthesized to look complete.

check_registry validates the complete nested shape (keys, types, enums, ranges, safe relative paths, unique IDs,
origin-specific fields, the summary recomputed from the rows) and the binding. registry_sha256 detects accidental
change only: a consistently rehashed file is not authenticated by it.
"""

import copy
import hashlib
import json
from pathlib import PurePosixPath
import re

from .code_review import RELATION_KINDS, RELATION_RESOLUTIONS

SCHEMA = "technical-relations/1"
FILE = "technical_relations.json"
STATUSES = ("collected", "partial", "not_collected")
_BASIS = ("worker_proposal rows are code-model proposals whose source, refs and target ranges lie in text the "
          "request supplied; a resolved target was shown to that request. host_static rows are static call sites: "
          "navigation, not execution, reachability or business order. A target that was not supplied or not resolved "
          "says nothing about the receiver's behaviour.")
_KEYS = frozenset({"schema", "snapshot_id", "review_run_fingerprint", "status", "worker", "host_static", "summary",
                   "basis", "registry_sha256"})
_ROW_KEYS = frozenset({"id", "origin", "kind", "source", "target", "resolution", "condition", "data", "refs",
                       "evidence_ids", "task_id"})
_HOST_KEYS = _ROW_KEYS | {"call_id"}
_REF_KEYS = frozenset({"path", "start_line", "end_line", "symbol"})
_HOST_SOURCE_KEYS = _REF_KEYS | {"column"}
_TARGET_KEYS = frozenset({"symbol_id", "ref", "label", "path", "start_line", "end_line", "qualname"})
_HOST_TARGET_KEYS = frozenset({"symbol_id", "path", "start_line", "end_line", "qualname", "label"})
_SUMMARY_KEYS = frozenset({"worker", "host_static", "worker_by_kind", "worker_by_resolution",
                           "host_static_by_resolution"})
_HASH = re.compile(r"[0-9a-f]{64}")
_ROW_ID = {"worker": re.compile(r"cr-[0-9a-f]{16}"), "host_static": re.compile(r"hr-[0-9a-f]{16}")}
_MAX_TEXT = 100_000  # a sanity bound of saved labels and notes, never a truncation


class RelationError(ValueError):
    """A registry that is malformed, altered or bound to another review."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _line(value):
    return value if type(value) is int and value >= 1 else None


def _graph_data(graph):
    data = graph.to_dict() if graph is not None else {}
    nodes = {}
    for record in (data.get("nodes") or []):
        if type(record) is dict and type(record.get("id")) is str and type(record.get("path")) is str:
            nodes[record["id"]] = record
    calls = [record for record in (data.get("calls") or []) if type(record) is dict
             and type(record.get("path")) is str and _line(record.get("start_line")) is not None]
    return nodes, calls


def _node_target(node, label=None):
    first, last = _line(node.get("start_line")), _line(node.get("end_line"))
    if first is None or last is None or last < first:
        first = last = None
    qualname = node.get("qualname") or node.get("name") or node["id"]
    return {"symbol_id": node["id"], "path": node["path"], "start_line": first, "end_line": last,
            "qualname": qualname if type(qualname) is str else node["id"], "label": label}


def _worker_target(target, nodes):
    """The worker target with every key present: the reply's symbol_id/ref/label plus the location it names."""
    clean = {"symbol_id": target.get("symbol_id"), "ref": copy.deepcopy(target.get("ref")), "label": target.get("label"),
             "path": None, "start_line": None, "end_line": None, "qualname": None}
    node = nodes.get(clean["symbol_id"]) if type(clean["symbol_id"]) is str else None
    if node is not None:
        located = _node_target(node)
        clean.update(path=located["path"], start_line=located["start_line"], end_line=located["end_line"],
                     qualname=located["qualname"])
    elif type(clean["ref"]) is dict:
        clean.update(path=clean["ref"]["path"], start_line=clean["ref"]["start_line"],
                     end_line=clean["ref"]["end_line"], qualname=clean["ref"].get("symbol"))
    return clean


def build_registry(review, graph):
    """The registry of one review result and its code graph; deterministic, self-hashed."""
    collected = review.get("relations") if type(review.get("relations")) is dict else None
    status = collected["status"] if collected is not None and collected.get("status") in STATUSES else "not_collected"
    nodes, calls = _graph_data(graph)
    worker = []
    for item in (collected or {}).get("items", []):
        worker.append({"id": item["id"], "origin": "worker_proposal", "kind": item["kind"],
                       "source": copy.deepcopy(item["source"]), "target": _worker_target(item["target"], nodes),
                       "resolution": item["resolution"], "condition": item.get("condition"), "data": item.get("data"),
                       "refs": copy.deepcopy(item["refs"]), "evidence_ids": list(item.get("evidence_ids", [])),
                       "task_id": item.get("task_id")})

    def position(record):
        column = record.get("column") if type(record.get("column")) is int else -1
        return record["path"], record["start_line"], column, str(record.get("id"))
    host, seen = [], set()
    for call in sorted(calls, key=position):
        owner = nodes.get(call.get("owner_id")) if type(call.get("owner_id")) is str else None
        target = nodes.get(call.get("target_id")) if type(call.get("target_id")) is str else None
        callee = call.get("callee") if type(call.get("callee")) is str and call["callee"].strip() else None
        call_id = call.get("id") if type(call.get("id")) is str and call["id"] else None
        column = call.get("column") if type(call.get("column")) is int and call["column"] >= 0 else None
        last = _line(call.get("end_line"))
        last = last if last is not None and last >= call["start_line"] else call["start_line"]
        # The call record's own ID and column belong to the identity: two calls on one line stay two rows.
        identifier = "hr-" + _sha([call["path"], call["start_line"], column, callee, call.get("target_id"),
                                   call_id])[:16]
        if identifier in seen:
            continue  # the same call record listed twice
        seen.add(identifier)
        symbol = owner.get("qualname") if owner is not None else None
        host.append({"id": identifier, "origin": "host_static", "kind": "static_call", "call_id": call_id,
                     "source": {"path": call["path"], "start_line": call["start_line"], "end_line": last,
                                "column": column, "symbol": symbol if type(symbol) is str else "<module>"},
                     "target": (_node_target(target, callee) if target is not None
                                else {"symbol_id": None, "path": None, "start_line": None, "end_line": None,
                                      "qualname": None, "label": callee}),
                     "resolution": "resolved" if target is not None else "unresolved",
                     "condition": None, "data": None, "refs": [], "evidence_ids": [], "task_id": None})
    registry = {"schema": SCHEMA, "snapshot_id": review.get("snapshot_id"),
                "review_run_fingerprint": review.get("run_fingerprint"), "status": status,
                "worker": worker, "host_static": host, "summary": _summary(worker, host), "basis": _BASIS}
    registry["registry_sha256"] = _sha(registry)
    return registry


def _summary(worker, host):
    return {"worker": len(worker), "host_static": len(host),
            "worker_by_kind": _counts(row["kind"] for row in worker),
            "worker_by_resolution": _counts(row["resolution"] for row in worker),
            "host_static_by_resolution": _counts(row["resolution"] for row in host)}


def _counts(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


# Nested validation of a saved registry: every failure names the exact row and field.

def _fail(where, message):
    raise RelationError(f"{where}: {message}")


def _safe_path(value):
    if type(value) is not str or not value or len(value) > 1000 or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in value.split("/"))


def _text(value, where, *, nullable):
    if value is None and nullable:
        return
    if type(value) is not str or not value.strip() or len(value) > _MAX_TEXT:
        _fail(where, "must be a non-empty bounded string" + (" or null" if nullable else ""))


def _keys(value, keys, where):
    if type(value) is not dict:
        _fail(where, "must be an object")
    if set(value) != keys:
        extra, missing = sorted(set(value) - keys), sorted(keys - set(value))
        _fail(where, f"has unexpected keys {extra} or missing keys {missing}")


def _range(value, where):
    """path, start_line and end_line of value: a safe relative path and a positive line range."""
    first, last = value.get("start_line"), value.get("end_line")
    if not _safe_path(value.get("path")):
        _fail(where + ".path", "must be a safe relative repository path")
    if type(first) is not int or type(last) is not int or not 1 <= first <= last:
        _fail(where + ".start_line", "line range must be integers with 1 <= start_line <= end_line")


def _location(value, where, *, required):
    """An optional target location: no path and no lines, or a safe path with no lines or a valid line range."""
    if value["path"] is None and not required:
        if value["start_line"] is not None or value["end_line"] is not None:
            _fail(where + ".path", "line numbers need a path")
        return
    if not _safe_path(value["path"]):
        _fail(where + ".path", "must be a safe relative repository path")
    if not (value["start_line"] is None and value["end_line"] is None):
        _range(value, where)


def _ref(value, where):
    _keys(value, _REF_KEYS, where)
    _range(value, where)
    _text(value["symbol"], where + ".symbol", nullable=False)


def _row(row, origin, where):
    host = origin == "host_static"
    _keys(row, _HOST_KEYS if host else _ROW_KEYS, where)
    if type(row["id"]) is not str or not _ROW_ID[origin].fullmatch(row["id"]):
        _fail(where + ".id", "is not a host relation ID of this origin")
    if row["origin"] != ("host_static" if host else "worker_proposal"):
        _fail(where + ".origin", "does not match the list that holds the row")
    if row["resolution"] not in RELATION_RESOLUTIONS:
        _fail(where + ".resolution", f"must be one of {list(RELATION_RESOLUTIONS)}")
    target = row["target"]
    if host:
        if row["kind"] != "static_call":
            _fail(where + ".kind", "must be static_call for a host row")
        if row["call_id"] is not None and (type(row["call_id"]) is not str or not row["call_id"]):
            _fail(where + ".call_id", "must be a call record ID or null")
        source = row["source"]
        _keys(source, _HOST_SOURCE_KEYS, where + ".source")
        _range(source, where + ".source")
        _text(source["symbol"], where + ".source.symbol", nullable=False)
        if source["column"] is not None and (type(source["column"]) is not int or source["column"] < 0):
            _fail(where + ".source.column", "must be a non-negative integer or null")
        _keys(target, _HOST_TARGET_KEYS, where + ".target")
        _text(target["label"], where + ".target.label", nullable=True)
        if row["resolution"] == "resolved":
            if type(target["symbol_id"]) is not str or not target["symbol_id"]:
                _fail(where + ".target.symbol_id", "a resolved static call needs the graph symbol ID")
            _location(target, where + ".target", required=True)
            _text(target["qualname"], where + ".target.qualname", nullable=False)
        elif row["resolution"] == "unresolved":
            if any(target[key] is not None for key in ("symbol_id", "path", "start_line", "end_line", "qualname")):
                _fail(where + ".target", "an unresolved static call names no symbol or location")
        else:
            _fail(where + ".resolution", "a static call is resolved or unresolved")
        if (row["condition"], row["data"], row["refs"], row["evidence_ids"], row["task_id"]) != (None, None, [], [], None):
            _fail(where, "a host row carries no condition, data, refs, evidence or task")
        return
    if row["kind"] not in RELATION_KINDS:
        _fail(where + ".kind", f"must be one of {list(RELATION_KINDS)}")
    _ref(row["source"], where + ".source")
    if type(row["refs"]) is not list or not row["refs"]:
        _fail(where + ".refs", "must be a non-empty list of refs")
    for index, ref in enumerate(row["refs"]):
        _ref(ref, f"{where}.refs[{index}]")
    for name in ("condition", "data"):
        _text(row[name], f"{where}.{name}", nullable=True)
    if (type(row["evidence_ids"]) is not list or not row["evidence_ids"]
            or any(type(item) is not str or not item for item in row["evidence_ids"])
            or len(set(row["evidence_ids"])) != len(row["evidence_ids"])):
        _fail(where + ".evidence_ids", "must be a non-empty list of unique evidence IDs")
    _text(row["task_id"], where + ".task_id", nullable=False)
    _keys(target, _TARGET_KEYS, where + ".target")
    _text(target["label"], where + ".target.label", nullable=True)
    if target["ref"] is not None:
        _ref(target["ref"], where + ".target.ref")
    if row["resolution"] == "resolved":
        if target["symbol_id"] is None and target["ref"] is None:
            _fail(where + ".target", "a resolved relation needs a shown symbol_id or a supplied ref")
        if target["symbol_id"] is not None and (type(target["symbol_id"]) is not str or not target["symbol_id"]):
            _fail(where + ".target.symbol_id", "must be a symbol ID or null")
        _location(target, where + ".target", required=False)
        _text(target["qualname"], where + ".target.qualname", nullable=True)
    else:
        if target["label"] is None or any(target[key] is not None for key in
                                          ("symbol_id", "ref", "path", "start_line", "end_line", "qualname")):
            _fail(where + ".target", f"an {row['resolution']} target is named by its label only")


def check_registry(registry, *, snapshot_id, review_run_fingerprint):
    """The saved registry, unaltered, bound to this snapshot and review run and complete in shape, else
    RelationError naming the first offending field."""
    if type(registry) is not dict or set(registry) != _KEYS or registry.get("schema") != SCHEMA:
        raise RelationError("registry: not a technical-relations/1 registry")
    if type(registry["registry_sha256"]) is not str or registry["registry_sha256"] != _sha(
            {key: value for key, value in registry.items() if key != "registry_sha256"}):
        raise RelationError("registry: the content differs from its registry_sha256")
    for name in ("snapshot_id", "review_run_fingerprint"):
        if type(registry[name]) is not str or not _HASH.fullmatch(registry[name]):
            _fail(name, "must be a SHA-256 hex string")
    if registry["snapshot_id"] != snapshot_id or registry["review_run_fingerprint"] != review_run_fingerprint:
        raise RelationError("registry: belongs to another snapshot or technical review")
    if registry["status"] not in STATUSES:
        _fail("status", f"must be one of {list(STATUSES)}")
    if registry["basis"] != _BASIS:
        _fail("basis", "is not the host statement of what the rows show")
    seen = set()
    for origin in ("worker", "host_static"):
        if type(registry[origin]) is not list:
            _fail(origin, "must be a list of rows")
        for index, row in enumerate(registry[origin]):
            where = f"{origin}[{index}]"
            _row(row, origin, where)
            if row["id"] in seen:
                _fail(where + ".id", f"duplicate relation ID {row['id']}; every row ID must be unique")
            seen.add(row["id"])
    if registry["status"] == "not_collected" and registry["worker"]:
        _fail("worker", "a registry whose relations were not collected holds no worker rows")
    summary = registry["summary"]
    _keys(summary, _SUMMARY_KEYS, "summary")
    if summary != _summary(registry["worker"], registry["host_static"]):
        raise RelationError("summary: the counts disagree with the rows")
    return registry


def binding(registry):
    if registry is None:
        return None
    return {"file": FILE, "schema": registry["schema"], "registry_sha256": registry["registry_sha256"],
            "review_run_fingerprint": registry["review_run_fingerprint"], "status": registry["status"],
            "worker": len(registry["worker"]), "host_static": len(registry["host_static"])}
