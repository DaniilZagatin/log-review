"""Deterministic whole-scope code review planning: no model calls, no filesystem reads.

plan_review assigns every redacted frozen line to exactly one interval and packs
planned intervals into requests whose exact size is len(system) + len(user). A plan
is accounting only. It records which text a task would supply, never that the text
was reviewed or understood. Graph rows are navigation hints, not execution,
reachability or dispatch evidence, and equal literals are never joined.

Request representation (shared with execution):
- system: the caller's system prompt, prefixed once with the line "TASK: code".
- user: canonical_json(brief), where brief has exactly the keys kind, instructions,
  owned_intervals, navigation, reply_schema, finding_kinds, interval_statuses,
  request_types and limits.
- owned_intervals items: id, path, start_line, end_line, symbol, text ("N: line"
  joined by newlines) and, for pieces of an oversized declaration, segment
  {parent_symbol_id, parent_qualname, parent_kind, index, count}.
- navigation: {rows, omitted_rows, omitted_by_type}; each row has a type "t" of
  symbol, call, log, literal or imports.
"""

import ast
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import posixpath

from .log_discovery import _ODD_LINES, discover_logging
from .source_tools import MAX_CITATION_LINES, SourceFile

TOOL = "code_review_plan/1"
TASK_LINE = "TASK: code"
MIN_SOURCE_BUDGET = 4000
MAX_INTERVALS_PER_TASK = 12
NAV_METADATA_RESERVE = 512
MAX_REF_LINES = 80
MAX_REFS_PER_FINDING = 8
MAX_DESCRIPTION_CHARS = 1200
MAX_NOTE_CHARS = 600
MAX_UNKNOWNS = 20
MAX_LABEL_CHARS = 300
MAX_CALLERS = 5
MAX_IMPORTS = 20
FINDING_KINDS = ("entry_point", "branch", "outcome", "error", "state_change", "transaction_boundary",
                 "logging", "wrapper", "identifier", "integration", "configuration", "other")
INTERVAL_STATUSES = ("reviewed", "partial", "not_understood")
REQUEST_TYPES = ("symbol", "span", "literal")
REPLY_SCHEMA = {
    "findings": [{"id": "string, unique in this reply", "kind": "one of finding_kinds", "description": "string",
                  "refs": [{"path": "string", "start_line": "integer", "end_line": "integer", "symbol": "string"}]}],
    "interval_status": [{"interval_id": "owned interval id", "status": "reviewed|partial|not_understood",
                         "note": "string"}],
    "requests": [{"type": "symbol|span|literal", "symbol_id": "symbol: an id shown in this brief",
                  "path": "span: snapshot path", "start_line": "span: integer", "end_line": "span: integer",
                  "literal": "literal: text shown in this brief", "reason": "string",
                  "finding_ids": ["finding id from this reply"]}],
    "unknowns": ["string"]}
INSTRUCTIONS = (
    "Review only the text in owned_intervals. Each line reads 'N: text' with its original line number; no other source lines were supplied.",
    "Reply with one JSON object with exactly the reply_schema keys. Omit request fields that do not apply to the request type.",
    "Give one interval_status for every owned interval: reviewed, partial or not_understood, with a short note.",
    "Cite refs with path, start_line, end_line and symbol. Each ref spans at most max_ref_lines lines and lies inside one owned interval or is covered without a missing line by touching or overlapping supplied intervals of the same path.",
    "Report what the text shows: entry points, branches, outcomes, errors, state changes, transaction boundaries, logging, wrappers, identifiers, integrations and configuration.",
    "Navigation rows are static hints. A call row does not prove execution, reachability or runtime dispatch, and equal literals are not connections.",
    "Use requests for text you need: symbol takes a symbol_id shown in this brief, span takes a snapshot path and at most max_read_lines lines, literal takes a literal shown in this brief. Link each request to finding_ids. Requested text is answered once, in a separate task.",
    "Do not claim verification or completeness; the host checks cited ranges against the frozen text only. Record what you could not determine in unknowns.",
)
# Opt-in relation collection (ReviewOptions.relations). Off keeps the original reply contract, request bytes and
# fingerprints exactly; on is a new technical identity (other prompt, brief and binding), never compatible with
# requests that did not ask for relations.
RELATION_KINDS = ("call", "dispatch", "callback", "branch", "return", "data_handoff", "control_handoff")
RELATION_RESOLUTIONS = ("resolved", "unresolved", "external")
MAX_RELATIONS_PER_TASK = 40
RELATION_SCHEMA = {
    "id": "string, unique in this reply", "kind": "one of relation_kinds",
    "source": {"path": "string", "start_line": "integer", "end_line": "integer", "symbol": "string"},
    "target": {"symbol_id": "a symbol id shown in this brief, or null",
               "ref": "a range inside owned_intervals {path, start_line, end_line, symbol}, or null",
               "label": "what is called, sent or returned to, as written in the text, or null"},
    "resolution": "resolved|unresolved|external", "condition": "string or null", "data": "string or null",
    "refs": [{"path": "string", "start_line": "integer", "end_line": "integer", "symbol": "string"}]}
