"""Callable bodies anchored only by context findings are primary work (direct-atomic-validator/7).

Tests-first. Synthetic Python and Java with arbitrary names (the fixtures of test_declaration_completion plus small own
files); scripted models only. A technical finding that is not an action signal (an error check, an identifier) makes
an exactly bound context anchor: a label, not a business decision. Declaration coverage policy context-callables/1
therefore lists every verified context command unit of a callable with a body as context work: it keeps its command
unit, ID, findings and links, is supplied whole on an atomic page (never offered to findings-only triage) and gets an
explicit decision, or stays explicitly deferred or failed under the same request, input, answer and one-repair
limits. Types, fields, abstract methods and candidate units add no such work. A saved inventory and record of the
original policy are still checked under that policy; a current record cannot claim it. Export recomputes the
inventory and refuses changes to it, its aggregates, the policy, the binding or the ledger. None of this shows that a
decision is semantically right or that the code runs.
"""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from process_observability import declaration_index, direct_atomic
from process_observability.code_review import ReviewOptions
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import ExportError, _atomic_accounting, export_onboarding
from test_action_refinement import Code, finding
from test_declaration_completion import (DOCK, FILES, FOUND, HOST, HOST_LINE, ORIGIN, QUESTION, STATION,
                                         CompletionCase, canonical_sha, decide, handlers, sent_units, snapshot)
from test_direct_atomic import AtomicCase, Business, account, atomic_requests, first_attempts

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "context_anchor"
POLICY = "context-callables/1"
CONTEXT_LINE = "Объявления, которые технический разбор отметил только как контекст"
INSPECT = finding("error", "Закрытый метод отклоняет пустой груз.", DOCK, 11, 15, "Dock.inspect")
STAMP = finding("identifier", "Код посылки переводится в верхний регистр.", STATION, 14, 14, "_stamp")
# Operations decide() creates for the FILES fixture when Gate.open_gate realizes nothing.
BASELINE = ["Отправить посылку", "Архивировать посылку", "Уведомить узел об архивации", "Принять сообщение очереди",
            "Принять груз", "Сохранить груз", "Принять сообщение о грузе"]
TICKETS = "pkg/gate/tickets.py"
TICKETS_TEXT = (
    "def verify_ticket(ticket):\n"            # 1   finding of kind error: context
    "    if ticket.void:\n"                   # 2
    "        raise ValueError(\"VOID\")\n"    # 3
    "    return ticket\n"                     # 4
    "\n\n"
    "class Board:\n"                          # 7
    "    def board_pass(self, ticket):\n"     # 8   candidate
    "        verify_ticket(ticket)\n"         # 9
    "        ticket.boarded = True\n"
    "        ticket.save()\n"
    "        return ticket\n"                 # 12
    "\n"
    "    def refund_pass(self, ticket):\n"    # 14  candidate
    "        verify_ticket(ticket)\n"         # 15
    "        ticket.refunded = True\n"
    "        ticket.save()\n"
    "        return ticket\n")                # 18
TICKETS_FOUND = [finding("entry_point", "Посадка по билету.", TICKETS, 8, 12, "Board.board_pass"),
                 finding("entry_point", "Возврат билета.", TICKETS, 14, 18, "Board.refund_pass"),
                 finding("error", "Аннулированный билет отклоняется.", TICKETS, 2, 3, "verify_ticket")]


def deciding(symbol, item):
    """decide() for every other unit; the unit symbol gets item(number) as its accounting item."""
    def reply(brief, attempt):
        own = [unit for unit in brief["units"] if unit["symbol"] == symbol]
        result = decide({**brief, "units": [unit for unit in brief["units"] if unit["symbol"] != symbol]}, attempt)
        result["unit_accounting"] += [item(unit["number"]) for unit in own]
        return result
    return reply


def ticket_reply(brief, attempt, *, drop=False):
    """Each command realizes its own terminal action in one working group (proposed with the first action, reused by
    number later); the shared check supports the commands on its page (or is dropped)."""
    known = [item["number"] for item in brief["working_groups"]]
    numbers = {unit["symbol"]: unit["number"] for unit in brief["units"]}
    operations, accounting, new_groups = [], [], []
    for unit in brief["units"]:
        if unit["symbol"] == "verify_ticket":
            if not drop:
                accounting.append(account(unit["number"], "supporting", reason="Проверка билета перед действием",
                                          serves=[numbers[name] for name in ("Board.board_pass", "Board.refund_pass")
                                                  if name in numbers]))
            continue
        if not known and not new_groups:
            new_groups.append({"name": "Приём", "contract": "Билеты проверяются и обрабатываются."})
        operations.append({"name": "Действие " + unit["symbol"], "description": None, "actor": None,
                           "kind": "operation", "group": known[0] if known else 1, "initiation": "api_call",
                           "initiation_basis": None, "exit": "terminal", "exit_question": None, "refs": []})
        accounting.append(account(unit["number"], "realized", [len(operations)]))
    return {"operations": operations, "new_groups": new_groups, "unit_accounting": accounting, "event_mappings": [],
            "operation_transitions": [], "unknowns": []}


def original_policy(catalog, index):
    """The inventory an earlier run (direct-atomic-validator/5 or /6) computed: no context units."""
    return ORIGINAL(catalog, index, None)


ORIGINAL = declaration_index.declaration_coverage


def context_line(bundle):
    [line] = [text for text in bundle["process_map"]["unknowns"] if text.startswith(CONTEXT_LINE)]
    return line


