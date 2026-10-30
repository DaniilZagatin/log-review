"""Transition support (direct-atomic-validator/10, transition-support/1): structural eligibility, never semantics.

OWN synthetic fixtures only: every Python fixture file starts with a raise, the Java-shaped one with a guard comment
(Java has no top-level statement), and none is imported, compiled or executed. Scripted code and business models read
the exact request JSON; no network, no paid call, no actual target. A proposed transition is active only when the host
recomputes one unit, a control chain of at most two links between units supplied on its page with the call cited, or a
contract cited in both endpoint units; everything else stays an inactive, addressed proposal. Passing these tests shows
host wiring and identity, not that an active order or its condition is semantically right: a real call chain with a
wrong predicate still passes this boundary and stays a question for independent review.
"""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from process_observability import direct_atomic, transition_support
from process_observability.code_review import ReviewOptions
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import (ExportError, _atomic_accounting, _index_calls, _support_sources,
                                                      _transition_support)
from process_observability.tracing import TraceRecorder
from test_action_refinement import Code, finding
from test_code_led_map import (FLOW_EDGES, FLOW_PATH, FLOW_SPEC, GUARD, Business, CodeLedCase, handlers,
                               page_reply)
from test_direct_atomic import atomic_requests
from test_technical_relations import owned_lines

ATOMIC, COMPOSE = "TASK: map-atomic", "TASK: map-compose"
INVENTED = ("cancel_order", 0, "notify_partner", 0, "source_condition", [(29, 30), (35, 35)], None)
CANCEL, PARTNER = "Отменить заказ", "Передать заказ партнёру"

# A queue handoff: the sender names the contract twice (lines 5 and 6), the receiver once (line 10).
QUEUE_PATH = "app/messages.py"
QUEUE = GUARD + (
    "\n\n"                                                     # 2-3
    "def send_parcel(parcel, bus):\n"                          # 4
    '    bus.publish("PARCEL_QUEUE", parcel.id)\n'             # 5
    '    bus.note("PARCEL_QUEUE")\n'                           # 6
    "    return parcel\n"                                      # 7
    "\n"                                                       # 8
    "def receive_parcel(message, bus):\n"                      # 9
    '    parcel = bus.consume("PARCEL_QUEUE")\n'               # 10
    "    parcel.received = True\n"                             # 11
    "    return parcel\n")                                     # 12
QUEUE_SPEC = {"send_parcel": [("Передать посылку", "operation", "Отправка", "continues", None)],
              "receive_parcel": [("Принять посылку", "operation", "Приём", "terminal", None)]}
QUEUE_FOUND = [finding("entry_point", "Передача посылки в очередь.", QUEUE_PATH, 4, 7, "send_parcel"),
               finding("entry_point", "Приём посылки из очереди.", QUEUE_PATH, 9, 12, "receive_parcel")]

# Three real call links in a row: a chain longer than the two-link bound.
STAGES_PATH = "app/stages.py"
STAGES = GUARD + (
    "\n\n"                                                     # 2-3
    "def stage_d(order):\n"                                    # 4
    "    order.done = True\n"                                  # 5
    "    return order\n"                                       # 6
    "\n\n"                                                     # 7-8
    "def stage_c(order):\n"                                    # 9
    "    return stage_d(order)\n"                              # 10
    "\n\n"                                                     # 11-12
    "def stage_b(order):\n"                                    # 13
    "    return stage_c(order)\n"                              # 14
    "\n\n"                                                     # 15-16
    "def stage_a(order):\n"                                    # 17
    "    return stage_b(order)\n")                             # 18
STAGES_FOUND = [finding("entry_point", "Начало обработки.", STAGES_PATH, 17, 18, "stage_a"),
                finding("state_change", "Отметка готовности.", STAGES_PATH, 4, 6, "stage_d")]
STAGES_SPEC = {"stage_a": [("Начать обработку", "operation", "Обработка", "continues", None)],
               "stage_d": [("Отметить готовность", "operation", "Обработка", "terminal", None)]}

# start calls eight checks and then relay; relay calls finish. With the dependency bound of eight, relay (the middle
# of start -> relay -> finish) is never supplied on start's page. Lines: 4-33 checks, 36-37 relay, 40-42 finish,
# 45-55 start.
CHAIN_PATH = "app/relay.py"
CHAIN = (GUARD + "\n\n"
         + "".join(f"def check_{index:02d}(order):\n    return order.flag_{index:02d}\n\n\n" for index in range(8))
         + "def relay(order):\n    return finish(order)\n\n\n"
         + "def finish(order):\n    order.finished = True\n    return order\n\n\n"
         + "def start(order):\n" + "".join(f"    check_{index:02d}(order)\n" for index in range(8))
         + "    relay(order)\n    return order\n")
CHAIN_FOUND = [finding("entry_point", "Начало обработки заказа.", CHAIN_PATH, 45, 55, "start"),
               finding("state_change", "Завершение обработки.", CHAIN_PATH, 40, 42, "finish")]
CHAIN_SPEC = {"start": [("Начать обработку", "operation", "Обработка", "continues", None)],
              "relay": [("Передать на завершение", "operation", "Обработка", "continues", None)],
              "finish": [("Завершить обработку", "operation", "Обработка", "terminal", None)]}