RELATION_INSTRUCTIONS = (
    "Also report relations the supplied text itself shows: calls, dispatch through a registry or mapping, callbacks, "
    "branches, returns and data or control handed to other code. source cites the lines where the relation is "
    "written; refs cite the lines that show it (the call, the registration, the condition).",
    "resolution resolved needs target.symbol_id shown in this brief or a target.ref inside the supplied text; "
    "unresolved names the target in target.label only; external names an outside system, endpoint, queue or topic in "
    "target.label. A navigation row is not a relation, and a target that is not supplied says nothing about the "
    "receiver's behaviour.",
    "condition and data only when the text shows them; otherwise null. Do not report every utility call; report "
    "relations that move control or data between process steps.",
)
_MERGEABLE = frozenset({"module", "declaration", "header", "body", "group"})
_IMPORT_FIELDS = ("alias", "level", "line", "module", "name", "start_line", "status", "target_path")
_EMPTY_NAVIGATION = {"rows": [], "omitted_rows": 0, "omitted_by_type": {}}


class ReviewPlanError(ValueError):
    """Invalid review configuration or a graph/snapshot mismatch, found before any model call."""


def _integer(name, value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ReviewPlanError(f"{name} must be an integer from {low} to {high}")


def _share(name, value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 0.5:
        raise ReviewPlanError(f"{name} must be a number from 0 to 0.5")


@dataclass(frozen=True)
class ReviewOptions:
    request_chars: int = 32000
    workers: int = 2
    max_followups: int = 32
    max_requests_per_task: int = 6
    max_findings_per_task: int = 40
    repair_attempts: int = 1
    index_share: float = 0.2
    parent_share: float = 0.25
    relations: bool = False  # opt-in relation contract; serialized only when on (see identity)

    def __post_init__(self):
        _integer("request_chars", self.request_chars, MIN_SOURCE_BUDGET, 4_000_000)
        _integer("workers", self.workers, 1, 8)
        _integer("max_followups", self.max_followups, 0, 10_000)
        _integer("max_requests_per_task", self.max_requests_per_task, 0, 50)
        _integer("max_findings_per_task", self.max_findings_per_task, 1, 200)
        _integer("repair_attempts", self.repair_attempts, 0, 1)
        _share("index_share", self.index_share)
        _share("parent_share", self.parent_share)
        if type(self.relations) is not bool:
            raise ReviewPlanError("relations must be boolean")

    def to_dict(self):
        values = asdict(self)
        if not self.relations:  # inert default: the original options record stays byte-identical
            del values["relations"]
        return values

    def identity(self):
        """Options that change requests or results; the worker count only changes scheduling."""
        values = self.to_dict()
        del values["workers"]
        return values


def canonical_json(value):
    """The one encoding for briefs, identities and hashes: compact, sorted keys, non-ASCII kept."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(prefix, *parts):
    return f"{prefix}-{sha256_text(canonical_json(list(parts)))[:16]}"


def request_sha256(system, user):
    """Identity of the exact request pair sent to a model."""
    return sha256_text(canonical_json({"system": system, "user": user}))


def task_system(system):
    """Prefix the caller's system prompt with the TASK line once; the text is otherwise kept."""
    if type(system) is not str or not system.strip():
        raise ReviewPlanError("A nonblank system prompt is required")
    first = system.split("\n", 1)[0]
    if first == TASK_LINE:
        return system
    if first.strip().startswith("TASK:"):
        raise ReviewPlanError("The system prompt names a different task")
    return TASK_LINE + "\n" + system


def numbered_text(source, start_line, end_line):
    """Redacted frozen lines start..end in the 'N: line' form that to_context uses."""
    lines = source.text.splitlines()
    if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line <= len(lines):
        raise ReviewPlanError("Invalid source line range")
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start_line, end_line + 1))


def limits(options):
    values = {"max_findings": options.max_findings_per_task, "max_requests": options.max_requests_per_task,
              "max_ref_lines": MAX_CITATION_LINES, "max_read_lines": MAX_REF_LINES,
              "max_refs_per_finding": MAX_REFS_PER_FINDING, "max_description_chars": MAX_DESCRIPTION_CHARS,
              "max_note_chars": MAX_NOTE_CHARS, "max_unknowns": MAX_UNKNOWNS}
    if options.relations:
        values["max_relations"] = MAX_RELATIONS_PER_TASK
    return values


def reply_keys(options):
    """Top-level reply arrays the brief asks for: the original four, plus relations when collected."""
    return frozenset(REPLY_SCHEMA) | ({"relations"} if options.relations else set())


def reply_contract(options):
    contract = {"reply_schema": REPLY_SCHEMA, "finding_kinds": list(FINDING_KINDS),
                "interval_statuses": list(INTERVAL_STATUSES), "request_types": list(REQUEST_TYPES),
                "limits": limits(options)}
    if options.relations:
        contract.update(reply_schema={**REPLY_SCHEMA, "relations": [RELATION_SCHEMA]},
                        relation_kinds=list(RELATION_KINDS), relation_resolutions=list(RELATION_RESOLUTIONS))
    return contract


def instructions(base, options):
    """The brief's instructions; relation collection appends its own lines, the original text stays first."""
    return [*base, *(RELATION_INSTRUCTIONS if options.relations else ())]


