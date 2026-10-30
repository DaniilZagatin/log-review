"""Executor tests (R01 S3). Scripted models read the exact brief JSON; nothing touches a network or runs target code.

Synthetic repositories are written to temporary directories, snapshotted once and, where the test says so,
deleted before review, so any later read of the repository would fail.
"""

from collections import Counter, defaultdict
from dataclasses import replace
import _thread
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from process_observability import review_execution
from process_observability.code_graph import build_graph
from process_observability.code_review import ReviewOptions, plan_review, review_repository as planner_entry
from process_observability.llm import LLMError
from process_observability.review_execution import (CODE_REVIEW_SYSTEM, ReviewExecutionError, ReviewResumeError,
                                                    api_request_sha256, review_repository)
from process_observability.review_reduction import reduce_findings, residual
from process_observability.source_tools import snapshot_repository
from process_observability.tracing import TraceRecorder

SYSTEM = "Review the supplied code."
# Small requests keep each padded synthetic file in its own task; tests assert that precondition.
SMALL = ReviewOptions(request_chars=14000, index_share=0.15, parent_share=0.1)
MARKER = re.compile(r"L(\d{3})_(\d{4})Z")


def pad(tag, count):
    return "".join(f"# {tag} filler {number:03d} {'z' * 44}\n" for number in range(count))


def marked(index, code):
    """Every nonblank line carries a unique marker, so foreign text in a prompt is detectable."""
    return "".join(f"{line}  # L{index:03d}_{number:04d}Z {'q' * 24}\n" if line.strip() else "\n"
                   for number, line in enumerate(code, 1))


def module_text(index, body=12):
    code = ["import os", "", "def decorator(fn):", "    return fn", "", "@decorator", f"def handler_{index}(value):"]
    code += [f"    value = value + {step}" for step in range(body)]
    code += ['    log.info("evt.done", case_id=value)', "    return value", "", f"class Thing{index}:",
             "    size = 1", "    def method(self):"]
    code += [f"        self.size += {step}" for step in range(body)]
    return marked(index, code + ["        return self.size", f"RESULT = handler_{index}(1)"])


def line_of(text, needle):
    """Original line number of the first 'N: text' line containing needle."""
    for line in text.splitlines():
        head, _, body = line.partition(": ")
        if needle in body:
            return int(head)
    raise AssertionError(needle)


def reply_for(brief, status="reviewed", kind="branch", requests=(), extra=(), unknowns=()):
    """A contract-conforming reply: one finding on the first line of every owned interval."""
    owned = brief["owned_intervals"]
    findings = [{"id": f"f{index}", "kind": kind, "description": f"Behaviour of {item['symbol']}",
                 "refs": [{"path": item["path"], "start_line": item["start_line"], "end_line": item["start_line"],
                           "symbol": item["symbol"]}]} for index, item in enumerate(owned)]
    return {"findings": findings + list(extra),
            "interval_status": [{"interval_id": item["id"], "status": status, "note": ""} for item in owned],
            "requests": list(requests), "unknowns": list(unknowns)}


class Scripted:
    """Thread-safe scripted worker. respond(brief, call_number) returns a reply or raises."""

    def __init__(self, respond=None, delay=0.0, trace=None):
        self.respond = respond or (lambda brief, number: reply_for(brief))
        self.delay, self.trace = delay, trace
        self.lock = threading.Lock()
        self.calls, self.threads = [], set()
        self.active = self.peak = 0

    def complete_json(self, system, user):
        with self.lock:
            self.calls.append((system, user))
            number = len(self.calls)
            self.threads.add(threading.current_thread().name)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.trace is not None:
                # Emulates the adapter observer, which writes from the worker thread.
                self.trace("api.response", {"request_sha256": hashlib.sha256((system + "\n" + user).encode()).hexdigest()})
            return self.respond(json.loads(user), number)
        finally:
            with self.lock:
                self.active -= 1

    def briefs(self, kind=None):
        found = [json.loads(user) for _, user in self.calls]
        return [brief for brief in found if kind is None or brief["kind"] == kind]


