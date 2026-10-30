"""Direct atomic map mode (direct-atomic plan D2-D4) through the ordinary review-mode run_analysis.

One small stage-skeleton call, then bounded atomic pages built from host-verified command units; optional triage
and compose. Real frozen snapshot, graph, logging inventory, code review, reduction, catalog and declaration
index; scripted code and business models read the exact request JSON. These checks cover host wiring, bounds,
identity and accounting. They say nothing about the semantic quality of a live model, and no live run is implied.
"""

import copy
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from process_observability import workflow
from process_observability.code_review import ReviewOptions
from process_observability.contracts import ArtifactStatus, EventMapping, ProcessMap, ProcessOperation, Stage
from process_observability.demo import RecordedModel, request_fingerprint
from process_observability.guidance import load_guidance
from process_observability.llm import LLMError
from process_observability.source_tools import snapshot_repository
from process_observability.workflow import ReviewSettings, WorkflowError, prepare_review, run_analysis
from test_action_refinement import (CHECKOUT, HEADER, REFUND, Business, Code, finding, mutation, repository)
from test_action_refinement import handlers as refine_handlers

BUSINESS_TEXT = ("Интернет-магазин: покупатель формирует корзину, указывает адреса и оформляет заказ; магазин "
                 "подтверждает или отменяет заказ; платёжная система сообщает о возвратах. Обязателен сценарий "
                 "возврата товара покупателем.")
COMPLETE = HEADER + (
    "def create_order_from_checkout(checkout):\n"                                 # 6
    "    order = checkout.to_order()\n"
    "    order.save()\n"
    "    logger.info(\"order.created\", order_id=order.pk)\n"                    # 9
    "    return order\n"                                                          # 10
    "\n\n"
    "class CheckoutComplete:\n"                                                   # 13
    "    def perform_mutation(self, checkout):\n"                                 # 14
    "        checkout.complete = True\n"
    "        checkout.save()\n"
    "        logger.info(\"checkout.completed\", checkout_id=checkout.pk)\n"     # 17
    "        return create_order_from_checkout(checkout)\n")                      # 18
# Redaction breaks this file's parse (as on the public pilot files); it has no logging call at all.
NOTE = HEADER + (
    "class CheckoutNoteUpdate:\n"                                                 # 6
    "    def perform_mutation(self, checkout, note):\n"                           # 7
    "        token = checkout.get_token(\n"
    "            note)\n"                                                         # 9
    "        checkout.note = note\n"                                              # 10
    "        checkout.save()\n"                                                   # 11
    "        return checkout\n")                                                  # 12
NOTE_PATH = CHECKOUT + "note_update.py"
STAGE_IDS = ["checkout", "order", "payment"]
FACT_IDS = {"cart.line_added", "order.created", "order.canceled", "refund.done", "refund.repeat", "refund.new"}
KEYED = ("literal", "resolved_constant")
ROLES = {"refund.requested": "request", "refund.started": "start", "refund.failed": "error",
         "refund.duplicate": "duplicate", "payment.release_funds_noop": "no_op",
         "payment.release_funds_canceling": "start"}
LINKS = {"checkout.lines_added": ["cart.line_added"], "order.created": ["order.created"],
         "refund.completed": ["refund.done"], "refund.duplicate": ["refund.repeat"]}
SUPPORTING = {"add_variants": "CheckoutLinesAdd.perform_mutation",
              "create_order_from_checkout": "CheckoutComplete.perform_mutation"}
COMPLETE_REASON = "Команда закрывает корзину и создаёт заказ: два самостоятельных эффекта"
GENEROUS = {"atomic_input_tokens": 500000, "atomic_input_tokens_hard": 900000, "business_request_chars": 2000000}


def atomic_repository(extra_files=None, extra_findings=()):
    files = {"shop/checkout/complete.py": COMPLETE, NOTE_PATH: NOTE, **(extra_files or {})}
    found = [finding("entry_point", "Завершение оформления: корзина закрыта, создаётся заказ.",
                     "shop/checkout/complete.py", 14, 18, "CheckoutComplete.perform_mutation"),
             finding("state_change", "Создание заказа из корзины и лог order.created.", "shop/checkout/complete.py",
                     6, 10, "create_order_from_checkout"),
             finding("entry_point", "Изменение примечания корзины: вход.", NOTE_PATH, 7, 9,
                     "CheckoutNoteUpdate.perform_mutation"),
             finding("state_change", "Сохранение примечания корзины.", NOTE_PATH, 10, 11, "perform_mutation"),
             *extra_findings]
    return repository(files, found)


def bulk_repository(count, body_lines=0):
    files, found = {}, []
    for index in range(count):
        name, path = f"BulkCommand{index:02d}", CHECKOUT + f"bulk_{index:02d}.py"
        body = [f"checkout.note_{index}_{step:02d} = 'значение шага {step:02d} команды {index:02d}'"
                for step in range(body_lines)]
        files[path] = mutation(name, f"checkout.flag_{index} = value", f"checkout.bulk_{index:02d}", body)
        found.append(finding("entry_point", f"{name}: отдельная команда корзины.", path, 7, 11 + body_lines,
                             f"{name}.perform_mutation"))
    return files, found


def line_of(text, needle):
    [number] = [number for number, line in enumerate(text.splitlines(), 1) if needle in line]
    return number


def fact(identifier, stage, meaning, roles):
    return {"id": identifier, "stage_id": stage, "meaning": meaning, "rationale": "Учёт результата этапа",
            "scope": "Все случаи", "condition": None, "metric_ids": [], "evidence_alternatives": [],
            "guidance_ids": ["FACT-QUESTION"], "lifecycle_roles": list(roles)}


def skeleton(*, extra_facts=(), scenarios=None):
    """Stages with contracts, a stage transition, facts with lifecycle roles and one required scenario only."""
    required = [{"id": "goods-return", "name": "Возврат товара покупателем",
                 "description": "Покупатель возвращает товар, магазин возвращает деньги.", "stage_ids": ["payment"],
                 "basis": "Бизнес-описание требует сценарий возврата товара"}]
    return {"process": {"purpose": "Покупка в интернет-магазине", "boundaries": None,
                        "case_definition": "Одна корзина и созданный из неё заказ"},
            "stages": [{"id": "checkout", "name": "Оформление корзины", "roles": ["покупатель"], "systems": [],
                        "contract": "Покупатель меняет состав и адреса корзины и завершает оформление."},
                       {"id": "order", "name": "Обработка заказа", "roles": ["менеджер"], "systems": [],
                        "contract": "Магазин создаёт, подтверждает или отменяет заказ."},
                       {"id": "payment", "name": "Оплата и возвраты", "roles": [], "systems": ["платёжный шлюз"],
                        "contract": "Платёжная система сообщает о возвратах; средства без заказа освобождаются."}],
            "transitions": [{"from_stage": "checkout", "to_stage": "order", "condition": "Оформление завершено"}],
            "facts": [fact("cart.line_added", "checkout", "Позиция добавлена в корзину", ["result"]),
                      fact("order.created", "order", "Заказ создан из корзины", ["result"]),
                      fact("order.canceled", "order", "Существующий заказ отменён", ["result"]),
                      fact("refund.done", "payment", "Возврат выполнен", ["result"]),
                      fact("refund.repeat", "payment", "Получено повторное уведомление о возврате", ["duplicate"]),
                      fact("refund.new", "payment", "Выполнен новый возврат", ["result"]), *extra_facts],
            "required_scenarios": required if scenarios is None else scenarios, "unknowns": []}


def stage_of(path):
    return "payment" if "/payment/" in path else "order" if "/order/" in path else "checkout"


def account(unit, disposition, operations=(), *, serves=(), exclusion=None, reason=None, question=None):
    return {"unit": unit, "disposition": disposition, "new_operations": list(operations), "known_operations": [],
            "serves": list(serves), "exclusion": exclusion, "reason": reason, "question": question}