def _brief(kind, items, navigation, options):
    return {"kind": kind, "instructions": instructions(INSTRUCTIONS, options), "owned_intervals": items,
            "navigation": navigation, **reply_contract(options)}


def _records(value):
    if isinstance(value, dict):
        return [value[key] for key in sorted(value, key=str)]
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(record, *names):
    for name in names:
        if type(record.get(name)) is str:
            return record[name]
    return None


def _line(record):
    value = record.get("start_line", record.get("line"))
    return value if type(value) is int else None


class _Graph:
    """Indices over graph.to_dict(); nodes and calls that contradict the snapshot are errors."""

    def __init__(self, graph, snapshot, counts):
        self.sha256, self.nodes, self.extractors, self.ignored = None, {}, {}, Counter()
        self.decls, self.calls, self.callers = defaultdict(list), defaultdict(list), defaultdict(list)
        self.imports, self.literals, self.literal_totals = defaultdict(list), defaultdict(list), Counter()
        if graph is None:
            return
        digest = getattr(graph, "sha256", None)
        digest = digest() if callable(digest) else digest
        if type(digest) is not str or not digest.strip():
            raise ReviewPlanError("The graph must expose its sha256 digest")
        data = graph.to_dict()
        if type(data) is not dict:
            raise ReviewPlanError("Graph to_dict() must return an object")
        if data.get("snapshot_id") not in (None, snapshot.snapshot_id):
            raise ReviewPlanError("The graph was built from a different snapshot")
        self.sha256 = digest
        self._files(data.get("files"), snapshot)
        for record in _records(data.get("nodes")):
            self._node(record, counts)
        for record in _records(data.get("calls")):
            self._call(record, counts)
        for name, store in (("import", self.imports), ("literal", self.literals)):
            for record in _records(data.get(name + "s")):
                path = record.get("path") if type(record) is dict else None
                line = _line(record) if type(record) is dict else None
                if (type(path) is not str or path not in counts or line is None or not 1 <= line <= counts[path]
                        or (name == "literal" and _text(record, "text", "literal", "value") is None)):
                    self.ignored[name] += 1
                else:
                    store[path].append(record)
        for nodes in self.decls.values():
            nodes.sort(key=lambda node: (node["start_line"], -node["end_line"], node["id"]))
        for calls in self.calls.values():
            calls.sort(key=lambda call: (call["start_line"], call.get("column") if type(call.get("column")) is int else 0,
                                         str(call.get("id"))))
        for records in self.literals.values():
            records.sort(key=_line)
            for record in records:
                self.literal_totals[_text(record, "text", "literal", "value")] += 1

    def _files(self, files, snapshot):
        pairs = (sorted(files.items(), key=lambda pair: str(pair[0])) if isinstance(files, dict)
                 else [(None, item) for item in _records(files)])
        for key, record in pairs:
            if type(record) is not dict:
                continue
            path = record.get("path", key)
            if type(path) is not str or path not in snapshot.files:
                continue
            if type(record.get("sha256")) is str and record["sha256"] != snapshot.files[path].sha256:
                raise ReviewPlanError(f"Graph file hash differs from the snapshot: {path}")
            if type(record.get("extractor")) is str:
                self.extractors[path] = record["extractor"]

    def _node(self, record, counts):
        if (type(record) is not dict or type(record.get("id")) is not str or not record["id"].strip()
                or type(record.get("path")) is not str):
            self.ignored["node"] += 1
            return
        path, first, last = record["path"], record.get("start_line"), record.get("end_line")
        kind = _text(record, "kind") or "unknown"
        if path not in counts:
            raise ReviewPlanError(f"Graph node is outside the snapshot: {path}")
        if kind != "module" and (type(first) is not int or type(last) is not int
                                 or not 1 <= first <= last <= counts[path]):
            raise ReviewPlanError(f"Graph node lines do not match the snapshot: {path}")
        if record["id"] in self.nodes:
            raise ReviewPlanError(f"Duplicate graph node ID: {record['id']}")
        qualname = _text(record, "qualname", "name") or record["id"]
        node = {"id": record["id"], "path": path, "qualname": qualname,
                "name": _text(record, "name") or qualname.rsplit(".", 1)[-1], "kind": kind,
                "start_line": first, "end_line": last}
        self.nodes[node["id"]] = node
        if kind != "module":
            self.decls[path].append(node)

    def _call(self, record, counts):
        if type(record) is not dict or type(record.get("path")) is not str or type(record.get("start_line")) is not int:
            self.ignored["call"] += 1
            return
        path = record["path"]
        if path not in counts or not 1 <= record["start_line"] <= counts[path]:
            raise ReviewPlanError(f"Graph call site does not match the snapshot: {path}")
        self.calls[path].append(record)
        if type(record.get("target_id")) is str:
            self.callers[record["target_id"]].append(record)


def _forest(nodes, ignored):
    """Nest declarations by line containment; partially overlapping nodes are ignored and counted."""
    roots, children, stack = [], {}, []
    for node in nodes:
        while stack and stack[-1]["end_line"] < node["start_line"]:
            stack.pop()
        if stack and node["end_line"] > stack[-1]["end_line"]:
            ignored["overlapping_node"] += 1
            continue
        (children[stack[-1]["id"]] if stack else roots).append(node)
        children[node["id"]] = []
        stack.append(node)
    return roots, children


