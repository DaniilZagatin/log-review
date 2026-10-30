"""Host-owned catalog of distinct action candidates, built from validated code-review findings (plan A1).

build_catalog(review, snapshot, *, graph=None, inventory=None) -> catalog (schema action-catalog/1)
check_catalog(catalog, *, snapshot_id=None) -> catalog, or CatalogError for malformed or foreign data
candidate_areas(catalog) -> [{"area", "files", "candidate_ids", "signals"}] in deterministic order

The catalog is built from the complete validated finding/unknown lists BEFORE any semantic reduction, the
frozen snapshot, the static code graph and the static logging inventory; no model is called and nothing is
executed. An anchor is the innermost graph declaration that contains a finding ref (the ref itself when no
declaration contains it). Findings are grouped only when they anchor to the same path, symbol, line range and
raw span hash: distinct commands in different declarations stay distinct, and the several findings of one
declaration stay separate entries, so distinct effects of one command remain visible.

An anchor with at least one finding of a SIGNAL_KINDS kind is an action candidate; others are context
anchors (validations, branches, logging details) attached to candidates through same-owner declarations and
static calls. Finding kinds are proposals of the code model, so a candidate is a navigation signal only: the
catalog never decides that it is a business action, who initiates it or whether it is a user interaction.
Invalid, unsafe or foreign refs are listed explicitly, never silently dropped.
"""

from collections import Counter, defaultdict
import copy
import hashlib
from pathlib import PurePosixPath

from .code_review import canonical_json, sha256_text, stable_id
from .source_tools import blocked_path
from .tracing import safe_data

SCHEMA = "action-catalog/1"
SIGNAL_KINDS = ("entry_point", "state_change", "transaction_boundary", "outcome", "integration")
_NOT_DECLARATIONS = frozenset({"module", "window"})
_CANDIDATE_KEYS = frozenset({
    "id", "role", "path", "symbol", "start_line", "end_line", "span_sha256", "file_sha256", "owner", "area",
    "symbol_source", "graph_node_id", "signals", "finding_ids", "findings", "event_sites", "review_gaps",
    "navigation"})
_TOP_KEYS = frozenset({
    "schema", "snapshot_id", "review", "policy", "basis", "candidates", "context_anchors", "rejected_refs",
    "unanchored_unknowns", "unattached_sites", "accounting", "catalog_sha256"})
BASIS = ("Host catalog of code locations named by validated code-review findings. Finding kinds and descriptions "
         "are code-model proposals; a candidate is a navigation signal, not an established business action, "
         "initiator, user interaction or order. Static event sites are possible emit locations, not delivered "
         "records.")
POLICY = {
    "anchor": "innermost graph declaration containing the finding ref; the ref range itself when none does",
    "grouping": "same path, symbol, line range and raw span hash only; no name or text similarity",
    "candidate": "anchor with at least one finding of a signal kind",
    "signal_kinds": list(SIGNAL_KINDS),
    "context": "anchors without a signal finding; related to candidates by same owner or a resolved static call",
    "event_sites": "static logging sites whose lines lie inside the anchor range",
}


class CatalogError(ValueError):
    """Malformed, foreign or inconsistent catalog data; never repaired or reinterpreted as an older form."""


def _graph_data(graph, snapshot):
    if graph is None:
        return [], []
    data = graph.to_dict() if callable(getattr(graph, "to_dict", None)) else graph
    if type(data) is not dict:
        raise CatalogError("The code graph must be an object")
    if data.get("snapshot_id") not in (None, snapshot.snapshot_id):
        raise CatalogError("The code graph was built from a different snapshot")
    nodes = [node for node in data.get("nodes") or [] if type(node) is dict and type(node.get("id")) is str
             and node.get("path") in snapshot.files and node.get("kind") not in _NOT_DECLARATIONS
             and type(node.get("start_line")) is int and type(node.get("end_line")) is int
             and 1 <= node["start_line"] <= node["end_line"]]
    calls = [call for call in data.get("calls") or [] if type(call) is dict and call.get("path") in snapshot.files
             and type(call.get("start_line")) is int]
    return nodes, calls


def _raw_lines(source):
    raw = getattr(source, "_raw_text", None)
    return (raw if type(raw) is str else source.text).splitlines(keepends=True)


def _span_sha(source, first, last):
    """SHA-256 of the original frozen lines first..last, the same text RepoSnapshot.evidence hashes."""
    return hashlib.sha256("".join(_raw_lines(source)[first - 1:last]).encode()).hexdigest()


