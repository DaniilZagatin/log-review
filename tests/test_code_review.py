from collections import defaultdict
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from process_observability import source_tools
from process_observability.code_review import (FINDING_KINDS, TASK_LINE, ReviewOptions, ReviewPlanError,
                                               canonical_json, numbered_text, plan_review, request_sha256)
from process_observability.log_discovery import discover_logging
from process_observability.redaction import redact_text
from process_observability.source_tools import RepoSnapshot, SourceError, SourceFile, snapshot_repository

MARKER = re.compile(r"L(\d{3})_(\d{4})Z")
BRIEF_KEYS = {"kind", "instructions", "owned_intervals", "navigation", "reply_schema", "finding_kinds",
              "interval_statuses", "request_types", "limits"}
SYSTEM = "Review the supplied code."


class StubGraph:
    """Pinned S1 interface only; the real graph module is deliberately not used here."""

    def __init__(self, nodes=(), calls=(), imports=(), literals=(), files=()):
        self.data = {"nodes": [dict(item) for item in nodes], "calls": [dict(item) for item in calls],
                     "imports": [dict(item) for item in imports], "references": [],
                     "literals": [dict(item) for item in literals], "files": [dict(item) for item in files],
                     "source_roots": [""]}
        self.sha256 = hashlib.sha256(json.dumps(self.data, sort_keys=True).encode()).hexdigest()

    def to_dict(self):
        return json.loads(json.dumps(self.data))

    def decls_in(self, path, first, last):
        return [item for item in self.data["nodes"]
                if item["path"] == path and first <= item["start_line"] and item["end_line"] <= last]

    def outgoing(self, symbol_id):
        return [item for item in self.data["calls"] if item["owner_id"] == symbol_id]

    def incoming(self, symbol_id):
        return [item for item in self.data["calls"] if item.get("target_id") == symbol_id]

    def literal_sites(self, text):
        return [item for item in self.data["literals"] if item["text"] == text]


def node(path, qualname, kind, first, last, parent=None):
    digest = hashlib.sha256(f"{path}:{qualname}:{first}:{last}".encode()).hexdigest()[:16]
    return {"id": "sym-" + digest, "path": path, "qualname": qualname, "name": qualname.rsplit(".", 1)[-1],
            "kind": kind, "parent_id": parent, "start_line": first, "end_line": last,
            "signature": f"{qualname}(...)", "extractor": "python_ast"}


def call(path, line, owner, callee, status="external_or_builtin", target=None):
    return {"id": f"call-{path}-{line}-{callee}", "path": path, "start_line": line, "end_line": line, "column": 4,
            "owner_id": owner, "callee": callee, "status": status,
            "target_id": target["id"] if target else None, "target_path": target["path"] if target else None}


def marked(index, code):
    """Every nonblank line carries a unique marker, so leaked foreign text is detectable."""
    return "".join(f"{line}  # L{index:03d}_{number:04d}Z {'q' * 24}\n" if line.strip() else "\n"
                   for number, line in enumerate(code, 1))


def module_code(index, body=12):
    code = ["import os", "", "@decorator", f"def handler_{index}(value):"]
    code += [f"    value = value + {step}" for step in range(body)]
    code += ['    log.info("evt.done", case_id=value)', "    return value", "", f"class Thing{index}:",
             "    size = 1", "    def method(self):"]
    code += [f"        self.size += {step}" for step in range(body)]
    return code + ["        return self.size", f"RESULT = handler_{index}(1)"]


def snapshot_of(files, partial=False):
    sources = {path: SourceFile(path, hashlib.sha256(text.encode()).hexdigest(), redact_text(text), text)
               for path, text in files.items()}
    return RepoSnapshot(sources, {"partial_input": partial, "limitations": []})


def large_scope(count=70):
    files, paths, nodes, calls = {}, {}, [], []
    for index in range(count):
        path = f"pkg{index % 5}/mod_{index:03d}.py"
        files[path], paths[index] = marked(index, module_code(index)), path
        handler = node(path, f"handler_{index}", "function", 3, 18)
        thing = node(path, f"Thing{index}", "class", 20, 35)
        nodes += [handler, thing, node(path, f"Thing{index}.method", "method", 22, 35, thing["id"])]
        calls += [call(path, 36, None, f"handler_{index}", "local_def", handler),
                  call(path, 17, handler["id"], "log.info")]
    return files, paths, StubGraph(nodes, calls)