def host_line(bundle):
    [line] = [text for text in bundle["process_map"]["unknowns"] if text.startswith(HOST_LINE)]
    return line


class IndependentRedReproduction(AtomicCase):
    """The attached independent reproduction (review-fixtures/test_context_body_reproduction.py), unchanged."""

    def test_python_context_only_callable_body_is_explicit_primary_work(self):
        b = Business(**handlers())
        bundle, _ = self.run_case(b, files=FILES, found=FOUND, triage='off', refine_max_requests=64)
        supplied = {u['symbol'] for c in first_attempts(b) for u in c['brief']['units']}
        self.assertIn('Gate.open_gate', supplied,
                      'A context/error anchor is not a business decision; its body must reach primary review')

    def test_java_private_context_only_body_is_not_silently_treated_as_decided(self):
        found = [*FOUND, finding('error', 'Private method may refuse invalid input', DOCK, 11, 15, 'Dock.inspect')]
        b = Business(**handlers())
        bundle, _ = self.run_case(b, files=FILES, found=found, triage='off', refine_max_requests=64)
        units = [u for c in first_attempts(b) for u in c['brief']['units'] if u['path'] == DOCK and u['start_line'] == 11]
        self.assertTrue(units, 'Private Java body with only a context anchor must receive an explicit semantic decision')