def atomic_reply(brief, attempt=1, *, stages=None, links=None, roles=None, complete_reason=COMPLETE_REASON):
    """Code-led page reply: one operation per unit in the working group named after its directory (proposed once
    as a new group, reused by number later); helpers support their caller; every keyed site of a page unit is mapped
    to its operation. links is kept for callers of the earlier helper: facts are derived later (compose_reply)."""
    groups = {item["name"]: item["number"] for item in brief["working_groups"]}
    new_groups = []
    symbols = {unit["symbol"]: unit["number"] for unit in brief["units"]}
    operations, accounting, owner = [], [], {}

    def operation(name, stage):
        if stage not in groups:
            new_groups.append({"name": stage, "contract": f"Действия области {stage}"})
            groups[stage] = len(brief["working_groups"]) + len(new_groups)
        operations.append({"name": name, "description": None, "actor": None, "kind": "operation",
                           "group": groups[stage], "initiation": "api_call", "initiation_basis": None,
                           "exit": "terminal", "exit_question": None, "refs": []})
        return len(operations)

    for unit in brief["units"]:
        symbol, number = unit["symbol"], unit["number"]
        if symbol == "CheckoutComplete.perform_mutation":
            done = operation("Завершить оформление корзины", "checkout")
            created = operation("Создать заказ из корзины", "order")
            owner[number] = {"checkout.completed": done, "order.created": created}
            accounting.append(account(number, "realized", [done, created], reason=complete_reason))
        elif symbol in SUPPORTING:
            served = symbols.get(SUPPORTING[symbol])
            accounting.append(account(number, "supporting", serves=[] if served is None else [served],
                                      reason="Вспомогательный код другой команды"))
        else:
            owner[number] = operation("Действие " + symbol, (stages or {}).get(symbol, stage_of(unit["path"])))
            accounting.append(account(number, "realized", [owner[number]]))
    for unit in brief["units"]:  # a supporting unit's emit sites belong to the operations it serves
        served = symbols.get(SUPPORTING.get(unit["symbol"]))
        if served in owner:
            owner[unit["number"]] = owner[served]
    mappings = []
    for site in brief["sites"]:
        target = next((owner[number] for number in site["units"] if number in owner), None)
        if isinstance(target, dict):
            target = target.get(site["event_key"])
        if site["key_status"] not in KEYED or target is None:
            continue
        mappings.append({"site": site["number"], "new_operation": target, "known_operation": None,
                         "lifecycle_role": (roles or ROLES).get(site["event_key"], "result"),
                         "role_basis": "Условие вокруг вызова в переданном коде",
                         "occurrence_rule": "Одна запись на выполнение команды", "ambiguity": None,
                         "refs": [{"path": site["path"], "start_line": site["start_line"],
                                   "end_line": site["end_line"]}]})
    return {"operations": operations, "new_groups": new_groups, "unit_accounting": accounting,
            "event_mappings": mappings, "operation_transitions": [], "unknowns": []}


def compose_reply(brief, attempt=1):
    """Stages from the working groups; one fact per action boundary with the roles its accepted records show."""
    names = {item["number"]: item["name"] for item in brief["working_groups"]}
    by_group = {}
    for op in brief["operations"]:
        by_group.setdefault(op["group"], []).append(op["number"])
    stages = [{"name": names.get(group, "Прочее"), "contract": f"Этап {names.get(group, 'прочее')}",
               "operations": numbers} for group, numbers in by_group.items()]
    stage_of = {number: index for index, stage in enumerate(stages, 1) for number in stage["operations"]}
    facts = []
    for op in brief["operations"]:
        roles = sorted({item["lifecycle_role"] for item in op["events"]} - {"unknown"}) or ["result"]
        for role in roles:
            facts.append({"stage": stage_of[op["number"]], "operations": [op["number"]], "obligation": None,
                          "meaning": f"{op['name']}: {role}", "rationale": "Граница принятого действия",
                          "scope": "Каждое выполнение", "condition": None, "metric_ids": [],
                          "evidence_alternatives": [], "guidance_ids": ["FACT-QUESTION"], "lifecycle_roles": [role]})
    return {"process": {"purpose": "Покупка в интернет-магазине", "boundaries": None,
                        "case_definition": "Одна корзина и созданный из неё заказ"},
            "stages": stages, "facts": facts,
            "obligations": [{"obligation": item["number"], "status": "not_found", "operations": [],
                             "question": "Где это реализовано?"} for item in brief["approved_obligations"]],
            "hypotheses": [], "scenarios": [], "groups": [], "unknowns": []}


def atomic_handlers(**overrides):
    # map_skeleton stays only so that an accidental skeleton request would be visible; code-led runs send none.
    base = {"map_skeleton": skeleton(), "map_atomic": atomic_reply, "map_compose": compose_reply,
            "instrumentation": {"cards": []}, "metrics": {"metrics": []}}
    return {**base, **overrides}


def fact_by_meaning(bundle, meaning):
    [fact] = [item for item in bundle["log_coverage"]["facts"] if item["meaning"] == meaning]
    return fact


def stage_names(bundle):
    """Derived stage ID -> name (code-led stage IDs are host IDs; these fixtures name stages after directories)."""
    return {stage["id"]: stage["name"] for stage in bundle["process_map"]["stages"]}


def first_attempts(business, task="TASK: map-atomic"):
    return [call for call in business.calls if call["task"] == task and "validation_feedback" not in call["brief"]]


def utf8(call):
    return len(call["system"].encode("utf-8")) + len(call["user"].encode("utf-8"))


def holds(call, symbol):
    return any(unit["symbol"] == symbol for unit in call["brief"]["units"])


def unit_state(bundle, symbol):
    [(identifier, state)] = [(key, value) for key, value in bundle["atomic_map"]["unit_states"].items()
                             if value["symbol"] == symbol]
    return identifier, state


def operations_of(bundle, state):
    return [op for op in bundle["process_map"]["operations"] if op["id"] in state["operation_ids"]]


def lifecycle(bundle):
    return {item["id"]: (item["instrumentation"], item["lifecycle_check"]) for item in bundle["log_coverage"]["facts"]}


def anchors(bundle, ids):
    registry = {item["id"]: item for item in bundle["evidence"]}
    return {(registry[key]["source_anchor"]["path"], registry[key]["source_anchor"]["start_line"],
             registry[key]["source_anchor"]["end_line"]) for key in ids}


def atomic_requests(bundle, kind="atomic"):
    return [item for item in bundle["atomic_map"]["requests"] if item["kind"] == kind]


def earlier_map(release_role="result"):
    """A saved process map of an earlier coarse run: old event -> operation links without evidence."""
    stages = [Stage(id="order", name="Обработка заказа"), Stage(id="payment", name="Оплата"),
              Stage(id="checkout", name="Корзина")]
    operations = [ProcessOperation(id="order-cancel", name="Отмена существующего заказа", stage_id="order"),
                  ProcessOperation(id="refund", name="Возврат средств", stage_id="payment"),
                  ProcessOperation(id="cart", name="Управление корзиной", stage_id="checkout")]

    def mapping(key, operation, role):
        return EventMapping(event_key=key, business_action=operation.name, operation=operation.name,
                            operation_id=operation.id, stage_id=operation.stage_id, lifecycle_role=role,
                            occurrence_rule="Одна запись")
    mappings = [mapping("payment.release_funds_canceling", operations[0], release_role),
                mapping("refund.event", operations[1], "result"), mapping("never.logged", operations[1], "result"),
                mapping("checkout.lines_added", operations[2], "result")]
    return ProcessMap(run_id="earlier", input_fingerprints={}, status=ArtifactStatus.DRAFT, evidence_ids=[],
                      purpose=None, boundaries=None, case_definition=None, stages=stages, transitions=[],
                      event_mappings=mappings, unknowns=[], operations=operations).to_dict()


class AtomicCase(unittest.TestCase):
    def run_case(self, business, *, files=None, found=None, options=None, trace=None, business_text=BUSINESS_TEXT,
                 code=None, default_bounds=False, **settings):
        default_files, default_found = atomic_repository()
        files, found = files or default_files, default_found if found is None else found
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        for relative, text in files.items():
            path = root / "repo" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        snapshot = snapshot_repository(root / "repo", max_files=None, max_chars=None)
        guidance = load_guidance()
        options = options or ReviewOptions()
        prepared = prepare_review(snapshot, guidance, options)
        (root / "out").mkdir()
        settings.setdefault("map_mode", "atomic")
        settings.setdefault("business_model_fingerprint", "scripted-business-fixture-v1")
        if not default_bounds:
            # Page sizes then follow the count bound; tests of the input/answer bounds set their own values.
            for key, value in GENEROUS.items():
                settings.setdefault(key, value)
        review = ReviewSettings(graph=prepared["graph"], plan=prepared["plan"], system=prepared["system"],
                                options=options, directory=root / "out", model_fingerprint="scripted-glm", **settings)
        self.code = code or Code(found)
        bundle = run_analysis(snapshot, business, run_id="atomic-test", data_origin="synthetic", model_mode="fake",
                              guidance=guidance, code_model=self.code, review=review, trace=trace,
                              business_text=business_text)
        return bundle, root / "out"