# Code-model relations: an unresolved dispatch through a registry, a resolved callback and a resolved data handoff.
DISPATCH_PATH = "app/dispatch.py"
DISPATCH = GUARD + (
    "\n\n"                                                     # 2-3
    "def reject_order(order):\n"                               # 4
    '    order.status = "rejected"\n'                          # 5
    "    order.save()\n"                                       # 6
    "    return order\n"                                       # 7
    "\n\n"                                                     # 8-9
    "def dispatch_order(order, registry):\n"                   # 10
    "    order.checked = True\n"                               # 11
    "    registry.choose(order.state).handle(order)\n"         # 12
    "    return order\n"                                       # 13
    "\n\n"                                                     # 14-15
    "def schedule_review(order, scheduler):\n"                 # 16
    "    order.review_pending = True\n"                        # 17
    "    scheduler.later(order, on_done=reject_order)\n"       # 18
    "    return order\n"                                       # 19
    "\n\n"                                                     # 20-21
    "def store_note(order, notes):\n"                          # 22
    "    notes[order.pk] = order.note\n"                       # 23
    "    return order\n"                                       # 24
    "\n\n"                                                     # 25-26
    "def read_note(order, notes):\n"                           # 27
    "    order.note = notes.get(order.pk)\n"                   # 28
    "    return order\n")                                      # 29
DISPATCH_FOUND = [finding("state_change", "Заказ отклоняется.", DISPATCH_PATH, 5, 6, "reject_order"),
                  finding("entry_point", "Распределение заказа по реестру.", DISPATCH_PATH, 11, 12, "dispatch_order"),
                  finding("entry_point", "Назначение проверки с обратным вызовом.", DISPATCH_PATH, 17, 18,
                          "schedule_review"),
                  finding("entry_point", "Сохранение примечания.", DISPATCH_PATH, 23, 23, "store_note"),
                  finding("entry_point", "Чтение примечания.", DISPATCH_PATH, 28, 28, "read_note")]
DISPATCH_SPEC = {"reject_order": [("Отклонить заказ", "operation", "Решение", "terminal", None)],
                 # The model claims an end; the unresolved dispatch keeps the continuation addressed anyway.
                 "dispatch_order": [("Распределить заказ", "operation", "Решение", "terminal", None)],
                 "schedule_review": [("Назначить проверку", "operation", "Решение", "continues", None)],
                 "store_note": [("Сохранить примечание", "operation", "Примечания", "continues", None)],
                 "read_note": [("Прочитать примечание", "operation", "Примечания", "terminal", None)]}
DISPATCH_EDGES = [("dispatch_order", 0, "reject_order", 0, "source_call", [(12, 12), (5, 6)], None),
                  ("schedule_review", 0, "reject_order", 0, "source_call", [(18, 18)], None),
                  ("store_note", 0, "read_note", 0, "source_condition", [(23, 23), (28, 28)], None)]
DISPATCH_LABEL = "registry.choose(order.state).handle"


def ref(path, first, last, symbol):
    return {"path": path, "start_line": first, "end_line": last, "symbol": symbol}


DISPATCH_RELATIONS = [
    {"id": "r-dispatch", "kind": "dispatch", "source": ref(DISPATCH_PATH, 12, 12, "dispatch_order"),
     "refs": [ref(DISPATCH_PATH, 12, 12, "dispatch_order")],
     "target": {"symbol_id": None, "ref": None, "label": DISPATCH_LABEL}, "resolution": "unresolved",
     "condition": None, "data": "order"},
    {"id": "r-callback", "kind": "callback", "source": ref(DISPATCH_PATH, 18, 18, "schedule_review"),
     "refs": [ref(DISPATCH_PATH, 18, 18, "schedule_review")],
     "target": {"symbol_id": None, "ref": ref(DISPATCH_PATH, 4, 7, "reject_order"), "label": "reject_order"},
     "resolution": "resolved", "condition": None, "data": None},
    {"id": "r-data", "kind": "data_handoff", "source": ref(DISPATCH_PATH, 23, 23, "store_note"),
     "refs": [ref(DISPATCH_PATH, 23, 23, "store_note")],
     "target": {"symbol_id": None, "ref": ref(DISPATCH_PATH, 27, 29, "read_note"), "label": "notes"},
     "resolution": "resolved", "condition": None, "data": "order.note"}]

# Java has no host call resolution: a link between these methods can only be a code-model relation (model provenance).
JAVA_PATH = "svc/yard/Crane.java"
JAVA = "\n".join((
    "// synthetic fixture: never compile or run",               # 1
    "package svc.yard;",                                       # 2
    "",                                                        # 3
    "public class Crane {",                                    # 4
    "    public void lift(String load) {",                     # 5
    "        check(load);",                                    # 6
    "        stack(load);",                                    # 7
    "    }",                                                   # 8
    "",                                                        # 9
    "    void stack(String load) {",                           # 10
    "        System.out.println(load);",                       # 11
    "    }",                                                   # 12
    "",                                                        # 13
    "    void check(String load) {",                           # 14
    "        System.out.println(load.length());",              # 15
    "    }",                                                   # 16
    "}")) + "\n"
JAVA_FOUND = [finding("entry_point", "Подъём груза.", JAVA_PATH, 5, 8, "Crane.lift"),
              finding("state_change", "Укладка груза.", JAVA_PATH, 10, 12, "Crane.stack")]
JAVA_SPEC = {"Crane.lift": [("Поднять груз", "operation", "Склад", "continues", None)],
             "Crane.stack": [("Уложить груз", "operation", "Склад", "terminal", None)]}
JAVA_EDGES = [("Crane.lift", 0, "Crane.stack", 0, "source_call", [(7, 7)], None)]
JAVA_RELATIONS = [{"id": "r-call", "kind": "call", "source": ref(JAVA_PATH, 7, 7, "Crane.lift"),
                   "refs": [ref(JAVA_PATH, 7, 7, "Crane.lift")],
                   "target": {"symbol_id": None, "ref": ref(JAVA_PATH, 10, 12, "Crane.stack"), "label": "stack"},
                   "resolution": "resolved", "condition": None, "data": None}]