def _statement_starts(path, text):
    """Python statement and handler starts, decorators included, as split points; parsing only."""
    if not path.endswith(".py") or _ODD_LINES.search(text):
        return set()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    return {min([node.lineno, *(item.lineno for item in getattr(node, "decorator_list", ()))])
            for node in ast.walk(tree) if isinstance(node, (ast.stmt, ast.ExceptHandler))}


def _item_overhead(path, count, decls):
    """Upper bound on an owned-interval item without its text, over this file's labels."""
    def widest(values):
        return max(values, key=lambda value: len(canonical_json(value)))
    label = widest(["<module>", *(node["qualname"] for node in decls)])
    segment = {"parent_symbol_id": widest(["", *(node["id"] for node in decls)]), "parent_qualname": label,
               "parent_kind": widest(["", *(node["kind"] for node in decls)]), "index": count, "count": count}
    item = {"id": stable_id("iv"), "path": path, "start_line": count, "end_line": count, "symbol": label,
            "segment": segment, "text": ""}
    return len(canonical_json(item)) - 2


class _FileUnits:
    """Readable boundaries for one file: declarations, then statements, blank lines and single lines."""

    def __init__(self, path, text, lines, decls, budget, ignored):
        self.budget, self.units = budget, []
        self.prefix = [0]
        for number, line in enumerate(lines, 1):
            self.prefix.append(self.prefix[-1] + len(json.dumps(f"{number}: {line}", ensure_ascii=False)) - 2)
        self.roots, self.children = _forest(decls, ignored)
        self.cuts = sorted(_statement_starts(path, text) | {node["start_line"] for node in decls}
                           | {node["end_line"] + 1 for node in decls})
        self.blank_cuts = [number + 1 for number, line in enumerate(lines, 1) if not line.strip()]
        self.overhead = _item_overhead(path, len(lines), decls)
        self.count = len(lines)

    def fits(self, first, last):
        # JSON text: escaped lines, two chars per newline escape and the two quotes.
        return self.overhead + self.prefix[last] - self.prefix[first - 1] + 2 * (last - first + 1) <= self.budget

    def build(self):
        self._span(1, self.count, self.roots, None)
        merged = []
        for unit in self.units:
            previous = merged[-1] if merged else None
            if (previous is not None and unit["parent"] is previous["parent"]
                    and {unit["kind"], previous["kind"]} <= _MERGEABLE
                    and self.fits(previous["start_line"], unit["end_line"])):
                merged[-1] = dict(previous, end_line=unit["end_line"], kind="group", node=None)
            else:
                merged.append(unit)
        pieces = defaultdict(list)
        for unit in merged:
            if unit["parent"] is not None:
                pieces[unit["parent"]["id"]].append(unit)
        for parent_id, members in pieces.items():
            for index, unit in enumerate(members, 1):
                unit["segment"] = {"parent_symbol_id": parent_id, "index": index, "count": len(members)}
        return merged

    def _unit(self, first, last, kind, parent, node=None, state="planned"):
        return {"start_line": first, "end_line": last, "kind": kind, "parent": parent, "node": node,
                "state": state, "segment": None}

    def _span(self, first, last, nodes, parent):
        cursor, kind = first, "header" if parent else "module"
        for node in nodes:
            if node["start_line"] > cursor:
                self._gap(cursor, node["start_line"] - 1, parent, kind)
            self._declaration(node, parent)
            cursor, kind = node["end_line"] + 1, "body" if parent else "module"
        if cursor <= last:
            self._gap(cursor, last, parent, kind)

    def _declaration(self, node, parent):
        first, last = node["start_line"], node["end_line"]
        if self.fits(first, last):
            self.units.append(self._unit(first, last, "declaration", parent, node))
        elif self.children[node["id"]]:
            self._span(first, last, self.children[node["id"]], node)
        else:
            self._lines(first, last, node)

    def _gap(self, first, last, parent, kind):
        if self.fits(first, last):
            self.units.append(self._unit(first, last, kind, parent))
        else:
            self._lines(first, last, parent)

    def _lines(self, first, last, parent):
        kind, start = "segment" if parent else "lines", first
        while start <= last:
            if not self.fits(start, start):
                self.units.append(self._unit(start, start, kind, parent, state="unsupported"))
                start += 1
                continue
            low, high = start, last
            while low < high:
                middle = (low + high + 1) // 2
                if self.fits(start, middle):
                    low = middle
                else:
                    high = middle - 1
            end = low
            if end < last:
                # Prefer a statement or declaration start, then a blank line, in the last three quarters.
                floor = start + (end - start) // 4
                for cuts in (self.cuts, self.blank_cuts):
                    index = bisect_right(cuts, end + 1) - 1
                    if index >= 0 and cuts[index] > floor:
                        end = cuts[index] - 1
                        break
            self.units.append(self._unit(start, end, kind, parent))
            start = end + 1


def _blank_record(path, sha, count):
    return {"id": stable_id("iv", path, sha, 1, count), "path": path, "start_line": 1, "end_line": count,
            "line_count": count, "kind": "empty", "state": "empty",
            "reason": "whitespace_only" if count else "no_content", "symbol": "<module>", "symbol_id": None,
            "symbol_ids": [], "segment": None, "task_id": None, "text_chars": None, "text_sha256": None}