class DirectAtomicFlowTests(AtomicCase):
    def test_atomic_pages_then_one_composition_derive_the_code_led_map(self):
        business = Business(**atomic_handlers())
        bundle, out = self.run_case(business)
        tasks = business.tasks()
        self.assertEqual(tasks[-3:], ["TASK: map-compose", "TASK: instrumentation", "TASK: metrics"])
        self.assertEqual(set(tasks[:-3]), {"TASK: map-atomic"})
        for task in ("TASK: map", "TASK: map-skeleton", "TASK: map-scope", "TASK: map-refine", "TASK: map-triage"):
            self.assertNotIn(task, tasks)  # no description-first frame is requested
        self.assertTrue(all(state["status"] == "completed" for state in bundle["manifest"]["stages"].values()))
        record = bundle["atomic_map"]
        self.assertEqual((record["schema"], record["status"]), ("atomic-map/2", "complete"))
        # Pages carry the business text only as a labelled hypothesis, and no stage or fact frame.
        for page in business.calls:
            if page["task"] == "TASK: map-atomic":
                brief = page["brief"]
                self.assertFalse({"stages", "facts", "business_text"} & set(brief))
                self.assertEqual(brief["business_hypothesis"], BUSINESS_TEXT)
        # Stages are derived from the accepted operations (here: the working groups the composition kept).
        stages = {stage["id"]: stage["name"] for stage in bundle["process_map"]["stages"]}
        self.assertEqual(set(stages.values()), set(STAGE_IDS))
        self.assertEqual(record["code_led"]["stages"]["source"], "compose")
        operations = bundle["process_map"]["operations"]
        self.assertEqual({op["id"] for op in operations}, {op["id"] for op in record["operations"]})
        self.assertTrue(operations and all(op["stage_id"] in stages for op in operations))
        self.assertEqual(len({op["name"] for op in operations}), len(operations))
        self.assertEqual(bundle["process_map"]["transitions"], [])  # no accepted operation transition, no order
        # Every catalog candidate belongs to one unit; every candidate unit has exactly one decision on one page.
        catalog = json.loads((out / "action_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(set(record["candidates"]), {item["id"] for item in catalog["candidates"]})
        candidate_units = {item["unit_id"] for item in record["candidates"].values()}
        context_units = {item['id'] for item in json.loads((out / 'declaration_coverage.json').read_text())['context_units']}
        self.assertEqual(candidate_units | context_units, set(record["unit_states"]))
        self.assertEqual((len(candidate_units), len(context_units)), (13, 2))
        self.assertFalse(candidate_units & context_units)  # each body has one primary identity
        self.assertEqual({state["state"] for state in record["unit_states"].values()}, {"accounted"})
        decided = [uid for request in atomic_requests(bundle) for uid in request["unit_ids"]]
        self.assertEqual(sorted(decided), sorted(record["unit_states"]))
        self.assertEqual(record["summary"]["decisions"], 15)
        # The saved record and units are the bundle's, and the units verify against the saved catalog and index.
        from process_observability.declaration_index import check_units
        self.assertEqual(json.loads((out / "atomic_map.json").read_text(encoding="utf-8")), record)
        index = json.loads((out / "declaration_index.json").read_text(encoding="utf-8"))
        units = json.loads((out / "command_units.json").read_text(encoding="utf-8"))
        check_units(units, catalog=catalog, index=index)
        self.assertEqual(bundle["command_units"]["units_sha256"], units["units_sha256"])
        self.assertEqual(record["units"]["units_sha256"], units["units_sha256"])

    def test_fragments_of_one_verified_declaration_are_supplied_and_decided_as_one_unit(self):
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business)
        identifier, state = unit_state(bundle, "CheckoutNoteUpdate.perform_mutation")
        self.assertEqual((state["status"], len(state["candidate_ids"]), state["state"]), ("verified", 2, "accounted"))
        [page] = [call for call in first_attempts(business) if holds(call, "CheckoutNoteUpdate.perform_mutation")]
        [row] = [unit for unit in page["brief"]["units"] if unit["symbol"] == "CheckoutNoteUpdate.perform_mutation"]
        self.assertEqual((row["status"], row["declaration_supplied"]), ("verified", "whole"))
        self.assertEqual({item["description"] for item in row["findings"]},
                         {"Изменение примечания корзины: вход.", "Сохранение примечания корзины."})
        excerpts = {item["number"]: item for item in page["brief"]["excerpts"]}
        self.assertEqual([(excerpts[number]["start_line"], excerpts[number]["end_line"]) for number in row["excerpts"]],
                         [(7, 12)])
        # The model sees redacted frozen lines; original text only located the declaration.
        text = excerpts[row["excerpts"][0]]["text"]
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("get_token(", text)
        self.assertEqual(row["sites"], [])  # a command without any log keeps its place in the accounting
        [operation] = operations_of(bundle, state)
        self.assertEqual(anchors(bundle, operation["evidence_ids"]), {(NOTE_PATH, 7, 9), (NOTE_PATH, 10, 11)})
        self.assertEqual(sum(identifier in request["unit_ids"] for request in atomic_requests(bundle)), 1)
        self.assertGreaterEqual(bundle["atomic_map"]["summary"]["units_without_logs"], 2)  # note and add_variants

    def test_one_command_may_realize_operations_in_two_stages_and_is_still_decided_once(self):
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business)
        identifier, state = unit_state(bundle, "CheckoutComplete.perform_mutation")
        operations = {op["name"]: op for op in operations_of(bundle, state)}
        names = stage_names(bundle)
        self.assertEqual({name: names[op["stage_id"]] for name, op in operations.items()},
                         {"Завершить оформление корзины": "checkout", "Создать заказ из корзины": "order"})
        self.assertEqual((state["disposition"], state["reason"], sorted(names[key] for key in state["stage_ids"])),
                         ("realized", COMPLETE_REASON, ["checkout", "order"]))
        helper_id, helper = unit_state(bundle, "create_order_from_checkout")
        self.assertEqual((helper["disposition"], helper["serves_unit_ids"], helper["operation_ids"]),
                         ("supporting", [identifier], []))
        # The called helper unit is planned and decided on its caller's page.
        [page] = [request for request in atomic_requests(bundle) if identifier in request["unit_ids"]]
        self.assertIn(helper_id, page["unit_ids"])
        # Its emit site lies in sources serving the order operation; nothing duplicates the command.
        [created] = [item for item in bundle["process_map"]["event_mappings"] if item["event_key"] == "order.created"]
        self.assertEqual((created["operation_id"], names[created["stage_id"]], created["lifecycle_role"],
                          created["role_support"]),
                         (operations["Создать заказ из корзины"]["id"], "order", "result", "emit_within_operation_sources"))
        fact = fact_by_meaning(bundle, "Создать заказ из корзины: result")  # proposed at the operation boundary
        self.assertEqual((fact["instrumentation"], fact["lifecycle_check"], fact["event_keys"]),
                         ("found", "compatible", ["order.created"]))
        self.assertEqual(sum(op["name"] == "Создать заказ из корзины" for op in bundle["process_map"]["operations"]), 1)

    def test_numbers_stage_choice_multi_effect_and_shared_operations_get_one_precise_repair(self):
        complete = "CheckoutComplete.perform_mutation"

        def first_realized(reply, skip=()):
            return [index for index, item in enumerate(reply["unit_accounting"])
                    if item["disposition"] == "realized" and item["reason"] is None and index not in skip]

        def no_reason(reply, brief):
            number = next(unit["number"] for unit in brief["units"] if unit["symbol"] == complete)
            index = next(i for i, item in enumerate(reply["unit_accounting"]) if item["unit"] == number)
            reply["unit_accounting"][index]["reason"] = None
            return f"unit_accounting[{index}].reason"

        def shared(reply, brief):
            first, second = first_realized(reply)[:2]
            reply["unit_accounting"][second]["new_operations"] = list(reply["unit_accounting"][first]["new_operations"])
            return f"unit_accounting[{first}].reason"

        def supporting_with_operation(reply, brief):
            [index] = first_realized(reply)[:1]
            reply["unit_accounting"][index]["disposition"] = "supporting"
            return f"unit_accounting[{index}]"

        def unknown_serves(reply, brief):
            index = next(i for i, item in enumerate(reply["unit_accounting"]) if item["disposition"] == "supporting")
            reply["unit_accounting"][index]["serves"] = [99]
            return f"unit_accounting[{index}].serves[0]"

        def unrealized(reply, brief):  # an expected action that no supplied unit realizes
            reply["operations"].append(dict(reply["operations"][0], name="Ожидаемое действие без кода"))
            return f"operations[{len(reply['operations']) - 1}]"

        def open_handoff(reply, brief):
            reply["operations"][0].update(exit="external_handoff", exit_question=None)
            return "operations[0].exit_question"

        def duplicate(reply, brief):
            reply["unit_accounting"].append(copy.deepcopy(reply["unit_accounting"][0]))
            return f"unit_accounting[{len(reply['unit_accounting']) - 1}].unit"

        cases = {
            "several operations without a reason": (no_reason, "missing_reason"),
            "shared operation without a reason": (shared, "missing_reason"),
            "unknown group": (lambda reply, brief: reply["operations"][0].update(group=99) or "operations[0].group",
                              "unknown_group"),
            "unknown unit": (lambda reply, brief: reply["unit_accounting"][0].update(unit=99) or
                             "unit_accounting[0].unit", "unknown_unit"),
            "unit twice": (duplicate, "duplicate_unit"),
            "missing unit": (lambda reply, brief: reply["unit_accounting"].pop(0) and "unit_accounting", "missing_units"),
            "supporting with operations": (supporting_with_operation, "disposition_fields"),
            "unknown served unit": (unknown_serves, "unknown_unit"),
            "operation without supplied code": (unrealized, "operation_without_unit"),
            "handoff without its question": (open_handoff, "exit_without_question"),
        }
        for label, (edit, code) in cases.items():
            with self.subTest(case=label):
                expected = {}

                def handler(brief, attempt, edit=edit, expected=expected):
                    reply = atomic_reply(brief)
                    if attempt == 1 and holds({"brief": brief}, complete):
                        expected["path"] = edit(reply, brief)
                    return reply
                business = Business(**atomic_handlers(map_atomic=handler))
                bundle, _ = self.run_case(business)
                first, repair = [call for call in business.calls if call["task"] == "TASK: map-atomic"
                                 and holds(call, complete)]
                [error] = repair["brief"].pop("validation_feedback")["errors"]
                self.assertEqual(repair["brief"], first["brief"])
                self.assertEqual((error["code"], error["path"]), (code, expected["path"]))
                self.assertEqual(error["reason"], workflow._REASONS[code])
                [request] = [item for item in atomic_requests(bundle)
                             if unit_state(bundle, complete)[0] in item["unit_ids"]]
                self.assertEqual(request["outcome"], "accepted_after_repair")

    def test_a_failed_page_commits_nothing_and_every_unit_keeps_an_explicit_state(self):
        canary = "Операция отклонённой страницы"

        def broken(brief, attempt):
            reply = atomic_reply(brief)
            if holds({"brief": brief}, "handle_refund"):
                reply["operations"].append({"name": canary, "description": None, "actor": None, "kind": "operation",
                                            "group": None, "initiation": "api_call", "initiation_basis": None,
                                            "exit": "terminal", "exit_question": None, "refs": []})
                reply["unit_accounting"] = reply["unit_accounting"][1:]
            return reply
        business = Business(**atomic_handlers(map_atomic=broken))
        bundle, out = self.run_case(business)
        record = bundle["atomic_map"]
        [failed] = [item for item in atomic_requests(bundle) if item["outcome"] == "rejected_after_repair"]
        self.assertEqual([item["code"] for item in failed["attempts"]], ["missing_units", "missing_units"])
        self.assertEqual(len([call for call in business.calls if call["task"] == "TASK: map-atomic"
                              and holds(call, "handle_refund")]), 2)  # one repair, never more
        for identifier in failed["unit_ids"]:
            state = record["unit_states"][identifier]
            self.assertEqual((state["state"], state["request_id"], state["operation_ids"]),
                             ("request_failed", failed["id"], []))
        # Nothing of the rejected page reaches the map, the facts or their event keys.
        self.assertNotIn(canary, json.dumps(bundle["process_map"], ensure_ascii=False))
        self.assertNotIn(canary, (out / "atomic_map.json").read_text(encoding="utf-8"))
        self.assertFalse([op for op in bundle["process_map"]["operations"] if op["name"] == "Действие handle_refund"])
        self.assertFalse([item for item in bundle["process_map"]["event_mappings"]
                          if item["event_key"].startswith("refund.")])
        self.assertFalse([item for item in bundle["log_coverage"]["facts"]
                          if any(key.startswith("refund.") for key in item["event_keys"])])
        # Other pages stay committed; the run is partial, the map stage itself completed.
        accepted = [item for item in atomic_requests(bundle) if item["outcome"] == "accepted"]
        self.assertTrue(accepted)
        for request in accepted:
            for identifier in request["unit_ids"]:
                self.assertEqual(record["unit_states"][identifier]["state"], "accounted")
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["summary"]["failed_by_reason"], {"request_rejected_after_repair": len(failed["unit_ids"])})
        self.assertEqual(bundle["manifest"]["stages"]["map"]["status"], "completed")
        self.assertTrue(any("не получено" in text for text in bundle["process_map"]["unknowns"]))

    def test_the_request_limit_defers_remaining_units_by_id_with_a_reason(self):
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, refine_max_requests=1)
        self.assertEqual(len(first_attempts(business)), 1)  # 12 primary units need two pages of at most 10
        record = bundle["atomic_map"]
        states = record["unit_states"].values()
        deferred = [state for state in states if state["state"] == "deferred"]
        self.assertTrue(deferred)
        self.assertEqual({state["reason"] for state in deferred}, {"request_limit_reached"})
        self.assertEqual({state["state"] for state in states}, {"accounted", "deferred"})
        self.assertEqual(record["summary"]["deferred_by_reason"], {"request_limit_reached": len(deferred)})
        self.assertEqual(record["status"], "partial")
        self.assertTrue(any("лимит запросов" in text for text in bundle["process_map"]["unknowns"]))