class ExecutionCase(unittest.TestCase):
    def repo(self, files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def scope(self, files):
        root = self.repo(files)
        snapshot = snapshot_repository(root, max_files=None, max_chars=None)
        return root, snapshot, build_graph(snapshot)

    def runs(self):
        # Resolved: macOS /var and /tmp are symlinks, and the checkpoint writer refuses symlinked ancestors.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    @staticmethod
    def forget(root):
        """Delete the repository after the snapshot: any later disk read would fail."""
        shutil.rmtree(root)
        root.mkdir()

    @staticmethod
    def owner(plan, path, line):
        [task_id] = [item["task_id"] for item in plan.intervals
                     if item["path"] == path and item["start_line"] <= line <= item["end_line"]]
        return task_id

    @staticmethod
    def task_row(result, task_id):
        [row] = [row for row in result["ledger"]["tasks"] if row["id"] == task_id]
        return row


class WholeScopeTests(ExecutionCase):
    def test_whole_scope_over_legacy_caps_is_reviewed_in_bounded_disjoint_requests(self):
        files = {f"pkg{index % 5}/mod_{index:03d}.py": module_text(index) for index in range(70)}
        files["big/huge.py"] = marked(900, ["def huge(value):", *[f"    value = value + {step}" for step in range(300)],
                                            "    return value"])
        files.update({"pkg0/__init__.py": "", "pkg0/notes.py": "# only a comment\n",
                      "conf/app.yaml": "".join(f"key_{number}: value_{number}\n" for number in range(30))})
        indexes = {**{index: f"pkg{index % 5}/mod_{index:03d}.py" for index in range(70)}, 900: "big/huge.py"}
        root, snapshot, graph = self.scope(files)
        numbered = sum(len(f"{number}: {line}") + 1 for source in snapshot.files.values()
                       for number, line in enumerate(source.text.splitlines(), 1))
        self.assertGreater(len(snapshot.files), 60)
        self.assertGreater(numbered, 120000)
        self.forget(root)
        options = ReviewOptions(request_chars=20000, workers=3)
        model = Scripted()
        result = review_repository(snapshot, graph, model, system=CODE_REVIEW_SYSTEM, options=options)

        self.assertTrue(result["complete"])
        self.assertTrue(result["coverage_complete"])
        summary = result["ledger"]["summary"]
        self.assertGreater(len(model.calls), 1)
        self.assertEqual(summary["model_calls"], len(model.calls))
        self.assertEqual((summary["base_tasks"], summary["followup_tasks"], summary["repairs"]), (len(model.calls), 0, 0))
        self.assertLessEqual(model.peak, 3)
        for system, user in model.calls:
            self.assertLessEqual(len(system) + len(user), options.request_chars)
            brief = json.loads(user)
            owned = {(item["path"], line) for item in brief["owned_intervals"]
                     for line in range(item["start_line"], item["end_line"] + 1)}
            for match in MARKER.finditer(user):
                self.assertIn((indexes[int(match[1])], int(match[2])), owned)
        by_path = defaultdict(list)
        for row in result["ledger"]["intervals"]:
            by_path[row["path"]].append(row)
        self.assertEqual(set(by_path), set(snapshot.files))
        for path, rows in by_path.items():
            covered = sorted(line for row in rows for line in range(row["start_line"], row["end_line"] + 1))
            self.assertEqual(covered, list(range(1, len(snapshot.files[path].text.splitlines()) + 1)), path)
        states = Counter(row["state"] for row in result["ledger"]["intervals"])
        self.assertEqual(set(states), {"reviewed", "empty"})
        self.assertEqual([row["path"] for row in result["ledger"]["intervals"] if row["state"] == "empty"],
                         ["pkg0/__init__.py"])
        self.assertTrue(all(row["supplied"] and row["accepted"] and row["model_status"] == "reviewed"
                            for row in result["ledger"]["intervals"] if row["state"] == "reviewed"))
        huge = by_path["big/huge.py"]
        self.assertGreater(len(huge), 2)
        self.assertTrue(all(row["segment"] for row in huge))
        self.assertEqual(summary["lines_accepted"], sum(row["line_count"] for row in result["ledger"]["intervals"]))
        self.assertEqual(len(result["findings"]), summary["intervals_accepted"])
        self.assertIn("do not establish semantic", summary["basis"])

    def test_oversized_line_is_never_sent_and_leaves_coverage_incomplete(self):
        files = {"app.py": ("def ok():\n    return 1\n\n" + "VALUE = '" + "CANARYLONG" + "w" * 9000 + "'\n"
                            + "\ndef after():\n    return 2\n")}
        _, snapshot, graph = self.scope(files)
        span = {"type": "span", "path": "app.py", "start_line": 4, "end_line": 4, "reason": "read the value"}
        model = Scripted(lambda brief, number: reply_for(brief, requests=[span]))
        result = review_repository(snapshot, graph, model, system=SYSTEM, options=ReviewOptions(request_chars=14000))
        self.assertTrue(model.calls)
        self.assertFalse(any("CANARYLONG" in user for _, user in model.calls))
        self.assertFalse(result["complete"])
        self.assertIn("unsupported_intervals", result["incomplete_reasons"])
        states = {row["start_line"]: row["state"] for row in result["ledger"]["intervals"]}
        self.assertEqual(states[4], "unsupported")
        self.assertEqual({state for line, state in states.items() if line != 4}, {"reviewed"})
        [request] = result["ledger"]["requests"]
        self.assertEqual((request["status"], request["status_reason"]), ("too_large", "target_includes_unsupported_line"))

    def test_only_empty_files_make_no_calls_and_report_no_reviewable_lines(self):
        _, snapshot, graph = self.scope({"a/__init__.py": "", "b/blank.py": "\n\n"})
        model = Scripted(lambda brief, number: self.fail("no model call is expected"))
        result = review_repository(snapshot, graph, model, system=SYSTEM)
        self.assertEqual(model.calls, [])
        self.assertFalse(result["complete"])
        self.assertIn("no_reviewable_lines", result["incomplete_reasons"])
        self.assertEqual({row["state"] for row in result["ledger"]["intervals"]}, {"empty"})

    def test_planner_module_reexports_the_executor(self):
        _, snapshot, graph = self.scope({"solo.py": "def solo():\n    return 1\n"})
        result = planner_entry(snapshot, graph, Scripted(), system=SYSTEM)
        self.assertTrue(result["complete"])
        self.assertEqual(result["schema"], review_execution.SCHEMA)


class FollowupTests(ExecutionCase):
    WRAPPER = {
        "src/shop/__init__.py": "",
        "src/shop/audit.py": ("import logging\n_logger = logging.getLogger(__name__)\n\n\n"
                              "def log_business(name, **attrs):\n    if not name:\n        raise ValueError('name')\n"
                              "    _logger.info(name, extra=attrs)\n\n\n" + pad("audit", 80)),
        "src/shop/orders.py": (pad("orders", 80) + "from shop.audit import log_business as record\n\n\n"
                               "def complete(order):\n    if order.cancelled:\n        return None\n"
                               "    record(\"order.completed\", order_id=order.id)\n    return order\n"),
        "src/shop/third.py": "def unrelated():\n    return 'THIRD_FILE_MARKER'\n\n\n" + pad("third", 80),
    }

    def test_cross_file_wrapper_followup_reads_wrapper_and_caller_context(self):
        root, snapshot, graph = self.scope(self.WRAPPER)
        self.forget(root)
        plan = plan_review(snapshot, graph, SYSTEM, SMALL)
        orders = snapshot.files["src/shop/orders.py"].text.splitlines()
        call_line = next(number for number, line in enumerate(orders, 1) if 'record("order.completed"' in line)
        # Precondition: the caller's task does not already contain the wrapper text.
        self.assertNotEqual(self.owner(plan, "src/shop/orders.py", call_line), self.owner(plan, "src/shop/audit.py", 8))

        def respond(brief, number):
            if brief["kind"] == "followup":
                target = brief["owned_intervals"][0]
                if "previous_reply_rejected" not in brief:
                    bad = {"id": "bad", "kind": "other", "description": "BAD_CITATION_TEXT",
                           "refs": [{"path": "src/shop/third.py", "start_line": 1, "end_line": 2, "symbol": "unrelated"}]}
                    return reply_for(brief, extra=[bad])
                caller = brief["parent_context"]["ranges"][0]
                logged = line_of(target["text"], "_logger.info")
                finding = {"id": "w1", "kind": "logging", "description": "The wrapper logs the given name with attributes.",
                           "refs": [{"path": target["path"], "start_line": logged, "end_line": logged,
                                     "symbol": "log_business"},
                                    {"path": caller["path"], "start_line": caller["start_line"],
                                     "end_line": caller["end_line"], "symbol": "complete"}]}
                deeper = {"type": "span", "path": "src/shop/orders.py", "start_line": 1, "end_line": 2, "reason": "more"}
                return reply_for(brief, extra=[finding], requests=[deeper])
            rows = [row for row in brief["navigation"]["rows"] if row["t"] == "call" and row.get("target_wrapper_candidate")]
            if not rows:
                return reply_for(brief)
            [row] = rows
            found = {"id": "call", "kind": "integration", "description": "complete() records order.completed",
                     "refs": [{"path": row["path"], "start_line": row["line"], "end_line": row["line"], "symbol": "complete"}]}
            request = {"type": "symbol", "symbol_id": row["target_id"], "reason": "What does record() log?",
                       "finding_ids": ["call"]}
            return reply_for(brief, extra=[found], requests=[request])

        model = Scripted(respond)
        result = review_repository(snapshot, graph, model, system=SYSTEM, options=replace(SMALL, workers=1), plan=plan)

        [first, repair] = model.briefs("followup")
        self.assertIn("_logger.info(name, extra=attrs)", first["owned_intervals"][0]["text"])
        self.assertEqual(first["owned_intervals"][0]["symbol"], "log_business")
        [caller] = first["parent_context"]["ranges"]
        self.assertIn('record("order.completed"', caller["text"])
        self.assertEqual(first["parent_context"]["questions"][0]["basis"], "graph_symbol")
        self.assertEqual(repair["previous_reply_rejected"], "ref_outside_supplied")
        followup_users = [user for _, user in model.calls if json.loads(user)["kind"] == "followup"]
        self.assertFalse(any("THIRD_FILE_MARKER" in user or "BAD_CITATION_TEXT" in user for user in followup_users))
        for brief in model.briefs("base"):
            if any(item["path"] == "src/shop/orders.py" for item in brief["owned_intervals"]):
                self.assertNotIn("_logger.info(name", json.dumps(brief))

        [call] = [item for item in result["findings"] if item["description"].startswith("complete() records")]
        [integrated] = [item for item in result["findings"] if item["description"].startswith("The wrapper logs")]
        self.assertEqual((integrated["integration"], integrated["parent_finding_ids"], integrated["followup_basis"]),
                         (True, [call["id"]], "graph_symbol"))
        self.assertEqual({ref["path"] for ref in integrated["refs"]}, {"src/shop/audit.py", "src/shop/orders.py"})
        [followup] = [row for row in result["ledger"]["tasks"] if row["kind"] == "followup"]
        self.assertEqual((followup["state"], followup["attempts"], followup["rejections"]),
                         ("completed", 2, ["ref_outside_supplied"]))
        statuses = {(row["task_kind"], row["type"]): row for row in result["ledger"]["requests"]}
        base_request = statuses[("base", "symbol")]
        self.assertEqual((base_request["status"], base_request["targets"][0]["followup_task_id"]),
                         ("dispatched", followup["id"]))
        self.assertEqual((statuses[("followup", "span")]["status"], statuses[("followup", "span")]["status_reason"]),
                         ("depth_limit", "one_followup_generation"))
        self.assertTrue(result["complete"])

    def test_ambiguous_receiver_stays_unresolved_and_topic_literal_is_literal_only(self):
        files = {
            "billing/invoices.py": ("class Orders:\n    def save(self):\n        return 1\n\n\n"
                                    "class Invoices:\n    def save(self):\n        return 2\n\n\n" + pad("billing", 80)),
            "flow/finish.py": pad("flow", 80) + "def finish(obj):\n    obj.save()\n    publish(\"order.done\", obj)\n",
            "workers/consumer.py": pad("workers", 80) + "def on_done(message):\n    subscribe(\"order.done\")\n    return message\n",
        }
        root, snapshot, graph = self.scope(files)
        self.forget(root)
        data = graph.to_dict()
        orders_save = [node["id"] for node in data["nodes"] if node["qualname"] == "Orders.save"][0]
        on_done = [node["id"] for node in data["nodes"] if node["qualname"] == "on_done"][0]
        self.assertEqual(graph.incoming(on_done), [])
        plan = plan_review(snapshot, graph, SYSTEM, SMALL)
        finish_task = self.owner(plan, "flow/finish.py", 82)
        self.assertNotIn(finish_task, {self.owner(plan, "billing/invoices.py", 2), self.owner(plan, "workers/consumer.py", 82)})
        [task] = [item for item in plan.tasks if item.id == finish_task]
        self.assertIn("order.done", task.exposed_literals)
        self.assertNotIn(orders_save, task.exposed_symbol_ids)

        receiver_rows = []

        def respond(brief, number):
            if brief["kind"] == "base" and any(item["path"] == "flow/finish.py" for item in brief["owned_intervals"]):
                receiver_rows.extend(row for row in brief["navigation"]["rows"]
                                     if row["t"] == "call" and row["callee"] == "obj.save")
                return reply_for(brief, requests=[
                    {"type": "symbol", "symbol_id": orders_save, "reason": "Which save runs?"},
                    {"type": "literal", "literal": "order.done", "reason": "Who else uses this topic?"}])
            return reply_for(brief)

        model = Scripted(respond)
        result = review_repository(snapshot, graph, model, system=SYSTEM, options=SMALL, plan=plan)
        [row] = receiver_rows
        self.assertEqual(row["status"], "unresolved_receiver")
        self.assertNotIn("target_id", row)
        requests = {row["type"]: row for row in result["ledger"]["requests"]}
        self.assertEqual((requests["symbol"]["status"], requests["symbol"]["status_reason"]),
                         ("unknown_symbol", "symbol_id_not_shown_to_requesting_task"))
        literal = requests["literal"]
        self.assertEqual(literal["status"], "dispatched")
        self.assertEqual([(target["path"], target["symbol"], target["basis"]) for target in literal["targets"]],
                         [("workers/consumer.py", "on_done", "literal_only")])
        [brief] = model.briefs("followup")
        self.assertEqual(brief["parent_context"]["questions"][0]["basis"], "literal_only")
        self.assertTrue(any("literal_only" in line for line in brief["instructions"]))
        [found] = [item for item in result["findings"] if item.get("integration")]
        self.assertEqual(found["followup_basis"], "literal_only")
        self.assertFalse(any(call.get("target_id") == on_done for call in data["calls"]))

    def test_request_statuses_caps_merges_depth_and_scope_are_deterministic(self):
        files = {
            "lib/helpers.py": ("def target_a():\n    return 'a'\n\n\ndef target_b():\n    return 'b'\n\n\n"
                               + pad("lib", 80)),
            "app/one.py": (pad("one", 80) + "from lib.helpers import target_a, target_b\n\n\n"
                           "def one():\n    target_a()\n    target_b()\n"),
            "app/two.py": pad("two", 80) + "from lib.helpers import target_a\n\n\ndef two():\n    target_a()\n",
            "app/settings.py": 'password = "hunter2canary"\n',
            ".env": "TOKEN=envcanary\n",
        }
        root, snapshot, graph = self.scope(files)
        self.forget(root)
        self.assertNotIn(".env", snapshot.files)
        nodes = {node["qualname"]: node["id"] for node in graph.to_dict()["nodes"]}

        def respond(brief, number):
            paths = {item["path"] for item in brief["owned_intervals"]}
            if brief["kind"] == "followup":
                return reply_for(brief, requests=[{"type": "symbol", "symbol_id": nodes["one"], "reason": "deeper"}])
            if "app/one.py" in paths:
                return reply_for(brief, requests=[
                    {"type": "symbol", "symbol_id": nodes["target_a"], "reason": "a?", "finding_ids": ["f0"]},
                    {"type": "symbol", "symbol_id": nodes["target_b"], "reason": "b?"},
                    {"type": "span", "path": "../outside.py", "start_line": 1, "end_line": 1, "reason": "x"},
                    {"type": "span", "path": ".env", "start_line": 1, "end_line": 1, "reason": "x"},
                    {"type": "literal", "literal": "never.shown", "reason": "x"},
                    {"type": "symbol", "symbol_id": "sym-bogus", "reason": "x"}])
            if "app/two.py" in paths:
                return reply_for(brief, requests=[
                    {"type": "symbol", "symbol_id": nodes["target_a"], "reason": "a again?", "finding_ids": ["f0"]},
                    {"type": "span", "path": "app/one.py", "start_line": 1, "end_line": 85, "reason": "x"}])
            return reply_for(brief)

        options = replace(SMALL, max_followups=1, workers=1)
        plan = plan_review(snapshot, graph, SYSTEM, options)
        one_task, two_task = self.owner(plan, "app/one.py", 84), self.owner(plan, "app/two.py", 84)
        self.assertEqual(len({one_task, two_task, self.owner(plan, "lib/helpers.py", 1)}), 3)
        model = Scripted(respond)
        result = review_repository(snapshot, graph, model, system=SYSTEM, options=options, plan=plan)
        rows = {(row["task_id"], row["index"]): row for row in result["ledger"]["requests"]}
        expected = {(one_task, 0): "dispatched", (one_task, 1): "cap_reached", (one_task, 2): "out_of_scope",
                    (one_task, 3): "excluded", (one_task, 4): "invalid", (one_task, 5): "unknown_symbol",
                    (two_task, 0): "merged", (two_task, 1): "too_large"}
        self.assertEqual({key: rows[key]["status"] for key in expected}, expected)
        [followup] = [row for row in result["ledger"]["tasks"] if row["kind"] == "followup"]
        self.assertEqual(followup["followup"]["request_ids"], [rows[(one_task, 0)]["id"], rows[(two_task, 0)]["id"]])
        self.assertEqual(followup["followup"]["parent_task_ids"], sorted([one_task, two_task]))
        [brief] = model.briefs("followup")
        self.assertEqual(len(brief["parent_context"]["questions"]), 2)
        self.assertIn("return 'a'", brief["owned_intervals"][0]["text"])
        [child] = [row for row in result["ledger"]["requests"] if row["task_kind"] == "followup"]
        self.assertEqual(child["status"], "depth_limit")
        self.assertEqual(result["ledger"]["summary"]["request_debt"], 7)
        for _, user in model.calls:
            self.assertNotIn("envcanary", user)
            self.assertNotIn("hunter2canary", user)

        again = review_repository(snapshot, graph, Scripted(respond), system=SYSTEM, options=replace(options, workers=3))
        self.assertEqual(again["run_fingerprint"], result["run_fingerprint"])
        self.assertEqual(again["findings"], result["findings"])
        self.assertEqual(again["ledger"]["requests"], result["ledger"]["requests"])
        self.assertEqual([row["id"] for row in again["ledger"]["tasks"]], [row["id"] for row in result["ledger"]["tasks"]])


class ValidationTests(ExecutionCase):
    def test_shape_repair_explains_empty_arrays_without_accepting_the_incomplete_reply(self):
        _, snapshot, graph = self.scope({'one.py': 'def one():\n    return 1\n'})

        def respond(brief, number):
            reply = reply_for(brief)
            del reply['requests']
            if 'previous_reply_rejected' in brief:
                self.assertEqual(brief['previous_reply_rejected'], 'shape')
                contract = brief['repair_contract']
                self.assertEqual(set(contract['required_top_level_arrays']),
                                 {'findings', 'interval_status', 'requests', 'unknowns'})
                for key in contract['required_top_level_arrays']:
                    reply.setdefault(key, [])
            return reply

        model = Scripted(respond)
        result = review_repository(snapshot, graph, model, system=CODE_REVIEW_SYSTEM)
        self.assertTrue(result['complete'])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['ledger']['summary']['repairs'], 1)

    BIG = ReviewOptions(request_chars=20000, index_share=0, parent_share=0.5)

    @staticmethod
    def lines(tag, count):
        return "".join(f"# {tag} line {number:03d} {'y' * 52}\n" for number in range(1, count + 1))

    def setUp(self):
        _, self.snapshot, self.graph = self.scope({"big.py": self.lines("big", 150), "zz/other.py": self.lines("other", 150)})
        self.plan = plan_review(self.snapshot, self.graph, SYSTEM, self.BIG)
        [self.big] = [task for task in self.plan.tasks if any(
            self.plan.interval_map()[item]["path"] == "big.py" for item in task.interval_ids)]
        first, second = (self.plan.interval_map()[item] for item in self.big.interval_ids)
        # Precondition: one task owns two adjacent intervals, so a citation could bridge them.
        self.assertEqual((first["path"], second["path"], first["end_line"] + 1), ("big.py", "big.py", second["start_line"]))

    @staticmethod
    def owns_big(brief):
        return any(item["path"] == "big.py" for item in brief["owned_intervals"])

    @staticmethod
    def broken(brief, variant):
        reply = reply_for(brief)
        first, second = brief["owned_intervals"][:2]
        finding = reply["findings"][0]
        ref = finding["refs"][0]
        finding["description"] = "REJECTED_TEXT " + finding["description"]
        if variant == "wrong_path":
            ref["path"] = "missing.py"
        elif variant == "beyond_file":
            ref.update(start_line=151, end_line=151)
        elif variant == "too_many_lines":
            ref.update(start_line=1, end_line=1001)
        elif variant == "other_task_file":
            ref.update(path="zz/other.py", start_line=1, end_line=1)
        elif variant == "across_adjacent":
            # Touching owned intervals with no missing line: accepted since the contiguous-cover check.
            ref.update(start_line=first["end_line"] - 1, end_line=second["start_line"] + 1)
        elif variant == "ref_verified":
            ref["verified"] = True
        elif variant == "finding_verified":
            finding["verified"] = True
        elif variant == "missing_status":
            reply["interval_status"].pop()
        elif variant == "duplicate_id":
            reply["findings"][1]["id"] = finding["id"]
        elif variant == "bad_kind":
            finding["kind"] = "business_rule"
        elif variant == "long_description":
            finding["description"] = "REJECTED_TEXT" + "x" * 1200
        elif variant == "unlinked_request":
            reply["requests"] = [{"type": "span", "path": "big.py", "start_line": 1, "end_line": 2, "reason": "x",
                                  "finding_ids": ["nope"]}]
        return reply

    def test_bad_replies_are_rejected_atomically_and_repaired_once(self):
        expected = {"wrong_path": "ref_outside_supplied", "beyond_file": "ref_outside_supplied",
                    "too_many_lines": "ref_range", "other_task_file": "ref_outside_supplied",
                    "ref_verified": "ref_fields",
                    "finding_verified": "finding_fields", "missing_status": "interval_status",
                    "duplicate_id": "finding_id", "bad_kind": "finding_kind", "long_description": "description",
                    "unlinked_request": "request_finding_ids"}
        for variant, category in expected.items():
            with self.subTest(variant=variant):
                seen = []

                def respond(brief, number):
                    if self.owns_big(brief) and not seen:
                        seen.append(number)
                        return self.broken(brief, variant)
                    return reply_for(brief)

                model = Scripted(respond)
                result = review_repository(self.snapshot, self.graph, model, system=SYSTEM, options=self.BIG,
                                           plan=self.plan)
                row = self.task_row(result, self.big.id)
                self.assertEqual((row["state"], row["attempts"], row["rejections"]), ("completed", 2, [category]))
                repairs = [json.loads(user) for _, user in model.calls if "previous_reply_rejected" in json.loads(user)]
                self.assertEqual([brief["previous_reply_rejected"] for brief in repairs], [category])
                self.assertFalse(any("REJECTED_TEXT" in user for _, user in model.calls))
                self.assertFalse(any("REJECTED_TEXT" in item["description"] for item in result["findings"]))
                self.assertTrue(result["complete"])
                for system, user in model.calls:
                    self.assertLessEqual(len(system) + len(user), self.BIG.request_chars)

    def test_second_rejection_fails_only_that_task(self):
        model = Scripted(lambda brief, number: self.broken(brief, "wrong_path") if self.owns_big(brief) else reply_for(brief))
        result = review_repository(self.snapshot, self.graph, model, system=SYSTEM, options=self.BIG, plan=self.plan)
        row = self.task_row(result, self.big.id)
        self.assertEqual((row["state"], row["category"], row["attempts"]), ("failed", "invalid_reply", 2))
        self.assertEqual(len(model.calls), 3)
        self.assertFalse(result["complete"])
        self.assertIn("base_tasks_failed", result["incomplete_reasons"])
        self.assertEqual({item["path"] for item in result["ledger"]["intervals"] if item["state"] == "failed"}, {"big.py"})
        self.assertEqual({ref["path"] for item in result["findings"] for ref in item["refs"]}, {"zz/other.py"})
        [error] = result["errors"]
        self.assertEqual((error["task_id"], error["rejections"]), (self.big.id, ["ref_outside_supplied"] * 2))

    def test_long_ref_repair_names_exact_field_span_and_expected_limit_without_rejected_text(self):
        events = []

        def respond(brief, number):
            if self.owns_big(brief) and "previous_reply_rejected" not in brief:
                raw = self.broken(brief, "too_many_lines")
                raw["findings"][0]["description"] = "REJECTED_SECRET_DESCRIPTION"
                return raw
            if self.owns_big(brief):
                self.assertEqual(brief["validation_feedback"], {
                    "code": "ref_range", "path": "findings[0].refs[0]",
                    "expected": "end_line - start_line + 1 <= 1000; cite smaller supplied ranges",
                    "actual_span_lines": 1001})
            return reply_for(brief)

        model = Scripted(respond)
        result = review_repository(self.snapshot, self.graph, model, system=SYSTEM, options=self.BIG,
                                   plan=self.plan, trace=lambda event, data: events.append((event, data)))
        row = self.task_row(result, self.big.id)
        self.assertEqual((row["state"], row["attempts"]), ("completed", 2))
        [detail] = row["rejection_details"]
        self.assertEqual((detail["attempt"], detail["path"], detail["actual_span_lines"]),
                         (1, "findings[0].refs[0]", 1001))
        repairs = [json.loads(user) for _, user in model.calls if "previous_reply_rejected" in json.loads(user)]
        self.assertEqual(len(repairs), 1)
        self.assertNotIn("REJECTED_SECRET_DESCRIPTION", json.dumps(repairs))
        [issue] = [data for event, data in events if event == "review.rejected"]
        self.assertEqual(issue["validation_feedback"], repairs[0]["validation_feedback"])
        for system, user in model.calls:
            self.assertLessEqual(len(system) + len(user), self.BIG.request_chars)

    def test_invalid_ref_bounds_report_field_and_do_not_echo_arbitrary_numbers(self):
        from types import SimpleNamespace
        supplied = SimpleNamespace(supplied=())
        for first, last, field in ((0, 4, "start_line"), (4, 3, "end_line"), (-10**100, 4, "start_line")):
            with self.subTest(first=first), self.assertRaises(review_execution._Rejected) as caught:
                review_execution._refs([{"path": "big.py", "start_line": first, "end_line": last}],
                                       supplied, self.snapshot)
            feedback = caught.exception.feedback
            self.assertEqual(feedback["path"], "refs[0]." + field)
            self.assertEqual(feedback["expected"], "1 <= start_line <= end_line")
            self.assertLess(len(json.dumps(feedback)), 220)

    def test_citation_limit_is_distinct_from_followup_read_budget_and_never_admits_unseen_lines(self):
        from types import SimpleNamespace
        from process_observability.code_review import limits
        _, snapshot, _ = self.scope({"long.py": "value = 1\n" * 1200})
        task = SimpleNamespace(supplied=({"id": "owned", "path": "long.py", "start_line": 1,
                                         "end_line": 630, "symbol": "<module>"},))
        ref = {"path": "long.py", "start_line": 1, "end_line": 630, "symbol": "<module>"}
        [(clean, evidence_id, holders)] = review_execution._refs([ref], task, snapshot)
        self.assertEqual(clean, ref)
        self.assertEqual(holders, ["owned"])
        self.assertEqual(evidence_id, snapshot.evidence(ref, max_lines=1000).id)
        self.assertEqual((limits(self.BIG)["max_ref_lines"], limits(self.BIG)["max_read_lines"]), (1000, 80))
        with self.assertRaises(review_execution._Rejected) as caught:
            review_execution._refs([{**ref, "end_line": 631}], task, snapshot)
        self.assertEqual(caught.exception.category, "ref_outside_supplied")

    def test_ref_across_touching_owned_intervals_is_accepted_with_every_contributing_interval(self):
        first, second = (self.plan.interval_map()[item] for item in self.big.interval_ids)

        def respond(brief, number):
            reply = reply_for(brief)
            if self.owns_big(brief):
                reply["findings"].append({"id": "joined", "kind": "branch", "description": "Joined branch text",
                                          "refs": [{"path": "big.py", "start_line": first["end_line"] - 1,
                                                    "end_line": second["start_line"] + 1, "symbol": first["symbol"]}]})
            return reply

        model = Scripted(respond)
        result = review_repository(self.snapshot, self.graph, model, system=SYSTEM, options=self.BIG, plan=self.plan)
        row = self.task_row(result, self.big.id)
        self.assertEqual((row["state"], row["attempts"], row["rejections"]), ("completed", 1, []))
        self.assertEqual(len(model.calls), len(self.plan.tasks))  # no repair was needed
        [joined] = [item for item in result["findings"] if item["description"] == "Joined branch text"]
        self.assertEqual(joined["refs"], [{"path": "big.py", "start_line": first["end_line"] - 1,
                                           "end_line": second["start_line"] + 1, "symbol": first["symbol"]}])
        self.assertEqual(joined["interval_ids"], [first["id"], second["id"]])
        self.assertEqual(joined["ref_interval_ids"], [[first["id"], second["id"]]])
        self.assertEqual(joined["ref_check"], "frozen_range_covered_by_contiguous_supplied_text")
        self.assertEqual(joined["evidence_ids"], [self.snapshot.evidence(joined["refs"][0]).id])
        # Findings inside one interval keep their earlier record exactly.
        for item in result["findings"]:
            if item is not joined:
                self.assertEqual((item["ref_check"], len(item["interval_ids"])), ("frozen_range_inside_supplied_text", 1))
                self.assertNotIn("ref_interval_ids", item)

    def test_ref_cover_rejects_a_missing_line_another_path_and_long_refs(self):
        from types import SimpleNamespace

        def task(*ranges):
            return SimpleNamespace(supplied=tuple(
                {"path": path, "start_line": first, "end_line": last, "symbol": f"s{first}", "id": f"{path}:{first}-{last}",
                 "role": "owned"} for path, first, last in ranges))

        def refs(path, first, last, symbol="<module>"):
            return [{"path": path, "start_line": first, "end_line": last, "symbol": symbol}]

        gap = task(("big.py", 1, 10), ("big.py", 12, 20), ("zz/other.py", 11, 11))
        halves = task(("big.py", 1, 50), ("big.py", 51, 100))
        for label, (cited, supplied, category) in {
                "line 11 missing": (refs("big.py", 9, 13), gap, "ref_outside_supplied"),
                "another path supplies the gap": (refs("big.py", 10, 12), gap, "ref_outside_supplied"),
                "path not supplied": (refs("zz/other.py", 1, 2), halves, "ref_outside_supplied"),
                "covered but 1001 lines": (refs("big.py", 1, 1001),
                    task(("big.py", 1, 500), ("big.py", 501, 1001)), "ref_range")}.items():
            with self.subTest(label), self.assertRaises(review_execution._Rejected) as caught:
                review_execution._refs(cited, supplied, self.snapshot)
            self.assertEqual(caught.exception.category, category)
        overlapping = task(("big.py", 1, 10), ("big.py", 8, 20), ("zz/other.py", 1, 20))
        [(clean, evidence, holders)] = review_execution._refs(refs("big.py", 5, 15), overlapping, self.snapshot)
        self.assertEqual(holders, ["big.py:1-10", "big.py:8-20"])
        self.assertEqual(evidence, self.snapshot.evidence(clean).id)
        # A ref inside one supplied range keeps that single holder even when another range also overlaps it.
        [(_, _, holders)] = review_execution._refs(refs("big.py", 2, 3), task(("big.py", 1, 10), ("big.py", 2, 5)),
                                                   self.snapshot)
        self.assertEqual(holders, ["big.py:1-10"])
        # Without a symbol the label comes from the range holding the first cited line.
        [(clean, _, _)] = review_execution._refs([{"path": "big.py", "start_line": 9, "end_line": 12}],
                                                 task(("big.py", 1, 10), ("big.py", 11, 20)), self.snapshot)
        self.assertEqual(clean["symbol"], "s1")

    def test_partial_and_not_understood_are_accepted_but_not_complete(self):
        def respond(brief, number):
            reply = reply_for(brief)
            if self.owns_big(brief):
                reply["interval_status"][0].update(status="partial", note="Branch table continues elsewhere")
                reply["interval_status"][1]["status"] = "not_understood"
            return reply

        result = review_repository(self.snapshot, self.graph, Scripted(respond), system=SYSTEM, options=self.BIG,
                                   plan=self.plan)
        states = Counter(row["state"] for row in result["ledger"]["intervals"] if row["path"] == "big.py")
        self.assertEqual(states, Counter({"partial": 1, "not_understood": 1}))
        self.assertTrue(all(row["supplied"] and row["accepted"] for row in result["ledger"]["intervals"]))
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["incomplete_reasons"], ["intervals_not_reviewed_by_worker"])
        flagged = [item for item in result["unknowns"] if item["origin"] == "interval_status"]
        self.assertEqual(sorted(item["model_status"] for item in flagged), ["not_understood", "partial"])
        self.assertTrue(any("Branch table continues elsewhere" in item["description"] for item in flagged))

    def test_json_repair_once_and_transport_failures_are_never_retried(self):
        cases = {
            "adapter_invalid_json": ([LLMError("bad", category="invalid_json")], "completed", 2, ["not_json"]),
            "text_not_json": (["not json at all"], "completed", 2, ["not_json"]),
            "invalid_json_twice": ([LLMError("bad", category="invalid_json")] * 2, "failed", 2, ["not_json"] * 2),
            "http": ([LLMError("HTTP 503", category="http", status=503)], "failed", 1, []),
            "connection": ([LLMError("down", category="connection")], "failed", 1, []),
            "tls": ([LLMError("tls", category="tls")], "failed", 1, []),
            "credentials": ([LLMError("key", category="credentials")], "failed", 1, []),
            "invalid_response": ([LLMError("truncated", category="invalid_response")], "failed", 1, []),
            "runtime": ([RuntimeError("boom")], "failed", 1, []),
        }
        _, snapshot, graph = self.scope({"solo.py": "def solo():\n    return 1\n"})
        for name, (script, state, calls, rejections) in cases.items():
            with self.subTest(name=name):
                def respond(brief, number, script=script):
                    step = script[number - 1] if number <= len(script) else None
                    if isinstance(step, BaseException):
                        raise step
                    return step if step is not None else reply_for(brief)

                model = Scripted(respond)
                result = review_repository(snapshot, graph, model, system=SYSTEM)
                [row] = result["ledger"]["tasks"]
                self.assertEqual((row["state"], row["attempts"], row["rejections"]), (state, calls, rejections))
                self.assertEqual(len(model.calls), calls)
                if name == "http":
                    self.assertEqual((row["category"], row["status_code"]), ("http", 503))
                if name == "runtime":
                    self.assertEqual((row["category"], row["error_type"]), ("model_error", "RuntimeError"))
                if calls == 2:
                    self.assertEqual(json.loads(model.calls[1][1])["previous_reply_rejected"], "not_json")
                    # Migration (technical reply recovery): every repairable rejection now also names its exact
                    # location; for a reply without answer JSON that is the whole reply ("$"). Nothing else is added.
                    self.assertEqual(json.loads(model.calls[1][1]).keys() - json.loads(model.calls[0][1]).keys(),
                                     {"previous_reply_rejected", "repair_contract", "validation_feedback"})
                    self.assertEqual(json.loads(model.calls[1][1])["validation_feedback"], {
                        "code": "not_json", "path": "$",
                        "expected": "one JSON object with exactly the required top-level arrays; no markdown, no prose"})
                    contract = json.loads(model.calls[1][1])["repair_contract"]
                    self.assertEqual(set(contract), {"required_top_level_arrays", "empty_array_policy", "ref_symbol_policy"})
                    self.assertEqual(set(contract["required_top_level_arrays"]), set(review_execution.REPLY_KEYS))