def _record(path, sha, unit, lines, rendered, decls, starts):
    first, last = unit["start_line"], unit["end_line"]
    owner = unit["node"] or unit["parent"]
    record = {"id": stable_id("iv", path, sha, first, last), "path": path, "start_line": first, "end_line": last,
              "line_count": last - first + 1, "kind": unit["kind"], "state": unit["state"], "reason": None,
              "symbol": owner["qualname"] if owner else "<module>", "symbol_id": owner["id"] if owner else None,
              "symbol_ids": [node["id"] for node in decls[bisect_left(starts, first):bisect_right(starts, last)]],
              "segment": unit["segment"], "task_id": None, "text_chars": None, "text_sha256": None}
    if unit["state"] == "unsupported":
        # Only the size and hash of the redacted line are kept; its text never enters a prompt.
        record.update(reason="line_exceeds_request_budget", line_chars=len(lines[first - 1]),
                      line_sha256=sha256_text(lines[first - 1]))
    else:
        text = "\n".join(rendered[first - 1:last])
        record.update(text_chars=len(text), text_sha256=sha256_text(text))
    return record


def _check_partition(path, count, records):
    cursor = 1
    for record in records:
        if record["start_line"] != cursor or record["end_line"] < record["start_line"] - (count == 0):
            raise ReviewPlanError(f"Internal interval partition error: {path}")
        cursor = record["end_line"] + 1
    if cursor != count + 1:
        raise ReviewPlanError(f"Internal interval partition error: {path}")


def _brief_item(record, rendered, nodes):
    item = {"id": record["id"], "path": record["path"], "start_line": record["start_line"],
            "end_line": record["end_line"], "symbol": record["symbol"],
            "text": "\n".join(rendered[record["start_line"] - 1:record["end_line"]])}
    if record["segment"]:
        parent = nodes[record["segment"]["parent_symbol_id"]]
        item["segment"] = dict(record["segment"], parent_qualname=parent["qualname"], parent_kind=parent["kind"])
    return item


def _oversized(value):
    if type(value) is str:
        return len(value) > MAX_LABEL_CHARS
    if type(value) is dict:
        return any(_oversized(item) for item in value.values())
    if type(value) is list:
        return any(_oversized(item) for item in value)
    return False


class _Navigator:
    """Index rows for a task's owned intervals only; graph labels carry no foreign source text."""

    def __init__(self, graph, logging):
        self.graph, self.sites = graph, defaultdict(list)
        for site in logging["sites"]:
            self.sites[site["path"]].append(site)
        self.wrappers = {node["id"] for wrapper in logging["wrappers"] for node in graph.decls.get(wrapper["path"], ())
                         if node["name"] == wrapper["name"] and node["start_line"] <= wrapper["start_line"] <= node["end_line"]}

    def rows(self, group):
        """Candidate rows in priority order: symbols, resolved calls, logging, literals, other calls, imports."""
        tiers, seen, paths = [[] for _ in range(6)], set(), []
        for record in group:
            path, first, last = record["path"], record["start_line"], record["end_line"]
            if path not in paths:
                paths.append(path)
            owners = [record["segment"]["parent_symbol_id"]] if record["segment"] else []
            owners += [record["symbol_id"]] if record["symbol_id"] else []
            for symbol_id in [*owners, *record["symbol_ids"]]:
                if symbol_id not in seen:
                    seen.add(symbol_id)
                    tiers[0].append(self._symbol(symbol_id))
            for call in self.graph.calls.get(path, ()):
                if first <= call["start_line"] <= last:
                    row = self._call(call)
                    tiers[1 if "target_id" in row else 4].append(row)
            tiers[2] += [self._log(site) for site in self.sites.get(path, ()) if first <= site["start_line"] <= last]
            tiers[3] += [self._literal(item) for item in self.graph.literals.get(path, ()) if first <= _line(item) <= last]
        tiers[5] += [self._imports(path, self.graph.imports[path]) for path in paths if self.graph.imports.get(path)]
        return [row for tier in tiers for row in tier]

    def _symbol(self, symbol_id):
        node = self.graph.nodes[symbol_id]
        callers = sorted(self.graph.callers.get(symbol_id, ()),
                         key=lambda call: (call["path"], call["start_line"], str(call.get("id"))))
        return {"t": "symbol", "id": symbol_id, "path": node["path"], "qualname": node["qualname"], "kind": node["kind"],
                "start_line": node["start_line"], "end_line": node["end_line"],
                "wrapper_candidate": symbol_id in self.wrappers, "callers_total": len(callers),
                "callers": [{"owner_id": _text(call, "owner_id"), "path": call["path"], "line": call["start_line"]}
                            for call in callers[:MAX_CALLERS]]}

    def _call(self, call):
        row = {"t": "call", "path": call["path"], "line": call["start_line"], "owner_id": _text(call, "owner_id"),
               "callee": _text(call, "callee"), "status": _text(call, "status")}
        target = self.graph.nodes.get(call["target_id"]) if type(call.get("target_id")) is str else None
        if target is not None:
            row.update(target_id=target["id"], target_path=target["path"], target_qualname=target["qualname"],
                       target_wrapper_candidate=target["id"] in self.wrappers)
        elif type(call.get("target_path")) is str:
            row["target_path"] = call["target_path"]
        matches = call.get("name_matches")
        matches = matches.get("count") if type(matches) is dict else matches
        if type(matches) is int:
            # A count of same-name declarations only; never edges.
            row["name_matches"] = matches
        return row

    def _log(self, site):
        return {"t": "log", "path": site["path"], "line": site["start_line"], "end_line": site["end_line"],
                "callee": site["callee"], "kind": site["kind"], "event_key": site["event_key"],
                "key_status": site["key_status"]}

    def _literal(self, record):
        text = _text(record, "text", "literal", "value")
        return {"t": "literal", "path": record["path"], "line": _line(record), "text": text,
                "role": _text(record, "kind", "role"), "matches_elsewhere": self.graph.literal_totals[text] - 1}

    def _imports(self, path, records):
        items = []
        for record in records:
            item = {key: record[key] for key in _IMPORT_FIELDS if type(record.get(key)) in (str, int, bool)
                    and not (type(record[key]) is str and len(record[key]) > MAX_LABEL_CHARS)}
            if item:
                items.append(item)
        return {"t": "imports", "path": path, "items": items[:MAX_IMPORTS], "total": len(records)}