class AtomicBoundsTests(AtomicCase):
    def test_the_count_bound_limits_primary_units_per_page_and_defaults_are_recorded(self):
        files, found = bulk_repository(25)
        business = Business(**atomic_handlers())
        self.run_case(business, files=files, found=found)
        pages = first_attempts(business)
        self.assertEqual([len(call["brief"]["units"]) for call in pages], [10, 10, 5])
        self.assertEqual({unit["role"] for call in pages for unit in call["brief"]["units"]}, {"primary"})
        wider = Business(**atomic_handlers())
        self.run_case(wider, files=files, found=found, atomic_page_units=16)
        self.assertEqual([len(call["brief"]["units"]) for call in first_attempts(wider)], [16, 9])
        # Untouched settings: the planned defaults are recorded, and every page respects them.
        small, small_found = bulk_repository(2)
        plain = Business(**atomic_handlers())
        bundle, _ = self.run_case(plain, files=small, found=small_found, default_bounds=True)
        settings = bundle["atomic_map"]["settings"]
        self.assertEqual({key: settings[key] for key in (
            "page_units", "max_page_units", "dependencies_per_unit", "shared_context_rows", "declaration_lines",
            "input_tokens_target", "input_tokens_hard", "answer_tokens", "repairs_per_request", "token_estimate",
            "max_requests", "triage", "compose")},
            {"page_units": 10, "max_page_units": 128, "dependencies_per_unit": 8, "shared_context_rows": 40,
             "declaration_lines": 120, "input_tokens_target": 40000, "input_tokens_hard": 80000,
             "answer_tokens": 10000, "repairs_per_request": 1, "token_estimate": "utf8_bytes_upper_bound",
             "max_requests": 16, "triage": "auto", "compose": False})
        calls = first_attempts(plain)
        requests = atomic_requests(bundle)
        self.assertEqual(len(calls), len(requests))
        for call, request in zip(calls, requests):
            # Without a tokenizer, UTF-8 bytes of the complete request are a conservative token upper bound.
            self.assertGreaterEqual(request["input_tokens"], utf8(call))
            self.assertLessEqual(request["input_tokens"], 80000)
            self.assertLessEqual(request["planned_answer_tokens"], 10000)
            if len(call["brief"]["units"]) > 1:
                self.assertLessEqual(request["input_tokens"], 40000)

    def test_the_input_target_splits_pages_by_utf8_bytes_and_an_oversized_unit_is_deferred_never_clipped(self):
        files, found = bulk_repository(12, body_lines=30)
        calibration = Business(**atomic_handlers())
        self.run_case(calibration, files=files, found=found, atomic_page_units=16)
        [whole] = first_attempts(calibration)  # precondition: one page holds every unit
        # The hard bound must also hold the one repair (trusted text plus bounded feedback), so it is wider.
        target, hard = utf8(whole) * 3 // 4, utf8(whole) * 2
        body = ["checkout.part_%03d = '%s'" % (step, "Ш" * 800) for step in range(150)]
        giant = CHECKOUT + "giant.py"
        files[giant] = mutation("GiantCommand", "checkout.giant = value", "checkout.giant", body)
        found = found + [finding("entry_point", "Огромная команда.", giant, 7, 161, "GiantCommand.perform_mutation")]
        self.assertGreater(len(files[giant].encode("utf-8")), hard)  # precondition: even its own ranges never fit
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, files=files, found=found, options=ReviewOptions(request_chars=400000),
                                  atomic_page_units=16, atomic_input_tokens=target, atomic_input_tokens_hard=hard)
        pages = first_attempts(business)
        self.assertGreater(len(pages), 1)
        for call in pages:
            self.assertLessEqual(utf8(call), hard)
            if len(call["brief"]["units"]) > 1:
                self.assertLessEqual(utf8(call), target)
        for call in business.calls:
            self.assertNotIn("Ш" * 150, call["user"])
        _, state = unit_state(bundle, "GiantCommand.perform_mutation")
        self.assertEqual((state["state"], state["reason"]), ("deferred", "unit_exceeds_request_budget"))
        accounted = [item for item in bundle["atomic_map"]["unit_states"].values() if item["state"] == "accounted"]
        self.assertEqual(len(accounted), 12)
        self.assertEqual(bundle["atomic_map"]["status"], "partial")

    def test_a_tokenizer_count_bounds_pages_and_the_one_million_ceiling_is_not_a_fill_target(self):
        from process_observability.token_budget import TokenBudget
        budget = TokenBudget(1000000, 131072, 8192, "a" * 64, lambda text: len(text.encode("utf-8")) // 2)
        files, found = bulk_repository(20, body_lines=60)
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, files=files, found=found, default_bounds=True, atomic_page_units=16,
                                  business_token_budget=budget, business_request_chars=2000000)
        pages = first_attempts(business)
        sizes = [len(call["brief"]["units"]) for call in pages]
        self.assertGreater(len(pages), 1)
        self.assertLess(sizes[0], 16)  # the input target, not the count bound or the 1M window, closed the page
        self.assertEqual(sum(sizes), 20)
        for call in pages:
            self.assertLessEqual(budget.count(call["system"]) + budget.count(call["user"]), 40000)
        for call in business.calls:
            self.assertTrue(budget.fits(call["system"], call["user"]))
        self.assertEqual(bundle["atomic_map"]["settings"]["token_estimate"], "tokenizer")

    def test_the_planned_answer_bound_splits_pages_and_each_page_has_at_most_one_repair(self):
        files, found = bulk_repository(12)
        calibration = Business(**atomic_handlers())
        bundle, _ = self.run_case(calibration, files=files, found=found, atomic_page_units=16)
        [request] = atomic_requests(bundle)  # precondition: one page holds every unit
        planned = request["planned_answer_tokens"]
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, files=files, found=found, atomic_page_units=16,
                                  atomic_answer_tokens=planned - 1)
        requests = atomic_requests(bundle)
        self.assertGreater(len(requests), 1)
        self.assertTrue(all(item["planned_answer_tokens"] <= planned - 1 for item in requests))
        self.assertEqual({state["state"] for state in bundle["atomic_map"]["unit_states"].values()}, {"accounted"})
        # Invalid JSON twice: one repair only, then the page fails and later pages still run.
        def invalid(brief, attempt):
            if holds({"brief": brief}, "BulkCommand00.perform_mutation"):
                return LLMError("no JSON object", category="invalid_json")
            return atomic_reply(brief)
        business = Business(**atomic_handlers(map_atomic=invalid))
        bundle, _ = self.run_case(business, files=files, found=found)
        self.assertEqual(len([call for call in business.calls if call["task"] == "TASK: map-atomic"
                              and holds(call, "BulkCommand00.perform_mutation")]), 2)
        [failed] = [item for item in atomic_requests(bundle) if item["outcome"] == "rejected_after_repair"]
        self.assertEqual([item["code"] for item in failed["attempts"]], ["invalid_json", "invalid_json"])
        self.assertTrue([item for item in atomic_requests(bundle) if item["outcome"] == "accepted"])
        # A transport refusal is never repaired and no further page is sent.
        def limited(brief, attempt):
            if holds({"brief": brief}, "BulkCommand00.perform_mutation"):
                return LLMError("rate limited", category="http", status=429)
            return atomic_reply(brief)
        business = Business(**atomic_handlers(map_atomic=limited))
        bundle, _ = self.run_case(business, files=files, found=found)
        self.assertEqual(business.tasks().count("TASK: map-atomic"), 1)
        [failed] = atomic_requests(bundle)
        self.assertEqual([(item["layer"], item["status"], item["retry"]) for item in failed["attempts"]],
                         [("transport", 429, "skipped_not_repairable")])
        later = {state["reason"] for state in bundle["atomic_map"]["unit_states"].values() if state["state"] == "deferred"}
        self.assertEqual(later, {"halted_after_transport_failure"})
        self.assertEqual(bundle["atomic_map"]["status"], "failed")


