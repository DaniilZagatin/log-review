"""Code-led map (direct-atomic-validator/9): actions and transitions from code first, stages and facts derived after.

Synthetic fixture text only; every file starts with a raise so that importing or running it would fail. Real frozen
snapshot, graph, review, catalog, declaration index and command units; scripted models read the exact request JSON.
These tests check host wiring, identities, bounds and accounting. Passing them is not semantic verification of a
live model's map.
"""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from process_observability import direct_atomic
from process_observability.code_review import ReviewOptions
from process_observability.contracts import ProcessMap
from process_observability.direct_atomic import load_obligations
from process_observability.llm import LLMError
from process_observability.onboarding_export import ExportError, _atomic_accounting, export_onboarding
from test_action_refinement import Code, finding
from test_direct_atomic import AtomicCase, Business, account, atomic_requests, first_attempts
from test_technical_relations import RelationCode

ROOT = Path(__file__).resolve().parents[1]
GUARD = 'raise RuntimeError("synthetic fixture: never execute")\n'
FLOW_PATH = "app/orders.py"
FLOW = GUARD + (
    "import logging\n"                                                    # 2
    "logger = logging.getLogger(__name__)\n"                              # 3
    "\n\n"                                                                # 4-5
    "def check_fraud(order):\n"                                           # 6
    "    if order.amount > 1000:\n"                                       # 7
    "        order.flagged = True\n"                                      # 8
    "        return False\n"                                              # 9
    "    return True\n"                                                   # 10
    "\n\n"                                                                # 11-12
    "def approve_order(order):\n"                                         # 13
    "    if not check_fraud(order):\n"                                    # 14
    "        return None\n"                                               # 15
    '    order.status = "approved"\n'                                     # 16
    "    order.save()\n"                                                  # 17
    "    return order\n"                                                  # 18
    "\n\n"                                                                # 19-20
    "def submit_order(order):\n"                                          # 21
    "    order.save()\n"                                                  # 22
    '    logger.info("order.submitted", order_id=order.pk)\n'             # 23
    "    approve_order(order)\n"                                          # 24
    "    return order\n"                                                  # 25
    "\n\n"                                                                # 26-27
    "def cancel_order(order):\n"                                          # 28
    '    order.status = "canceled"\n'                                     # 29
    "    order.save()\n"                                                  # 30
    "    return order\n"                                                  # 31
    "\n\n"                                                                # 32-33
    "def notify_partner(order, client):\n"                                # 34
    '    client.post("/partner/orders", json={"id": order.pk})\n'         # 35
    "    return order\n")                                                 # 36
FLOW_FOUND = [finding("entry_point", "Приём заказа и запись order.submitted.", FLOW_PATH, 21, 25, "submit_order"),
              finding("entry_point", "Отмена заказа без записи в лог.", FLOW_PATH, 28, 31, "cancel_order"),
              finding("state_change", "Заказ утверждается автоматически.", FLOW_PATH, 16, 17, "approve_order"),
              finding("outcome", "Проверка суммы помечает заказ.", FLOW_PATH, 7, 9, "check_fraud"),
              finding("integration", "Отправка заказа партнёру.", FLOW_PATH, 35, 35, "notify_partner")]
# A misleading generated description: a manual approval stage and a payment step in a fixed order.
MISLEADING = ("Менеджер вручную утверждает каждый заказ на этапе «Ручное утверждение». Порядок строго такой: "
              "оформление → оплата → ручное утверждение → отправка.")
FLOW_SPEC = {
    "submit_order": [("Принять заказ", "start", "Приём заказов", "continues", None)],
    "check_fraud": [("Проверить заказ на мошенничество", "decision", "Приём заказов", "continues", None)],
    "approve_order": [("Автоматически утвердить заказ", "operation", "Приём заказов", "terminal", None)],
    "cancel_order": [("Отменить заказ", "operation", "Отмена", "terminal", None)],
    "notify_partner": [("Передать заказ партнёру", "operation", "Партнёры", "external_handoff",
                        "Что партнёр делает с заказом, в анализируемом коде не видно")]}
FLOW_EDGES = [("submit_order", 0, "check_fraud", 0, "source_call", [(24, 24), (14, 14)], None),
              ("check_fraud", 0, "approve_order", 0, "source_condition", [(14, 16)], None)]


def locate(brief, symbol, index, positions):
    """A host identity only: a new operation of this page, or an accepted one that the host named for this code."""
    if symbol in positions:
        return "new", positions[symbol][index]
    for row in brief["shared_context"]:
        if row["symbol"] == symbol and len(row["accepted_operations"]) > index:
            return "known", row["accepted_operations"][index]
    return None