class PlanAssertions(unittest.TestCase):
    def assert_partition(self, plan, snapshot):
        by_path = defaultdict(list)
        for interval in plan.intervals:
            by_path[interval["path"]].append(interval)
        self.assertEqual(set(by_path), set(snapshot.files))
        for path, intervals in by_path.items():
            count = len(snapshot.files[path].text.splitlines())
            owned = [line for item in intervals for line in range(item["start_line"], item["end_line"] + 1)]
            self.assertEqual(sorted(owned), list(range(1, count + 1)), path)
            self.assertEqual(sum(item["line_count"] for item in intervals), count)

    def assert_tasks(self, plan, snapshot, options, paths=None):
        planned = {item["id"]: item for item in plan.intervals if item["state"] == "planned"}
        seen = []
        for task in plan.tasks:
            self.assertEqual(task.request_chars, len(task.system) + len(task.user))
            self.assertLessEqual(task.request_chars, options.request_chars)
            self.assertEqual(task.request_sha256, request_sha256(task.system, task.user))
            self.assertEqual(task.user, canonical_json(json.loads(task.user)))
            brief = json.loads(task.user)
            self.assertEqual(set(brief), BRIEF_KEYS)
            self.assertLessEqual(len(brief["owned_intervals"]), 12)
            self.assertEqual([item["id"] for item in brief["owned_intervals"]], list(task.interval_ids))
            self.assertEqual([(r["path"], r["start_line"], r["end_line"]) for r in task.supplied_ranges],
                             [(i["path"], i["start_line"], i["end_line"]) for i in brief["owned_intervals"]])
            supplied = set()
            for item in brief["owned_intervals"]:
                interval = planned[item["id"]]
                self.assertEqual((item["path"], item["start_line"], item["end_line"]),
                                 (interval["path"], interval["start_line"], interval["end_line"]))
                self.assertEqual(item["text"], numbered_text(snapshot.files[item["path"]], item["start_line"], item["end_line"]))
                self.assertEqual(interval["task_id"], task.id)
                supplied |= {(item["path"], line) for line in range(item["start_line"], item["end_line"] + 1)}
            if paths is not None:
                for index, line in MARKER.findall(task.user):
                    self.assertIn((paths[int(index)], int(line)), supplied)
            seen += task.interval_ids
        self.assertEqual(sorted(seen), sorted(planned))


class OptionsTests(unittest.TestCase):
    def test_defaults_and_rejected_values(self):
        options = ReviewOptions()
        self.assertEqual(options.to_dict(), {"request_chars": 32000, "workers": 2, "max_followups": 32,
                                             "max_requests_per_task": 6, "max_findings_per_task": 40,
                                             "repair_attempts": 1, "index_share": 0.2, "parent_share": 0.25})
        self.assertNotIn("workers", options.identity())
        for bad in [dict(workers=0), dict(workers=9), dict(workers=True), dict(request_chars=True),
                    dict(request_chars=3999), dict(request_chars=32000.0), dict(max_followups=-1),
                    dict(max_followups=False), dict(max_requests_per_task=None), dict(max_findings_per_task=0),
                    dict(repair_attempts=2), dict(index_share=True), dict(index_share=float("nan")),
                    dict(index_share=0.6), dict(parent_share=-0.1), dict(parent_share="0.2")]:
            with self.subTest(bad=bad), self.assertRaises(ReviewPlanError):
                ReviewOptions(**bad)
        with self.assertRaises(ReviewPlanError):
            plan_review(snapshot_of({"a.py": "x = 1\n"}), None, SYSTEM, {"request_chars": 32000})