class ConcurrencyAndResumeTests(ExecutionCase):
    def files(self, count):
        return {f"m{index:02d}/f.py": pad(f"m{index}", 80) + f"def run_{index}():\n    return {index}\n"
                for index in range(count)}

    def test_bounded_workers_coordinator_only_writes_and_ordered_trace(self):
        _, snapshot, graph = self.scope(self.files(9))
        runs = self.runs()
        trace = TraceRecorder(runs)
        model = Scripted(delay=0.01, trace=trace)
        writers = []
        original = review_execution.atomic_json

        def recording(path, value):
            writers.append(threading.current_thread() is threading.main_thread())
            return original(path, value)

        options = replace(SMALL, workers=3)
        with mock.patch.object(review_execution, "atomic_json", recording):
            result = review_repository(snapshot, graph, model, system=SYSTEM, options=options,
                                       checkpoint_dir=runs / "review", trace=trace)
        trace.close()
        self.assertTrue(result["complete"])
        self.assertGreaterEqual(len(model.calls), 6)
        self.assertLessEqual(model.peak, 3)
        self.assertTrue(model.threads and all(name.startswith("review-worker-") for name in model.threads))
        self.assertTrue(writers and all(writers))
        rows = [json.loads(line) for line in (runs / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["seq"] for row in rows], list(range(1, len(rows) + 1)))
        terminal = Counter(row["data"]["task_id"] for row in rows
                           if row["event"] in ("review.task_completed", "review.task_failed", "review.task_interrupted"))
        self.assertEqual(terminal, Counter({row["id"]: 1 for row in result["ledger"]["tasks"]}))
        requests = [row["data"] for row in rows if row["event"] == "model.request"]
        self.assertEqual(len(requests), len(model.calls))
        self.assertEqual({item["api_request_sha256"] for item in requests},
                         {api_request_sha256(system, user) for system, user in model.calls})
        self.assertEqual({row["data"]["request_sha256"] for row in rows if row["event"] == "api.response"},
                         {item["api_request_sha256"] for item in requests})
        [system_event] = [row for row in rows if row["event"] == "review.system"]
        self.assertEqual(system_event["data"]["system"], model.calls[0][0])
        checkpoints = sorted((runs / "review" / "tasks").glob("*.json"))
        self.assertEqual(len(checkpoints), len(model.calls))
        for path in checkpoints:
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "succeeded")
            self.assertNotIn("filler", json.dumps(stored))  # no source text or prompt in task checkpoints

    def test_interrupt_returns_promptly_keeps_completed_work_and_resume_finishes(self):
        _, snapshot, graph = self.scope(self.files(5))
        options = replace(SMALL, workers=1)
        plan = plan_review(snapshot, graph, SYSTEM, options)
        self.assertGreaterEqual(len(plan.tasks), 4)
        runs = self.runs()
        trace = TraceRecorder(runs)
        release, finished = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def respond(brief, number):
            if number == 3:
                _thread.interrupt_main()
                release.wait(30)  # a blocked transport; released below, bounded only as a test safety net
                trace("api.response", {"late": True})
                finished.set()
            return reply_for(brief)

        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            review_repository(snapshot, graph, Scripted(respond), system=SYSTEM, options=options,
                              checkpoint_dir=runs / "first", trace=trace, plan=plan)
        self.assertLess(time.monotonic() - started, 10)
        trace.close()
        lines = (runs / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line)["event"] for line in lines]
        self.assertIn("review.interrupted", events)
        self.assertEqual(events.count("review.task_interrupted"), 1)
        ledger = json.loads((runs / "first" / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(Counter(row["state"] for row in ledger["tasks"]),
                         Counter({"completed": 2, "interrupted": 1, "pending": len(plan.tasks) - 3}))
        self.assertEqual(len(list((runs / "first" / "tasks").glob("*.json"))), 2)
        release.set()
        self.assertTrue(finished.wait(5))
        self.assertEqual((runs / "trace.jsonl").read_text(encoding="utf-8").splitlines(), lines)

        model = Scripted()
        resumed = review_repository(snapshot, graph, model, system=SYSTEM, options=options,
                                    checkpoint_dir=runs / "second", resume_from=runs / "first", plan=plan)
        self.assertEqual(len(model.calls), len(plan.tasks) - 2)
        self.assertEqual(resumed["ledger"]["summary"]["cache_hits"], 2)
        fresh = review_repository(snapshot, graph, Scripted(), system=SYSTEM, options=options, plan=plan)
        self.assertEqual(resumed["findings"], fresh["findings"])
        self.assertTrue(resumed["complete"])

    def test_resume_refuses_mismatches_and_reruns_only_bad_checkpoints(self):
        root, snapshot, graph = self.scope(self.files(3))
        runs = self.runs()
        base = {"system": SYSTEM, "options": SMALL, "model_fingerprint": "glm|model-x", "run_context": {"guidance": "g1"}}
        first = review_repository(snapshot, graph, Scripted(), checkpoint_dir=runs / "a", **base)
        self.assertEqual(first["ledger"]["summary"]["base_tasks"], 3)

        def files_of(directory):
            return {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*") if path.is_file()}

        before = files_of(runs / "a")
        changed_root = self.repo({**self.files(3), "m00/f.py": self.files(3)["m00/f.py"] + "# edited\n"})
        changed = snapshot_repository(changed_root, max_files=None, max_chars=None)
        cases = [({"model_fingerprint": "glm|model-y"}, ["model"]), ({"system": "Different review."}, ["prompt"]),
                 ({"options": replace(SMALL, request_chars=15000)}, ["options"]),
                 ({"run_context": {"guidance": "g2"}}, ["context.guidance"]),
                 ({"snapshot": changed, "graph": build_graph(changed)}, ["snapshot", "graph"])]
        for index, (change, categories) in enumerate(cases):
            with self.subTest(categories=categories):
                arguments = {**base, "snapshot": snapshot, "graph": graph, **change}
                model = Scripted()
                with self.assertRaises(ReviewResumeError) as caught:
                    review_repository(arguments.pop("snapshot"), arguments.pop("graph"), model,
                                      checkpoint_dir=runs / f"refused{index}", resume_from=runs / "a", **arguments)
                self.assertEqual(list(caught.exception.categories), categories)
                self.assertEqual(model.calls, [])
        with self.assertRaises(ReviewExecutionError):
            review_repository(snapshot, graph, Scripted(), checkpoint_dir=runs / "a", resume_from=runs / "a", **base)

        model = Scripted()
        reused = review_repository(snapshot, graph, model, checkpoint_dir=runs / "b", resume_from=runs / "a",
                                   **{**base, "options": replace(SMALL, workers=3)})
        self.assertEqual(model.calls, [])
        self.assertEqual(reused["findings"], first["findings"])
        self.assertEqual(files_of(runs / "a"), before)
        self.assertEqual(json.loads((runs / "b" / "run.json").read_text())["resumed_from"], first["run_fingerprint"])

        tasks = first["ledger"]["tasks"]
        (runs / "a" / "tasks" / f"{tasks[0]['id']}.json").write_text("{not json", encoding="utf-8")
        tampered = runs / "a" / "tasks" / f"{tasks[1]['id']}.json"
        stored = json.loads(tampered.read_text(encoding="utf-8"))
        # A cached reply may not smuggle a citation outside the text its task supplied.
        stored["response"]["findings"][0]["refs"][0].update(path=tasks[2]["supplied_ranges"][0]["path"], start_line=1, end_line=1)
        tampered.write_text(json.dumps(stored), encoding="utf-8")
        model = Scripted()
        rerun = review_repository(snapshot, graph, model, checkpoint_dir=runs / "c", resume_from=runs / "a", **base)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(rerun["ledger"]["summary"]["checkpoints_not_reused"], {"corrupt": 1, "invalid_reply": 1})
        self.assertEqual(rerun["ledger"]["summary"]["cache_hits"], 1)
        self.assertEqual(rerun["findings"], first["findings"])

    def test_legacy_whole_pack_binding_is_reused_only_when_every_technical_dependency_is_equal(self):
        """Runs bound to the whole guidance pack: reuse needs proof; any real technical difference still refuses."""
        _, snapshot, graph = self.scope(self.files(3))
        runs = self.runs()
        excerpt = "### FACT-LIFECYCLE — technical orientation\nRequest, start and result differ."
        system = SYSTEM + "\n\nSelected technical guidance:\n" + excerpt
        protocol, reducer = "workflow-mvp/2", "r" * 64
        legacy = {"guidance_pack_sha256": "pack-before-map-edit", "workflow_protocol": protocol,
                  "reducer_system_sha256": reducer}
        old = review_repository(snapshot, graph, Scripted(), system=system, options=SMALL, model_fingerprint="glm|x",
                                checkpoint_dir=runs / "legacy", run_context=legacy)
        stage = review_execution.technical_run_context(excerpt, ["FACT-LIFECYCLE"], protocol, reducer)
        base = {"system": system, "options": SMALL, "model_fingerprint": "glm|x", "run_context": stage,
                "technical_guidance": excerpt}

        def files_of(directory):
            return {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*")
                    if path.is_file()}
        before = files_of(runs / "legacy")
        model = Scripted()
        reused = review_repository(snapshot, graph, model, checkpoint_dir=runs / "migrated", resume_from=runs / "legacy",
                                   **base)
        self.assertEqual(model.calls, [])
        self.assertEqual(reused["ledger"]["summary"]["cache_hits"], old["ledger"]["summary"]["base_tasks"])
        self.assertEqual(reused["findings"], old["findings"])
        self.assertNotEqual(reused["run_fingerprint"], old["run_fingerprint"])
        record = json.loads((runs / "migrated" / "run.json").read_text(encoding="utf-8"))
        proof = record["compatibility"]
        self.assertEqual((proof["mode"], proof["previous_run_fingerprint"], proof["previous_guidance_pack_sha256"]),
                         ("legacy-whole-guidance-pack/1", old["run_fingerprint"], "pack-before-map-edit"))
        self.assertIn("prompt", proof["verified_equal"])
        self.assertEqual(reused["ledger"]["summary"]["checkpoint_compatibility"], "legacy-whole-guidance-pack/1")
        self.assertEqual(files_of(runs / "legacy"), before)  # old checkpoints are read, never rewritten
        # New checkpoints carry the new identity; the next resume needs no compatibility path at all.
        stored = {json.loads(path.read_text(encoding="utf-8"))["run_fingerprint"]
                  for path in (runs / "migrated" / "tasks").glob("*.json")}
        self.assertEqual(stored, {reused["run_fingerprint"]})
        model = Scripted()
        chained = review_repository(snapshot, graph, model, checkpoint_dir=runs / "chained",
                                    resume_from=runs / "migrated", **base)
        self.assertEqual((model.calls, chained["compatibility"]), ([], None))

        changed_root = self.repo({**self.files(3), "m00/f.py": self.files(3)["m00/f.py"] + "# edited\n"})
        changed = snapshot_repository(changed_root, max_files=None, max_chars=None)
        other_excerpt = excerpt + " Retry differs."
        negative = {
            "source": ({"snapshot": changed, "graph": build_graph(changed)}, ["snapshot", "graph"]),
            "model": ({"model_fingerprint": "glm|y"}, ["model"]),
            "technical request": ({"system": system + " Changed."}, ["prompt"]),
            "technical guidance text": ({"system": SYSTEM + "\n\nSelected technical guidance:\n" + other_excerpt,
                                         "run_context": review_execution.technical_run_context(
                                             other_excerpt, ["FACT-LIFECYCLE"], protocol, reducer),
                                         "technical_guidance": other_excerpt}, ["prompt"]),
            "options": ({"options": replace(SMALL, request_chars=15000)}, ["options"]),
            "workflow policy": ({"run_context": review_execution.technical_run_context(
                excerpt, ["FACT-LIFECYCLE"], "workflow-mvp/3", reducer)}, ["context.workflow_protocol"]),
            "reducer policy": ({"run_context": review_execution.technical_run_context(
                excerpt, ["FACT-LIFECYCLE"], protocol, "s" * 64)}, ["context.reducer_system_sha256"]),
            "no technical text": ({"technical_guidance": None}, ["context.binding_version"]),
            "unproven hash": ({"run_context": {**stage, "technical_guidance_sha256": "0" * 64}},
                              ["context.binding_version"]),
        }
        for index, (label, (change, expected)) in enumerate(negative.items()):
            with self.subTest(label=label):
                arguments = {**base, "snapshot": snapshot, "graph": graph, **change}
                model = Scripted()
                with self.assertRaises(ReviewResumeError) as caught:
                    review_repository(arguments.pop("snapshot"), arguments.pop("graph"), model,
                                      checkpoint_dir=runs / f"refused{index}", resume_from=runs / "legacy", **arguments)
                for category in expected:
                    self.assertIn(category, caught.exception.categories)
                self.assertEqual(model.calls, [])
        # A tampered legacy record (binding edited, fingerprint kept) or extra legacy context is never trusted.
        for index, edit in enumerate((lambda data: data["binding"]["context"].update(guidance_pack_sha256="other"),
                                      lambda data: data["binding"]["context"].update(extra="x"))):
            with self.subTest(tampered=index):
                copy_dir = runs / f"tampered{index}"
                shutil.copytree(runs / "legacy", copy_dir)
                data = json.loads((copy_dir / "run.json").read_text(encoding="utf-8"))
                edit(data)
                (copy_dir / "run.json").write_text(json.dumps(data), encoding="utf-8")
                model = Scripted()
                with self.assertRaises(ReviewResumeError):
                    review_repository(snapshot, graph, model, checkpoint_dir=runs / f"tampered-out{index}",
                                      resume_from=copy_dir, **base)
                self.assertEqual(model.calls, [])
        with self.assertRaisesRegex(ReviewExecutionError, "contained in the worker system"):
            review_repository(snapshot, graph, Scripted(), checkpoint_dir=runs / "bad-text",
                              **{**base, "technical_guidance": "not in the system"})

    def test_legacy_failed_checkpoints_stay_failed_and_are_not_converted(self):
        _, snapshot, graph = self.scope(self.files(3))
        runs = self.runs()
        excerpt = "### FACT-SOURCE-CATALOG — orientation"
        system = SYSTEM + "\n\n" + excerpt
        legacy = {"guidance_pack_sha256": "pack-a", "workflow_protocol": "workflow-mvp/2", "reducer_system_sha256": "r"}
        failing = Scripted(lambda brief, number: (_ for _ in ()).throw(LLMError("busy", category="http", status=503))
                           if number == 1 else reply_for(brief))
        old = review_repository(snapshot, graph, failing, system=system, options=replace(SMALL, workers=1),
                                model_fingerprint="glm|x", checkpoint_dir=runs / "legacy", run_context=legacy)
        self.assertEqual(len(old["errors"]), 1)
        stage = review_execution.technical_run_context(excerpt, ["FACT-SOURCE-CATALOG"], "workflow-mvp/2", "r")
        refusing = Scripted(lambda brief, number: (_ for _ in ()).throw(LLMError("offline", category="connection")))
        resumed = review_repository(snapshot, graph, refusing, system=system, options=replace(SMALL, workers=1),
                                    model_fingerprint="glm|x", checkpoint_dir=runs / "resumed",
                                    resume_from=runs / "legacy", run_context=stage, technical_guidance=excerpt)
        summary = resumed["ledger"]["summary"]
        self.assertEqual(summary["cache_hits"], old["ledger"]["summary"]["base_tasks"] - 1)
        self.assertEqual(summary["checkpoints_not_reused"], {"previous_failure": 1})
        self.assertEqual(len(resumed["errors"]), 1)  # retried by the ordinary resume rule, still failed: no success
        self.assertFalse(resumed["complete"])

    def test_symlinked_checkpoint_directory_is_refused_before_calls(self):
        _, snapshot, graph = self.scope({"solo.py": "def solo():\n    return 1\n"})
        runs = self.runs()
        (runs / "real").mkdir()
        os.symlink(runs / "real", runs / "link")
        model = Scripted()
        with self.assertRaises(ReviewExecutionError):
            review_repository(snapshot, graph, model, system=SYSTEM, checkpoint_dir=runs / "link" / "review")
        self.assertEqual(model.calls, [])


class ReducerContractTests(ExecutionCase):
    def test_review_output_feeds_reduction_and_rare_branch_stays_in_residual(self):
        files = {"app/flow.py": ("def settle(case):\n    if case.fraud:\n        return 'blocked'\n"
                                 "    return 'paid'\n")}
        _, snapshot, graph = self.scope(files)

        def respond(brief, number):
            item = brief["owned_intervals"][0]
            rare = {"id": "rare", "kind": "branch", "description": "Fraud cases are blocked.",
                    "refs": [{"path": item["path"], "start_line": 2, "end_line": 3, "symbol": "settle"}]}
            usual = {"id": "usual", "kind": "outcome", "description": "Other cases are paid.",
                     "refs": [{"path": item["path"], "start_line": 4, "end_line": 4, "symbol": "settle"}]}
            reply = reply_for(brief, unknowns=["Where fraud flags are set is not visible."])
            reply["findings"] = [rare, usual]
            return reply

        review = review_repository(snapshot, graph, Scripted(respond), system=SYSTEM)
        reduced = reduce_findings(review, graph, None, digest_chars=10**6)
        self.assertTrue(reduced["complete"])
        self.assertEqual(reduced["accounting"]["leaf_count"], 3)
        self.assertEqual(reduced["accounting"]["unknown_count"], 1)
        [rare] = [item for item in review["findings"] if item["description"].startswith("Fraud")]
        [usual] = [item for item in review["findings"] if item["description"].startswith("Other")]
        process_map = {"steps": [{"refs": [usual["refs"][0]]}]}
        left = residual(process_map, reduced["leaves"], snapshot_id=review["snapshot_id"])
        self.assertIn(rare["id"], left)
        self.assertNotIn(usual["id"], left)


if __name__ == "__main__":
    unittest.main()