class ContextCase(CompletionCase):
    def ran(self, found=(), files=None, business=None, **settings):
        self.business = business or Business(**handlers())
        bundle, self.out = self.run_case(self.business, files={**FILES, **(files or {})},
                                         found=[*copy.deepcopy(FOUND), *found], **settings)
        return bundle

    def contexts(self):
        """(qualname, first line) -> context view of the saved coverage document."""
        return {(view["qualname"], view["start_line"]): view
                for view in self.saved("declaration_coverage.json")["context_units"]}

    def rewrite(self, bundle):
        from process_observability.cli import _write_bundle
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        (self.out / "atomic_map.json").write_text(json.dumps(bundle["atomic_map"], ensure_ascii=False),
                                                  encoding="utf-8")

    def refused(self, label, documents):
        """Write the documents into the run, expect a refused export without output, restore the saved bytes."""
        saved = {name: (self.out / name).read_bytes() for name in documents}
        self.refusals = getattr(self, "refusals", 0) + 1
        target = self.out.parent / f"refused-{self.refusals}"
        try:
            for name, data in documents.items():
                (self.out / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.subTest(tamper=label):
                with self.assertRaises(ExportError):
                    export_onboarding(self.out, target)
                self.assertFalse(target.exists())
        finally:
            for name, data in saved.items():
                (self.out / name).write_bytes(data)

    def record_refused(self, label, edit):
        bundle = json.loads((self.out / "bundle.json").read_text(encoding="utf-8"))
        edit(bundle["atomic_map"])
        self.refused(label, {"bundle.json": bundle, "atomic_map.json": bundle["atomic_map"]})

    def document_refused(self, label, edit):
        data = json.loads((self.out / "declaration_coverage.json").read_text(encoding="utf-8"))
        edit(data)
        data["coverage_sha256"] = canonical_sha(data, "coverage_sha256")
        self.refused(label, {"declaration_coverage.json": data})


class CoveragePolicyTests(unittest.TestCase):
    FLOW = ("def entry(item):\n"                 # 1  candidate, plus a context anchor inside it
            "    _check(item)\n"                 # 2
            "    return item\n"                  # 3
            "\n\n"
            "def _check(item):\n"                # 6  private helper, context only
            "    if item is None:\n"             # 7
            "        raise ValueError(\"EMPTY\")\n"  # 8
            "    return item\n"                  # 9
            "\n\n"
            "class Board:\n"                     # 12 a type, context only
            "    def pin(self, note):\n"         # 13 context only
            "        return note\n")             # 14
    RACK = "\n".join((
        "package svc.rack;",                                     # 1
        "",
        "public class Rack {",                                   # 3
        '    private final String tag = "r";',                   # 4  a field: no declaration of its own
        "",
        "    private void weigh(String box) {",                  # 6  overload 1, context only
        "        if (box.isEmpty()) {",
        '            throw new IllegalStateException("empty");',
        "        }",
        "    }",                                                 # 10
        "",
        "    private void weigh(int grams) {",                   # 12 overload 2, no anchor
        "        if (grams < 0) {",
        '            throw new IllegalStateException("negative");',
        "        }",
        "    }",                                                 # 16
        "",
        "    interface Probe {",                                 # 18
        "        void ping();",                                  # 19 abstract, context only
        "    }",
        "}")) + "\n"

    def build(self):
        built = snapshot({"lib/flow.py": self.FLOW, "svc/rack/Rack.java": self.RACK})
        index = declaration_index.build_declaration_index(built)

        def anchor(identifier, path, first, last, symbol, role):
            return {"id": identifier, "role": role, "path": path, "start_line": first, "end_line": last,
                    "symbol": symbol, "file_sha256": built.files[path].sha256, "finding_ids": [identifier + "-f"],
                    "findings": [{"id": identifier + "-f"}]}
        flow, rack = "lib/flow.py", "svc/rack/Rack.java"
        catalog = {"snapshot_id": built.snapshot_id, "catalog_sha256": "b" * 64,
                   "candidates": [anchor("c-entry", flow, 1, 3, "entry", "candidate")],
                   "context_anchors": [anchor("x-entry", flow, 2, 2, "entry", "context"),
                                       anchor("x-check", flow, 7, 8, "_check", "context"),
                                       anchor("x-board", flow, 12, 12, "Board", "context"),
                                       anchor("x-pin", flow, 14, 14, "Board.pin", "context"),
                                       anchor("x-tag", rack, 4, 4, "Rack", "context"),
                                       anchor("x-weigh", rack, 7, 9, "Rack.weigh", "context"),
                                       anchor("x-ping", rack, 19, 19, "Rack.Probe.ping", "context")]}
        return index, catalog

    def test_only_context_callables_with_a_body_become_work_under_their_own_command_unit(self):
        index, catalog = self.build()
        before = copy.deepcopy((index, catalog))
        units = declaration_index.build_command_units(catalog, index)
        fresh = declaration_index.declaration_coverage(catalog, index)
        self.assertEqual((index, catalog), before)  # inputs are not changed
        self.assertEqual(declaration_index.build_command_units(catalog, index), units)  # nor are the command units
        self.assertEqual(fresh["policy"], POLICY)
        self.assertEqual(declaration_index.declaration_coverage(catalog, index, POLICY), fresh)  # the default
        by_declaration = {unit["declaration_id"]: unit for unit in units["units"] if unit["declaration_id"]}
        views = [(view["qualname"], view["kind"], view["start_line"]) for view in fresh["context_units"]]
        self.assertEqual(views, [("_check", "function", 6), ("Board.pin", "method", 13), ("Rack.weigh", "method", 6)])
        for view in fresh["context_units"]:
            unit = by_declaration[view["declaration_id"]]
            self.assertEqual((view["id"], view["origin"], unit["id"], unit["role"], unit["status"], unit["candidate_ids"]),
                             (view["declaration_id"], CONTEXT, view["declaration_id"], "context", "verified", []))
            self.assertEqual(view["finding_ids"], unit["finding_ids"])
            for key in ("candidate_ids", "members", "member_ids", "disposition", "operation_ids"):
                self.assertNotIn(key, view)  # a work view: no candidate, member or decision of its own
        # The candidate with a context fragment inside stays one candidate unit; types, the field and the bodiless
        # method stay context command units without work.
        [entry] = [unit for unit in units["units"] if unit["declaration"] and unit["declaration"]["qualname"] == "entry"]
        self.assertEqual((entry["role"], sorted(entry["member_ids"])), ("candidate", ["c-entry", "x-entry"]))
        quiet = {unit["declaration"]["qualname"] for unit in units["units"] if unit["role"] == "context"
                 and unit["status"] == "verified" and unit["id"] not in {view["id"] for view in fresh["context_units"]}}
        self.assertEqual(quiet, {"Board", "Rack", "Rack.Probe.ping"})
        # Links of the command unit are kept: the candidate still calls the helper's own unit.
        [check] = [unit for unit in units["units"] if unit["id"] == fresh["context_units"][0]["id"]]
        self.assertEqual(check["navigation"]["caller_unit_ids"], [entry["id"]])
        # The other overload stays host work with its own ID; nothing is listed twice.
        self.assertEqual([(unit["declaration"]["qualname"], unit["declaration"]["start_line"])
                          for unit in fresh["primary_units"]], [("Rack.weigh", 12)])
        self.assertTrue(fresh["primary_units"][0]["id"].startswith("hdecl-"))
        self.assertFalse({view["declaration_id"] for view in fresh["context_units"]}
                         & {unit["declaration_id"] for unit in fresh["primary_units"]})
        self.assertEqual((fresh["summary"]["context_units"], fresh["summary"]["primary_units"]), (3, 1))
        self.assertRegex(fresh["basis"], r"context label decides nothing")

    def test_the_original_inventory_stays_checkable_and_every_policy_change_is_refused(self):
        index, catalog = self.build()
        fresh = declaration_index.declaration_coverage(catalog, index)
        original = declaration_index.declaration_coverage(catalog, index, None)
        self.assertFalse({"policy", "context_units"} & set(original))
        self.assertNotIn("context_units", original["summary"])
        for key in ("primary_units", "unrepresented", "unindexed_files", "open_scope", "units_sha256"):
            self.assertEqual(original[key], fresh[key], key)
        self.assertNotEqual(original["coverage_sha256"], fresh["coverage_sha256"])
        self.assertTrue(fresh["basis"].startswith(original["basis"]))
        for document in (fresh, original):  # each is checked under the policy it records
            self.assertIs(declaration_index.check_coverage(document, catalog=catalog, index=index), document)
        with self.assertRaises(declaration_index.DeclarationError) as caught:
            declaration_index.declaration_coverage(catalog, index, "context-callables/2")
        self.assertEqual(caught.exception.code, "coverage_mismatch")

        def dropped(data):
            data["context_units"].pop()
            data["summary"]["context_units"] -= 1

        def foreign(data):
            data["context_units"][0]["finding_ids"] = ["x-foreign-f"]

        def recount(data):
            data["summary"]["context_units"] += 1

        def other_policy(data):
            data["policy"] = "context-callables/2"

        def unlabelled(data):  # the new inventory under the original label
            data.pop("policy")

        def null_policy(data):
            data["policy"] = None

        def labelled(data):  # the original inventory under the new label
            data["policy"] = POLICY
        for source, change in ((fresh, dropped), (fresh, foreign), (fresh, recount), (fresh, other_policy),
                               (fresh, unlabelled), (fresh, null_policy), (original, labelled)):
            with self.subTest(change=change.__name__):
                data = copy.deepcopy(source)
                change(data)
                data["coverage_sha256"] = canonical_sha(data, "coverage_sha256")
                with self.assertRaises(declaration_index.DeclarationError) as caught:
                    declaration_index.check_coverage(data, catalog=catalog, index=index)
                self.assertEqual(caught.exception.code, "coverage_mismatch")


class ContextWorkTests(ContextCase):
    def test_python_and_java_context_bodies_keep_their_unit_and_get_exactly_one_decision(self):
        bundle = self.ran(found=[INSPECT, STAMP])
        units, catalog, index = (self.saved(name) for name in ("command_units.json", "action_catalog.json",
                                                                "declaration_index.json"))
        contexts = self.contexts()
        self.assertEqual(sorted(contexts), [("Dock.inspect", 11), ("Gate.open_gate", 34), ("_stamp", 13)])
        # command_units.json is exactly the one built from the saved catalog and index: nothing cloned or added.
        declaration_index.check_units(units, catalog=catalog, index=index)
        self.assertEqual(units["candidate_count"], len(catalog["candidates"]))
        command = {unit["id"]: unit for unit in units["units"]}
        for view in contexts.values():
            unit = command[view["id"]]
            self.assertEqual((unit["id"], unit["role"], unit["status"], unit["candidate_ids"]),
                             (view["declaration_id"], "context", "verified", []))
            self.assertTrue(unit["finding_ids"])
        # Overloads stay apart: the anchored private one keeps its command unit, the other stays host work.
        hosts = self.host_by_line()
        self.assertEqual(sorted(hosts), sorted(key for key in HOST if key not in (("Dock.inspect", 11), ("_stamp", 13))))
        self.assertTrue(hosts[("Dock.inspect", 17)]["id"].startswith("hdecl-"))
        self.assertNotEqual(contexts[("Dock.inspect", 11)]["id"], hosts[("Dock.inspect", 17)]["id"])
        record = bundle["atomic_map"]
        self.assertEqual((record["status"], record["status_reasons"]), ("complete", []))
        self.assertEqual(set(record["candidates"]), {item["id"] for item in catalog["candidates"]})
        self.assertEqual(len(record["unit_states"]), units["accounting"]["candidate_units"] + len(hosts) + len(contexts))
        for key, view in contexts.items():
            state = record["unit_states"][view["id"]]
            self.assertEqual((state["origin"], state["declaration_id"], state["candidate_ids"], state["finding_ids"],
                              state["state"]),
                             (CONTEXT, view["id"], [], command[view["id"]]["finding_ids"], "accounted"), key)
            self.assertEqual(sum(view["id"] in item["unit_ids"] for item in atomic_requests(bundle)), 1, key)
        inspect, stamp = (record["unit_states"][contexts[key]["id"]] for key in (("Dock.inspect", 11), ("_stamp", 13)))
        self.assertEqual((inspect["disposition"], inspect["question"]), ("unresolved", QUESTION))
        self.assertEqual(stamp["disposition"], "supporting")
        self.assertEqual(record["unit_states"][hosts[("Dock.inspect", 17)]["id"]]["disposition"], "outside_process")
        self.assertEqual((record["summary"]["context_primary_units"], record["summary"]["declaration_units"],
                          record["summary"]["decisions"]), (3, len(HOST) - 2, len(record["unit_states"])))
        self.assertEqual(record["declaration_coverage"]["context_units"], 3)
        # Each body is supplied whole on its page with the findings that anchored it, and nothing is invented.
        known = {item for anchor in [*catalog["candidates"], *catalog["context_anchors"]] for item in anchor["finding_ids"]}
        self.assertLessEqual({item for state in record["unit_states"].values() for item in state["finding_ids"]}, known)
        rows = {(row["symbol"], row["start_line"]): (call, row) for call in first_attempts(self.business)
                for row in call["brief"]["units"]}
        for key, view in contexts.items():
            call, row = rows[key]
            self.assertEqual((row["origin"], row["declaration_supplied"], row["status"]), (CONTEXT, "whole", "verified"))
            self.assertEqual(len(row["findings"]), len(command[view["id"]]["finding_ids"]))
            self.assertNotIn("declaration_anchor", row)
            excerpts = {item["number"]: item for item in call["brief"]["excerpts"]}
            self.assertTrue(any(excerpts[number]["path"] == view["path"]
                                and excerpts[number]["start_line"] <= view["start_line"]
                                and view["end_line"] <= excerpts[number]["end_line"] for number in row["excerpts"]), key)
        # The private Python helper is decided on its caller's page.
        self.assertIs(rows[("_stamp", 13)][0], rows[("dispatch_parcel", 6)][0])
        self.assertEqual(rows[("_stamp", 13)][1]["role"], "supporting")
        self.assertRegex(direct_atomic.ATOMIC_SYSTEM, r"origin context_anchor .*context label decides nothing")
        _atomic_accounting(bundle, ProcessMap.from_dict(bundle["process_map"]), record["catalog"]["snapshot_id"])

    def test_abstract_type_field_and_candidate_anchors_add_no_context_work(self):
        bundle = self.ran(found=[finding("identifier", "Метод интерфейса без тела.", DOCK, 36, 36, "Dock.Hook.fire"),
                                 finding("identifier", "Поле метки груза.", DOCK, 4, 4, "Dock"),
                                 finding("identifier", "Класс шлюза.", STATION, 33, 33, "Gate"),
                                 finding("error", "Отправка проверяет посылку.", STATION, 8, 8, "dispatch_parcel")])
        units, record = self.saved("command_units.json"), bundle["atomic_map"]
        self.assertEqual(sorted(self.contexts()), [("Gate.open_gate", 34)])
        self.assertEqual(sorted(self.host_by_line()), sorted(HOST))
        context = [unit for unit in units["units"] if unit["role"] == "context"]
        self.assertGreater(len(context), 1)  # the extra anchors are context units that add no work
        promoted = {view["id"] for view in self.contexts().values()}
        for unit in context:
            self.assertEqual(unit["id"] in record["unit_states"], unit["id"] in promoted, unit["id"])
        for state in record["unit_states"].values():
            if state.get("origin") == CONTEXT:
                self.assertEqual(state["symbol"], "Gate.open_gate")
        # A context finding inside a candidate declaration joins the one candidate unit, never a second unit.
        [dispatch] = [unit for unit in units["units"]
                      if unit["declaration"] and unit["declaration"]["qualname"] == "dispatch_parcel"]
        self.assertEqual((dispatch["role"], len(dispatch["finding_ids"])), ("candidate", 2))
        self.assertNotIn("origin", record["unit_states"][dispatch["id"]])
        self.assertEqual(sum(op["name"] == "Отправить посылку" for op in bundle["process_map"]["operations"]), 1)
        self.assertEqual(record["status"], "complete")

    def test_a_context_body_may_realize_nothing_and_no_operation_is_invented(self):
        items = {"supporting": lambda number: account(number, "supporting", reason="Проверка для вызывающего кода"),
                 "excluded": lambda number: account(number, "excluded", exclusion="technical_infrastructure",
                                                    reason="Только техническая проверка входа"),
                 "outside_process": lambda number: account(number, "outside_process",
                                                           reason="Проверка не относится к процессу"),
                 "unresolved": lambda number: account(number, "unresolved",
                                                      question="Какое действие защищает эта проверка?")}
        for disposition, item in items.items():
            with self.subTest(disposition=disposition):
                bundle = self.ran(business=Business(**handlers(map_atomic=deciding("Gate.open_gate", item))))
                record = bundle["atomic_map"]
                [gate] = [key for key, state in record["unit_states"].items() if state.get("origin") == CONTEXT]
                state = record["unit_states"][gate]
                self.assertEqual((state["state"], state["disposition"], state["operation_ids"]),
                                 ("accounted", disposition, []))
                self.assertEqual(sorted(op["name"] for op in bundle["process_map"]["operations"]), sorted(BASELINE))
                self.assertFalse([op for op in record["operations"] if gate in op["unit_ids"]])
                self.assertEqual(record["status"], "complete")

    def test_triage_never_offers_context_work_and_cannot_exclude_it(self):
        def triage(brief, attempt):  # the code-led triage shape has no stage hint: no stage exists before code
            return {"units": [{"unit": unit["number"], "decision": "outside_process",
                               "reason": "Описание находки не относится к процессу", "question": None}
                              for unit in brief["units"]], "unknowns": []}
        bundle = self.ran(business=Business(**handlers(map_triage=triage)), triage="on")
        offered = [unit["symbol"] for call in self.business.calls if call["task"] == "TASK: map-triage"
                   for unit in call["brief"]["units"]]
        self.assertEqual(offered, ["dispatch_parcel"])
        [gate] = self.contexts().values()
        state = bundle["atomic_map"]["unit_states"][gate["id"]]
        self.assertEqual((state["state"], state["disposition"]), ("accounted", "realized"))
        self.assertTrue(any(row["symbol"] == "Gate.open_gate" for call in first_attempts(self.business)
                            for row in call["brief"]["units"]))

    def test_a_shared_context_row_is_not_a_decision(self):
        business = Business(**handlers(map_atomic=ticket_reply))
        bundle, self.out = self.run_case(business, files={TICKETS: TICKETS_TEXT}, found=TICKETS_FOUND,
                                         atomic_page_units=1)
        pages = first_attempts(business)
        self.assertEqual([[row["symbol"] for row in call["brief"]["units"]] for call in pages],
                         [["Board.board_pass", "verify_ticket"], ["Board.refund_pass"]])
        [shared] = [row for row in pages[1]["brief"]["shared_context"] if row["symbol"] == "verify_ticket"]
        self.assertEqual(shared["symbol"], "verify_ticket")
        self.assertNotIn("origin", shared)  # context rows keep their earlier shape
        record = bundle["atomic_map"]
        [helper] = [key for key, state in record["unit_states"].items() if state.get("origin") == CONTEXT]
        first, second = atomic_requests(bundle)
        self.assertEqual((record["unit_states"][helper]["state"], record["unit_states"][helper]["request_id"],
                          record["unit_states"][helper]["disposition"]), ("accounted", first["id"], "supporting"))
        self.assertEqual((helper in first["unit_ids"], helper in second["unit_ids"],
                          helper in second["shared_context_unit_ids"]), (True, False, True))
        self.assertEqual((record["summary"]["units"], record["summary"]["decisions"],
                          record["summary"]["context_appearances"]), (3, 3, 1))
        # When the page that holds the helper is rejected, showing it later as context decides nothing.
        business = Business(**handlers(map_atomic=lambda brief, attempt: ticket_reply(brief, attempt, drop=True)))
        bundle, self.out = self.run_case(business, files={TICKETS: TICKETS_TEXT}, found=TICKETS_FOUND,
                                         atomic_page_units=1)
        record = bundle["atomic_map"]
        failed, accepted = atomic_requests(bundle)
        self.assertEqual((failed["outcome"], accepted["outcome"]), ("rejected_after_repair", "accepted"))
        self.assertIn(helper, accepted["shared_context_unit_ids"])
        self.assertEqual((record["unit_states"][helper]["state"], record["unit_states"][helper]["reason"]),
                         ("request_failed", "request_rejected_after_repair"))
        self.assertEqual(record["status"], "partial")
        line = context_line(bundle)
        for part in ("найдено 1;", "(включая неудачные) — 1;", "решение получено — 0;",
                     "без решения из-за ошибки запроса — 1."):
            self.assertIn(part, line)


class ContextLimitTests(ContextCase):
    def test_a_long_context_body_is_supplied_whole_not_by_its_finding_ranges(self):
        path = "pkg/relay/ledger.py"
        body = "".join(f"    entry.field_{step:03d} = rows[{step}]\n" for step in range(130))
        text = "def fill_ledger(entry, rows):\n    if not rows:\n        raise ValueError(\"EMPTY\")\n" + body \
            + "    return entry\n"
        self.ran(files={path: text}, found=[finding("error", "Пустые строки отклоняются.", path, 2, 3, "fill_ledger")])
        [view] = [view for view in self.contexts().values() if view["qualname"] == "fill_ledger"]
        self.assertGreater(view["end_line"] - view["start_line"] + 1, direct_atomic.DECLARATION_LINES)
        [(call, row)] = [(call, row) for call in first_attempts(self.business) for row in call["brief"]["units"]
                         if row["symbol"] == "fill_ledger"]
        self.assertEqual(row["declaration_supplied"], "whole")
        excerpts = {item["number"]: item for item in call["brief"]["excerpts"]}
        self.assertTrue(any(excerpts[number]["path"] == path and excerpts[number]["start_line"] <= view["start_line"]
                            and view["end_line"] <= excerpts[number]["end_line"] for number in row["excerpts"]))
        self.assertIn("entry.field_129", call["user"])

    def test_an_oversized_context_body_is_deferred_never_cut_and_never_counted_as_sent(self):
        path = "pkg/relay/bulky.py"
        body = "".join("    item.part_%03d = '%s'\n" % (step, "Ш" * 800) for step in range(150))
        text = "def bulky_check(item):\n    if item is None:\n        raise ValueError(\"EMPTY\")\n" + body \
            + "    return item\n"
        bundle = self.ran(files={path: text}, found=[finding("error", "Пустой элемент отклоняется.", path, 2, 3,
                                                             "bulky_check")],
                          default_bounds=True, options=ReviewOptions(request_chars=400000))
        self.assertGreater(len(text.encode("utf-8")), 80000)  # precondition: above the default hard input bound
        [view] = [view for view in self.contexts().values() if view["qualname"] == "bulky_check"]
        record = bundle["atomic_map"]
        self.assertEqual((record["unit_states"][view["id"]]["state"], record["unit_states"][view["id"]]["reason"]),
                         ("deferred", "unit_exceeds_request_budget"))
        self.assertNotIn(view["id"], sent_units(bundle))
        for call in self.business.calls:
            self.assertNotIn("Ш" * 150, call["user"])
        line = context_line(bundle)
        self.assertIn("найдено 2;", line)
        self.assertIn("отложено — 1;", line)
        self.assertEqual(record["status"], "partial")

    def test_the_request_limit_defers_context_work_explicitly(self):
        bundle = self.ran(refine_max_requests=1, atomic_page_units=3)
        [view] = self.contexts().values()
        state = bundle["atomic_map"]["unit_states"][view["id"]]
        self.assertEqual((state["state"], state["reason"]), ("deferred", "request_limit_reached"))
        line = context_line(bundle)
        for part in ("найдено 1;", "(включая неудачные) — 0;", "из сохранённых ответов — 0;", "решение получено — 0;",
                     "отложено — 1;", "без решения из-за ошибки запроса — 0."):
            self.assertIn(part, line)
        self.assertIn("найдено 11;", host_line(bundle))
        self.assertEqual(bundle["atomic_map"]["status"], "partial")

    def test_one_repair_then_an_explicit_failure_covers_context_work(self):
        def gate_page(call):
            return call["task"] == "TASK: map-atomic" and any(row["symbol"] == "Gate.open_gate"
                                                               for row in call["brief"]["units"])
        repaired = self.ran(business=Business(**handlers(
            map_atomic=lambda brief, attempt: decide(brief, attempt, drop="Gate.open_gate", drop_on=(1,)))))
        calls = [call for call in self.business.calls if gate_page(call)]
        self.assertEqual(len(calls), 2)
        [error] = calls[1]["brief"]["validation_feedback"]["errors"]
        self.assertEqual(error["code"], "missing_units")
        [view] = self.contexts().values()
        self.assertEqual(repaired["atomic_map"]["unit_states"][view["id"]]["state"], "accounted")
        failed = self.ran(business=Business(**handlers(
            map_atomic=lambda brief, attempt: decide(brief, attempt, drop="Gate.open_gate"))))
        self.assertEqual(len([call for call in self.business.calls if gate_page(call)]), 2)  # one repair, never more
        state = failed["atomic_map"]["unit_states"][view["id"]]
        self.assertEqual((state["state"], state["reason"]), ("request_failed", "request_rejected_after_repair"))
        self.assertIn("без решения из-за ошибки запроса — 1.", context_line(failed))
        result, document = self.exported(failed, "packet")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(text.startswith("Atomic map partial") for text in document["partial_reasons"]))