def _checked_ref(ref, snapshot):
    """(clean ref, None) or (None, stable rejection code); nothing outside the frozen, safe snapshot passes."""
    if type(ref) is not dict:
        return None, "ref_shape"
    path, first, last = ref.get("path"), ref.get("start_line"), ref.get("end_line")
    if type(path) is not str or path not in snapshot.files:
        return None, "path_outside_snapshot"
    if blocked_path(path):
        return None, "blocked_path"
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(_raw_lines(snapshot.files[path])):
        return None, "range_outside_file"
    symbol = ref.get("symbol")
    return {"path": path, "start_line": first, "end_line": last,
            "symbol": symbol if type(symbol) is str and symbol.strip() else "<module>"}, None


def _owner(symbol):
    return symbol.rsplit(".", 1)[0] if "." in symbol and not symbol.startswith("<") else None


def _area(path):
    parent = PurePosixPath(path).parent.as_posix()
    return "." if parent in ("", ".") else parent


def _site(site):
    first = site.get("start_line", site.get("line"))
    last = site.get("end_line", site.get("line"))
    identifier = site.get("id") if type(site.get("id")) is str else stable_id(
        "log", site.get("path"), first, last, site.get("event_key"))
    symbol = site.get("symbol")
    return {"id": identifier, "path": site.get("path"), "start_line": first, "end_line": last,
            "symbol": symbol if type(symbol) is str and symbol.strip() else "<module>",
            "event_key": site.get("event_key"), "key_status": site.get("key_status"), "kind": site.get("kind"),
            "fields": [name for name in site.get("fields") or [] if type(name) is str]}


def _finding_copy(item):
    return {key: copy.deepcopy(item[key]) for key in ("id", "kind", "description", "refs", "task_id", "task_kind")
            if key in item}