def wide_confirm():
    lines = ["class OrderConfirm:", "    def get_instance(self, order):", "        if order.status != \"unconfirmed\":",
             "            raise ValueError(\"INVALID\")", "        return order", ""]
    for index in range(12):
        lines += [f"    def clean_note_{index:02d}(self, order):", f"        if order.note_{index:02d} is None:",
                  f"            raise ValueError(\"NOTE_{index:02d}\")", ""]
    lines += ["    def perform_mutation(self, order):", "        order = self.get_instance(order)",
              "        order.status = \"unfulfilled\"", "        order.save()",
              "        logger.info(\"order.confirmed\", order_id=order.pk)", "        return order"]
    return HEADER + "\n".join(lines) + "\n"


def checks_module(prefix, count, callers):
    """count module-level validation helpers, then one command class per (name, called helper indexes)."""
    lines = []
    for index in range(count):
        lines += [f"def {prefix}_{index:02d}(order):", f"    if order.{prefix}_{index:02d}:",
                  f"        raise ValueError(\"{prefix.upper()}_{index:02d}\")", "", ""]
    for name, called in callers:
        lines += [f"class {name}:", "    def perform_mutation(self, order):",
                  *(f"        {prefix}_{index:02d}(order)" for index in called), "        order.save()",
                  "        return order", "", ""]
    return HEADER + "\n".join(lines).rstrip("\n") + "\n"


def context_repository():
    files = {"shop/order/mutations/order_confirm.py": wide_confirm(),
             "shop/order/audit.py": checks_module("check", 10, [("OrderAudit", range(10))]),
             "shop/order/recheck.py": checks_module("common", 1, [("OrderReaudit", [0]), ("OrderRecheck", [0])]),
             **{f"shop/order/review_{k}.py": checks_module(f"rule{k}", 8, [(f"OrderReview{k}", range(8))])
                for k in range(5)}}
    found = []
    confirm = files["shop/order/mutations/order_confirm.py"]
    found.append(finding("error", "Подтверждение только неподтверждённого заказа.", "shop/order/mutations/order_confirm.py",
                         line_of(confirm, "if order.status"), line_of(confirm, "\"INVALID\""), "OrderConfirm.get_instance"))
    for index in range(12):
        start = line_of(confirm, f"if order.note_{index:02d}")
        found.append(finding("error", f"Проверка примечания {index:02d}.", "shop/order/mutations/order_confirm.py",
                             start, start + 1, f"OrderConfirm.clean_note_{index:02d}"))
    found.append(finding("state_change", "Подтверждение заказа и сохранение.", "shop/order/mutations/order_confirm.py",
                         line_of(confirm, "order.status = \"unfulfilled\""), line_of(confirm, "order.save()"),
                         "OrderConfirm.perform_mutation"))
    for path, text in files.items():
        if path.endswith("order_confirm.py"):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            match = re.match(r"def (\w+)\(order\):", line)
            if match:
                found.append(finding("error", f"Проверка {match[1]}.", path, number + 1, number + 2, match[1]))
            match = re.match(r"class (\w+):", line)
            if match:
                calls = len(re.findall(r"^        \w+_\d\d\(order\)$", text.split(line, 1)[1].split("class ", 1)[0], re.M))
                found.append(finding("entry_point", f"Команда {match[1]}.", path, number + 1, number + 3 + calls,
                                     f"{match[1]}.perform_mutation"))
    return files, found


def overlaps(excerpt, path, first, last):
    return excerpt["path"] == path and excerpt["start_line"] <= last and first <= excerpt["end_line"]