class ProposingCode(Code):
    """The scripted code model, also proposing fixed relations whose lines the brief supplies (relation contract)."""

    def __init__(self, findings, relations):
        super().__init__(findings)
        self.relations = relations

    def complete_json(self, system, user):
        reply = super().complete_json(system, user)
        brief = json.loads(user)
        if "relation_kinds" in brief:
            lines = owned_lines(brief)

            def places(item):
                return [item["source"], *item["refs"], *([item["target"]["ref"]] if item["target"]["ref"] else [])]
            reply["relations"] = [copy.deepcopy(item) for item in self.relations if all(
                (place["path"], line) in lines for place in places(item)
                for line in range(place["start_line"], place["end_line"] + 1))]
        return reply


def support_of(bundle):
    return bundle["atomic_map"]["code_led"]["transition_support"]


def names(bundle):
    return {op["id"]: op["name"] for op in bundle["process_map"]["operations"]}


def routes(bundle):
    named = names(bundle)
    return sorted((named[edge["from_operation"]], named[edge["to_operation"]])
                  for edge in bundle["process_map"]["operation_transitions"])


def proposal(bundle, source, target):
    named = names(bundle)
    [found] = [item for item in support_of(bundle)["proposals"]
               if (named[item["from"]], named[item["to"]]) == (source, target)]
    return found


def gaps(bundle, kind):
    return [gap for gap in bundle["atomic_map"]["code_led"]["gaps"] if gap["kind"] == kind]


def unit_of(bundle, symbol):
    [identifier] = [key for key, state in bundle["atomic_map"]["unit_states"].items() if state["symbol"] == symbol]
    return identifier


def check(bundle):
    record = bundle["atomic_map"]
    _atomic_accounting(bundle, ProcessMap.from_dict(bundle["process_map"]), record["catalog"]["snapshot_id"])


class TransitionCase(CodeLedCase):
    def reproduction(self, basis="source_condition", ranges=((29, 30), (35, 35)), **settings):
        invented = ("cancel_order", 0, "notify_partner", 0, basis, list(ranges), None)
        return self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, [*FLOW_EDGES, invented]))), **settings)

    def run_fixture(self, path, text, found, spec, edges, mutate=None, **settings):
        return self.flow(Business(**handlers(atomic=page_reply(spec, edges, path=path, mutate=mutate))),
                         files={path: text}, found=copy.deepcopy(found), text="", **settings)

    def saved(self):
        """known units, registry rows and declaration-index calls of the saved run, as export reads them."""
        def read(name):
            return json.loads((self.out / name).read_text(encoding="utf-8"))
        known = {unit["id"]: unit for unit in read("command_units.json")["units"]}
        if (self.out / "declaration_coverage.json").exists():
            known.update((unit["id"], unit) for unit in read("declaration_coverage.json")["primary_units"])
        registry = read("technical_relations.json")
        return known, {row["id"]: row for row in [*registry["worker"], *registry["host_static"]]}, _index_calls(
            read("declaration_index.json"))

    def traced(self, business, **settings):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        recorder = TraceRecorder(directory.name)
        bundle = self.flow(business, trace=recorder, **settings)
        shutil.copyfile(recorder.path, self.out / "trace.jsonl")  # the CLI writes its trace into the run directory
        return bundle


class ReproductionTests(TransitionCase):
    def test_an_invented_order_between_independent_commands_is_kept_inactive_and_addressed(self):
        baseline = self.flow()
        baseline_calls = self.business.tasks()
        for basis in ("source_condition", "source_call"):
            with self.subTest(basis=basis):
                bundle = self.reproduction(basis)
                # The two real links stay; the invented one is no operation transition and joins no fragment.
                self.assertEqual(routes(bundle), routes(baseline))
                self.assertEqual(len(routes(bundle)), 2)
                record, named = bundle["atomic_map"], names(bundle)
                frontier = record["code_led"]["frontier"]
                self.assertEqual(len(frontier["fragments"]), 3)
                self.assertTrue({CANCEL, PARTNER} <= {named[key] for key in frontier["entries"]})
                # Kept with its request, raw basis and citations; demoted locally: no repair, no extra model call.
                item = proposal(bundle, CANCEL, PARTNER)
                self.assertEqual((item["state"], item["reason"], item["support"], item["basis"]),
                                 ("inactive", "no_source_chain", None, basis))
                [request] = atomic_requests(bundle)
                self.assertEqual((item["request_id"], item["page"]), (request["id"], request["page"]))
                self.assertEqual(len(item["evidence_ids"]), 2)
                self.assertEqual(item["endpoints"], {"from_unit_ids": [unit_of(bundle, "cancel_order")],
                                                     "to_unit_ids": [unit_of(bundle, "notify_partner")]})
                self.assertEqual(self.business.tasks(), baseline_calls)
                self.assertEqual([attempt["outcome"] for attempt in request["attempts"]], ["accepted"])
                # An addressed host gap names both actions, their units, the proposal and its citations.
                [gap] = gaps(bundle, "unsupported_transition")
                self.assertEqual((gap["operation_ids"], gap["source"]), ([item["from"], item["to"]], "host"))
                self.assertEqual(gap["unit_ids"], [unit_of(bundle, "cancel_order"), unit_of(bundle, "notify_partner")])
                for text in (item["id"], "app/orders.py:28-31", "app/orders.py:34-36", "app/orders.py:29-30"):
                    self.assertIn(text, gap["question"])
                self.assertTrue(any(gap["id"] in line for line in bundle["process_map"]["unknowns"]))
                self.assertEqual((record["status"], record["status_reasons"]), ("partial", ["open_gaps"]))
                # Candidate and unit accounting are exactly those of the baseline run.
                for key in ("decisions", "units", "candidates", "requests", "requests_by_outcome", "dispositions",
                            "operations_added", "mappings_added"):
                    self.assertEqual(record["summary"][key], baseline["atomic_map"]["summary"][key], key)
                self.assertEqual({key: (state["state"], state["disposition"], state["operation_ids"])
                                  for key, state in record["unit_states"].items()},
                                 {key: (state["state"], state["disposition"], state["operation_ids"])
                                  for key, state in baseline["atomic_map"]["unit_states"].items()})
                self.assertEqual({key: record["summary"][key] for key in transition_summary()},
                                 {"transitions_added": 2, "transition_proposals": 3, "transitions_inactive": 1,
                                  "transitions_inactive_by_reason": {"no_source_chain": 1}, "open_control_links": 0})
                check(bundle)

    def test_the_composition_sees_active_orders_and_unsupported_proposals_apart(self):
        bundle = self.reproduction()
        [call] = [item for item in self.business.calls if item["task"] == COMPOSE]
        brief = call["brief"]
        self.assertEqual(len(brief["transitions"]), 2)
        [unsupported] = brief["unsupported_transitions"]
        numbers = {op["number"]: op["name"] for op in brief["operations"]}
        self.assertEqual((numbers[unsupported["from"]], numbers[unsupported["to"]], unsupported["reason"]),
                         (CANCEL, PARTNER, "no_source_chain"))
        self.assertNotIn("condition", unsupported)
        self.assertNotIn("supplied code showed", direct_atomic.COMPOSE_SYSTEM)
        self.assertIn("unsupported_transitions", direct_atomic.COMPOSE_SYSTEM)
        self.assertEqual(len(bundle["process_map"]["operation_transitions"]), 2)

    def test_real_call_quotes_of_other_units_supply_no_ownership(self):
        bundle = self.reproduction("source_call", ((24, 24), (14, 14)))
        self.assertEqual(proposal(bundle, CANCEL, PARTNER)["reason"], "no_source_chain")
        self.assertEqual(len(routes(bundle)), 2)