def page_reply(spec, edges=(), path=FLOW_PATH, mutate=None):
    """Scripted page decisions keyed by symbol; groups by name; every keyed site of a unit maps to its first action."""
    def reply(brief, attempt):
        groups = {item["name"]: item["number"] for item in brief["working_groups"]}
        new_groups, operations, accounting, positions = [], [], [], {}

        def group_number(name):
            if name not in groups:
                new_groups.append({"name": name, "contract": f"Действия: {name}"})
                groups[name] = len(brief["working_groups"]) + len(new_groups)
            return groups[name]
        for unit in brief["units"]:
            specs = spec.get(unit["symbol"])
            if not specs:
                accounting.append(account(unit["number"], "supporting", reason="Вспомогательный код"))
                continue
            numbers = []
            for name, kind, group, exit_, question in specs:
                operations.append({"name": name, "description": None, "actor": None, "kind": kind,
                                   "group": None if group is None else group_number(group), "initiation": "api_call",
                                   "initiation_basis": None, "exit": exit_, "exit_question": question, "refs": [],
                                   "details": None})
                numbers.append(len(operations))
            positions[unit["symbol"]] = numbers
            accounting.append(account(unit["number"], "realized", numbers,
                                      reason="Несколько действий одной команды" if len(numbers) > 1 else None))
        by_number = {unit["number"]: unit["symbol"] for unit in brief["units"]}
        mappings = []
        for site in brief["sites"]:
            owners = [by_number[number] for number in site["units"] if by_number[number] in positions]
            if owners and site["key_status"] in ("literal", "resolved_constant"):
                mappings.append({"site": site["number"], "new_operation": positions[owners[0]][0],
                                 "known_operation": None, "lifecycle_role": "result",
                                 "role_basis": "Запись после сохранения", "occurrence_rule": "Одна запись",
                                 "ambiguity": None, "refs": [{"path": site["path"], "start_line": site["start_line"],
                                                              "end_line": site["end_line"]}]})
        transitions = []
        for source, source_index, target, target_index, basis, lines, contract in edges:
            start, end = locate(brief, source, source_index, positions), locate(brief, target, target_index, positions)
            if start is None or end is None or "new" not in (start[0], end[0]):
                continue
            transitions.append({"from_new": start[1] if start[0] == "new" else None,
                                "from_known": start[1] if start[0] == "known" else None,
                                "to_new": end[1] if end[0] == "new" else None,
                                "to_known": end[1] if end[0] == "known" else None, "condition": None,
                                "basis": basis, "contract": contract, "basis_note": "Порядок виден в коде",
                                "refs": [{"path": path, "start_line": first, "end_line": last} for first, last in lines]})
        result = {"operations": operations, "new_groups": new_groups, "unit_accounting": accounting,
                  "event_mappings": mappings, "operation_transitions": transitions, "unknowns": []}
        return result if mutate is None else mutate(result, brief, attempt)
    return reply


def compose_reply(obligations=None, hypotheses=None):
    """Stages from working groups; one fact per action boundary; obligation and hypothesis checks as scripted."""
    def reply(brief, attempt):
        names = {item["number"]: item["name"] for item in brief["working_groups"]}
        by_group = {}
        for op in brief["operations"]:
            by_group.setdefault(op["group"], []).append(op["number"])
        stages = [{"name": names.get(group, "Прочие действия"), "contract": f"Этап: {names.get(group, 'прочее')}",
                   "operations": numbers} for group, numbers in by_group.items()]
        stage_of = {number: index for index, stage in enumerate(stages, 1) for number in stage["operations"]}
        facts = [{"stage": stage_of[op["number"]], "operations": [op["number"]], "obligation": None,
                  "meaning": f"Выполнено: {op['name']}", "rationale": "Граница принятого действия",
                  "scope": "Каждое выполнение", "condition": None, "metric_ids": [], "evidence_alternatives": [],
                  "guidance_ids": ["FACT-QUESTION"], "lifecycle_roles": ["result"]} for op in brief["operations"]]
        checked = [obligations(item, brief) for item in brief["approved_obligations"]] if obligations else [
            {"obligation": item["number"], "status": "not_found", "operations": [], "question": "Где это реализовано?"}
            for item in brief["approved_obligations"]]
        return {"process": {"purpose": "Обработка заказов", "boundaries": None, "case_definition": "Один заказ"},
                "stages": stages, "facts": facts, "obligations": checked,
                "hypotheses": [] if hypotheses is None else hypotheses(brief), "scenarios": [], "groups": [],
                "unknowns": []}
    return reply