class AtomicContextTests(AtomicCase):
    def test_context_bodies_in_the_original_shared_fixture_each_get_one_primary_decision(self):
        files, found = context_repository()
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, files=files, found=found,
                                  options=ReviewOptions(max_findings_per_task=200))
        pages = first_attempts(business)
        self.assertEqual(sum(len(call['brief']['units']) for call in pages), 73)
        self.assertEqual(bundle['atomic_map']['summary']['decisions'], 73)
        self.assertEqual(sum(state.get('origin') == 'context_anchor'
                             for state in bundle['atomic_map']['unit_states'].values()), 64)
        for call in pages:
            self.assertLessEqual(len(call['brief']['shared_context']), 40)
            for unit in call['brief']['units']:
                self.assertLessEqual(len(unit['called_units']) + len(unit['context']), 8)
        [audit_page] = [call for call in pages if holds(call, 'OrderAudit.perform_mutation')]
        units = {unit['number']: unit for unit in audit_page['brief']['units']}
        [audit] = [unit for unit in units.values() if unit['symbol'] == 'OrderAudit.perform_mutation']
        self.assertEqual([units[number]['symbol'] for number in audit['called_units']],
                         [f'check_{index:02d}' for index in range(8)])
        # The bounded control closure now actually supplies the two additional callees as source only.
        self.assertEqual(audit['context_omitted'], [])
        shared = {row['symbol'] for row in [*audit_page['brief']['units'], *audit_page['brief']['shared_context']]}
        self.assertTrue({'check_08', 'check_09'} <= shared)

    def test_dependencies_and_the_shared_table_are_bounded_and_same_owner_siblings_are_never_supplied(self):
        files, found = context_repository()
        business = Business(**atomic_handlers())
        # Preserve the original shared-table negative controls under their recorded original policy.
        from process_observability import declaration_index, direct_atomic
        coverage = declaration_index.declaration_coverage
        with patch.object(declaration_index, 'declaration_coverage',
                          side_effect=lambda catalog, index: coverage(catalog, index, policy=None)), \
             patch.object(direct_atomic, 'VALIDATOR_VERSION', 'direct-atomic-validator/6'), \
             patch.object(direct_atomic, 'CONTROL_CONTEXT_DEPTH', 0):
            bundle, _ = self.run_case(business, files=files, found=found,
                                      options=ReviewOptions(max_findings_per_task=200))
        pages = first_attempts(business)
        # 9 primary units fit one page by count, but their 50 distinct dependencies do not fit one shared table.
        self.assertEqual(sum(len(call["brief"]["units"]) for call in pages), 9)
        self.assertGreaterEqual(len(pages), 2)
        confirm = files["shop/order/mutations/order_confirm.py"]
        notes = [line_of(confirm, f"def clean_note_{index:02d}") for index in range(12)]
        for call in pages:
            brief = call["brief"]
            rows = {row["number"]: row for row in brief["shared_context"]}
            self.assertLessEqual(len(rows), 40)
            self.assertEqual(len({row["symbol"] for row in rows.values()}), len(rows))  # each anchor once per page
            self.assertFalse([row for row in rows.values() if row["symbol"].startswith("OrderConfirm.clean_note")])
            for unit in brief["units"]:
                self.assertLessEqual(len(unit["context"]), 8)
                self.assertEqual({item["reason"] for item in unit["context_omitted"]} - {"dependency_limit"}, set())
            for excerpt in brief["excerpts"]:
                for start in notes:
                    self.assertFalse(overlaps(excerpt, "shop/order/mutations/order_confirm.py", start, start + 2))
        [audit_page] = [call for call in pages if holds(call, "OrderAudit.perform_mutation")]
        [audit] = [unit for unit in audit_page["brief"]["units"] if unit["symbol"] == "OrderAudit.perform_mutation"]
        self.assertEqual(len(audit["context"]), 8)
        self.assertEqual(len(audit["context_omitted"]), 2)  # named with an address for a separate bounded read
        for item in audit["context_omitted"]:
            self.assertEqual(item["reason"], "dependency_limit")
            self.assertEqual(item["path"], "shop/order/audit.py")
            self.assertTrue(item["symbol"].startswith("check_") and item["start_line"] <= item["end_line"])
        [confirm_page] = [call for call in pages if holds(call, "OrderConfirm.perform_mutation")]
        rows = {row["number"]: row for row in confirm_page["brief"]["shared_context"]}
        [row] = [unit for unit in confirm_page["brief"]["units"] if unit["symbol"] == "OrderConfirm.perform_mutation"]
        self.assertEqual([rows[number]["symbol"] for number in row["context"]], ["OrderConfirm.get_instance"])
        # One helper used by two commands is one row that names both users.
        [common_page] = [call for call in pages if holds(call, "OrderReaudit.perform_mutation")]
        self.assertTrue(holds(common_page, "OrderRecheck.perform_mutation"))
        numbers = {unit["symbol"]: unit["number"] for unit in common_page["brief"]["units"]}
        [common] = [row for row in common_page["brief"]["shared_context"] if row["symbol"] == "common_00"]
        self.assertEqual(sorted(common["used_by"]),
                         sorted([numbers["OrderReaudit.perform_mutation"], numbers["OrderRecheck.perform_mutation"]]))
        summary = bundle["atomic_map"]["summary"]
        self.assertEqual(summary["decisions"], 9)
        self.assertGreaterEqual(summary["context_appearances"], 50)  # 8 + 1 + 1 + 8 * 5 distinct rows at least

    def test_a_declaration_over_120_lines_supplies_only_its_cited_ranges_and_gaps_stay_uncitable(self):
        path = "shop/order/mutations/order_import.py"
        body = "".join(f"        order.field_{step:03d} = rows[{step}]\n" for step in range(130))
        text = (HEADER + "class OrderImport:\n    def perform_mutation(self, order, rows):\n" + body +
                "        order.save()\n        logger.info(\"order.imported\", order_id=order.pk)\n        return order\n")
        save = line_of(text, "order.save()")
        found = [finding("entry_point", "Импорт заказа: начало.", path, 7, 9, "OrderImport.perform_mutation"),
                 finding("state_change", "Импорт заказа: сохранение и лог.", path, save, save + 1,
                         "OrderImport.perform_mutation")]

        def reply(brief, attempt):
            result = atomic_reply(brief)
            if attempt == 1:
                result["operations"][0]["refs"] = [{"path": path, "start_line": 7, "end_line": save + 1}]
            return result
        business = Business(**atomic_handlers(map_atomic=reply))
        bundle, _ = self.run_case(business, files={path: text}, found=found)
        first, repair = [call for call in business.calls if call["task"] == "TASK: map-atomic"]
        [row] = first["brief"]["units"]
        self.assertEqual(row["declaration_supplied"], "cited_ranges_only")
        excerpts = sorted((item["start_line"], item["end_line"]) for item in first["brief"]["excerpts"])
        self.assertEqual(excerpts, [(7, 9), (save, save + 1)])
        self.assertNotIn("order.field_060", first["user"])
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual((error["code"], error["path"]), ("ref_outside_supplied", "operations[0].refs[0]"))
        self.assertEqual(error["missing_ranges"], [{"start_line": 10, "end_line": save - 1}])
        [request] = atomic_requests(bundle)
        self.assertEqual(request["outcome"], "accepted_after_repair")