def build_catalog(review, snapshot, *, graph=None, inventory=None):
    """Deterministic catalog from the complete validated review result; never calls a model."""
    if type(review) is not dict or type(review.get("findings")) is not list or type(review.get("unknowns", [])) is not list:
        raise CatalogError("The review result needs findings and unknowns lists")
    if review.get("snapshot_id") not in (None, snapshot.snapshot_id):
        raise CatalogError("The review result belongs to a different snapshot")
    nodes, calls = _graph_data(graph, snapshot)
    decls = defaultdict(list)
    for node in nodes:
        decls[node["path"]].append(node)

    def innermost(path, first, last):
        containing = [node for node in decls.get(path, ()) if node["start_line"] <= first and last <= node["end_line"]]
        return max(containing, key=lambda node: (node["start_line"], -node["end_line"], node["id"]), default=None)

    anchors, rejected, unattached_findings = {}, [], []
    for finding in review["findings"]:
        if type(finding) is not dict or not {"id", "kind", "description", "refs"} <= set(finding):
            raise CatalogError("A finding needs id, kind, description and refs")
        placed = False
        for index, ref in enumerate(finding["refs"] if type(finding["refs"]) is list else []):
            clean, problem = _checked_ref(ref, snapshot)
            if problem is not None:
                rejected.append({"finding_id": finding["id"], "ref_index": index, "reason": problem})
                continue
            node = innermost(clean["path"], clean["start_line"], clean["end_line"])
            if node is not None:
                path, symbol, first, last = node["path"], node.get("qualname") or node["id"], node["start_line"], node["end_line"]
                source_kind, node_id = "graph_declaration", node["id"]
            else:
                path, symbol, first, last = clean["path"], clean["symbol"], clean["start_line"], clean["end_line"]
                source_kind, node_id = "finding_ref", None
            source = snapshot.files[path]
            span = _span_sha(source, first, last)
            key = (path, symbol, first, last, span)
            anchor = anchors.get(key)
            if anchor is None:
                anchor = anchors[key] = {
                    "id": stable_id("act", path, symbol, first, last, span), "path": path, "symbol": symbol,
                    "start_line": first, "end_line": last, "span_sha256": span, "file_sha256": source.sha256,
                    "owner": _owner(symbol), "area": _area(path), "symbol_source": source_kind,
                    "graph_node_id": node_id, "findings": [], "event_sites": [], "review_gaps": []}
            if all(item["id"] != finding["id"] for item in anchor["findings"]):
                anchor["findings"].append(_finding_copy(finding))
            placed = True
        if not placed:
            unattached_findings.append(finding["id"])
    # Deterministic order independent of the order in which reviews returned their findings.
    for anchor in anchors.values():
        anchor["findings"].sort(key=lambda item: (min(((ref.get("path", ""), ref.get("start_line", 0), ref.get("end_line", 0))
                                                       for ref in item["refs"] if type(ref) is dict),
                                                      default=("", 0, 0)), item["kind"], item["id"]))
    rejected.sort(key=lambda item: (item["finding_id"], item["ref_index"]))
    unattached_findings.sort()

    sites = [_site(site) for site in (inventory or {}).get("sites", []) if type(site) is dict]
    attached = set()
    for anchor in anchors.values():
        for site in sites:
            if (site["path"] == anchor["path"] and type(site["start_line"]) is int and type(site["end_line"]) is int
                    and anchor["start_line"] <= site["start_line"] and site["end_line"] <= anchor["end_line"]):
                anchor["event_sites"].append(dict(site))
                attached.add(site["id"])

    unanchored = []
    for item in review.get("unknowns", []):
        where = item.get("range") if type(item) is dict else None
        clean = _checked_ref(where, snapshot)[0] if type(where) is dict else None
        hits = [anchor for anchor in anchors.values() if clean is not None and anchor["path"] == clean["path"]
                and anchor["start_line"] <= clean["end_line"] and clean["start_line"] <= anchor["end_line"]]
        for anchor in hits:
            anchor["review_gaps"].append({"unknown_id": item.get("id"), "model_status": item.get("model_status"),
                                          "description": item.get("description")})
        if not hits:
            unanchored.append({"unknown_id": item.get("id") if type(item) is dict else None,
                               "origin": item.get("origin") if type(item) is dict else "text",
                               "interval_ids": list(item.get("interval_ids", [])) if type(item) is dict else []})

    ordered = sorted(anchors.values(), key=lambda item: (item["path"], item["start_line"], item["end_line"], item["symbol"]))
    by_node = {item["graph_node_id"]: item for item in ordered if item["graph_node_id"] is not None}
    calls_by_target, calls_by_path = defaultdict(list), defaultdict(list)
    for call in calls:
        calls_by_path[call["path"]].append(call)
        if type(call.get("target_id")) is str:
            calls_by_target[call["target_id"]].append(call)
    node_names = {node["id"]: node.get("qualname") or node["id"] for node in nodes}
    owners = defaultdict(list)
    for anchor in ordered:
        if anchor["owner"] is not None:
            owners[(anchor["path"], anchor["owner"])].append(anchor["id"])
    for anchor in ordered:
        kinds = Counter(item["kind"] for item in anchor["findings"])
        anchor["role"] = "candidate" if any(kinds[kind] for kind in SIGNAL_KINDS) else "context"
        anchor["signals"] = dict(sorted(kinds.items()))
        anchor["finding_ids"] = [item["id"] for item in anchor["findings"]]
        callers = calls_by_target.get(anchor["graph_node_id"], []) if anchor["graph_node_id"] else []
        callee_ids = []
        for call in calls_by_path.get(anchor["path"], ()):
            target = by_node.get(call.get("target_id"))
            if (anchor["start_line"] <= call["start_line"] <= anchor["end_line"] and target is not None
                    and target["id"] != anchor["id"] and target["id"] not in callee_ids):
                callee_ids.append(target["id"])
        siblings = [identifier for identifier in owners.get((anchor["path"], anchor["owner"]), ())
                    if identifier != anchor["id"]]
        anchor["navigation"] = {
            "callers_in_snapshot": len(callers),
            "caller_symbols": sorted({node_names.get(call.get("owner_id"), call.get("owner_id") or "<unknown>")
                                      for call in callers}),
            "callee_anchor_ids": callee_ids, "same_owner_anchor_ids": siblings,
            "basis": "static graph navigation; not proof of execution, reachability or invocation by a user"}
    candidates = [{key: anchor[key] for key in sorted(_CANDIDATE_KEYS)} for anchor in ordered if anchor["role"] == "candidate"]
    context = [{key: anchor[key] for key in sorted(_CANDIDATE_KEYS)} for anchor in ordered if anchor["role"] == "context"]
    in_candidates = {item for anchor in candidates for item in anchor["finding_ids"]}
    in_context = {item for anchor in context for item in anchor["finding_ids"]}
    catalog = {
        "schema": SCHEMA, "snapshot_id": snapshot.snapshot_id,
        "review": {"run_fingerprint": review.get("run_fingerprint"), "complete": review.get("complete"),
                   "coverage_complete": review.get("coverage_complete"),
                   "incomplete_reasons": list(review.get("incomplete_reasons") or []),
                   "failed_tasks": len(review.get("errors") or [])},
        "policy": POLICY, "basis": BASIS, "candidates": candidates, "context_anchors": context,
        "rejected_refs": rejected, "unanchored_unknowns": unanchored,
        "unattached_sites": [site["id"] for site in sites if site["id"] not in attached],
        "accounting": {
            "findings": len(review["findings"]), "findings_in_candidates": len(in_candidates),
            "findings_only_in_context": len(in_context - in_candidates),
            "findings_without_valid_ref": len(unattached_findings),
            "findings_without_valid_ref_ids": unattached_findings, "rejected_refs": len(rejected),
            "unknowns": len(review.get("unknowns", [])), "unknowns_attached_as_review_gaps":
                len(review.get("unknowns", [])) - len(unanchored),
            "unanchored_unknowns": len(unanchored), "anchors": len(ordered), "candidates": len(candidates),
            "context_anchors": len(context), "sites": len(sites), "sites_attached": len(attached),
            "signals": dict(sorted(Counter(kind for anchor in candidates for kind in anchor["signals"]
                                           if kind in SIGNAL_KINDS).items())),
            "basis": "Counts show what the catalog holds, not that any candidate is a business action."},
    }
    # Redact before hashing, so the saved (redacted) file verifies against its own catalog_sha256.
    catalog = safe_data(catalog)
    catalog["catalog_sha256"] = _digest(catalog)
    return catalog