def handlers(atomic=None, compose=None, **extra):
    return {"map_atomic": atomic or page_reply(FLOW_SPEC, FLOW_EDGES), "map_compose": compose or compose_reply(),
            "instrumentation": {"cards": []}, "metrics": {"metrics": []}, **extra}


def number_of(brief, name):
    return next(op["number"] for op in brief["operations"] if op["name"] == name)


class CodeLedCase(AtomicCase):
    def flow(self, business=None, files=None, found=None, text=MISLEADING, **settings):
        self.business = business or Business(**handlers())
        bundle, self.out = self.run_case(self.business, files=files or {FLOW_PATH: FLOW},
                                         found=copy.deepcopy(found or FLOW_FOUND), business_text=text, **settings)
        return bundle

    def exported(self, bundle, name):
        from process_observability.cli import _write_bundle
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        result = export_onboarding(self.out, self.out.parent / name)
        return result, json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))

    def operation(self, bundle, name):
        [op] = [item for item in bundle["process_map"]["operations"] if item["name"] == name]
        return op


class CodeFirstTests(CodeLedCase):
    def test_no_stage_order_or_fact_exists_before_code_and_the_description_stays_a_hypothesis(self):
        def contradicted(brief):
            return [{"statement": "Менеджер вручную утверждает заказ", "status": "contradicted",
                     "operations": [number_of(brief, "Автоматически утвердить заказ")],
                     "note": "Код утверждает автоматически"}]
        bundle = self.flow(Business(**handlers(compose=compose_reply(hypotheses=contradicted))))
        tasks = self.business.tasks()
        self.assertNotIn("TASK: map-skeleton", tasks)  # no description-first frame request exists any more
        self.assertEqual(tasks.count("TASK: map-compose"), 1)
        [page] = first_attempts(self.business)
        brief = page["brief"]
        self.assertFalse({"stages", "facts", "stage_transitions", "business_text"} & set(brief))
        self.assertEqual(brief["business_hypothesis"], MISLEADING)
        self.assertEqual(brief["working_groups"], [])
        self.assertIn("never creates", direct_atomic.ATOMIC_SYSTEM)
        process = bundle["process_map"]
        names = {stage["name"] for stage in process["stages"]}
        self.assertEqual(names, {"Приём заказов", "Отмена", "Партнёры"})
        self.assertFalse([op for op in process["operations"] if "Ручн" in op["name"] or "оплат" in op["name"].lower()])
        # Stage order only from accepted operation transitions: these all stay inside one stage, so none exists.
        self.assertEqual(process["transitions"], [])
        self.assertEqual(len(process["operation_transitions"]), 2)
        record = bundle["atomic_map"]
        self.assertEqual((record["schema"], record["code_led"]["stages"]["source"]), ("atomic-map/2", "compose"))
        [hypothesis] = record["code_led"]["hypotheses"]
        self.assertEqual((hypothesis["status"], hypothesis["source"]), ("contradicted", "business_hypothesis"))
        self.assertTrue(any("Гипотеза" in line for line in process["unknowns"]))
        self.assertIsNone(record["code_led"]["obligations"])  # none supplied, none invented
        self.assertEqual(record["required_scenarios"], [])

    def test_code_only_and_log_free_actions_survive_with_independent_entries_and_an_addressed_handoff_gap(self):
        bundle = self.flow()
        process, record = bundle["process_map"], bundle["atomic_map"]
        fraud, cancel = self.operation(bundle, "Проверить заказ на мошенничество"), self.operation(bundle, "Отменить заказ")
        self.assertEqual(cancel["event_keys"], [])  # no logger call: the action still exists
        self.assertTrue(fraud["evidence_ids"] and cancel["evidence_ids"])
        frontier = record["code_led"]["frontier"]
        submit, partner = self.operation(bundle, "Принять заказ"), self.operation(bundle, "Передать заказ партнёру")
        self.assertEqual(set(frontier["entries"]), {submit["id"], cancel["id"], partner["id"]})
        self.assertEqual(len(frontier["fragments"]), 3)  # never joined for connectivity
        [gap] = record["code_led"]["gaps"]
        self.assertEqual((gap["kind"], gap["operation_ids"], gap["source"]),
                         ("unresolved_handoff", [partner["id"]], "model"))
        self.assertTrue(gap["unit_ids"])
        self.assertEqual((record["status"], record["status_reasons"]), ("partial", ["open_gaps"]))
        self.assertTrue(any(gap["id"] in line for line in process["unknowns"]))
        facts = {fact["id"]: fact for fact in bundle["log_coverage"]["facts"]}
        rows = {row["operation_ids"][0]: row for row in record["code_led"]["facts"]}
        self.assertEqual(facts[rows[cancel["id"]]["id"]]["event_keys"], [])
        self.assertEqual(facts[rows[submit["id"]]["id"]]["event_keys"], ["order.submitted"])
        self.assertEqual(set(facts), {row["id"] for row in record["code_led"]["facts"]})
        _atomic_accounting(bundle, ProcessMap.from_dict(process), record["catalog"]["snapshot_id"])

    def test_an_action_without_supplied_code_and_text_based_order_are_rejected_on_the_page(self):
        def manual(result, brief, attempt):
            if attempt == 1:
                result["operations"].append({"name": "Ручное утверждение менеджером", "description": None, "actor": None,
                                             "kind": "operation", "group": None, "initiation": "api_call",
                                             "initiation_basis": None, "exit": "continues", "exit_question": None,
                                             "refs": [], "details": None})
            return result
        bundle = self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, FLOW_EDGES, mutate=manual))))
        first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual(error["code"], "operation_without_unit")  # expected by the text, realized by no code
        self.assertFalse([op for op in bundle["process_map"]["operations"] if "Ручное" in op["name"]])

        def text_order(result, brief, attempt):
            if attempt == 1 and result["operation_transitions"]:
                result["operation_transitions"][0]["basis"] = "business_text"
            return result
        self.flow(Business(**handlers(atomic=page_reply(FLOW_SPEC, FLOW_EDGES, mutate=text_order))))
        first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual(error["layer"], "schema")  # business_text is no transition basis any more