class AtomicPriorsAndFactsTests(AtomicCase):
    def shared_key_repository(self):
        shared = REFUND.replace("refund.duplicate", "refund.event").replace("refund.completed", "refund.event")
        return atomic_repository({"shop/payment/webhooks.py": shared})

    def test_earlier_log_mappings_guide_retrieval_but_never_become_truth(self):
        from process_observability.direct_atomic import priors_from_map
        priors = priors_from_map(earlier_map(), source="earlier-run")
        files, found = self.shared_key_repository()
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, files=files, found=found, map_priors=priors)
        record = bundle["atomic_map"]
        self.assertEqual(record["priors"]["source"], "earlier-run")
        self.assertRegex(record["priors"]["source_sha256"], r"^[0-9a-f]{64}$")
        resolved = {item["event_key"]: item for item in record["priors"]["mappings"]}
        self.assertEqual({key: item["status"] for key, item in resolved.items()},
                         {"payment.release_funds_canceling": "single_site", "refund.event": "multiple_sites",
                          "never.logged": "key_not_found", "checkout.lines_added": "single_site"})
        self.assertEqual(len(resolved["refund.event"]["site_ids"]), 2)  # one key, two emit sites, kept apart
        self.assertEqual((resolved["never.logged"]["site_ids"], resolved["never.logged"]["unit_ids"]), ([], []))
        self.assertEqual(len(resolved["payment.release_funds_canceling"]["unit_ids"]), 1)
        # Pages show earlier proposals as labelled, evidence-free hints on the exact sites, never as operations.
        sites = [site for call in first_attempts(business) for site in call["brief"]["sites"]]
        [release] = [site for site in sites if site["event_key"] == "payment.release_funds_canceling"]
        [proposal] = release["earlier_proposals"]
        self.assertEqual((proposal["operation_label"], proposal["stage_id"], proposal["lifecycle_role"],
                          proposal["evidence"]), ("Отмена существующего заказа", "order", "result", "none"))
        self.assertNotIn("known_operation", proposal)
        shared = [site for site in sites if site["event_key"] == "refund.event"]
        self.assertEqual(sorted(site["start_line"] for site in shared), [9, 17])
        for site in shared:
            [proposal] = site["earlier_proposals"]
            self.assertEqual(proposal["sites_with_key"], 2)
        # The disagreement is a diagnostic; the old binding never enters the map or coverage.
        mappings = bundle["process_map"]["event_mappings"]
        self.assertFalse([item for item in mappings if item["operation"] == "Отмена существующего заказа"])
        self.assertFalse([op for op in bundle["process_map"]["operations"] if op["name"] == "Отмена существующего заказа"])
        self.assertFalse([item for item in mappings if item["event_key"] == "never.logged"])
        kinds = {(item["event_key"], item["kind"]) for item in record["prior_diagnostics"]}
        self.assertIn(("payment.release_funds_canceling", "role_differs"), kinds)
        # Stages do not exist on pages any more, so an earlier stage label is never compared or adopted.
        self.assertNotIn(("payment.release_funds_canceling", "stage_differs"), kinds)
        self.assertFalse([fact for fact in bundle["log_coverage"]["facts"] if "Отмена существующего" in fact["meaning"]])
        # Units without logs or priors keep their decision.
        self.assertEqual(unit_state(bundle, "CheckoutNoteUpdate.perform_mutation")[1]["state"], "accounted")
        self.assertEqual({state["state"] for state in record["unit_states"].values()}, {"accounted"})

    def test_lifecycle_controls_keep_duplicates_no_ops_and_funds_release_out_of_new_results(self):
        def compose(brief, attempt):
            reply = compose_reply(brief, attempt)
            release = next(op for op in brief["operations"] if op["name"] == "Действие release_funds_task")
            stage = next(fact["stage"] for fact in reply["facts"] if fact["operations"] == [release["number"]])
            # A proposed "new result" of the release action: its records show only no-op and start roles.
            reply["facts"].append({**reply["facts"][0], "stage": stage, "operations": [release["number"]],
                                   "meaning": "Средства корзины без заказа освобождены",
                                   "lifecycle_roles": ["result"]})
            return reply
        bundle, _ = self.run_case(Business(**atomic_handlers(map_compose=compose)))
        refund_ops = [op for op in bundle["process_map"]["operations"] if op["name"] == "Действие handle_refund"]
        self.assertEqual(len(refund_ops), 1)
        duplicate = fact_by_meaning(bundle, "Действие handle_refund: duplicate")
        result = fact_by_meaning(bundle, "Действие handle_refund: result")
        self.assertEqual((duplicate["instrumentation"], duplicate["lifecycle_check"], duplicate["event_keys"]),
                         ("found", "compatible", ["refund.duplicate"]))   # what the duplicate record shows
        self.assertEqual((result["instrumentation"], result["lifecycle_check"]), ("found", "compatible"))
        self.assertNotIn("refund.duplicate", result["event_keys"])  # a duplicate is no new refund
        released = fact_by_meaning(bundle, "Средства корзины без заказа освобождены")
        self.assertEqual((released["instrumentation"], released["event_keys"]), ("unknown", []))  # nothing to do


class AtomicScenarioTests(AtomicCase):
    def test_generated_text_never_becomes_a_required_scenario_and_scenarios_are_off_by_default(self):
        bundle, _ = self.run_case(Business(**atomic_handlers()))
        self.assertEqual(bundle["process_map"]["scenarios"], [])
        self.assertEqual(bundle["atomic_map"]["required_scenarios"], [])  # only approved obligations are required
        self.assertIsNone(bundle["atomic_map"]["code_led"]["obligations"])
        stages = {stage["id"] for stage in bundle["process_map"]["stages"]}
        self.assertTrue(all(op["stage_id"] in stages for op in bundle["process_map"]["operations"]))

    def test_composition_only_groups_accepted_operations_and_cannot_invent_one(self):
        def compose(brief, attempt):
            reply = compose_reply(brief, attempt)
            names = {item["number"]: item["name"] for item in brief["working_groups"]}
            checkout = next(index for index, stage in enumerate(reply["stages"], 1) if stage["name"] == "checkout")
            refund = next(item["number"] for item in brief["operations"] if item["name"] == "Действие handle_refund")
            cart = [item["number"] for item in brief["operations"] if names.get(item["group"]) == "checkout"]
            reply.update(scenarios=[{"name": "Возврат товара", "description": None,
                                     "operations": [99 if attempt == 1 else refund], "obligation": None}],
                         groups=[{"name": "Работа с корзиной", "stage": checkout, "operations": cart}])
            return reply
        business = Business(**atomic_handlers(map_compose=compose))
        bundle, _ = self.run_case(business, compose=True)
        first, repair = [call for call in business.calls if call["task"] == "TASK: map-compose"]
        self.assertFalse({"excerpts", "units", "sites", "shared_context"} & set(first["brief"]))
        self.assertTrue(first["brief"]["scenarios_requested"])
        self.assertEqual(set(first["brief"]["operations"][0]),
                         {"number", "name", "group", "kind", "initiation", "exit", "description", "units", "events"})
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual((error["code"], error["path"]), ("unknown_operation_number", "scenarios[0].operations[0]"))
        refund = next(op["id"] for op in bundle["process_map"]["operations"] if op["name"] == "Действие handle_refund")
        [scenario] = bundle["process_map"]["scenarios"]
        self.assertEqual(scenario["operation_ids"], [refund])
        [group] = bundle["atomic_map"]["groups"]
        names = stage_names(bundle)
        cart = [op["id"] for op in bundle["process_map"]["operations"] if names[op["stage_id"]] == "checkout"]
        self.assertEqual((names[group["stage_id"]], sorted(group["operation_ids"])), ("checkout", sorted(cart)))
        self.assertEqual(len(bundle["process_map"]["operations"]), len(bundle["atomic_map"]["operations"]))
        [request] = atomic_requests(bundle, "compose")
        self.assertEqual(request["outcome"], "accepted_after_repair")

    def test_triage_runs_only_when_selected_or_needed_and_never_creates_operations(self):
        health = HEADER + "class HealthCheck:\n    def perform(self, request):\n        logger.info(\"health.ok\")\n        return \"ok\"\n"
        files, found = atomic_repository({"shop/core/health.py": health},
                                         [finding("entry_point", "Проверка доступности сервиса.", "shop/core/health.py",
                                                  7, 9, "HealthCheck.perform")])

        def triage(brief, attempt):
            reply = {"units": [{"unit": unit["number"],
                                "decision": "outside_process" if unit["symbol"] == "HealthCheck.perform" else "atomic_review",
                                "reason": "Техническая проверка доступности" if unit["symbol"] == "HealthCheck.perform" else None,
                                "question": None} for unit in brief["units"]], "unknowns": []}
            if attempt == 1:
                reply["operations"] = []  # triage has no operations to create
            return reply
        business = Business(**atomic_handlers(map_triage=triage))
        bundle, _ = self.run_case(business, files=files, found=found, triage="on")
        tasks = business.tasks()
        self.assertLess(tasks.index("TASK: map-triage"), tasks.index("TASK: map-atomic"))
        first, repair = [call for call in business.calls if call["task"] == "TASK: map-triage"]
        self.assertNotIn("excerpts", first["brief"])
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual((error["code"], error["path"]), ("unexpected_keys", "$"))
        identifier, state = unit_state(bundle, "HealthCheck.perform")
        self.assertEqual((state["state"], state["disposition"], state["reason"]),
                         ("triaged", "outside_process", "Техническая проверка доступности"))
        self.assertFalse([call for call in business.calls if call["task"] == "TASK: map-atomic"
                          and holds(call, "HealthCheck.perform")])
        others = [item for key, item in bundle["atomic_map"]["unit_states"].items() if key != identifier]
        self.assertEqual({item["state"] for item in others}, {"accounted"})
        self.assertEqual(len(bundle["process_map"]["operations"]), len(bundle["atomic_map"]["operations"]))
        for mode in ("off", "auto"):  # nothing in this small plan needs triage
            with self.subTest(triage=mode):
                quiet = Business(**atomic_handlers())
                self.run_case(quiet, files=files, found=found, triage=mode)
                self.assertNotIn("TASK: map-triage", quiet.tasks())