def _cap_navigation(rows, cap):
    """Keep rows in priority order within cap chars; every omitted row is counted by type."""
    # Omission counts must survive index_share=0. Their bounded space is
    # reserved separately from row content in every task's request skeleton.
    room = cap
    kept, used, omitted = [], 0, Counter()
    for row in rows:
        if _oversized(row):
            omitted["oversized_label"] += 1
            continue
        size = len(canonical_json(row)) + bool(kept)
        if used + size <= room:
            kept.append(row)
            used += size
        else:
            omitted[row["t"]] += 1
    navigation = {"rows": kept, "omitted_rows": sum(omitted.values()), "omitted_by_type": dict(sorted(omitted.items()))}
    if len(canonical_json(navigation)) - len(canonical_json(_EMPTY_NAVIGATION)) > cap + NAV_METADATA_RESERVE:
        raise ReviewPlanError("Internal navigation budget accounting error")
    return navigation


def _pack(records, sizes, budget):
    """Greedy locality packing in directory, path and line order; at most 12 intervals per task."""
    groups, current, used, directory = [], [], 0, None
    for record in sorted(records, key=lambda item: (posixpath.dirname(item["path"]), item["path"], item["start_line"])):
        here, size = posixpath.dirname(record["path"]), sizes[record["id"]]
        if current and (len(current) == MAX_INTERVALS_PER_TASK or used + 1 + size > budget
                        or (here != directory and 2 * used >= budget)):
            groups.append(current)
            current, used = [], 0
        used += size + bool(current)
        current.append(record)
        directory = here
    if current:
        groups.append(current)
    return groups


@dataclass(frozen=True)
class ReviewTask:
    """One exact request. supplied_ranges are the only source ranges its user payload contains."""
    id: str
    kind: str
    system: str = field(repr=False)
    user: str = field(repr=False)
    request_sha256: str
    request_chars: int
    interval_ids: tuple
    supplied_ranges: tuple
    exposed_symbol_ids: tuple
    exposed_literals: tuple
    navigation_rows: int
    navigation_omitted: dict

    def to_dict(self, include_requests=False):
        data = {"id": self.id, "kind": self.kind, "state": "pending", "request_sha256": self.request_sha256,
                "request_chars": self.request_chars, "system_chars": len(self.system),
                "system_sha256": sha256_text(self.system), "user_chars": len(self.user),
                "user_sha256": sha256_text(self.user), "interval_ids": list(self.interval_ids),
                "supplied_ranges": [dict(item) for item in self.supplied_ranges],
                "exposed_symbol_ids": list(self.exposed_symbol_ids), "exposed_literals": list(self.exposed_literals),
                "navigation_rows": self.navigation_rows, "navigation_omitted": dict(self.navigation_omitted)}
        if include_requests:
            data.update(system=self.system, user=self.user)
        return data


@dataclass(frozen=True)
class ReviewPlan:
    plan_id: str | None
    binding: dict
    run_fingerprint: str
    budget: dict
    reply_contract: dict = field(repr=False)
    files: tuple = field(repr=False)
    policy_excluded: tuple = field(repr=False)
    intervals: tuple = field(repr=False)
    tasks: tuple = field(repr=False)
    logging_inventory: dict = field(repr=False)
    summary: dict = field(repr=False)

    def interval_map(self):
        return {record["id"]: record for record in self.intervals}

    def to_dict(self, include_requests=False):
        """Full local ledger; prompt texts are included only on request (their hashes always are)."""
        return copy.deepcopy({
            "tool": TOOL, "plan_id": self.plan_id, "binding": self.binding, "run_fingerprint": self.run_fingerprint,
            "budget": self.budget, "reply_contract": self.reply_contract, "files": list(self.files),
            "policy_excluded": list(self.policy_excluded), "intervals": list(self.intervals),
            "tasks": [task.to_dict(include_requests) for task in self.tasks],
            "logging_inventory": self.logging_inventory, "summary": self.summary})