class TransitionTests(CodeLedCase):
    LAYERED = GUARD + (
        "\n\n"                                                     # 2-3
        "def reserve_stock(item):\n"                               # 4
        "    item.reserved = True\n"                               # 5
        "    item.save()\n"                                        # 6
        "    return item\n"                                        # 7
        "\n\n"                                                     # 8-9
        "def bulk_import(rows):\n"                                 # 10
        "    for row in rows:\n"                                   # 11
        "        row.save()\n"                                     # 12
        "    return rows\n"                                        # 13
        "\n\n"                                                     # 14-15
        "def place_order(item):\n"                                 # 16
        "    item.placed = True\n"                                 # 17
        "    reserve_stock(item)\n"                                # 18
        "    return item\n")                                       # 19

    def test_a_page_three_transition_reaches_a_page_one_operation_beyond_the_window_by_host_identity(self):
        path = "app/stock.py"
        found = [finding("entry_point", "Резерв товара.", path, 4, 7, "reserve_stock"),
                 finding("entry_point", "Массовый импорт строк.", path, 10, 13, "bulk_import"),
                 finding("entry_point", "Размещение заказа с резервом.", path, 16, 19, "place_order")]
        spec = {"reserve_stock": [("Зарезервировать товар", "operation", "Склад", "terminal", None)],
                "bulk_import": [(f"Импортировать строку {index:02d}", "operation", "Импорт", "terminal", None)
                                for index in range(85)],
                "place_order": [("Разместить заказ", "operation", "Заказы", "continues", None)]}
        edges = [("place_order", 0, "reserve_stock", 0, "source_call", [(18, 18)], None)]
        bundle = self.flow(Business(**handlers(atomic=page_reply(spec, edges, path=path))), files={path: self.LAYERED},
                           found=found, text="", atomic_page_units=1)
        pages = first_attempts(self.business)
        self.assertEqual([[unit["symbol"] for unit in call["brief"]["units"]] for call in pages],
                         [["reserve_stock"], ["bulk_import"], ["place_order"]])
        record = bundle["atomic_map"]
        reserve = self.operation(bundle, "Зарезервировать товар")
        place = self.operation(bundle, "Разместить заказ")
        ids = [op["id"] for op in record["operations"]]
        self.assertGreater(ids.index(place["id"]) - ids.index(reserve["id"]), direct_atomic.KNOWN_OPERATIONS)
        third = pages[2]["brief"]
        [row] = third["shared_context"]
        self.assertEqual(row["symbol"], "reserve_stock")
        [number] = row["accepted_operations"]
        self.assertEqual(third["known_operations"][number - 1]["name"], "Зарезервировать товар")
        self.assertEqual(third["context_omissions"]["known_operations"]["relevant_omitted"], 0)
        [request] = [item for item in atomic_requests(bundle) if place["id"] in
                     [op["id"] for op in record["operations"] if op["request_id"] == item["id"]]]
        self.assertIn(reserve["id"], request["known_operation_ids"])
        [edge] = bundle["process_map"]["operation_transitions"]
        self.assertEqual((edge["from_operation"], edge["to_operation"]), (place["id"], reserve["id"]))
        self.assertTrue(edge["evidence_ids"])
        # /10: active only because the known action's unit was supplied on this page as a shared row and the cited
        # call line of place_order links the two units (structural support, not semantic proof).
        units = {state["symbol"]: key for key, state in record["unit_states"].items()}
        [item] = record["code_led"]["transition_support"]["proposals"]
        self.assertEqual((item["state"], item["support"]["kind"], item["support"]["unit_ids"]),
                         ("active", "call_chain", [units["place_order"], units["reserve_stock"]]))
        self.assertIn(units["reserve_stock"], request["shared_context_unit_ids"])
        self.assertEqual(item["support"]["links"][0]["call_lines"], [["app/stock.py", 18, 18]])
        # The stage order is derived from that one accepted transition only.
        stages = {stage["id"]: stage["name"] for stage in bundle["process_map"]["stages"]}
        self.assertEqual([(stages[item["from_stage"]], stages[item["to_stage"]])
                          for item in bundle["process_map"]["transitions"]], [("Заказы", "Склад")])

    def test_false_ids_ranges_conditions_and_contract_free_handoffs_are_rejected(self):
        path = "app/bus.py"
        text = GUARD + (
            "\n\n"                                                  # 2-3
            "def publish_order(bus, order):\n"                      # 4
            '    bus.send("orders.created", order.pk)\n'            # 5
            "    return order\n"                                    # 6
            "\n\n"                                                  # 7-8
            "def consume_order(message):\n"                         # 9
            '    # subscribed to "orders.created"\n'                # 10
            "    order = message.order\n"                           # 11
            '    order.status = "processing"\n'                     # 12
            "    return order\n")                                   # 13
        found = [finding("entry_point", "Публикация заказа в шину.", path, 4, 6, "publish_order"),
                 finding("entry_point", "Обработка сообщения о заказе.", path, 9, 13, "consume_order")]
        spec = {"publish_order": [("Опубликовать заказ", "operation", "Шина", "continues", None)],
                "consume_order": [("Начать обработку заказа", "operation", "Шина", "terminal", None)]}
        good = [("publish_order", 0, "consume_order", 0, "source_handoff", [(5, 5), (10, 10)], "orders.created")]
        broken = {
            "one-sided contract": ([("publish_order", 0, "consume_order", 0, "source_handoff", [(5, 5)],
                                     "orders.created")], "handoff_without_contract"),
            "missing contract": ([("publish_order", 0, "consume_order", 0, "source_handoff", [(5, 5), (10, 10)],
                                   None)], "handoff_without_contract"),
            "unsupplied range": ([("publish_order", 0, "consume_order", 0, "source_condition", [(40, 41)], None)],
                                 None),
            "condition without refs": ([("publish_order", 0, "consume_order", 0, "source_condition", [], None)],
                                       "transition_basis")}
        for label, (edges, code) in broken.items():
            with self.subTest(case=label):
                def first_bad(brief, attempt, edges=edges):  # broken on the first attempt, repaired on the second
                    return page_reply(spec, edges if attempt == 1 else good, path=path)(brief, attempt)
                bundle = self.flow(Business(**handlers(atomic=first_bad)), files={path: text}, found=found, text="")
                first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
                [error] = repair["brief"]["validation_feedback"]["errors"]
                if code is not None:
                    self.assertEqual(error["code"], code)
                else:
                    self.assertEqual(error["layer"], "reference")
                [edge] = bundle["process_map"]["operation_transitions"]  # the repaired, contract-bound handoff
                self.assertEqual(bundle["atomic_map"]["transitions"][0]["contract"], "orders.created")

        def unknown_known(result, brief, attempt):
            if attempt == 1:
                result["operation_transitions"] = [{
                    "from_new": 1, "from_known": None, "to_new": None, "to_known": 99, "condition": None,
                    "basis": "source_call", "contract": None, "basis_note": "x",
                    "refs": [{"path": path, "start_line": 5, "end_line": 5}]}]
            return result
        self.flow(Business(**handlers(atomic=page_reply(spec, good, path=path, mutate=unknown_known))),
                  files={path: text}, found=found, text="")
        first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual(error["code"], "unknown_known_operation")