class AtomicIdentityTests(AtomicCase):
    def settings(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        (root / "repo").mkdir()
        (root / "repo" / "app.py").write_text(HEADER + "def submit(order):\n    order.save()\n", encoding="utf-8")
        snapshot = snapshot_repository(root / "repo", max_files=None, max_chars=None)
        options = ReviewOptions()
        prepared = prepare_review(snapshot, load_guidance(), options)
        return ReviewSettings(graph=prepared["graph"], plan=prepared["plan"], system=prepared["system"], options=options,
                              directory=root, model_fingerprint="scripted-glm", map_mode="atomic")

    def test_business_identity_binds_mode_prompts_bounds_and_priors(self):
        from process_observability import direct_atomic
        from process_observability.direct_atomic import priors_from_map
        review = self.settings()
        base = workflow._review_config(review, "c" * 64)
        variants = {"coarse refine": replace(review, map_mode="coarse_refine"),
                    "page units": replace(review, atomic_page_units=12),
                    "input target": replace(review, atomic_input_tokens=30000),
                    "hard input": replace(review, atomic_input_tokens_hard=70000),
                    "answer": replace(review, atomic_answer_tokens=8000),
                    "triage": replace(review, triage="on"), "compose": replace(review, compose=True),
                    "priors": replace(review, map_priors=priors_from_map(earlier_map(), source="earlier-run"))}
        hashes = {label: workflow._review_config(settings, "c" * 64) for label, settings in variants.items()}
        for label, value in hashes.items():
            with self.subTest(setting=label):
                self.assertNotEqual(value, base)
        other = replace(review, map_priors=priors_from_map(earlier_map("start"), source="earlier-run"))
        self.assertNotEqual(workflow._review_config(other, "c" * 64), hashes["priors"])
        from process_observability.direct_atomic import load_obligations
        with_obligations = replace(review, approved_obligations=load_obligations("Одно требование", source="a.txt"))
        self.assertNotEqual(workflow._review_config(with_obligations, "c" * 64), base)
        self.assertFalse(hasattr(direct_atomic, "SKELETON_SYSTEM"))  # no description-first frame prompt remains
        for name in ("ATOMIC_SYSTEM", "TRIAGE_SYSTEM", "COMPOSE_SYSTEM"):
            with self.subTest(prompt=name), patch.object(direct_atomic, name, getattr(direct_atomic, name) + "\n- x"):
                self.assertNotEqual(workflow._review_config(review, "c" * 64), base)

    def test_technical_review_reduction_and_catalog_are_reused_exactly_across_map_modes(self):
        first, first_out = self.run_case(Business(**refine_handlers()), map_mode="coarse_refine", action_refinement=True)
        self.assertEqual(first["action_refinement"]["status"], "complete")
        self.assertNotIn("atomic_map", first)  # the explicit legacy mode keeps its earlier bundle
        code = Code(atomic_repository()[1])
        second, second_out = self.run_case(Business(**atomic_handlers()), code=code, resume_from=first_out)
        self.assertEqual(code.calls, [])  # no technical review and no reduction request
        summary = second["code_review"]["summary"]
        self.assertEqual((summary["model_calls"], summary["cache_hits"]), (0, summary["base_tasks"]))
        self.assertEqual(second["action_catalog"]["catalog_sha256"], first["action_catalog"]["catalog_sha256"])
        context = [json.loads((out / "review" / "run.json").read_text(encoding="utf-8"))["binding"]["context"]
                   for out in (first_out, second_out)]
        self.assertEqual(context[0], context[1])
        self.assertNotEqual(second["manifest"]["provenance"]["config_sha256"],
                            first["manifest"]["provenance"]["config_sha256"])

    def test_an_old_coarse_map_reply_is_never_replayed_into_code_led_pages(self):
        recorded = {}

        class Recording(Business):
            def complete_json(self, system, user):
                reply = super().complete_json(system, user)
                recorded[request_fingerprint(system, user)] = copy.deepcopy(reply)
                return reply
        _, first_out = self.run_case(Recording(**refine_handlers()), map_mode="coarse_refine", action_refinement=True)
        replay = first_out.parent / "replay.json"
        replay.write_text(json.dumps({"data_origin": "synthetic", "records": recorded}, ensure_ascii=False),
                          encoding="utf-8")
        model, seen = RecordedModel(replay), []

        class Watching:
            def complete_json(self, system, user):
                seen.append(system.split("\n", 1)[0])
                return model.complete_json(system, user)
        bundle, _ = self.run_case(Watching(), resume_from=first_out)
        self.assertTrue(seen)
        self.assertNotIn("TASK: map-skeleton", seen)  # no frame request exists to be answered by an old reply
        self.assertEqual(seen[0], "TASK: map-atomic")  # a replay miss, never another task's answer
        self.assertFalse([item for item in atomic_requests(bundle)
                          if item["outcome"] in ("accepted", "accepted_after_repair", "accepted_from_checkpoint")])
        self.assertEqual(bundle["atomic_map"]["status"], "failed")
        self.assertEqual(bundle["process_map"]["operations"], [])
        self.assertEqual(self.code.calls, [])

    def test_accepted_atomic_pages_are_reused_only_under_their_exact_request_identity(self):
        first, first_out = self.run_case(Business(**atomic_handlers()))
        saved = sorted((first_out / "business_checkpoints" / "map-atomic").glob("*.json"))
        self.assertEqual(len(saved), len(atomic_requests(first)))
        for path in saved:
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((stored["schema"], stored["identity_fields"]["task"]),
                             ("atomic-page-checkpoint/1", "map-atomic"))
        business = Business(**atomic_handlers())
        second, _ = self.run_case(business, resume_from=first_out)
        self.assertIn("TASK: map-compose", business.tasks())  # the composition is not checkpointed: asked again
        self.assertNotIn("TASK: map-atomic", business.tasks())
        requests = atomic_requests(second)
        self.assertEqual({item["outcome"] for item in requests}, {"accepted_from_checkpoint"})
        self.assertEqual([item["api_request_sha256"] for item in requests],
                         [item["api_request_sha256"] for item in atomic_requests(first)])
        self.assertEqual([op["id"] for op in second["process_map"]["operations"]],
                         [op["id"] for op in first["process_map"]["operations"]])
        self.assertEqual(second["atomic_map"]["status"], "complete")
        # Another bound is another identity: nothing is reused and the pages are asked again.
        changed = Business(**atomic_handlers())
        self.run_case(changed, resume_from=first_out, atomic_page_units=9)
        self.assertIn("TASK: map-atomic", changed.tasks())

    def test_invalid_atomic_settings_stop_before_any_model_call(self):
        cases = {"mode": {"map_mode": "fast"}, "page units 0": {"atomic_page_units": 0},
                 "page units 129": {"atomic_page_units": 129}, "triage": {"triage": "maybe"},
                 "compose": {"compose": "yes"},
                 "hard below target": {"atomic_input_tokens": 50000, "atomic_input_tokens_hard": 40000},
                 "above the safety ceiling": {"atomic_input_tokens_hard": 1000001},
                 "answer": {"atomic_answer_tokens": 0}, "priors": {"map_priors": {"schema": "atomic-priors/0"}},
                 "obligations": {"approved_obligations": {"schema": "approved-obligations/0"}}}
        for label, settings in cases.items():
            with self.subTest(case=label):
                business = Business(**atomic_handlers())
                with self.assertRaises(WorkflowError):
                    self.run_case(business, **settings)
                self.assertEqual((business.calls, self.code.calls), ([], []))

    def test_explicit_coarse_refine_mode_keeps_the_legacy_requests(self):
        business = Business(**refine_handlers())
        bundle, _ = self.run_case(business, map_mode="coarse_refine", action_refinement=True)
        tasks = business.tasks()
        self.assertEqual(tasks[:2], ["TASK: map", "TASK: map-scope"])
        self.assertFalse({"TASK: map-skeleton", "TASK: map-atomic", "TASK: map-triage", "TASK: map-compose"} & set(tasks))
        self.assertEqual(bundle["action_refinement"]["schema"], "action-refinement/1")


if __name__ == "__main__":
    unittest.main()