class PlanningTests(PlanAssertions):
    def test_more_than_sixty_files_and_120k_chars_are_all_planned_within_exact_budgets(self):
        files, paths, graph = large_scope()
        snapshot, options = snapshot_of(files), ReviewOptions()
        rendered = sum(len(numbered_text(source, 1, len(source.text.splitlines()))) for source in snapshot.files.values())
        self.assertGreater(len(files), 60)
        self.assertGreater(rendered, 120000)
        plan = plan_review(snapshot, graph, SYSTEM, options)
        self.assert_partition(plan, snapshot)
        self.assert_tasks(plan, snapshot, options, paths)
        self.assertGreater(len(plan.tasks), 1)
        self.assertEqual({item["state"] for item in plan.intervals}, {"planned"})
        summary = plan.summary
        self.assertTrue(summary["planned_coverage_complete"])
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["lines_by_state"], {"planned": summary["lines_total"]})
        self.assertEqual(summary["request_chars_total"], sum(task.request_chars for task in plan.tasks))
        for interval in plan.intervals:
            # The decorator line stays with its declaration.
            self.assertTrue(interval["start_line"] <= 3 <= 4 <= interval["end_line"]
                            or not interval["start_line"] <= 3 <= interval["end_line"])
        for task in plan.tasks:
            self.assertTrue(task.system.startswith(TASK_LINE + "\n"))
            navigation = json.loads(task.user)["navigation"]
            self.assertEqual(navigation["omitted_rows"], sum(navigation["omitted_by_type"].values()))
            self.assertEqual(task.navigation_rows, len(navigation["rows"]))
            self.assertTrue(any(row["t"] == "symbol" for row in navigation["rows"]))
            self.assertTrue(set(task.exposed_symbol_ids) <= {item["id"] for item in graph.data["nodes"]})
        self.assertEqual(json.loads(plan.tasks[0].user)["finding_kinds"], list(FINDING_KINDS))
        # With room for the whole index, every row kind appears and nothing is omitted.
        roomy = ReviewOptions(index_share=0.5)
        wide = plan_review(snapshot, graph, SYSTEM, roomy)
        self.assert_tasks(wide, snapshot, roomy, paths)
        self.assertEqual(wide.summary["navigation_rows_omitted"], 0)
        rows = [row for task in wide.tasks for row in json.loads(task.user)["navigation"]["rows"]]
        self.assertTrue(any(row["t"] == "symbol" and row["callers_total"] == 1 for row in rows))
        self.assertTrue(any(row["t"] == "call" and row["status"] == "local_def" and "target_id" in row for row in rows))
        self.assertTrue(any(row["t"] == "log" and row["event_key"] == "evt.done" for row in rows))
        self.assertTrue(all("evt.done" in task.exposed_literals for task in wide.tasks))

    def test_oversized_function_and_class_split_into_segments_with_module_lines_and_decorators_owned(self):
        code = ["import os", "SETTING = 1", "# leading comment", "@decorator", "def huge(value):"]
        code += [f"    value = value + {step}" for step in range(260)]
        code += ["    return value", "", "class Big:", '    """Docstring."""', "    size = 1"]
        for number in range(6):
            code += [f"    def method_{number}(self):"] + [f"        self.size += {step}" for step in range(30)]
            code += ["        return self.size"]
        code += ["TAIL = 2"]
        self.assertEqual(len(code), 463)
        huge, big = node("big.py", "huge", "function", 4, 266), node("big.py", "Big", "class", 268, 462)
        methods = [node("big.py", f"Big.method_{n}", "method", 271 + 32 * n, 302 + 32 * n, big["id"]) for n in range(6)]
        snapshot = snapshot_of({"big.py": marked(1, code)})
        options = ReviewOptions(request_chars=16000)
        plan = plan_review(snapshot, StubGraph([huge, big, *methods]), SYSTEM, options)
        self.assert_partition(plan, snapshot)
        self.assert_tasks(plan, snapshot, options, {1: "big.py"})
        self.assertTrue(plan.summary["planned_coverage_complete"])

        def pieces(parent):
            found = [item for item in plan.intervals if item["segment"] and item["segment"]["parent_symbol_id"] == parent["id"]]
            self.assertEqual([item["segment"]["index"] for item in found], list(range(1, len(found) + 1)))
            self.assertEqual({item["segment"]["count"] for item in found}, {len(found)})
            return found
        segments = pieces(huge)
        self.assertGreater(len(segments), 1)
        self.assertEqual({item["kind"] for item in segments}, {"segment"})
        self.assertEqual((segments[0]["start_line"], segments[-1]["end_line"]), (4, 266))
        self.assertEqual(sum(item["line_count"] for item in segments), 263)
        class_pieces = pieces(big)
        self.assertGreater(len(class_pieces), 1)
        self.assertEqual((class_pieces[0]["start_line"], class_pieces[-1]["end_line"]), (268, 462))
        self.assertIn(class_pieces[0]["kind"], {"header", "group"})
        self.assertEqual({item["symbol"] for item in class_pieces}, {"Big"})
        self.assertEqual({big["id"], *(method["id"] for method in methods)},
                         {i for item in class_pieces for i in item["symbol_ids"]})
        module_lines = [item for item in plan.intervals if item["segment"] is None]
        self.assertEqual(sorted(line for item in module_lines for line in range(item["start_line"], item["end_line"] + 1)),
                         [1, 2, 3, 267, 463])
        items = {item["id"]: item for task in plan.tasks for item in json.loads(task.user)["owned_intervals"]}
        for interval in segments:
            self.assertEqual(items[interval["id"]]["segment"], dict(interval["segment"], parent_qualname="huge",
                                                                    parent_kind="function"))
            self.assertEqual(items[interval["id"]]["symbol"], "huge")

    def test_non_python_line_windows_break_at_blank_lines(self):
        lines = []
        for block in range(60):
            lines += [f"block_{block}:"] + [f"  key_{key}: value_{block}_{key}_{'v' * 40}" for key in range(9)] + [""]
        text = "\n".join(lines) + "\n"
        snapshot, options = snapshot_of({"conf/app.yaml": text}), ReviewOptions()
        plan = plan_review(snapshot, None, SYSTEM, options)
        self.assert_partition(plan, snapshot)
        self.assert_tasks(plan, snapshot, options)
        windows = plan.intervals
        self.assertGreater(len(windows), 1)
        self.assertEqual({item["kind"] for item in windows}, {"lines"})
        for window in windows[:-1]:
            self.assertEqual(lines[window["end_line"] - 1], "")

    def test_line_longer_than_the_budget_is_unsupported_accounted_and_never_sent(self):
        line = "b = '" + "Z" * 40000 + "CANARYLONG'"
        snapshot, options = snapshot_of({"app.py": f"a = 1\n{line}\nc = 3\n", "ok.py": "x = 1\n"}), ReviewOptions()
        plan = plan_review(snapshot, None, SYSTEM, options)
        self.assert_partition(plan, snapshot)
        self.assert_tasks(plan, snapshot, options)
        [unsupported] = [item for item in plan.intervals if item["state"] == "unsupported"]
        self.assertEqual((unsupported["path"], unsupported["start_line"], unsupported["end_line"]), ("app.py", 2, 2))
        self.assertEqual(unsupported["reason"], "line_exceeds_request_budget")
        self.assertEqual(unsupported["line_chars"], len(line))
        self.assertEqual(unsupported["line_sha256"], hashlib.sha256(line.encode()).hexdigest())
        self.assertIsNone(unsupported["task_id"])
        self.assertTrue(all("CANARYLONG" not in task.user for task in plan.tasks))
        self.assertNotIn("CANARYLONG", json.dumps(plan.to_dict()))
        self.assertEqual(plan.summary["lines_by_state"], {"planned": 3, "unsupported": 1})
        self.assertFalse(plan.summary["planned_coverage_complete"])
        self.assertIn("unsupported_intervals", plan.summary["incomplete_reasons"])
        self.assertEqual({item["path"]: item["state"] for item in plan.files},
                         {"app.py": "partially_unsupported", "ok.py": "planned"})

    def test_scope_with_only_unsupported_lines_is_not_complete(self):
        snapshot = snapshot_of({"blob.py": "x = '" + "Z" * 40000 + "'"})
        plan = plan_review(snapshot, None, SYSTEM, ReviewOptions())
        self.assertEqual(plan.tasks, ())
        self.assertEqual([item["state"] for item in plan.intervals], ["unsupported"])
        self.assertFalse(plan.summary["planned_coverage_complete"])
        self.assertFalse(plan.summary["complete"])
        self.assertEqual(plan.summary["incomplete_reasons"], ["unsupported_intervals", "no_reviewable_lines"])
        self.assertEqual(plan.summary["files_by_state"], {"unsupported": 1})

    def test_empty_and_blank_files_have_records_and_no_request(self):
        snapshot = snapshot_of({"empty.py": "", "blank.py": "\n\n", "comment.py": "# only a comment\n",
                                "conf.yaml": "a: 1\n"})
        plan = plan_review(snapshot, None, SYSTEM, ReviewOptions())
        self.assert_partition(plan, snapshot)
        records = {item["path"]: item for item in plan.intervals}
        self.assertEqual((records["empty.py"]["state"], records["empty.py"]["reason"], records["empty.py"]["line_count"]),
                         ("empty", "no_content", 0))
        self.assertEqual((records["blank.py"]["state"], records["blank.py"]["reason"], records["blank.py"]["line_count"]),
                         ("empty", "whitespace_only", 2))
        self.assertIsNone(records["empty.py"]["task_id"])
        self.assertEqual({path for task in plan.tasks for path in (r["path"] for r in task.supplied_ranges)},
                         {"comment.py", "conf.yaml"})
        self.assertEqual({item["path"]: item["state"] for item in plan.files},
                         {"empty.py": "empty", "blank.py": "empty", "comment.py": "planned", "conf.yaml": "planned"})
        self.assertTrue(plan.summary["planned_coverage_complete"])

    def test_plans_are_deterministic_and_bound_to_source_system_and_options(self):
        files, _, graph = large_scope(12)
        first = plan_review(snapshot_of(files), graph, SYSTEM, ReviewOptions())
        second = plan_review(snapshot_of(dict(files)), graph, SYSTEM, ReviewOptions())
        self.assertEqual(canonical_json(first.to_dict(include_requests=True)),
                         canonical_json(second.to_dict(include_requests=True)))
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(ReviewOptions(workers=4).identity(), ReviewOptions().identity())
        self.assertEqual(plan_review(snapshot_of(files), graph, SYSTEM, ReviewOptions(workers=4)).plan_id, first.plan_id)
        changed_path = sorted(files)[0]
        changed = dict(files, **{changed_path: files[changed_path] + "EXTRA = 1\n"})
        third = plan_review(snapshot_of(changed), graph, SYSTEM, ReviewOptions())
        self.assertNotEqual(third.plan_id, first.plan_id)
        self.assertFalse({task.id for task in first.tasks} & {task.id for task in third.tasks})
        before = {item["path"]: item["id"] for item in first.intervals}
        after = {item["path"]: item["id"] for item in third.intervals}
        self.assertNotEqual(before[changed_path], after[changed_path])
        self.assertEqual({p: i for p, i in before.items() if p != changed_path},
                         {p: i for p, i in after.items() if p != changed_path})
        for variant in (plan_review(snapshot_of(files), graph, "Another system prompt.", ReviewOptions()),
                        plan_review(snapshot_of(files), graph, SYSTEM, ReviewOptions(request_chars=30000)),
                        plan_review(snapshot_of(files), graph, SYSTEM, ReviewOptions(max_findings_per_task=10))):
            self.assertFalse({task.id for task in first.tasks} & {task.id for task in variant.tasks})
        self.assertNotIn("time", json.dumps(sorted(first.to_dict())))

    def test_navigation_index_is_capped_with_explicit_omissions(self):
        code = ["def target():", "    return 1", "def caller():"] + ["    target()" for _ in range(200)]
        target = node("calls.py", "target", "function", 1, 2)
        owner = node("calls.py", "caller", "function", 3, 203)
        calls = [call("calls.py", line, owner["id"], "target", "local_def", target) for line in range(4, 204)]
        literals = [{"id": f"lit-{line}", "path": "calls.py", "start_line": line, "text": "orders.created", "kind": "publish"}
                    for line in (4, 5)]
        graph = StubGraph([target, owner], calls, literals=literals)
        snapshot = snapshot_of({"calls.py": "\n".join(code) + "\n"})
        empty = len(canonical_json({"rows": [], "omitted_rows": 0, "omitted_by_type": {}}))
        for share in (0.05, 0.0):
            options = ReviewOptions(index_share=share)
            plan = plan_review(snapshot, graph, SYSTEM, options)
            self.assert_tasks(plan, snapshot, options)
            [task] = plan.tasks
            navigation = json.loads(task.user)["navigation"]
            self.assertLessEqual(len(canonical_json(navigation['rows']))-2,int(options.request_chars*share))
            self.assertLessEqual(len(canonical_json(navigation))-empty,
                                 int(options.request_chars*share)+plan.budget['navigation_metadata_reserve'])
            self.assertGreater(navigation["omitted_rows"], 0)
            self.assertEqual(navigation["omitted_rows"], sum(navigation["omitted_by_type"].values()))
            self.assertEqual(navigation["omitted_rows"] + len(navigation["rows"]), 2 + 200 + 2)
            self.assertEqual(plan.summary["navigation_rows_omitted"], navigation["omitted_rows"])
            shown = {row["id"] for row in navigation["rows"] if row["t"] == "symbol"}
            shown |= {row.get(key) for row in navigation["rows"] if row["t"] == "call" for key in ("owner_id", "target_id")}
            shown |= {item["owner_id"] for row in navigation["rows"] if row["t"] == "symbol" for item in row["callers"]}
            self.assertEqual(set(task.exposed_symbol_ids), {item for item in shown if item})
        self.assertEqual(navigation["rows"], [])
        self.assertEqual(task.exposed_symbol_ids, ())
        symbols = [row for row in json.loads(plan_review(snapshot, graph, SYSTEM).tasks[0].user)["navigation"]["rows"]
                   if row["t"] == "symbol" and row["id"] == target["id"]]
        self.assertEqual((symbols[0]["callers_total"], len(symbols[0]["callers"])), (200, 5))

    def test_planning_makes_no_model_network_or_file_access(self):
        files, _, graph = large_scope(3)
        snapshot = snapshot_of(files)
        forbidden = mock.Mock(side_effect=AssertionError("planning must not perform I/O"))
        with mock.patch("builtins.open", forbidden), mock.patch.object(io, "open", forbidden), \
                mock.patch.object(os, "open", forbidden), mock.patch.object(socket, "socket", forbidden), \
                mock.patch.object(socket, "create_connection", forbidden):
            plan = plan_review(snapshot, graph, SYSTEM, ReviewOptions())
        self.assertGreater(len(plan.tasks), 0)
        forbidden.assert_not_called()
        self.assertNotIn("model", inspect.signature(plan_review).parameters)

    def test_system_prefix_and_budget_preflight(self):
        snapshot = snapshot_of({"a.py": "x = 1\n"})
        self.assertEqual(plan_review(snapshot, None, SYSTEM).tasks[0].system, TASK_LINE + "\n" + SYSTEM)
        prefixed = TASK_LINE + "\nAlready routed."
        self.assertEqual(plan_review(snapshot, None, prefixed).tasks[0].system, prefixed)
        for bad in ("", "   ", 5, None, "TASK: map\nOther stage."):
            with self.subTest(bad=bad), self.assertRaises(ReviewPlanError):
                plan_review(snapshot, None, bad)
        with self.assertRaisesRegex(ReviewPlanError, "source budget"):
            plan_review(snapshot, None, "x" * 20000)
        with self.assertRaisesRegex(ReviewPlanError, "source budget"):
            plan_review(snapshot, None, SYSTEM, ReviewOptions(request_chars=8000))

    def test_graph_that_contradicts_the_snapshot_is_rejected(self):
        snapshot = snapshot_of({"a.py": "x = 1\ny = 2\n"})
        for graph in (StubGraph([node("missing.py", "f", "function", 1, 1)]),
                      StubGraph([node("a.py", "f", "function", 1, 3)]),
                      StubGraph(calls=[call("a.py", 9, None, "f")]),
                      StubGraph(files=[{"path": "a.py", "sha256": "0" * 64}])):
            with self.assertRaises(ReviewPlanError):
                plan_review(snapshot, graph, SYSTEM)
        snapshot = snapshot_of({"b.py": "x = 1\ny = 2\nz = 3\n"})
        overlapping = StubGraph([node("b.py", "g", "function", 1, 2), node("b.py", "h", "function", 2, 3)])
        plan = plan_review(snapshot, overlapping, SYSTEM)
        self.assert_partition(plan, snapshot)
        self.assertEqual(plan.summary["graph_records_ignored"], {"overlapping_node": 1})