class ContextAccountingTests(ContextCase):
    def test_context_work_is_counted_apart_and_never_called_unanchored(self):
        bundle = self.ran()
        record = bundle["atomic_map"]
        self.assertEqual((record["summary"]["declaration_units"], record["summary"]["context_primary_units"],
                          record["summary"]["context_primary_units_by_state"], record["summary"]["units"],
                          record["summary"]["decisions"]), (11, 1, {"accounted": 1}, 13, 13))
        self.assertIn("найдено 11;", host_line(bundle))  # the host line still counts only unanchored declarations
        line = context_line(bundle)
        for part in ("найдено 1;", "(включая неудачные) — 1;", "из сохранённых ответов — 0;", "решение получено — 1;",
                     "отложено — 0;", "без решения из-за ошибки запроса — 0."):
            self.assertIn(part, line)
        self.assertNotIn("без находок", line)
        result, document = self.exported(bundle, "packet")
        self.assertEqual(document["atomic_accounting"]["declaration_coverage"], record["declaration_coverage"])
        page = (Path(result["export"]) / "process.md").read_text(encoding="utf-8")
        self.assertIn("Объявлений без находок технического разбора найдено: 11;", page)
        self.assertIn("Объявлений, отмеченных техническим разбором только как контекст: 1; состояния: accounted — 1.",
                      page)