def transition_summary():
    return ("transitions_added", "transition_proposals", "transitions_inactive", "transitions_inactive_by_reason",
            "open_control_links")


class PositiveSupportTests(TransitionCase):
    def test_caller_to_callee_over_two_links_and_a_return_into_the_caller_stay_active_with_host_provenance(self):
        bundle = self.flow()
        submit, approve, fraud = (unit_of(bundle, name) for name in ("submit_order", "approve_order", "check_fraud"))
        chain = proposal(bundle, "Принять заказ", "Проверить заказ на мошенничество")
        self.assertEqual((chain["state"], chain["support"]["kind"], chain["support"]["unit_ids"]),
                         ("active", "call_chain", [submit, approve, fraud]))
        self.assertEqual([(link["from_unit"], link["to_unit"]) for link in chain["support"]["links"]],
                         [(submit, approve), (approve, fraud)])
        self.assertEqual([link["call_lines"] for link in chain["support"]["links"]],
                         [[[FLOW_PATH, 24, 24]], [[FLOW_PATH, 14, 14]]])
        self.assertTrue(all("navigation" in link["origins"] for link in chain["support"]["links"]))
        self.assertEqual(chain["support"]["provenance"], "host")
        self.assertEqual(chain["support"]["unit_spans"][submit], [[FLOW_PATH, 21, 25]])
        back = proposal(bundle, "Проверить заказ на мошенничество", "Автоматически утвердить заказ")
        # check_fraud returns into approve_order: the one link is traversed from the callee side.
        self.assertEqual((back["state"], back["support"]["unit_ids"]), ("active", [fraud, approve]))
        self.assertEqual([(link["from_unit"], link["to_unit"]) for link in back["support"]["links"]],
                         [(approve, fraud)])
        direct = self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, [
            ("submit_order", 0, "approve_order", 0, "source_call", [(24, 24)], None)]))))
        item = proposal(direct, "Принять заказ", "Автоматически утвердить заказ")
        self.assertEqual((item["state"], len(item["support"]["links"])), ("active", 1))
        check(bundle)
        known, rows, calls = self.saved()
        _support_sources(support_of(direct), known, rows, calls)

    def test_two_actions_of_one_unit_with_a_citation_in_its_own_source_are_active(self):
        spec = {**FLOW_SPEC, "submit_order": [("Принять заказ", "start", "Приём заказов", "continues", None),
                                              ("Записать приём заказа", "operation", "Приём заказов", "continues",
                                               None)]}
        bundle = self.flow(Business(**handlers(atomic=page_reply(spec, [
            ("submit_order", 0, "submit_order", 1, "source_condition", [(22, 23)], None)]))))
        item = proposal(bundle, "Принять заказ", "Записать приём заказа")
        submit = unit_of(bundle, "submit_order")
        self.assertEqual((item["state"], item["support"]["kind"], item["support"]["unit_ids"]),
                         ("active", "same_unit", [submit]))
        # The same claim citing another unit's lines has no witness in the shared unit and stays inactive.
        elsewhere = self.flow(Business(**handlers(atomic=page_reply(spec, [
            ("submit_order", 0, "submit_order", 1, "source_condition", [(35, 35)], None)]))))
        self.assertEqual(proposal(elsewhere, "Принять заказ", "Записать приём заказа")["reason"], "no_source_chain")
        check(bundle)
        check(elsewhere)

    def test_a_contract_cited_in_the_sender_and_in_the_receiver_is_active(self):
        bundle = self.run_fixture(QUEUE_PATH, QUEUE, QUEUE_FOUND, QUEUE_SPEC, [
            ("send_parcel", 0, "receive_parcel", 0, "source_handoff", [(5, 5), (10, 10)], "PARCEL_QUEUE")])
        item = proposal(bundle, "Передать посылку", "Принять посылку")
        send, receive = unit_of(bundle, "send_parcel"), unit_of(bundle, "receive_parcel")
        self.assertEqual((item["state"], item["support"]["kind"], item["support"]["unit_ids"]),
                         ("active", "contract_both_sides", [send, receive]))
        self.assertEqual(item["support"]["unit_spans"], {send: [[QUEUE_PATH, 4, 7]], receive: [[QUEUE_PATH, 9, 12]]})
        self.assertEqual(len(bundle["atomic_map"]["code_led"]["frontier"]["fragments"]), 1)
        check(bundle)
        known, rows, calls = self.saved()
        _support_sources(support_of(bundle), known, rows, calls)

    def test_a_resolved_callback_relation_supports_its_order_and_keeps_its_model_provenance(self):
        bundle = self.run_fixture(DISPATCH_PATH, DISPATCH, DISPATCH_FOUND, DISPATCH_SPEC, DISPATCH_EDGES,
                                  code=ProposingCode(copy.deepcopy(DISPATCH_FOUND), DISPATCH_RELATIONS),
                                  options=ReviewOptions(relations=True))
        known, rows, calls = self.saved()
        [callback] = [row for row in rows.values() if row["origin"] == "worker_proposal" and row["kind"] == "callback"]
        item = proposal(bundle, "Назначить проверку", "Отклонить заказ")
        self.assertEqual(item["state"], "active")
        [link] = item["support"]["links"]
        self.assertIn("worker_proposal", link["origins"])
        self.assertIn(callback["id"], link["relation_ids"])
        self.assertEqual(link["call_lines"], [[DISPATCH_PATH, 18, 18]])
        check(bundle)
        _support_sources(support_of(bundle), known, rows, calls)

    def test_a_java_shaped_order_is_active_only_through_a_resolved_model_relation(self):
        related = self.run_fixture(JAVA_PATH, JAVA, JAVA_FOUND, JAVA_SPEC, JAVA_EDGES,
                                   code=ProposingCode(copy.deepcopy(JAVA_FOUND), JAVA_RELATIONS),
                                   options=ReviewOptions(relations=True))
        item = proposal(related, "Поднять груз", "Уложить груз")
        self.assertEqual((item["state"], item["support"]["provenance"]), ("active", "model_relation"))
        [link] = item["support"]["links"]
        self.assertEqual(link["origins"], ["worker_proposal"])  # never relabelled as host
        known, rows, calls = self.saved()
        _support_sources(support_of(related), known, rows, calls)
        check(related)
        plain = self.run_fixture(JAVA_PATH, JAVA, JAVA_FOUND, JAVA_SPEC, JAVA_EDGES)
        self.assertEqual(proposal(plain, "Поднять груз", "Уложить груз")["reason"], "no_source_chain")
        self.assertEqual(plain["process_map"]["operation_transitions"], [])
        check(plain)

    def test_a_real_chain_with_an_arbitrary_condition_stays_active_as_a_stated_semantic_limit(self):
        def wrong(result, brief, attempt):
            for item in result["operation_transitions"]:
                item["condition"] = "Заказ оплачен картой"  # the code checks the amount, not a card payment
            return result
        bundle = self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, FLOW_EDGES, mutate=wrong))))
        # Structural support is eligibility only: the predicate stays model text for independent semantic review.
        self.assertTrue(all(item["state"] == "active" for item in support_of(bundle)["proposals"]))
        self.assertEqual({edge["condition"] for edge in bundle["process_map"]["operation_transitions"]},
                         {"Заказ оплачен картой"})