def _sources(snapshot):
    files, scope = getattr(snapshot, "files", None), getattr(snapshot, "scope", None)
    if type(files) is not dict or type(scope) is not dict or type(scope.get("partial_input")) is not bool:
        raise ReviewPlanError("Expected a frozen repository snapshot")
    for path, source in files.items():
        if type(path) is not str or not isinstance(source, SourceFile) or source.path != path or type(source.text) is not str:
            raise ReviewPlanError("Snapshot files must be SourceFile records keyed by path")
    return dict(sorted(files.items()))


def _inventory(snapshot, sources):
    """Structured whole-scope inventory, when the snapshot has one; it must match the frozen files."""
    inventory = getattr(snapshot, "inventory", None)
    if inventory is None:
        return None, [], []
    if type(inventory) is not dict or type(inventory.get("entries")) is not list:
        raise ReviewPlanError("Snapshot inventory is malformed")
    failures, policy, eligible = [], [], set()
    for entry in inventory["entries"]:
        status = entry.get("status") if type(entry) is dict else None
        if status == "eligible":
            eligible.add(entry.get("path"))
        elif status == "policy_excluded":
            policy.append(dict(entry))
        elif status == "resource_unsupported":
            failures.append(dict(entry, state="unsupported"))
        else:
            raise ReviewPlanError("Snapshot inventory has an unknown entry status")
    if eligible != set(sources):
        raise ReviewPlanError("Snapshot inventory does not match the frozen files")
    return inventory, failures, policy


def _budget(system, options):
    index_cap = int(options.request_chars * options.index_share)
    parent_cap = int(options.request_chars * options.parent_share)
    skeleton = len(canonical_json(_brief("base", [], _EMPTY_NAVIGATION, options)))
    source = options.request_chars - len(system) - skeleton - index_cap - parent_cap - NAV_METADATA_RESERVE
    if source < MIN_SOURCE_BUDGET:
        raise ReviewPlanError(
            f"Review source budget is {source} chars after the system prompt, brief skeleton, index and parent "
            f"shares; at least {MIN_SOURCE_BUDGET} are required. Raise request_chars or shorten the system prompt")
    # Every interval fits source_budget, so it also fits beside parent context in a follow-up.
    # Base tasks may fill pack_budget, which leaves out only the parent share.
    return {"request_chars": options.request_chars, "system_chars": len(system), "skeleton_chars": skeleton,
            "index_cap": index_cap, "navigation_metadata_reserve": NAV_METADATA_RESERVE,
            "parent_cap": parent_cap, "source_budget": source,
            "pack_budget": options.request_chars - len(system) - skeleton - index_cap - NAV_METADATA_RESERVE}


def _file_record(path, source, lines, records, graph, declarations):
    states = set(record["state"] for record in records)
    state = ("empty" if states == {"empty"} else "unsupported" if states == {"unsupported"}
             else "partially_unsupported" if "unsupported" in states else "planned")
    return {"path": path, "kind": "file", "sha256": source.sha256, "status": "eligible", "state": state,
            "reason": None, "lines": len(lines), "chars": len(source.text), "extractor": graph.extractors.get(path),
            "declarations": declarations, "intervals": len(records)}


def _task(group, items, navigator, nodes, system, options, budget, run_fingerprint):
    navigation = _cap_navigation(navigator.rows(group), budget["index_cap"])
    user = canonical_json(_brief("base", [items[record["id"]] for record in group], navigation, options))
    if len(system) + len(user) > options.request_chars:
        raise ReviewPlanError("Internal request budget accounting error")
    exposed = {record["segment"]["parent_symbol_id"] for record in group if record["segment"]}
    literals = set()
    for row in navigation["rows"]:
        if row["t"] == "symbol":
            exposed.add(row["id"])
            exposed.update(caller["owner_id"] for caller in row["callers"])
        elif row["t"] == "call":
            exposed.update((row["owner_id"], row.get("target_id")))
        elif row["t"] == "literal":
            literals.add(row["text"])
        elif row["t"] == "log" and row["event_key"] is not None:
            literals.add(row["event_key"])
    digest = request_sha256(system, user)
    interval_ids = tuple(record["id"] for record in group)
    return ReviewTask(
        id=stable_id("task", run_fingerprint, "base", list(interval_ids), digest), kind="base", system=system,
        user=user, request_sha256=digest, request_chars=len(system) + len(user), interval_ids=interval_ids,
        supplied_ranges=tuple({"interval_id": record["id"], "path": record["path"], "start_line": record["start_line"],
                               "end_line": record["end_line"], "role": "owned"} for record in group),
        exposed_symbol_ids=tuple(sorted(item for item in exposed if item in nodes)),
        exposed_literals=tuple(sorted(literals)), navigation_rows=len(navigation["rows"]),
        navigation_omitted=navigation["omitted_by_type"])