def _digest(catalog):
    return sha256_text(canonical_json({key: value for key, value in catalog.items() if key != "catalog_sha256"}))


def check_catalog(catalog, *, snapshot_id=None):
    """Validate an in-memory or loaded catalog exactly; foreign, altered or older shapes are refused."""
    if type(catalog) is not dict or set(catalog) != _TOP_KEYS or catalog["schema"] != SCHEMA:
        raise CatalogError("Not an action-catalog/1 document")
    if catalog["catalog_sha256"] != _digest(catalog):
        raise CatalogError("The action catalog content does not match its catalog_sha256")
    if snapshot_id is not None and catalog["snapshot_id"] != snapshot_id:
        raise CatalogError("The action catalog belongs to a different source snapshot")
    seen = set()
    for collection, role in (("candidates", "candidate"), ("context_anchors", "context")):
        if type(catalog[collection]) is not list:
            raise CatalogError(f"{collection} must be a list")
        for item in catalog[collection]:
            if type(item) is not dict or set(item) != _CANDIDATE_KEYS or item["role"] != role or item["id"] in seen:
                raise CatalogError(f"Invalid or duplicate {role} anchor")
            if (type(item["start_line"]) is not int or type(item["end_line"]) is not int
                    or not 1 <= item["start_line"] <= item["end_line"] or type(item["findings"]) is not list):
                raise CatalogError("Invalid anchor range or findings")
            if item["id"] != stable_id("act", item["path"], item["symbol"], item["start_line"], item["end_line"],
                                       item["span_sha256"]):
                raise CatalogError("An anchor ID does not match its path, symbol, range and span hash")
            seen.add(item["id"])
    return catalog


def anchors_by_id(catalog):
    return {item["id"]: item for item in (*catalog["candidates"], *catalog["context_anchors"])}


def candidate_areas(catalog):
    """Candidates grouped by parent directory, in path order; every candidate belongs to exactly one area."""
    groups = {}
    for item in catalog["candidates"]:
        group = groups.setdefault(item["area"], {"area": item["area"], "files": set(), "candidate_ids": [],
                                                 "signals": Counter()})
        group["files"].add(item["path"])
        group["candidate_ids"].append(item["id"])
        group["signals"].update({kind: count for kind, count in item["signals"].items() if kind in SIGNAL_KINDS})
    return [{"area": group["area"], "files": sorted(group["files"]), "candidate_ids": group["candidate_ids"],
             "signals": dict(sorted(group["signals"].items()))} for _, group in sorted(groups.items())]


def summary(catalog, file_name="action_catalog.json"):
    """Bundle view: identity and accounting; the full catalog stays on disk."""
    return {"file": file_name, "schema": catalog["schema"], "catalog_sha256": catalog["catalog_sha256"],
            "snapshot_id": catalog["snapshot_id"], "review": copy.deepcopy(catalog["review"]),
            "accounting": copy.deepcopy(catalog["accounting"])}