class FrontierAndBudgetTests(CodeLedCase):
    def test_an_undecided_local_callee_is_pending_work_and_unprocessed_inventory_stays_listed(self):
        spec = {**FLOW_SPEC, "approve_order": [("Автоматически утвердить заказ", "operation", "Приём заказов",
                                                "continues", None)]}
        bundle = self.flow(Business(**handlers(atomic=page_reply(spec, []))), refine_max_requests=2,
                           atomic_page_units=1, triage="off")
        record = bundle["atomic_map"]
        self.assertEqual(self.business.tasks().count("TASK: map-atomic"), 1)
        self.assertEqual(self.business.tasks().count("TASK: map-compose"), 1)  # reserved for the composition
        deferred = {state["symbol"]: state["reason"] for state in record["unit_states"].values()
                    if state["state"] == "deferred"}
        self.assertEqual(deferred, {"check_fraud": "request_limit_reached", "cancel_order": "request_limit_reached",
                                    "notify_partner": "request_limit_reached"})
        approve = self.operation(bundle, "Автоматически утвердить заказ")
        gaps = {gap["kind"]: gap for gap in record["code_led"]["gaps"]}
        self.assertEqual(gaps["pending_local_work"]["operation_ids"], [approve["id"]])
        [check_unit] = [key for key, state in record["unit_states"].items() if state["symbol"] == "check_fraud"]
        self.assertEqual(gaps["pending_local_work"]["unit_ids"], [check_unit])
        self.assertNotIn("unresolved_handoff", gaps)  # a local unread callee is not a missing external receiver
        self.assertEqual(record["status"], "partial")
        self.assertIn("units_without_decision", record["status_reasons"])
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")

    def test_regrouping_keeps_operation_ids_and_mappings_and_a_failed_composition_stays_provisional(self):
        composed = self.flow()
        failed = self.flow(Business(**handlers(compose=LLMError("no JSON object", category="invalid_json"))))
        self.assertEqual(self.business.tasks().count("TASK: map-compose"), 2)  # one repair, never more
        for bundle, source in ((composed, "compose"), (failed, "provisional_groups")):
            record = bundle["atomic_map"]
            self.assertEqual(record["code_led"]["stages"]["source"], source)
            ops = {op["id"]: op for op in bundle["process_map"]["operations"]}
            for mapping in bundle["process_map"]["event_mappings"]:
                self.assertEqual(mapping["stage_id"], ops[mapping["operation_id"]]["stage_id"])
        self.assertEqual([op["id"] for op in composed["process_map"]["operations"]],
                         [op["id"] for op in failed["process_map"]["operations"]])
        self.assertEqual([(item["event_key"], item["operation_id"]) for item in composed["process_map"]["event_mappings"]],
                         [(item["event_key"], item["operation_id"]) for item in failed["process_map"]["event_mappings"]])
        record = failed["atomic_map"]
        self.assertEqual(record["status"], "partial")
        self.assertIn("final_grouping_incomplete", record["status_reasons"])
        self.assertEqual({row["origin"] for row in record["code_led"]["facts"]}, {"host_boundary"})
        self.assertEqual(failed["process_map"]["transitions"], [])