class NegativeSupportTests(TransitionCase):
    def test_a_shared_object_field_is_no_link(self):
        bundle = self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, [
            *FLOW_EDGES, ("cancel_order", 0, "approve_order", 0, "source_condition", [(29, 29), (16, 16)], None)]))))
        self.assertEqual(proposal(bundle, CANCEL, "Автоматически утвердить заказ")["reason"], "no_source_chain")
        self.assertEqual(len(routes(bundle)), 2)

    def test_a_real_link_without_a_citation_in_the_calling_unit_is_not_witnessed(self):
        bundle = self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, [
            ("submit_order", 0, "approve_order", 0, "source_call", [(14, 14)], None)]))))
        self.assertEqual(proposal(bundle, "Принять заказ", "Автоматически утвердить заказ")["reason"],
                         "chain_without_cited_call")
        self.assertEqual(bundle["process_map"]["operation_transitions"], [])

    def test_two_contract_citations_on_the_sender_side_do_not_show_the_receiver(self):
        bundle = self.run_fixture(QUEUE_PATH, QUEUE, QUEUE_FOUND, QUEUE_SPEC, [
            ("send_parcel", 0, "receive_parcel", 0, "source_handoff", [(5, 5), (6, 6)], "PARCEL_QUEUE")])
        item = proposal(bundle, "Передать посылку", "Принять посылку")
        self.assertEqual((item["state"], item["reason"], item["contract"]), ("inactive", "handoff_one_side",
                                                                              "PARCEL_QUEUE"))
        self.assertEqual(bundle["process_map"]["operation_transitions"], [])
        self.assertEqual(len(bundle["atomic_map"]["code_led"]["frontier"]["fragments"]), 2)
        self.assertEqual(self.business.tasks().count(ATOMIC), 1)
        check(bundle)

    def test_a_data_handoff_relation_is_never_a_control_link(self):
        bundle = self.run_fixture(DISPATCH_PATH, DISPATCH, DISPATCH_FOUND, DISPATCH_SPEC, DISPATCH_EDGES,
                                  code=ProposingCode(copy.deepcopy(DISPATCH_FOUND), DISPATCH_RELATIONS),
                                  options=ReviewOptions(relations=True))
        _, rows, _ = self.saved()
        self.assertTrue([row for row in rows.values() if row["kind"] == "data_handoff"
                         and row["resolution"] == "resolved"])  # the relation exists: it is just not control
        self.assertEqual(proposal(bundle, "Сохранить примечание", "Прочитать примечание")["reason"],
                         "no_source_chain")
        self.assertNotIn(("Сохранить примечание", "Прочитать примечание"), routes(bundle))

    def test_an_unresolved_dispatch_keeps_an_addressed_open_link_whatever_exit_the_model_claimed(self):
        bundle = self.run_fixture(DISPATCH_PATH, DISPATCH, DISPATCH_FOUND, DISPATCH_SPEC, DISPATCH_EDGES,
                                  code=ProposingCode(copy.deepcopy(DISPATCH_FOUND), DISPATCH_RELATIONS),
                                  options=ReviewOptions(relations=True))
        named = {name: key for key, name in names(bundle).items()}
        self.assertEqual(proposal(bundle, "Распределить заказ", "Отклонить заказ")["reason"], "no_source_chain")
        dispatch = unit_of(bundle, "dispatch_order")
        [link] = [item for item in support_of(bundle)["open_links"] if item["unit_id"] == dispatch]
        self.assertEqual((link["kind"], link["target"], link["where"], link["target_unit_id"]),
                         ("unresolved_relation", DISPATCH_LABEL,
                          {"path": DISPATCH_PATH, "start_line": 12, "end_line": 12}, None))
        [gap] = gaps(bundle, "unresolved_control_link")
        self.assertEqual((gap["operation_ids"], gap["unit_ids"], gap["source"]),
                         ([named["Распределить заказ"]], [dispatch], "host"))
        self.assertIn(DISPATCH_LABEL, gap["question"])
        self.assertIn(f"{DISPATCH_PATH}:12-12", gap["question"])
        ops = {op["id"]: op for op in bundle["atomic_map"]["operations"]}
        self.assertEqual(ops[named["Распределить заказ"]]["exit"], "terminal")
        self.assertFalse(gaps(bundle, "unresolved_handoff"))  # not relabelled as an external receiver
        check(bundle)
        known, rows, calls = self.saved()
        _support_sources(support_of(bundle), known, rows, calls)

    def test_a_chain_longer_than_two_links_is_not_support(self):
        bundle = self.run_fixture(STAGES_PATH, STAGES, STAGES_FOUND, STAGES_SPEC, [
            ("stage_a", 0, "stage_d", 0, "source_call", [(18, 18), (14, 14), (10, 10)], None)])
        self.assertEqual(len(atomic_requests(bundle)), 1)  # all four units on one page
        self.assertEqual(proposal(bundle, "Начать обработку", "Отметить готовность")["reason"], "no_source_chain")
        self.assertEqual(bundle["process_map"]["operation_transitions"], [])
        check(bundle)

    def test_a_middle_unit_not_supplied_leaves_the_order_inactive_and_the_omitted_callee_addressed(self):
        def from_known(result, brief, attempt):  # on relay's page: an order from start, whose code is not supplied
            known = {item["name"]: item["number"] for item in brief["known_operations"]}
            relay = [index for index, op in enumerate(result["operations"], 1)
                     if op["name"] == "Передать на завершение"]
            if relay and "Начать обработку" in known:
                result["operation_transitions"].append({
                    "from_new": None, "from_known": known["Начать обработку"], "to_new": relay[0], "to_known": None,
                    "condition": None, "basis": "source_call", "contract": None, "basis_note": "Вызов в коде",
                    "refs": [{"path": CHAIN_PATH, "start_line": 37, "end_line": 37}]})
            return result
        edges = [("start", 0, "finish", 0, "source_call", [(54, 54), (41, 41)], None),
                 ("relay", 0, "finish", 0, "source_call", [(37, 37)], None)]
        # Deliberately disable automatic control closure to preserve this missing-middle-source negative control.
        with patch.object(direct_atomic, "CONTROL_CONTEXT_DEPTH", 0):
            bundle = self.run_fixture(CHAIN_PATH, CHAIN, CHAIN_FOUND, CHAIN_SPEC, edges, mutate=from_known,
                                      atomic_page_units=2, triage="off")
        self.assertEqual(len(atomic_requests(bundle)), 2)
        # start -> relay -> finish is a real chain, but relay was not supplied on start's page.
        self.assertEqual(proposal(bundle, "Начать обработку", "Завершить обработку")["reason"], "no_source_chain")
        self.assertEqual(proposal(bundle, "Начать обработку", "Передать на завершение")["reason"],
                         "endpoint_source_not_supplied")
        self.assertEqual(proposal(bundle, "Передать на завершение", "Завершить обработку")["state"], "active")
        self.assertEqual(routes(bundle), [("Передать на завершение", "Завершить обработку")])
        start, relay = unit_of(bundle, "start"), unit_of(bundle, "relay")
        [link] = support_of(bundle)["open_links"]
        self.assertEqual((link["kind"], link["unit_id"], link["target_unit_id"], link["target"]),
                         ("dependency_not_supplied", start, relay, "relay"))
        [gap] = gaps(bundle, "unresolved_control_link")
        self.assertEqual(gap["unit_ids"], [start])
        self.assertIn(f"{CHAIN_PATH}:36-37", gap["question"])
        self.assertEqual(len(gaps(bundle, "unsupported_transition")), 2)
        check(bundle)
        known, rows, calls = self.saved()
        _support_sources(support_of(bundle), known, rows, calls)