class LegacyAndIdentityTests(ContextCase):
    def legacy_run(self, **settings):
        """A run as direct-atomic-validator/6 wrote it: the original inventory, no context descriptor."""
        with patch.object(declaration_index, "declaration_coverage", original_policy), \
                patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/6"):
            bundle = self.ran(**settings)
        bundle["atomic_map"]["settings"]["declaration_completion"].pop("context_units")
        self.rewrite(bundle)
        return bundle

    def test_a_saved_original_policy_run_exports_as_it_was_and_cannot_be_relabelled(self):
        bundle = self.legacy_run()
        record, coverage = bundle["atomic_map"], self.saved("declaration_coverage.json")
        catalog, index = self.saved("action_catalog.json"), self.saved("declaration_index.json")
        self.assertFalse({"policy", "context_units"} & set(coverage))
        self.assertFalse({"policy", "context_units"} & set(record["declaration_coverage"]))
        self.assertFalse({"context_primary_units", "context_primary_units_by_state"} & set(record["summary"]))
        self.assertFalse([state for state in record["unit_states"].values() if state.get("origin") == CONTEXT])
        # Checked under its own policy, never against the larger inventory of a current run.
        declaration_index.check_coverage(coverage, catalog=catalog, index=index)
        self.assertNotEqual(coverage, declaration_index.declaration_coverage(catalog, index))
        saved = (self.out / "declaration_coverage.json").read_bytes()
        result = export_onboarding(self.out, self.out.parent / "legacy-export")
        document = json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(document["atomic_accounting"]["declaration_coverage"], record["declaration_coverage"])
        self.assertEqual((self.out / "declaration_coverage.json").read_bytes(), saved)  # nothing is rewritten
        page = (Path(result["export"]) / "process.md").read_text(encoding="utf-8")
        self.assertNotIn("только как контекст", page)
        # The same command unit was there before: promotion later keeps its ID.
        gate = [unit for unit in self.saved("command_units.json")["units"]
                if unit["declaration"] and unit["declaration"]["qualname"] == "Gate.open_gate"]
        self.assertEqual([(unit["role"], unit["id"]) for unit in gate],
                         [("context", gate[0]["declaration"]["id"])])

        def current_validator(data):
            data["settings"]["validator"] = direct_atomic.VALIDATOR_VERSION

        def claims_policy(data):
            data["settings"]["declaration_completion"]["context_units"] = {"policy": POLICY}

        def binding_claims_policy(data):
            data["declaration_coverage"].update(policy=POLICY, context_units=0)
        for edit in (current_validator, claims_policy, binding_claims_policy):
            self.record_refused(edit.__name__, edit)
        self.document_refused("original inventory labelled with the new policy",
                              lambda data: data.update(policy=POLICY, context_units=[]))
        export_onboarding(self.out, self.out.parent / "legacy-export-again")

    def test_a_current_run_cannot_pass_as_the_original_policy(self):
        bundle = self.ran()
        self.exported(bundle, "packet")
        catalog, index = self.saved("action_catalog.json"), self.saved("declaration_index.json")
        original = declaration_index.declaration_coverage(catalog, index, None)
        legacy_binding = direct_atomic._coverage_binding(original)
        [gate] = self.contexts().values()

        def as_original(data):
            data["declaration_coverage"] = copy.deepcopy(legacy_binding)
            data["settings"]["validator"] = "direct-atomic-validator/6"
            data["settings"]["declaration_completion"].pop("context_units")
            for key in ("context_primary_units", "context_primary_units_by_state"):
                data["summary"].pop(key)

        def without_context_state(data):
            as_original(data)
            data["unit_states"].pop(gate["id"])
            data["summary"]["units"] -= 1
            data["summary"]["decisions"] -= 1
        current = json.loads((self.out / "bundle.json").read_text(encoding="utf-8"))
        for label, edit, relabel in (("binding only", lambda data: data.update(
                                          declaration_coverage=copy.deepcopy(legacy_binding)), False),
                                     ("relabelled with its context state", as_original, True),
                                     ("relabelled without its context state", without_context_state, True)):
            changed = copy.deepcopy(current)
            edit(changed["atomic_map"])
            documents = {"bundle.json": changed, "atomic_map.json": changed["atomic_map"]}
            if relabel:
                documents["declaration_coverage.json"] = original
            self.refused(label, documents)
        export_onboarding(self.out, self.out.parent / "packet-again")

    def test_business_identity_is_new_while_the_technical_review_is_reused(self):
        self.legacy_run()
        earlier = self.out
        code = Code(copy.deepcopy(FOUND))
        current = self.ran(code=code, resume_from=earlier)
        self.assertEqual(code.calls, [])  # technical review and reduction reused under the same technical requests
        self.assertIn("TASK: map-atomic", self.business.tasks())
        self.assertNotIn("accepted_from_checkpoint", {item["outcome"] for item in atomic_requests(current)})
        names = [{path.name for path in (out / "business_checkpoints" / "map-atomic").glob("*.json")}
                 for out in (earlier, self.out)]
        self.assertTrue(names[0] and names[1] and not names[0] & names[1])
        settings = current["atomic_map"]["settings"]
        self.assertEqual((settings["validator"], settings["declaration_completion"]["context_units"]["policy"]),
                         (direct_atomic.VALIDATOR_VERSION, POLICY))
        self.assertNotEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/6")
        again = self.ran(resume_from=self.out)
        self.assertNotIn("TASK: map-atomic", self.business.tasks())
        self.assertEqual({item["outcome"] for item in atomic_requests(again)}, {"accepted_from_checkpoint"})
        self.assertEqual(again["atomic_map"]["declaration_coverage"], current["atomic_map"]["declaration_coverage"])
        line = context_line(again)
        self.assertIn("(включая неудачные) — 0;", line)
        self.assertIn("из сохранённых ответов — 1;", line)

    def test_the_strict_recorded_demo_replies_are_unchanged(self):
        from test_operation_metadata import R07_DEMO_REPLIES
        data = (ROOT / "examples/synthetic/model-responses.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), R07_DEMO_REPLIES)


class ExportTamperTests(ContextCase):
    def test_export_refuses_changes_to_the_inventory_policy_binding_and_ledger(self):
        bundle = self.ran()
        result, _ = self.exported(bundle, "packet")
        [gate] = self.contexts().values()
        catalog, index = self.saved("action_catalog.json"), self.saved("declaration_index.json")
        candidate = next(key for key, state in bundle["atomic_map"]["unit_states"].items() if "origin" not in state)

        def dropped_view(data):
            data["context_units"].pop()
            data["summary"]["context_units"] -= 1

        def foreign_findings(data):
            data["context_units"][0]["finding_ids"] = ["error-foreign"]

        def moved(data):
            data["context_units"][0]["end_line"] += 1

        def recount(data):
            data["summary"]["context_units"] += 1

        def other_policy(data):
            data["policy"] = "context-callables/2"
        for edit in (dropped_view, foreign_findings, moved, recount, other_policy):
            self.document_refused(edit.__name__, edit)
        self.refused("inventory recomputed under the original policy",
                     {"declaration_coverage.json": declaration_index.declaration_coverage(catalog, index, None)})

        def state(data):
            return data["unit_states"][gate["id"]]

        def drop_state(data):
            data["unit_states"].pop(gate["id"])
            data["summary"]["units"] -= 1
            data["summary"]["decisions"] -= 1

        def rename(data):
            data["unit_states"]["hdecl-" + "1" * 24] = data["unit_states"].pop(gate["id"])

        def drop_binding_policy(data):
            for key in ("policy", "context_units"):
                data["declaration_coverage"].pop(key)

        def drop_summary(data):
            for key in ("context_primary_units", "context_primary_units_by_state"):
                data["summary"].pop(key)
        edits = {"binding count": lambda data: data["declaration_coverage"].update(context_units=2),
                 "binding policy": lambda data: data["declaration_coverage"].update(policy="context-callables/2"),
                 "binding policy dropped": drop_binding_policy,
                 "descriptor dropped": lambda data: data["settings"]["declaration_completion"].pop("context_units"),
                 "descriptor policy": lambda data: data["settings"]["declaration_completion"]["context_units"].update(
                     policy="context-callables/0"),
                 "original validator": lambda data: data["settings"].update(validator="direct-atomic-validator/6"),
                 "context state dropped": drop_state,
                 "context findings dropped": lambda data: state(data).update(finding_ids=[]),
                 "context findings invented": lambda data: state(data).update(
                     finding_ids=[*state(data)["finding_ids"], "error-invented"]),
                 "context as host unit": lambda data: state(data).update(origin=ORIGIN),
                 "context with a candidate": lambda data: state(data).update(candidate_ids=["act-0000000000000000"]),
                 "context of another declaration": lambda data: state(data).update(declaration_id="decl-" + "0" * 24),
                 "context renamed": rename,
                 "summary count": lambda data: data["summary"].update(context_primary_units=0),
                 "summary states": lambda data: data["summary"].update(context_primary_units_by_state={"deferred": 1}),
                 "summary keys dropped": drop_summary,
                 "candidate claims context": lambda data: data["unit_states"][candidate].update(
                     origin=CONTEXT, declaration_id=candidate)}
        for label, edit in edits.items():
            self.record_refused(label, edit)
        self.assertEqual(export_onboarding(self.out, self.out.parent / "packet-again")["status"], result["status"])


if __name__ == "__main__":
    unittest.main()