class WholeScopeInputTests(unittest.TestCase):
    LEGACY_SCOPE = {"eligible_files", "selected_files", "selected_chars", "excluded_entries", "partial_input",
                    "limitations", "include_patterns", "selection_policy"}

    def repository(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "app.py").write_text("value = 1\n")
        (root / ".env").write_text("API_KEY=CANARY\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "pkg.js").write_text("x\n")
        (root / "notes.txt").write_text("text\n")
        return root

    def test_policy_exclusions_and_resource_failures_are_separate(self):
        root = self.repository()
        clean = snapshot_repository(root, max_files=None, max_chars=None)
        self.assertFalse(clean.scope["partial_input"])
        policy = {(e["path"], e["kind"], e["reason"]) for e in clean.inventory["entries"] if e["status"] == "policy_excluded"}
        self.assertEqual(policy, {(".env", "file", "secret"), ("node_modules", "directory", "dependency_or_generated"),
                                  ("notes.txt", "file", "unsupported_extension")})
        plan = plan_review(clean, None, SYSTEM)
        self.assertTrue(plan.summary["planned_coverage_complete"])
        self.assertEqual(plan.summary["policy_excluded_entries"], 3)
        self.assertEqual(plan.summary["inventory"], "structured")
        self.assertNotIn("CANARY", json.dumps(plan.to_dict(include_requests=True)))
        (root / "big.py").write_text("x = 1\n" * 400)
        (root / "nul.py").write_bytes(b"x = 1\x00\n")
        (root / "latin.py").write_bytes(b"name = '\xe9'\n")
        expected = [("big.py", "exceeds_max_file_bytes"), ("latin.py", "not_utf8"), ("nul.py", "binary")]
        with self.assertRaises(SourceError) as caught:
            snapshot_repository(root, max_files=None, max_chars=None, max_file_bytes=1000)
        self.assertEqual([(item["path"], item["reason"]) for item in caught.exception.failures], expected)
        self.assertIn("big.py (exceeds_max_file_bytes)", str(caught.exception))
        partial = snapshot_repository(root, max_files=None, max_chars=None, max_file_bytes=1000, allow_partial=True)
        self.assertTrue(partial.scope["partial_input"])
        self.assertEqual(list(partial.files), ["app.py"])
        self.assertEqual(partial.inventory["counts"], {"eligible": 4, "selected": 1, "policy_excluded": 3,
                                                       "resource_unsupported": 3})
        failed = [(e["path"], e["reason"]) for e in partial.inventory["entries"] if e["status"] == "resource_unsupported"]
        self.assertEqual(failed, expected)
        plan = plan_review(partial, None, SYSTEM)
        self.assertFalse(plan.summary["planned_coverage_complete"])
        self.assertEqual(plan.summary["incomplete_reasons"], ["snapshot_partial_input", "resource_unsupported_inputs"])
        self.assertEqual(plan.summary["resource_unsupported_by_reason"],
                         {"exceeds_max_file_bytes": 1, "not_utf8": 1, "binary": 1})
        self.assertEqual(plan.summary["eligible_files"], 4)
        self.assertEqual({item["path"]: item["state"] for item in plan.files},
                         {"app.py": "planned", "big.py": "unsupported", "latin.py": "unsupported", "nul.py": "unsupported"})

    def test_changed_and_unreadable_inputs_are_recorded(self):
        root = self.repository()
        (root / "changed.py").write_text("y = 2\n")
        real, calls = os.fstat, []
        def changing(descriptor):
            result = real(descriptor)
            calls.append(descriptor)
            if len(calls) == 3:  # app.py uses calls 1-2; changed.py is opened next.
                return SimpleNamespace(st_ino=result.st_ino, st_dev=result.st_dev, st_size=result.st_size + 1,
                                       st_mtime_ns=result.st_mtime_ns)
            return result
        with mock.patch.object(source_tools.os, "fstat", changing):
            snapshot = snapshot_repository(root, max_files=None, max_chars=None, allow_partial=True)
        reasons = {e["path"]: e["reason"] for e in snapshot.inventory["entries"] if e["status"] == "resource_unsupported"}
        self.assertEqual(reasons, {"changed.py": "changed_during_snapshot"})
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            self.skipTest("Permission-based read failure needs a non-root POSIX user")
        (root / "changed.py").chmod(0)
        self.addCleanup((root / "changed.py").chmod, 0o600)
        snapshot = snapshot_repository(root, max_files=None, max_chars=None, allow_partial=True)
        reasons = {e["path"]: e["reason"] for e in snapshot.inventory["entries"] if e["status"] == "resource_unsupported"}
        self.assertEqual(reasons, {"changed.py": "read_error"})

    def test_legacy_bounded_snapshot_and_discovery_defaults_are_unchanged(self):
        defaults = inspect.signature(snapshot_repository).parameters
        self.assertEqual((defaults["max_files"].default, defaults["max_chars"].default,
                          defaults["max_file_bytes"].default), (60, 120000, 512000))
        discovery = inspect.signature(discover_logging).parameters
        self.assertEqual((discovery["max_sites"].default, discovery["max_per_file"].default,
                          discovery["max_call_lines"].default, discovery["max_hints"].default), (300, 40, 12, 100))
        root = self.repository()
        (root / "nul.py").write_bytes(b"x = 1\x00\n")
        with self.assertRaisesRegex(SourceError, "^Cannot snapshot selected inputs; narrow --include"):
            snapshot_repository(root)
        legacy = snapshot_repository(root, allow_partial=True)
        self.assertIsNone(legacy.inventory)
        self.assertEqual(set(legacy.scope), self.LEGACY_SCOPE)
        self.assertEqual(legacy.scope["limitations"], ["Selected source omitted: nul.py"])
        context = legacy.to_context()
        self.assertEqual(set(context), {"files", "candidates", "scope", "logging_inventory"})
        self.assertEqual(set(context["logging_inventory"]["scope"]),
                         {"files_scanned", "partial_input", "call_names", "extractors", "sites_found", "sites_returned",
                          "truncated", "omitted_by_path", "hints_omitted", "wrappers_omitted"})
        (root / "b.py").write_text("second = 2\n")
        bounded = snapshot_repository(root, max_files=1, allow_partial=True)
        self.assertEqual(bounded.scope["limitations"], ["Selected source omitted: b.py", "Selected source omitted: nul.py"])

    def test_discovery_can_return_every_site_without_changing_legacy_caps(self):
        snapshot = snapshot_of({"app.py": "".join(f'log.info("event.{n}")\n' for n in range(45))})
        bounded = discover_logging(snapshot)
        self.assertEqual((bounded["scope"]["sites_found"], bounded["scope"]["sites_returned"]), (45, 40))
        self.assertTrue(bounded["scope"]["truncated"])
        whole = discover_logging(snapshot, max_sites=None, max_per_file=None, max_hints=None)
        self.assertEqual((whole["scope"]["sites_returned"], whole["scope"]["truncated"]), (45, False))
        self.assertEqual(whole["scope"]["omitted_by_path"], {})
        self.assertEqual(set(whole["scope"]), set(bounded["scope"]))
        for bad in (dict(max_sites=True), dict(max_hints=0), dict(max_per_file=-1), dict(max_call_lines=None)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                discover_logging(snapshot, **bad)
        plan = plan_review(snapshot, None, SYSTEM)
        self.assertEqual(len(plan.logging_inventory["sites"]), 45)


if __name__ == "__main__":
    unittest.main()