class ReplayAndIdentityTests(TransitionCase):
    def test_a_checkpoint_replay_derives_the_same_support_without_a_model_call(self):
        first = self.reproduction()
        again = self.reproduction(resume_from=self.out)
        self.assertNotIn(ATOMIC, self.business.tasks())
        self.assertEqual({item["outcome"] for item in atomic_requests(again)}, {"accepted_from_checkpoint"})
        self.assertEqual(support_of(again), support_of(first))
        self.assertEqual(again["atomic_map"]["code_led"]["gaps"], first["atomic_map"]["code_led"]["gaps"])
        self.assertEqual(again["process_map"]["operation_transitions"], first["process_map"]["operation_transitions"])

    def test_a_validator_9_checkpoint_is_never_reused_as_validator_10(self):
        with patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/9"):
            self.reproduction()
        current = self.reproduction(resume_from=self.out)  # no trace: no recorded reply to revalidate
        self.assertIn(ATOMIC, self.business.tasks())
        requests = atomic_requests(current)
        self.assertEqual({item["outcome"] for item in requests}, {"accepted"})
        self.assertFalse([item for item in requests if "recovered" in item])
        self.assertTrue(all(item["checkpoint"]["fields"]["validator"] == "direct-atomic-validator/11"
                            for item in requests))

    def test_an_exactly_recorded_validator_9_reply_is_revalidated_with_its_original_provenance(self):
        fresh = self.reproduction()
        reply = page_reply(FLOW_SPEC, [*FLOW_EDGES, INVENTED])
        with patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/9"):
            prior = self.traced(Business(**handlers(atomic=reply)))
        prior_out = self.out
        self.assertEqual(prior["atomic_map"]["settings"]["validator"], "direct-atomic-validator/9")
        current = self.flow(Business(**handlers(atomic=reply)), resume_from=prior_out)
        self.assertNotIn(ATOMIC, self.business.tasks())  # the recorded reply, never a fake or a new call
        [request] = atomic_requests(current)
        source = request["recovered"]
        self.assertEqual((request["outcome"], source["prior_validator"], source["revalidated_under"]),
                         ("accepted_from_checkpoint", "direct-atomic-validator/9", "direct-atomic-validator/11"))
        self.assertEqual(request["accepted"]["response_sha256"],
                         atomic_requests(prior)[0]["accepted"]["response_sha256"])
        # The current policy classifies the recorded reply exactly as a fresh /10 run does.
        self.assertEqual(support_of(current)["proposals"], support_of(fresh)["proposals"])
        saved = json.loads((self.out / direct_atomic.CHECKPOINTS / f"{request['checkpoint']['identity']}.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(saved["recovered"]["prior_validator"], "direct-atomic-validator/9")
        check(current)

    def test_the_validator_identity_covers_the_support_helper(self):
        self.assertEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/11")
        data = (Path(direct_atomic.__file__).read_bytes() + Path(transition_support.__file__).read_bytes()
                + Path(direct_atomic.atomic_sources.__file__).read_bytes())
        self.assertEqual(direct_atomic.VALIDATOR_SHA256, hashlib.sha256(data).hexdigest())
        settings = direct_atomic._settings(type("Review", (), {
            "atomic_page_units": 10, "atomic_input_tokens": 1, "atomic_input_tokens_hard": 2,
            "atomic_answer_tokens": 3, "business_token_budget": None, "refine_max_requests": 4,
            "business_request_chars": 5, "triage": "off", "compose": False, "approved_obligations": None})())
        self.assertEqual(settings["code_led"]["transitions"]["support"]["policy"], transition_support.POLICY)


class ExportTests(TransitionCase):
    def assertRefused(self, bundle, edit):
        changed = copy.deepcopy(bundle)
        edit(changed["atomic_map"])
        with self.assertRaises(ExportError):
            check(changed)

    def test_the_proposals_their_active_subset_and_their_gaps_are_checked_exactly(self):
        bundle = self.reproduction()
        check(bundle)
        operations = [op["id"] for op in bundle["atomic_map"]["operations"]]

        def inactive(record):
            return next(item for item in record["code_led"]["transition_support"]["proposals"]
                        if item["state"] == "inactive")

        def active(record):
            return next(item for item in record["code_led"]["transition_support"]["proposals"]
                        if item["state"] == "active")

        def promoted(record):  # a forged active subset: an inactive proposal claims a copied support
            item = inactive(record)
            item.update(state="active", reason=None, support=copy.deepcopy(active(record)["support"]))

        def dropped(record):
            record["code_led"]["transition_support"]["proposals"].remove(inactive(record))

        def foreign(record):
            inactive(record)["to"] = "aop-0000000000000000"

        def unknown_reason(record):
            inactive(record)["reason"] = "semantically_doubtful"

        def borrowed_witness(record):
            item = active(record)
            item["support"]["witness_evidence_ids"] = [inactive(record)["evidence_ids"][0]]

        def stale_policy(record):
            record["code_led"]["transition_support"]["policy"] = "transition-support/0"

        def unsupported_gap_dropped(record):
            record["code_led"]["gaps"] = [gap for gap in record["code_led"]["gaps"]
                                          if gap["kind"] != "unsupported_transition"]

        def endpoint_of_another_action(record):
            inactive(record)["endpoints"]["from_unit_ids"] = [record["operations"][0]["unit_ids"][0]]

        def counted_as_accepted(record):
            record["summary"]["transitions_inactive"] = 0

        def not_declared(record):
            record["settings"]["code_led"]["transitions"].pop("support")

        def missing_support(record):
            record["code_led"].pop("transition_support")
        for edit in (promoted, dropped, foreign, unknown_reason, borrowed_witness, stale_policy,
                     unsupported_gap_dropped, endpoint_of_another_action, counted_as_accepted, not_declared,
                     missing_support):
            with self.subTest(edit=edit.__name__):
                self.assertRefused(bundle, edit)
        self.assertTrue(operations)

    def test_a_legacy_record_without_transition_support_still_validates_without_claiming_the_policy(self):
        bundle = self.flow()  # no inactive proposal and no open link: a record of /9 differs only by the new keys
        legacy = copy.deepcopy(bundle)
        record = legacy["atomic_map"]
        record["code_led"].pop("transition_support")
        record["settings"]["code_led"]["transitions"].pop("support")
        record["settings"]["validator"] = "direct-atomic-validator/9"
        for key in transition_summary()[1:]:
            record["summary"].pop(key)
        check(legacy)

        def new_gap_kind(data):
            data["code_led"]["gaps"][0]["kind"] = "unsupported_transition"

        def new_counts(data):
            data["summary"]["transition_proposals"] = 2
        for edit in (new_gap_kind, new_counts):
            with self.subTest(edit=edit.__name__):
                self.assertRefused(legacy, edit)

    def test_a_relation_id_cannot_be_rebound_to_other_units_or_call_lines(self):
        bundle = self.reproduction("source_call")
        known, rows, calls = self.saved()
        support = support_of(bundle)
        _support_sources(support, known, rows, calls)
        item = next(entry for entry in support["proposals"] if entry["state"] == "inactive")
        sender, receiver = item["endpoints"]["from_unit_ids"][0], item["endpoints"]["to_unit_ids"][0]
        unrelated = next(row for row in rows.values()
                         if row["origin"] == "host_static" and row["resolution"] == "resolved")
        spans = {sender: [[FLOW_PATH, 28, 31]], receiver: [[FLOW_PATH, 34, 36]]}

        def forged(origins, relation_ids, lines):
            changed = copy.deepcopy(support)
            target = next(entry for entry in changed["proposals"] if entry["id"] == item["id"])
            target.update(state="active", reason=None, support={
                "kind": "call_chain", "unit_ids": [sender, receiver], "provenance": "host",
                "witness_evidence_ids": [item["evidence_ids"][0]], "unit_spans": copy.deepcopy(spans),
                "links": [{"from_unit": sender, "to_unit": receiver, "origins": origins, "relation_ids": relation_ids,
                           "call_lines": lines, "witness_evidence_id": item["evidence_ids"][0]}]})
            return changed
        for label, changed in (("rebound host relation", forged(["host_static"], [unrelated["id"]],
                                                                 [[FLOW_PATH, 29, 30]])),
                               ("claimed navigation", forged(["navigation"], [], [])),
                               ("helper call without index", forged(["host_static"], [unrelated["id"]],
                                                                    [[FLOW_PATH, 29, 30]]))):
            with self.subTest(case=label):
                with self.assertRaises(ExportError):
                    _support_sources(changed, known, rows, None if "index" in label else calls)
        # A real link with a forged call line fails as well.
        chain = copy.deepcopy(support)
        real = next(entry for entry in chain["proposals"] if entry["state"] == "active" and entry["support"]["links"])
        real["support"]["links"][0]["call_lines"] = [[FLOW_PATH, 29, 30]]
        with self.assertRaises(ExportError):
            _support_sources(chain, known, rows, calls)
        # So does an owned range outside its unit's lines.
        outside = copy.deepcopy(support)
        real = next(entry for entry in outside["proposals"] if entry["state"] == "active")
        unit = real["support"]["unit_ids"][0]
        real["support"]["unit_spans"][unit] = [[FLOW_PATH, 1, 40]]
        with self.assertRaises(ExportError):
            _support_sources(outside, known, rows, calls)

    def test_handoff_witnesses_must_lie_in_the_exact_source_of_their_sender_and_receiver(self):
        bundle = self.run_fixture(QUEUE_PATH, QUEUE, QUEUE_FOUND, QUEUE_SPEC, [
            ("send_parcel", 0, "receive_parcel", 0, "source_handoff", [(5, 5), (6, 6), (10, 10)], "PARCEL_QUEUE")])
        record = bundle["atomic_map"]
        operations = {op["id"]: op for op in record["operations"]}

        def reject(message):
            raise ExportError(message)
        _transition_support(record, operations, bundle, reject)
        evidence = {item["id"]: item for item in bundle["evidence"]}
        item = support_of(bundle)["proposals"][0]
        sender_only = [key for key in item["evidence_ids"] if evidence[key]["source_anchor"]["end_line"] <= 6]
        self.assertEqual(len(sender_only), 2)
        receive = unit_of(bundle, "receive_parcel")

        def both_in_sender(data):
            data["code_led"]["transition_support"]["proposals"][0]["support"]["witness_evidence_ids"] = sender_only
            data["transitions"] = copy.deepcopy(data["code_led"]["transition_support"]["proposals"])

        def receiver_source_moved(data):  # the receiver now claims the sender's lines as its own source
            both_in_sender(data)
            data["code_led"]["transition_support"]["proposals"][0]["support"]["unit_spans"][receive] = [
                [QUEUE_PATH, 5, 6]]
            data["transitions"] = copy.deepcopy(data["code_led"]["transition_support"]["proposals"])
        changed = copy.deepcopy(bundle)
        both_in_sender(changed["atomic_map"])
        with self.assertRaises(ExportError):
            _transition_support(changed["atomic_map"], operations, changed, reject)
        # Moving the receiver's owned source passes the record check but not the saved unit lines.
        moved = copy.deepcopy(bundle)
        receiver_source_moved(moved["atomic_map"])
        _transition_support(moved["atomic_map"], operations, moved, reject)
        known, rows, calls = self.saved()
        with self.assertRaises(ExportError):
            _support_sources(support_of(moved), known, rows, calls)
        _support_sources(support_of(bundle), known, rows, calls)

    def test_the_whole_export_accepts_the_reproduction_and_carries_its_proposals(self):
        bundle = self.reproduction()
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(document)


if __name__ == "__main__":
    unittest.main()