class ObligationTests(CodeLedCase):
    def test_an_approved_obligation_absent_from_code_stays_required_and_is_never_implemented(self):
        obligations = load_obligations("Уведомлять клиента о возврате средств\nПринимать заказы\n", source="approved.txt")

        def check(item, brief):
            if item["number"] == 2:
                return {"obligation": 2, "status": "matched", "operations": [number_of(brief, "Принять заказ")],
                        "question": None}
            return {"obligation": 1, "status": "not_found", "operations": [],
                    "question": "Где клиенту сообщают о возврате?"}
        bundle = self.flow(Business(**handlers(compose=compose_reply(obligations=check))),
                           approved_obligations=obligations)
        [call] = [item for item in self.business.calls if item["task"] == "TASK: map-compose"]
        self.assertEqual([item["text"] for item in call["brief"]["approved_obligations"]],
                         ["Уведомлять клиента о возврате средств", "Принимать заказы"])
        led = bundle["atomic_map"]["code_led"]
        rows = {item["id"]: item for item in led["obligations"]["items"]}
        self.assertEqual((rows["obl-001"]["status"], rows["obl-002"]["status"]), ("not_found", "matched"))
        [fact_row] = [row for row in led["facts"] if row["obligation_id"] == "obl-001"]
        self.assertEqual((fact_row["origin"], fact_row["operation_ids"], fact_row["stage_id"]), ("obligation", [], None))
        [fact] = [item for item in bundle["log_coverage"]["facts"] if item["id"] == fact_row["id"]]
        self.assertEqual((fact["requirement"]["state"], fact["requirement"]["decision_id"]), ("accepted", "obl-001"))
        self.assertEqual(fact["event_keys"], [])
        self.assertFalse([op for op in bundle["process_map"]["operations"] if "возврат" in op["name"].lower()])
        self.assertTrue(any("obl-001" in line for line in bundle["process_map"]["unknowns"]))