def _summary(snapshot, inventory, failures, policy, files, intervals, tasks, graph):
    states, lines, omitted = Counter(), Counter(), Counter()
    for record in intervals:
        states[record["state"]] += 1
        lines[record["state"]] += record["line_count"]
    for task in tasks:
        omitted.update(task.navigation_omitted)
    reasons = [reason for reason, present in (
        ("snapshot_partial_input", snapshot.scope["partial_input"]), ("resource_unsupported_inputs", bool(failures)),
        ("unsupported_intervals", bool(states["unsupported"])), ("no_reviewable_lines", not tasks)) if present]
    return {"eligible_files": len(files) + sum(item.get("kind") == "file" for item in failures),
            "snapshot_files": len(files), "files_by_state": dict(Counter(record["state"] for record in files)),
            "resource_unsupported_entries": len(failures),
            "resource_unsupported_by_reason": dict(Counter(item.get("reason") for item in failures)),
            "policy_excluded_entries": len(policy),
            "policy_excluded_by_reason": dict(Counter(item.get("reason") for item in policy)),
            "inventory": "unavailable" if inventory is None else "structured",
            "snapshot_partial_input": snapshot.scope["partial_input"],
            "lines_total": sum(record["lines"] for record in files), "lines_by_state": dict(lines),
            "intervals_by_state": dict(states), "tasks": len(tasks),
            "request_chars_total": sum(task.request_chars for task in tasks),
            "request_chars_max": max((task.request_chars for task in tasks), default=0),
            "navigation_rows_supplied": sum(task.navigation_rows for task in tasks),
            "navigation_rows_omitted": sum(omitted.values()), "navigation_omitted_by_type": dict(sorted(omitted.items())),
            "graph_records_ignored": dict(sorted(graph.ignored.items())),
            "planned_coverage_complete": not reasons, "incomplete_reasons": reasons,
            # Completeness needs supplied, validated replies; a plan never sets it.
            "complete": False,
            "basis": "Planning only: each line is assigned to a task or recorded as unsupported or empty. Nothing has "
                     "been supplied or reviewed, and a plan does not establish semantic understanding."}


def plan_review(snapshot, graph, system, options=None):
    """Partition the frozen snapshot into owned intervals and bounded base tasks.

    Reads only snapshot text (never the filesystem or raw text) and makes no model
    call. graph may be None, which leaves only line, statement and logging hints.
    """
    options = ReviewOptions() if options is None else options
    if type(options) is not ReviewOptions:
        raise ReviewPlanError("options must be ReviewOptions")
    sources = _sources(snapshot)
    system = task_system(system)
    inventory, failures, policy = _inventory(snapshot, sources)
    lines = {path: source.text.splitlines() for path, source in sources.items()}
    graph_index = _Graph(graph, snapshot, {path: len(value) for path, value in lines.items()})
    logging = discover_logging(snapshot, call_names=tuple(snapshot.logging_calls), max_sites=None,
                               max_per_file=None, max_hints=None)
    budget = _budget(system, options)
    intervals, rendered, files = [], {}, []
    for path, source in sources.items():
        file_lines, decls = lines[path], graph_index.decls.get(path, [])
        rendered[path] = [f"{number}: {line}" for number, line in enumerate(file_lines, 1)]
        if not any(line.strip() for line in file_lines):
            records = [_blank_record(path, source.sha256, len(file_lines))]
        else:
            units = _FileUnits(path, source.text, file_lines, decls, budget["source_budget"], graph_index.ignored).build()
            starts = [node["start_line"] for node in decls]
            records = [_record(path, source.sha256, unit, file_lines, rendered[path], decls, starts) for unit in units]
        _check_partition(path, len(file_lines), records)
        intervals.extend(records)
        files.append(_file_record(path, source, file_lines, records, graph_index, len(decls)))
    planned = [record for record in intervals if record["state"] == "planned"]
    items = {record["id"]: _brief_item(record, rendered[record["path"]], graph_index.nodes) for record in planned}
    sizes = {key: len(canonical_json(item)) for key, item in items.items()}
    binding = {"tool": TOOL, "snapshot_id": snapshot.snapshot_id, "graph_sha256": graph_index.sha256,
               "system_sha256": sha256_text(system), "options": options.identity()}
    run_fingerprint = sha256_text(canonical_json(binding))
    navigator, tasks = _Navigator(graph_index, logging), []
    for group in _pack(planned, sizes, budget["pack_budget"]):
        task = _task(group, items, navigator, graph_index.nodes, system, options, budget, run_fingerprint)
        for record in group:
            record["task_id"] = task.id
        tasks.append(task)
    summary = _summary(snapshot, inventory, failures, policy, files, intervals, tasks, graph_index)
    plan = ReviewPlan(plan_id=None, binding=binding, run_fingerprint=run_fingerprint, budget=budget,
                      reply_contract=reply_contract(options),
                      files=tuple(sorted(files + failures, key=lambda item: (item.get("path") or "", item.get("kind") or ""))),
                      policy_excluded=tuple(policy), intervals=tuple(intervals), tasks=tuple(tasks),
                      logging_inventory=logging, summary=summary)
    return replace(plan, plan_id=stable_id("plan", plan.to_dict()))


def review_repository(snapshot, graph, model, **kwargs):
    """Execute a plan with validated replies; see review_execution.review_repository.

    Imported lazily: the executor depends on this planner, not the other way round.
    """
    from .review_execution import review_repository as execute
    return execute(snapshot, graph, model, **kwargs)
