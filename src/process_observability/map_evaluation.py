"""Offline comparison of a saved map with an oracle declared as human-authored (R08); no model, network or runtime.

evaluate_map(bundle, oracle) -> result         compares one saved bundle (process_map + evidence) with an oracle
evaluation_is_current(result, bundle, oracle)  whether a result still belongs to exactly this map, evidence, oracle
evaluate_run(run, oracle_path, out)            validates a saved run directory and writes a new JSON result

Matching is a source-range overlap between the oracle's anchors and the map's cited code ranges. Every status is a
navigation candidate for a human reviewer, never semantic correctness: candidate_match (exactly one map operation,
which no other expected operation shares), ambiguous (several map operations overlap one expected operation),
collapsed (one map operation overlaps several expected operations: a possible merge) and missing (no overlap).
Map operations outside every anchor are listed as unlabelled without a verdict. Conclusions hold only for the
labelled scope of the oracle; operation counts and names are never compared.

authored_by="human" is a declaration of the oracle file, not verified provenance. An equal source snapshot ID means
the saved map and the oracle name the same frozen snapshot; this comparison never rereads source files. A result is
bound to SHA-256 digests of the saved map, its evidence registry, its atomic operation evidence and the oracle, so a
rename, a semantic edit or a moved citation makes it stale.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = "operation-evaluation/1"
ORACLE_SCHEMA = "operation-oracle/1"
STATUSES = ("candidate_match", "ambiguous", "collapsed", "missing")
MAX_ORACLE_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_ORACLE_KEYS = {"schema", "id", "authored_by", "source_snapshot_id", "scope", "operations", "notes"}
_EXPECTED_KEYS = {"id", "label", "anchors", "notes"}
_ANCHOR_KEYS = {"path", "start_line", "end_line"}
LIMITATIONS = [
    "Совпадение определяется только пересечением диапазонов строк: это кандидат для проверки человеком, а не "
    "доказательство смысловой правильности операции.",
    "Выводы действуют только для размеченной области эталона; операции вне неё перечислены без оценки.",
    "Число операций и совпадение названий не сравниваются и не означают полноты.",
    "Признак authored_by=human — заявление файла эталона; его происхождение этой проверкой не подтверждено.",
    "Совпадение идентификатора снимка означает, что карта и эталон ссылаются на один сохранённый снимок; файлы "
    "исходников при сравнении повторно не читались.",
    "Результат действителен только для тех же хешей карты, доказательств и эталона.",
]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _text(value, label):
    if type(value) is not str or not value.strip():
        raise ValueError(f"oracle {label} must be nonblank text")
    return value


def check_oracle(oracle):
    """The oracle exactly as declared, or ValueError: strict keys, unique IDs, relative paths and line ranges."""
    if type(oracle) is not dict or set(oracle) - _ORACLE_KEYS or _ORACLE_KEYS - {"notes"} - set(oracle):
        raise ValueError("oracle needs exactly schema, id, authored_by, source_snapshot_id, scope, operations "
                         "(and optional notes)")
    if oracle["schema"] != ORACLE_SCHEMA:
        raise ValueError(f"oracle schema must be {ORACLE_SCHEMA}")
    if oracle["authored_by"] != "human":
        raise ValueError('oracle authored_by must declare "human"; model or tool output is not an oracle')
    _text(oracle["id"], "id")
    _text(oracle["scope"], "scope")
    if type(oracle["source_snapshot_id"]) is not str or not _HASH.fullmatch(oracle["source_snapshot_id"]):
        raise ValueError("oracle source_snapshot_id must be a 64-character lowercase SHA-256")
    if "notes" in oracle:
        _text(oracle["notes"], "notes")
    expected = oracle["operations"]
    if type(expected) is not list or not expected:
        raise ValueError("oracle operations must be a nonempty list")
    seen = set()
    for item in expected:
        if type(item) is not dict or set(item) - _EXPECTED_KEYS or {"id", "label", "anchors"} - set(item):
            raise ValueError("each expected operation needs exactly id, label, anchors (and optional notes)")
        identifier = _text(item["id"], "operation id")
        if identifier in seen:
            raise ValueError("oracle operation IDs repeat")
        seen.add(identifier)
        _text(item["label"], "operation label")
        if "notes" in item:
            _text(item["notes"], "operation notes")
        if type(item["anchors"]) is not list or not item["anchors"]:
            raise ValueError("each expected operation needs at least one source anchor")
        for anchor in item["anchors"]:
            if type(anchor) is not dict or set(anchor) != _ANCHOR_KEYS:
                raise ValueError("an oracle anchor needs exactly path, start_line, end_line")
            path = _text(anchor["path"], "anchor path")
            parts = PurePosixPath(path).parts
            if PurePosixPath(path).is_absolute() or ".." in parts or "\\" in path or ":" in path:
                raise ValueError("an oracle anchor path must be relative to the source snapshot")
            first, last = anchor["start_line"], anchor["end_line"]
            if type(first) is not int or type(last) is not int or not 1 <= first <= last:
                raise ValueError("an oracle anchor needs integer lines with 1 <= start_line <= end_line")
    return oracle


def _saved(bundle):
    if type(bundle) is not dict or type(bundle.get("process_map")) is not dict or type(bundle.get("evidence")) is not list:
        raise ValueError("a saved bundle with process_map and evidence is required")
    operations = bundle["process_map"].get("operations")
    if type(operations) is not list or any(type(op) is not dict or type(op.get("id")) is not str for op in operations):
        raise ValueError("the saved process map has malformed operations")
    return operations


def _anchor_evidence(bundle):
    """Which evidence anchors each operation: its own model refs when an atomic record names them, else all of it."""
    atomic = bundle.get("atomic_map") if type(bundle.get("atomic_map")) is dict else {}
    record = {item.get("id"): item for item in atomic.get("operations", []) if type(item) is dict}
    chosen = {}
    for op in bundle["process_map"]["operations"]:
        own = record.get(op["id"], {}).get("model_evidence_ids")
        if type(own) is list and own:
            chosen[op["id"]] = ("model_refs", [key for key in own if type(key) is str])
        else:
            chosen[op["id"]] = ("operation_evidence", [key for key in op.get("evidence_ids", []) if type(key) is str])
    return chosen


def map_digest(bundle):
    """SHA-256 of everything the comparison reads: the process map, the evidence registry and anchor choices."""
    _saved(bundle)
    return _digest({"process_map": bundle["process_map"], "evidence": bundle["evidence"],
                    "anchor_evidence": _anchor_evidence(bundle)})


def _snapshot(bundle):
    found = {item.get("snapshot_id") for item in bundle["evidence"]
             if type(item) is dict and item.get("kind") == "code"}
    return next(iter(found)) if len(found) == 1 and type(next(iter(found))) is str else None


def evaluate_map(bundle, oracle):
    oracle = check_oracle(oracle)
    operations = _saved(bundle)
    snapshot = _snapshot(bundle)
    result = {"schema": SCHEMA, "oracle_id": oracle["id"], "oracle_provenance": "declared_human_not_verified",
              "scope": oracle["scope"], "source_snapshot_id": snapshot,
              "oracle_source_snapshot_id": oracle["source_snapshot_id"],
              "source_snapshot_verified": snapshot is not None and snapshot == oracle["source_snapshot_id"],
              "source_snapshot_check": "saved_snapshot_id_equal_files_not_reread",
              "semantic_verification": False, "map_sha256": map_digest(bundle), "oracle_sha256": _digest(oracle),
              "expected_operations": len(oracle["operations"]), "results": [],
              "counts": dict.fromkeys(STATUSES, 0), "unlabelled_operation_ids": [], "limitations": list(LIMITATIONS)}
    if not result["source_snapshot_verified"]:
        result["status"] = "not_evaluated_snapshot_mismatch"
        return result
    registry = {item.get("id"): item for item in bundle["evidence"] if type(item) is dict}
    chosen = _anchor_evidence(bundle)
    ranges = {}
    for op in operations:
        spans = []
        for key in chosen[op["id"]][1]:
            anchor = (registry.get(key) or {}).get("source_anchor")
            if type(anchor) is dict and type(anchor.get("path")) is str and all(
                    type(anchor.get(name)) is int for name in ("start_line", "end_line")):
                spans.append((anchor["path"], anchor["start_line"], anchor["end_line"]))
        ranges[op["id"]] = spans

    def overlapping(anchors):
        return [op["id"] for op in operations if any(
            path == anchor["path"] and first <= anchor["end_line"] and anchor["start_line"] <= last
            for path, first, last in ranges[op["id"]] for anchor in anchors)]
    candidates = {item["id"]: overlapping(item["anchors"]) for item in oracle["operations"]}
    shared = {}
    for expected, found in candidates.items():
        for identifier in found:
            shared.setdefault(identifier, []).append(expected)
    for item in oracle["operations"]:
        found = candidates[item["id"]]
        status = ("missing" if not found else "ambiguous" if len(found) > 1
                  else "collapsed" if len(shared[found[0]]) > 1 else "candidate_match")
        result["counts"][status] += 1
        result["results"].append({"expected_id": item["id"], "label": item["label"], "status": status,
                                  "operation_ids": found,
                                  "anchor_basis": sorted({chosen[identifier][0] for identifier in found})})
    result["unlabelled_operation_ids"] = [op["id"] for op in operations if op["id"] not in shared]
    result["status"] = "evaluated_labelled_scope_only"
    return result


def evaluation_is_current(result, bundle, oracle):
    """True only when the result was computed from exactly this map, evidence and oracle."""
    try:
        oracle = check_oracle(oracle)
        current = (map_digest(bundle), _digest(oracle), _snapshot(bundle))
    except ValueError:
        return False
    return (type(result) is dict and result.get("schema") == SCHEMA
            and (result.get("map_sha256"), result.get("oracle_sha256"), result.get("source_snapshot_id")) == current)


def evaluate_run(run, oracle_path, out):
    """Validate a saved run (as export-onboarding does), compare it and write a new JSON file; never overwrite."""
    from .contracts import strict_json_loads
    from .onboarding_export import _run_directory, load_run
    directory = _run_directory(run)
    target = Path(out).expanduser().absolute()
    if any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError("Symlink evaluation output path refused")
    if target.name in {"", ".", ".."} or not target.parent.is_dir():
        raise ValueError("--out must name a new file in an existing directory")
    target = target.parent.resolve() / target.name
    if target == directory or directory in target.parents:
        raise ValueError("--out must lie outside the saved run directory")
    source = Path(oracle_path).expanduser().absolute()
    if any(path.is_symlink() for path in (source, *source.parents)):
        raise ValueError("Symlink oracle input path refused")
    if not source.is_file() or source.stat().st_size > MAX_ORACLE_BYTES:
        raise ValueError("--oracle must be a regular JSON file of at most 8 MiB")
    oracle = strict_json_loads(source.read_text(encoding="utf-8"))
    view = load_run(directory)
    result = evaluate_map(view["bundle"], oracle)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with target.open("x", encoding="utf-8") as handle:  # an existing result is never overwritten
        handle.write(payload)
    return result