class CompositionGuidanceTests(CodeLedCase):
    """Facts moved into the composition: its fact attribution keeps the bounded post-repair omission, nothing else."""

    @staticmethod
    def guided(mutate=None):
        base = compose_reply()

        def reply(brief, attempt):  # the same invalid attribution on the first attempt and on the real repair
            result = base(brief, attempt)
            result["facts"][0]["guidance_ids"] = ["FACT-QUESTION", "NOT-A-LOADED-RULE"]
            return result if mutate is None else mutate(result)
        return reply

    def test_invalid_fact_attribution_after_the_real_repair_is_omitted_with_a_warning(self):
        bundle = self.flow(Business(**handlers(compose=self.guided())))
        self.assertEqual(self.business.tasks().count("TASK: map-compose"), 2)  # one real repair, no third call
        [request] = atomic_requests(bundle, "compose")
        local = request["guidance_revalidation"]
        self.assertEqual((request["outcome"], local["outcome"], local["model_calls"]),
                         ("accepted_after_local_revalidation", "accepted", 0))
        self.assertEqual([attempt["code"] for attempt in request["attempts"]], ["unknown_guidance_id"] * 2)
        self.assertEqual([(item["path"], item["code"], item["attribution"]) for item in local["warnings"]],
                         [("facts[0].guidance_ids[1]", "unknown_guidance_id", "partial")])
        self.assertEqual(request["accepted"], {"attempt": 2, "api_request_sha256": request["attempts"][1]["api_request_sha256"],
                                               "response_sha256": local["sanitized_response_sha256"],
                                               "derivation": "local_guidance_omission",
                                               "raw_response_sha256": request["attempts"][1]["response_sha256"]})
        self.assertNotEqual(local["sanitized_response_sha256"], local["raw_response_sha256"])
        record = bundle["atomic_map"]
        self.assertEqual(record["code_led"]["stages"]["source"], "compose")
        first = record["code_led"]["facts"][0]["id"]
        self.assertEqual(bundle["guidance_links"]["facts"][first], ["FACT-QUESTION"])
        self.assertNotIn("NOT-A-LOADED-RULE", json.dumps(bundle["guidance_links"]))
        self.assertTrue(any("guidance_revalidation" in line for line in bundle["process_map"]["unknowns"]))
        process, snapshot = ProcessMap.from_dict(bundle["process_map"]), record["catalog"]["snapshot_id"]
        _atomic_accounting(bundle, process, snapshot)
        for edit in (lambda entry: entry["guidance_revalidation"]["warnings"][0].update(path="facts[0].meaning"),
                     lambda entry: entry["accepted"].update(response_sha256=entry["attempts"][1]["response_sha256"]),
                     lambda entry: entry.update(outcome="accepted_after_repair")):
            changed = copy.deepcopy(bundle)
            edit(atomic_requests(changed, "compose")[0])
            with self.assertRaises(ExportError):
                _atomic_accounting(changed, process, snapshot)

    def test_an_unknown_role_is_never_a_role_a_composed_fact_can_require(self):
        base = compose_reply()

        def reply(brief, attempt):  # a record of unknown role must never confirm a fact
            result = base(brief, attempt)
            if attempt == 1:
                result["facts"][0]["lifecycle_roles"] = ["unknown"]
            return result
        bundle = self.flow(Business(**handlers(compose=reply)))
        first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-compose"]
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual((error["layer"], error["path"]), ("schema", "facts[0].lifecycle_roles[0]"))
        self.assertNotIn("unknown", direct_atomic.FACT_ROLES)
        self.assertFalse([row for row in bundle["atomic_map"]["code_led"]["facts"] if "unknown" in row["lifecycle_roles"]])

    def test_a_core_composition_error_beside_invalid_attribution_keeps_the_composition_rejected(self):
        def mismatch(result):  # a fact of another stage: a core error that no omission may hide
            result["facts"][1]["stage"] = result["facts"][1]["stage"] % len(result["stages"]) + 1
            return result
        bundle = self.flow(Business(**handlers(compose=self.guided(mismatch))))
        self.assertEqual(self.business.tasks().count("TASK: map-compose"), 2)
        [request] = atomic_requests(bundle, "compose")
        local = request["guidance_revalidation"]
        self.assertEqual((request["outcome"], request["accepted"], local["outcome"], local["sanitized_response_sha256"]),
                         ("rejected_after_repair", None, "rejected", None))
        self.assertEqual(local["final"]["code"], "fact_stage_mismatch")
        record = bundle["atomic_map"]
        self.assertEqual(record["code_led"]["stages"]["source"], "provisional_groups")
        self.assertIn("final_grouping_incomplete", record["status_reasons"])
        _atomic_accounting(bundle, ProcessMap.from_dict(bundle["process_map"]), record["catalog"]["snapshot_id"])


class IdentityAndExportTests(CodeLedCase):
    def test_a_changed_business_prompt_misses_the_earlier_raw_page_reply(self):
        self.flow()
        earlier = self.out
        same = self.flow(resume_from=earlier)
        self.assertTrue(all(item["outcome"] == "accepted_from_checkpoint" for item in atomic_requests(same)))
        with patch.object(direct_atomic, "ATOMIC_SYSTEM", direct_atomic.ATOMIC_SYSTEM + "\n- changed wording"):
            changed = self.flow(resume_from=earlier)
        self.assertFalse([item for item in atomic_requests(changed) if item["outcome"] == "accepted_from_checkpoint"])
        # /10: transition support is the current host identity; the page prompt above is the /9 one.
        self.assertEqual(direct_atomic.VALIDATOR_VERSION, "direct-atomic-validator/11")

    def test_relations_partial_export_and_strict_nested_validation(self):
        bundle = self.flow(code=RelationCode(copy.deepcopy(FLOW_FOUND)), options=ReviewOptions(relations=True))
        record = bundle["atomic_map"]
        binding = record["code_led"]["technical_relations"]
        self.assertEqual(binding["status"], "collected")
        self.assertGreater(binding["worker"], 0)
        self.assertEqual(bundle["technical_relations"], binding)
        rows = [row for call in first_attempts(self.business) for unit in call["brief"]["units"]
                for row in unit["relations"]]
        self.assertTrue(any(row["origin"] == "worker_proposal" for row in rows))
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")
        self.assertIn("technical_relations.json", document["source_run"]["files"])
        process, snapshot = ProcessMap.from_dict(bundle["process_map"]), record["catalog"]["snapshot_id"]
        submit = self.operation(bundle, "Принять заказ")

        def stray_gap(data):
            data["code_led"]["gaps"][0]["operation_ids"] = ["aop-0000000000000000"]

        def invented_order(data):
            stage = data["code_led"]["stages"]["items"][0]["id"]
            other = data["code_led"]["stages"]["items"][1]["id"]
            data["code_led"]["stages"]["stage_transitions"].append(
                {"from_stage": stage, "to_stage": other, "transition_ids": []})

        def claimed_complete(data):
            data.update(status="complete", status_reasons=[])

        def foreign_relations(data):
            data["code_led"]["technical_relations"]["review_run_fingerprint"] = "0" * 64

        def moved_operation(data):
            items = data["code_led"]["stages"]["items"]
            items[0]["operation_ids"].remove(submit["id"]) if submit["id"] in items[0]["operation_ids"] else None
        for edit in (stray_gap, invented_order, claimed_complete, foreign_relations, moved_operation):
            with self.subTest(edit=edit.__name__):
                changed = copy.deepcopy(bundle)
                edit(changed["atomic_map"])
                with self.assertRaises(ExportError):
                    _atomic_accounting(changed, process, snapshot)
        path = self.out / "technical_relations.json"
        saved = path.read_bytes()
        data = json.loads(saved)
        data["worker"] = data["worker"][1:]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ExportError):
            export_onboarding(self.out, self.out.parent / "refused")
        path.write_bytes(saved)
        export_onboarding(self.out, self.out.parent / "packet-again")

    def test_the_strict_recorded_demo_replies_are_unchanged(self):
        from test_operation_metadata import R07_DEMO_REPLIES
        data = (ROOT / "examples/synthetic/model-responses.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), R07_DEMO_REPLIES)


if __name__ == "__main__":
    unittest.main()
