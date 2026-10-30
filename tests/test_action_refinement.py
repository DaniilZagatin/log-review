"""Action refinement inside the map stage (plan A2/A3) through the ordinary review-mode run_analysis.

Real frozen snapshot, graph, logging inventory, code review, reduction and catalog; scripted code and business
models read the exact request JSON. These checks cover host wiring, retrieval, budgets, accounting and repair
feedback; they say nothing about the semantic quality of a live model.
"""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from process_observability import workflow
from process_observability.action_catalog import check_catalog
from process_observability.action_refinement import _RESERVE
from process_observability.code_review import ReviewOptions
from process_observability.guidance import load_guidance
from process_observability.llm import LLMError
from process_observability.source_tools import snapshot_repository
from process_observability.workflow import ReviewSettings, prepare_review, run_analysis

HEADER = "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"


def mutation(name, effect, key, body=()):
    """class on line 6, perform_mutation from line 7 to its return; extra body lines go before the save."""
    lines = [f"class {name}:", "    def perform_mutation(self, checkout, value):", f"        {effect}",
             *(f"        {line}" for line in body), "        checkout.save()",
             f"        logger.info(\"{key}\", checkout_id=checkout.pk)", "        return checkout"]
    return HEADER + "\n".join(lines) + "\n"


LINES_ADD = (HEADER + "def add_variants(checkout, lines):\n    for line in lines:\n        checkout.lines.append(line)\n"
             "    return checkout\n\n\nclass CheckoutLinesAdd:\n    def perform_mutation(self, checkout, lines):\n"
             "        if not lines:\n            raise ValueError(\"no lines\")\n"
             "        checkout = add_variants(checkout, lines)\n        checkout.save()\n"
             "        logger.info(\"checkout.lines_added\", checkout_id=checkout.pk)\n        return checkout\n")
ORDER_CANCEL = (HEADER + "def clean_order_cancel(order):\n    if order is None or not order.can_cancel():\n"
                "        raise ValueError(\"CANNOT_CANCEL_ORDER\")\n\n\nclass OrderCancel:\n"
                "    def perform_mutation(self, order):\n        clean_order_cancel(order)\n"
                "        order.status = \"canceled\"\n        order.save()\n"
                "        logger.info(\"order.canceled\", order_id=order.pk)\n        return order\n")
ORDER_CONFIRM = (HEADER + "class OrderConfirm:\n    def get_instance(self, order):\n"
                 "        if order.status != \"unconfirmed\":\n            raise ValueError(\"INVALID\")\n"
                 "        return order\n\n    def perform_mutation(self, order):\n        order = self.get_instance(order)\n"
                 "        order.status = \"unfulfilled\"\n        order.save()\n"
                 "        logger.info(\"order.confirmed\", order_id=order.pk)\n        return order\n")
RELEASE = (HEADER + "def release_funds_task(transactions):\n"
           "    pending = [item for item in transactions if item.order_id is None]\n    if not pending:\n"
           "        logger.warning(\"payment.release_funds_noop\")\n        return 0\n    for item in pending:\n"
           "        logger.info(\"payment.release_funds_canceling\", transaction_id=item.pk)\n        item.cancel()\n"
           "    return len(pending)\n")
REFUND = (HEADER + "def handle_refund(event, payment):\n    logger.info(\"refund.requested\", payment_id=payment.pk)\n"
          "    if payment.refund_processed:\n        logger.info(\"refund.duplicate\", payment_id=payment.pk)\n"
          "        return None\n    logger.info(\"refund.started\", payment_id=payment.pk)\n    try:\n"
          "        payment.refund(event.amount)\n    except RuntimeError:\n"
          "        logger.error(\"refund.failed\", payment_id=payment.pk)\n        raise\n"
          "    logger.info(\"refund.completed\", payment_id=payment.pk)\n    return payment\n")
COMMANDS = {"lines_delete.py": ("CheckoutLinesDelete", "checkout.lines.remove(value)", "checkout.lines_deleted"),
            "lines_update.py": ("CheckoutLinesUpdate", "checkout.lines.update(value)", "checkout.lines_updated"),
            "shipping_address_update.py": ("CheckoutShippingAddressUpdate", "checkout.shipping_address = value",
                                           "checkout.shipping_address_updated"),
            "billing_address_update.py": ("CheckoutBillingAddressUpdate", "checkout.billing_address = value",
                                          "checkout.billing_address_updated")}
CHECKOUT = "shop/checkout/mutations/"


def finding(kind, description, path, start, end, symbol):
    return {"id": f"{kind}-{path}-{start}", "kind": kind, "description": description,
            "refs": [{"path": path, "start_line": start, "end_line": end, "symbol": symbol}]}


def repository(extra_files=None, extra_findings=()):
    files = {CHECKOUT + "lines_add.py": LINES_ADD, "shop/order/mutations/order_cancel.py": ORDER_CANCEL,
             "shop/order/mutations/order_confirm.py": ORDER_CONFIRM, "shop/payment/tasks.py": RELEASE,
             "shop/payment/webhooks.py": REFUND}
    found = [
        finding("entry_point", "Добавление строк корзины: проверка, add_variants, сохранение, checkout.lines_added.",
                CHECKOUT + "lines_add.py", 13, 19, "CheckoutLinesAdd.perform_mutation"),
        finding("entry_point", "add_variants добавляет строки в корзину.", CHECKOUT + "lines_add.py", 6, 9, "add_variants"),
        finding("error", "Отмена невозможна: CANNOT_CANCEL_ORDER.", "shop/order/mutations/order_cancel.py", 6, 8,
                "clean_order_cancel"),
        finding("transaction_boundary", "Отмена заказа: статус canceled, сохранение, лог order.canceled.",
                "shop/order/mutations/order_cancel.py", 12, 17, "OrderCancel.perform_mutation"),
        finding("error", "Подтверждение только неподтверждённого заказа, иначе INVALID.",
                "shop/order/mutations/order_confirm.py", 7, 10, "OrderConfirm.get_instance"),
        finding("state_change", "Подтверждение заказа: статус unfulfilled и сохранение.",
                "shop/order/mutations/order_confirm.py", 12, 17, "OrderConfirm.perform_mutation"),
        finding("entry_point", "Задача освобождения средств по транзакциям без заказа (order_id None).",
                "shop/payment/tasks.py", 6, 14, "release_funds_task"),
        finding("integration", "Обработка уведомления о возврате: повтор уже обработанного, ошибка и успех.",
                "shop/payment/webhooks.py", 6, 18, "handle_refund"),
    ]
    for name, (cls, effect, key) in sorted(COMMANDS.items()):
        files[CHECKOUT + name] = mutation(cls, effect, key)
        found.append(finding("entry_point", f"{cls}: своё изменение корзины, сохранение и лог {key}.",
                             CHECKOUT + name, 7, 11, f"{cls}.perform_mutation"))
    files.update(extra_files or {})
    return files, found + list(extra_findings)


class Code:
    """Review replies hold the scripted findings inside the owned intervals; one reducer summary per batch."""

    def __init__(self, findings):
        self.findings, self.calls, self.lock = findings, [], threading.Lock()

    def complete_json(self, system, user):
        with self.lock:
            self.calls.append(system.split("\n", 1)[0])
        brief = json.loads(user)
        if system.startswith("TASK: code-reduce"):
            return {"summaries": [{"description": "Корзина, заказ, оплата и возвраты.",
                                   "source_ids": [item["id"] for item in brief["items"]]}]}
        owned = brief["owned_intervals"]
        inside = [item for item in self.findings if any(
            part["path"] == item["refs"][0]["path"] and part["start_line"] <= item["refs"][0]["start_line"]
            and item["refs"][0]["end_line"] <= part["end_line"] for part in owned)]
        return {"findings": copy.deepcopy(inside), "requests": [], "unknowns": [],
                "interval_status": [{"interval_id": part["id"], "status": "reviewed", "note": ""} for part in owned]}


class Business:
    """Handlers by task line; a handler gets (brief, attempt) and returns a reply or an exception to raise."""

    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    def complete_json(self, system, user):
        task = system.split("\n", 1)[0]
        brief = json.loads(user)
        self.calls.append({"task": task, "system": system, "user": user, "brief": brief})
        handler = self.handlers[task.split(": ", 1)[1].replace("-", "_")]
        reply = handler(brief, 2 if "validation_feedback" in brief else 1) if callable(handler) else handler
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)

    def tasks(self):
        return [call["task"] for call in self.calls]

    def of(self, task, stage=None):
        return [call for call in self.calls if call["task"] == task
                and (stage is None or call["brief"].get("stage", {}).get("id") == stage)]


WEBHOOK_REF = {"path": "shop/payment/webhooks.py", "start_line": 6, "end_line": 18, "symbol": "handle_refund"}
TASKS_REF = {"path": "shop/payment/tasks.py", "start_line": 6, "end_line": 14, "symbol": "release_funds_task"}


def mapping_item(key, operation_id, role, refs=()):
    return {"event_key": key, "business_action": "Возврат и отмена", "operation": "операция", "operation_id":
            operation_id, "stage_id": "payment", "lifecycle_role": role, "occurrence_rule": "Одна запись",
            "refs": list(refs)}


def fact_item(identifier, meaning, key, roles=None):
    item = {"id": identifier, "stage_id": "payment", "meaning": meaning, "rationale": "Учёт результата",
            "scope": "Все случаи", "condition": None, "metric_ids": [], "guidance_ids": ["FACT-QUESTION"],
            "evidence_alternatives": [], "event_keys": [key], "refs": []}
    if roles is not None:
        item["lifecycle_roles"] = list(roles)
    return item


def coarse_map():
    def op(identifier, name, stage, actor=None):
        return {"id": identifier, "name": name, "stage_id": stage, "kind": "operation", "description": None,
                "actor": actor, "event_keys": [], "refs": []}
    mapping, fact = mapping_item, fact_item
    return {
        "process": {"purpose": "Покупка в интернет-магазине", "boundaries": None, "case_definition": "Одна корзина"},
        "stages": [{"id": "checkout", "name": "Оформление корзины", "roles": ["покупатель"], "systems": [], "refs": []},
                   {"id": "order", "name": "Обработка заказа", "roles": ["менеджер"], "systems": [], "refs": []},
                   {"id": "payment", "name": "Оплата и возвраты", "roles": [], "systems": [], "refs": []}],
        "transitions": [{"from_stage": "checkout", "to_stage": "order", "condition": None, "refs": []}],
        "operations": [op("cart-manage", "Управление составом корзины", "checkout", "Покупатель"),
                       op("order-handle", "Обработка заказа", "order"), op("refund", "Возврат средств", "payment"),
                       op("order-cancel", "Отмена существующего заказа", "payment")],
        "operation_transitions": [],
        "scenarios": [{"id": "buy", "name": "Покупка", "description": None, "operation_ids": ["cart-manage"],
                       "transition_ids": []}],
        "event_mappings": [mapping("refund.completed", "refund", "result"), mapping("refund.duplicate", "refund", "unknown"),
                           mapping("payment.release_funds_canceling", "order-cancel", "result")],
        "facts": [fact("refund.done", "Возврат выполнен", "refund.completed", ["result"]),
                  fact("refund.repeat", "Получено повторное уведомление о возврате", "refund.duplicate", ["duplicate"]),
                  fact("order.canceled", "Существующий заказ отменён", "payment.release_funds_canceling", ["result"]),
                  fact("refund.any", "Запись о возврате без объявленной роли", "refund.completed")],
        "unknowns": []}


def scope_reply(brief, attempt=1):
    numbers = {stage["id"]: stage["number"] for stage in brief["stages"]}

    def stages(area):
        return [numbers["payment" if "payment" in area else "order" if "order" in area else "checkout"]]
    return {"areas": [{"area": item["number"], "disposition": "stages", "stages": stages(item["area"]), "reason": None,
                       "question": None} for item in brief["areas"]], "unknowns": []}


ROLES = {"refund.requested": "request", "refund.started": "start", "refund.completed": "result",
         "refund.failed": "error", "refund.duplicate": "duplicate", "payment.release_funds_noop": "no_op"}


def site_ref(site):
    return {"path": site["path"], "start_line": site["start_line"], "end_line": site["end_line"], "symbol": "site"}


def refine_reply(brief, attempt=1, *, known_for=None, exclude=(), parent=None, extra=(), role_of=None):
    """One new operation per candidate, every keyed site mapped to its candidate's operation, no transitions."""
    operations, accounting, mappings, position = [], [], [], {}
    parents = {row["name"]: row["number"] for row in brief["known_operations"] if row["origin"] == "coarse_map"}
    for candidate in brief["candidates"]:
        if candidate["symbol"] in exclude:
            accounting.append({"candidate": candidate["number"], "disposition": "excluded", "new_operations": [],
                               "known_operations": [], "exclusion": "helper", "reason": "Вызывается другой командой",
                               "question": None})
            continue
        operations.append({"name": "Действие " + candidate["symbol"], "description": None, "actor": None,
                           "kind": "operation", "initiation": "api_call", "initiation_basis": None,
                           "parent_operation": parents.get(parent), "refs": []})
        position[candidate["number"]] = len(operations)
        accounting.append({"candidate": candidate["number"], "disposition": "realized",
                           "new_operations": [len(operations)], "known_operations": [], "exclusion": None,
                           "reason": None, "question": None})
    for site in brief["sites"]:
        known = known_for(site, brief) if known_for else None
        owner = position.get(site["candidates"][0])
        if site["event_key"] is None or (known is None and owner is None):
            continue
        role = role_of(site) if role_of else ROLES.get(site["event_key"], "unknown")
        mappings.append({"site": site["number"], "new_operation": None if known else owner, "known_operation": known,
                         "lifecycle_role": role, "role_basis": None if role == "unknown" else "Условие вокруг вызова",
                         "occurrence_rule": "Одна запись на выполнение команды", "ambiguity": None,
                         "refs": [] if role == "unknown" else [site_ref(site)]})
    return {"operations": operations + list(extra), "candidate_accounting": accounting, "event_mappings": mappings,
            "operation_transitions": [], "unknowns": []}


def handlers(**overrides):
    base = {"map": coarse_map(), "map_scope": scope_reply, "map_refine": refine_reply,
            "instrumentation": {"cards": []}, "metrics": {"metrics": []}}
    return {**base, **overrides}


class RefinementCase(unittest.TestCase):
    def run_case(self, business, *, files=None, found=None, options=None, guidance=None, trace=None,
                 business_text="", **settings):
        default_files, default_found = repository()
        files, found = files or default_files, default_found if found is None else found
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        for relative, text in files.items():
            path = root / "repo" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        snapshot = snapshot_repository(root / "repo", max_files=None, max_chars=None)
        guidance = guidance or load_guidance()
        options = options or ReviewOptions()
        prepared = prepare_review(snapshot, guidance, options)
        (root / "out").mkdir()
        settings.setdefault("action_refinement", True)
        review = ReviewSettings(graph=prepared["graph"], plan=prepared["plan"], system=prepared["system"],
                                options=options, directory=root / "out", model_fingerprint="scripted-glm", **settings)
        self.code = Code(found)
        bundle = run_analysis(snapshot, business, run_id="refine-test", data_origin="synthetic", model_mode="fake",
                              guidance=guidance, code_model=self.code, review=review, trace=trace,
                              business_text=business_text)
        return bundle, root / "out"

    @staticmethod
    def candidate(bundle, symbol):
        [(identifier, head)] = [(key, value) for key, value in bundle["action_refinement"]["candidates"].items()
                                if value["symbol"] == symbol]
        return identifier, head

    def operation_of(self, bundle, symbol):
        _, head = self.candidate(bundle, symbol)
        [state] = [item for item in head["stages"] if item["state"] == "accounted"]
        [op_id] = state["operation_ids"]
        [operation] = [op for op in bundle["process_map"]["operations"] if op["id"] == op_id]
        return operation

    @staticmethod
    def anchors(bundle, ids):
        registry = {item["id"]: item for item in bundle["evidence"]}
        return {(registry[key]["source_anchor"]["path"], registry[key]["source_anchor"]["start_line"],
                 registry[key]["source_anchor"]["end_line"]) for key in ids}


class RefinementTests(RefinementCase):
    def test_each_command_gets_its_own_sources_and_operation_and_every_candidate_is_accounted(self):
        business = Business(**handlers())
        bundle, out = self.run_case(business)
        self.assertEqual(business.tasks(), ["TASK: map", "TASK: map-scope", "TASK: map-refine", "TASK: map-refine",
                                            "TASK: map-refine", "TASK: instrumentation", "TASK: metrics"])
        self.assertTrue(all(state["status"] == "completed" for state in bundle["manifest"]["stages"].values()))
        record = bundle["action_refinement"]
        self.assertEqual((record["status"], record["summary"]["requests_by_outcome"]), ("complete", {"accepted": 4}))
        [checkout] = business.of("TASK: map-refine", "checkout")
        brief = checkout["brief"]
        descriptions = {finding["description"] for candidate in brief["candidates"] for finding in candidate["findings"]}
        for name, (cls, _, key) in COMMANDS.items():
            with self.subTest(command=cls):
                # The complete review finding text and its own frozen excerpt are in the request.
                self.assertIn(f"{cls}: своё изменение корзины, сохранение и лог {key}.", descriptions)
                [excerpt] = [item for item in brief["excerpts"] if item["path"] == CHECKOUT + name]
                self.assertEqual((excerpt["start_line"], excerpt["end_line"]), (7, 11))
                self.assertIn(f"9: {' ' * 8}checkout.save()", excerpt["text"])
                operation = self.operation_of(bundle, f"{cls}.perform_mutation")
                self.assertEqual(self.anchors(bundle, operation["evidence_ids"]), {(CHECKOUT + name, 7, 11)})
                self.assertEqual(operation["event_keys"], [key])
        names = [op["name"] for op in bundle["process_map"]["operations"] if op["stage_id"] == "checkout"]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("Управление составом корзины", names)  # the coarse step is kept, never replaced
        self.assertEqual(bundle["process_map"]["operation_transitions"], [])  # no order was invented
        for head in record["candidates"].values():
            self.assertEqual(head["scope"]["state"], "assigned")
            self.assertEqual([state["state"] for state in head["stages"]], ["accounted"])
        catalog = json.loads((out / "action_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(check_catalog(catalog)["catalog_sha256"], record["catalog"]["catalog_sha256"])
        self.assertEqual(json.loads((out / "action_refinement.json").read_text(encoding="utf-8")), record)
        # A cited excerpt shows a create/change effect: the save line of its own command, not another file.
        add = self.operation_of(bundle, "CheckoutLinesAdd.perform_mutation")
        self.assertEqual(self.anchors(bundle, add["evidence_ids"]), {(CHECKOUT + "lines_add.py", 13, 19)})

    def test_lifecycle_records_are_one_operation_and_duplicates_or_foreign_emits_cover_nothing(self):
        def known_for(site, brief):
            if site["event_key"] != "payment.release_funds_canceling":
                return None
            return next(row["number"] for row in brief["known_operations"] if row["name"] == "Отмена существующего заказа")
        business = Business(**handlers(map_refine=lambda brief, attempt: refine_reply(brief, known_for=known_for)))
        bundle, _ = self.run_case(business)
        refund = self.operation_of(bundle, "handle_refund")
        refined = [item for item in bundle["process_map"]["event_mappings"] if item["operation_id"] == refund["id"]]
        self.assertEqual(sorted((item["event_key"], item["lifecycle_role"], item["role_support"]) for item in refined),
                         [("refund.completed", "result", "emit_within_operation_sources"),
                          ("refund.duplicate", "duplicate", "emit_within_operation_sources"),
                          ("refund.failed", "error", "emit_within_operation_sources"),
                          ("refund.requested", "request", "emit_within_operation_sources"),
                          ("refund.started", "start", "emit_within_operation_sources")])
        self.assertEqual(sum(op["name"] == "Действие handle_refund" for op in bundle["process_map"]["operations"]), 1)
        facts = {fact["id"]: (fact["instrumentation"], fact["lifecycle_check"]) for fact in bundle["log_coverage"]["facts"]}
        self.assertEqual(facts["refund.done"], ("found", "compatible"))
        # The repeated notification is exactly what its duplicate record observes: covered, without a new refund.
        self.assertEqual(facts["refund.repeat"], ("found", "compatible"))
        # A fact without declared lifecycle roles cannot use an assessed site: unknown, and the reason is explicit.
        self.assertEqual(facts["refund.any"], ("unknown", "requirement_undeclared"))
        # Releasing funds of carts (order_id None) mapped to cancelling an order: the emit lies in another candidate.
        [foreign] = [item for item in bundle["process_map"]["event_mappings"]
                     if item["operation_id"] == "order-cancel" and item["role_support"] != "not_assessed"]
        self.assertEqual((foreign["role_support"], foreign["reconciliation"]),
                         ("emit_outside_operation_sources", "unsupported"))
        self.assertEqual(facts["order.canceled"], ("unknown", "ambiguous"))
        self.assertTrue(any("payment.release_funds_canceling" in text and "не подтверждена" in text
                            for text in bundle["process_map"]["unknowns"]))
        noop = [item for item in bundle["process_map"]["event_mappings"]
                if item["event_key"] == "payment.release_funds_noop"]
        self.assertEqual([item["lifecycle_role"] for item in noop], ["no_op"])

    def lifecycle_of(self, bundle):
        return {fact["id"]: (fact["instrumentation"], fact["lifecycle_check"]) for fact in bundle["log_coverage"]["facts"]}

    def test_drafted_result_bindings_with_valid_refs_cannot_bypass_refined_evidence(self):
        """The confirmed reproduction: drafted bindings cite real ranges, refinement disagrees on the same sites."""
        drafted = coarse_map()
        for item in drafted["event_mappings"]:
            if item["event_key"] == "refund.duplicate":
                item.update(lifecycle_role="result", refs=[WEBHOOK_REF])
            elif item["event_key"] == "payment.release_funds_canceling":
                item.update(refs=[TASKS_REF])
        drafted["facts"].append(fact_item("refund.new", "Выполнен новый возврат", "refund.duplicate", ["result"]))

        def known_for(site, brief):
            if site["event_key"] != "payment.release_funds_canceling":
                return None
            return next(row["number"] for row in brief["known_operations"] if row["name"] == "Отмена существующего заказа")
        business = Business(**handlers(map=drafted, map_refine=lambda brief, attempt: refine_reply(brief, known_for=known_for)))
        bundle, _ = self.run_case(business)
        facts = self.lifecycle_of(bundle)
        self.assertEqual(facts["refund.new"], ("unknown", "ambiguous"))      # drafted result vs refined duplicate
        self.assertEqual(facts["order.canceled"], ("unknown", "ambiguous"))  # refined: emit outside the operation
        self.assertEqual(facts["refund.repeat"], ("unknown", "ambiguous"))   # a refined label is not trusted over a conflict
        self.assertEqual(facts["refund.done"], ("found", "compatible"))      # a separate, clean result site
        # Every proposal is retained and marked by the host; the earlier result bindings were shown to the model.
        states = {(item["event_key"], item["lifecycle_role"], item["role_support"]): item["reconciliation"]
                  for item in bundle["process_map"]["event_mappings"]}
        self.assertEqual(states[("refund.duplicate", "result", "not_assessed")], "contradicted")
        self.assertEqual(states[("refund.duplicate", "duplicate", "emit_within_operation_sources")], "contradicted")
        self.assertEqual(states[("payment.release_funds_canceling", "result", "not_assessed")], "unsupported")
        [payment] = business.of("TASK: map-refine", "payment")
        [duplicate_site] = [site for site in payment["brief"]["sites"] if site["event_key"] == "refund.duplicate"]
        self.assertEqual([item["lifecycle_role"] for item in duplicate_site["earlier_mappings"]], ["result"])
        unknowns = bundle["process_map"]["unknowns"]
        self.assertTrue(any("refund.duplicate (shop/payment/webhooks.py:9)" in text and "расходятся" in text
                            for text in unknowns))
        self.assertTrue(any("payment.release_funds_canceling (shop/payment/tasks.py:12)" in text and "ранняя" in text
                            for text in unknowns))
        [site] = [item for item in bundle["coverage_lifecycle"]["sites"] if item["event_key"] == "refund.duplicate"]
        self.assertEqual((site["start_line"], site["status"], site["roles"]), (9, "contradicted", ["duplicate", "result"]))
        self.assertEqual(len(site["mapping_indexes"]), 2)

    def test_a_duplicate_fact_is_supported_by_its_own_site_without_a_new_result(self):
        drafted = coarse_map()
        for item in drafted["event_mappings"]:
            if item["event_key"] == "refund.duplicate":
                item.update(lifecycle_role="duplicate", refs=[WEBHOOK_REF])
        drafted["facts"].append(fact_item("refund.new", "Выполнен новый возврат", "refund.duplicate", ["result"]))

        def reply(brief, attempt):  # the atomic actions detail the drafted refund step: no operation conflict
            return refine_reply(brief, parent="Возврат средств" if brief["stage"]["id"] == "payment" else None)
        for refinement in (True, False):
            with self.subTest(refinement=refinement):
                bundle, _ = self.run_case(Business(**handlers(map=drafted, map_refine=reply)),
                                          action_refinement=refinement)
                facts = self.lifecycle_of(bundle)
                self.assertEqual(facts["refund.repeat"], ("found", "compatible"))
                self.assertEqual(facts["refund.new"], ("unknown", "no_compatible_site"))
                [site] = [item for item in bundle["coverage_lifecycle"]["sites"] if item["event_key"] == "refund.duplicate"]
                self.assertEqual(site["status"], "consistent" if refinement else "not_assessed")

    def test_two_sites_of_one_key_stay_separate_and_conflicts_are_explained(self):
        shared = REFUND.replace("refund.duplicate", "refund.event").replace("refund.completed", "refund.event")
        files, found = repository({"shop/payment/webhooks.py": shared})
        drafted = coarse_map()
        drafted["event_mappings"].append(mapping_item("refund.event", "refund", "result", [WEBHOOK_REF]))
        drafted["facts"] += [fact_item("refund.event.done", "Выполнен новый возврат", "refund.event", ["result"]),
                             fact_item("refund.event.repeat", "Повторное уведомление", "refund.event", ["duplicate"])]

        def role_of(site):
            if site["event_key"] == "refund.event":
                return {9: "duplicate", 17: "result"}[site["start_line"]]
            return ROLES.get(site["event_key"], "unknown")

        def reply(brief, attempt):
            return refine_reply(brief, role_of=role_of,
                                parent="Возврат средств" if brief["stage"]["id"] == "payment" else None)
        bundle, _ = self.run_case(Business(**handlers(map=drafted, map_refine=reply)), files=files, found=found)
        facts = self.lifecycle_of(bundle)
        self.assertEqual(facts["refund.event.done"], ("found", "compatible"))      # line 17 is a clean result site
        self.assertEqual(facts["refund.event.repeat"], ("unknown", "ambiguous"))   # line 9: drafted result vs duplicate
        sites = {item["start_line"]: item for item in bundle["coverage_lifecycle"]["sites"]
                 if item["event_key"] == "refund.event"}
        self.assertEqual({line: site["status"] for line, site in sites.items()}, {9: "contradicted", 17: "consistent"})
        [done] = [item for item in bundle["coverage_lifecycle"]["facts"] if item["fact_id"] == "refund.event.done"]
        order = bundle["coverage_lifecycle"]["sites"]
        self.assertEqual([order[index]["start_line"] for index in done["event_keys"][0]["usable_sites"]], [17])
        refined = [item for item in bundle["process_map"]["event_mappings"]
                   if item["event_key"] == "refund.event" and item["role_support"] != "not_assessed"]
        self.assertEqual(sorted(item["lifecycle_role"] for item in refined), ["duplicate", "result"])  # both retained
        self.assertTrue(any("refund.event (shop/payment/webhooks.py:9)" in text for text in bundle["process_map"]["unknowns"]))
        self.assertFalse(any("refund.event (shop/payment/webhooks.py:17)" in text
                             for text in bundle["process_map"]["unknowns"]))

    def test_helper_retrieved_by_static_navigation_is_supplied_and_citable(self):
        related = {}

        def cancel(brief, attempt):
            reply = refine_reply(brief)
            for index, candidate in enumerate(brief["candidates"]):
                related[candidate["symbol"]] = [(item["relation"], item["symbol"]) for item in candidate["related"]]
                if candidate["symbol"] == "OrderCancel.perform_mutation":
                    reply["operations"][index]["refs"] = [{"path": "shop/order/mutations/order_cancel.py",
                                                           "start_line": 6, "end_line": 8, "symbol": "clean_order_cancel"}]
            return reply
        business = Business(**handlers(map_refine=cancel))
        bundle, _ = self.run_case(business)
        self.assertEqual(related["OrderCancel.perform_mutation"], [("static_call", "clean_order_cancel")])
        self.assertEqual(related["OrderConfirm.perform_mutation"], [("static_call", "OrderConfirm.get_instance")])
        self.assertEqual(related["CheckoutLinesAdd.perform_mutation"], [])  # a candidate helper is not "related"
        operation = self.operation_of(bundle, "OrderCancel.perform_mutation")
        self.assertEqual(self.anchors(bundle, operation["evidence_ids"]),
                         {("shop/order/mutations/order_cancel.py", 6, 8), ("shop/order/mutations/order_cancel.py", 12, 17)})
        self.assertNotIn("OrderConfirm.get_instance", [head["symbol"] for head in
                                                       bundle["action_refinement"]["candidates"].values()])

    def test_interaction_needs_its_own_cited_handler_source(self):
        button = ("export function CartButton({ onAdd }) {\n  function handleClick() {\n    onAdd();\n  }\n"
                  "  return <button onClick={handleClick}>Add</button>;\n}\n")
        files, found = repository({"web/src/CartButton.tsx": button},
                                  [finding("entry_point", "Обработчик нажатия кнопки вызывает onAdd.",
                                           "web/src/CartButton.tsx", 2, 4, "CartButton.handleClick")])

        def reply(brief, attempt):
            result = refine_reply(brief)
            for index, candidate in enumerate(brief["candidates"]):
                if candidate["path"] == "web/src/CartButton.tsx":
                    result["operations"][index].update(
                        initiation="user_interaction", initiation_basis="Обработчик onClick кнопки в исходнике",
                        refs=[{"path": "web/src/CartButton.tsx", "start_line": 2, "end_line": 4}])
            return result
        bundle, _ = self.run_case(Business(**handlers(map_refine=reply)), files=files, found=found)
        [operation] = [item for item in bundle["action_refinement"]["operations"]
                       if item["initiation"] == "user_interaction"]
        self.assertEqual(operation["initiation_basis"], "Обработчик onClick кнопки в исходнике")
        self.assertEqual(self.anchors(bundle, operation["model_evidence_ids"]), {("web/src/CartButton.tsx", 2, 4)})
        others = {item["initiation"] for item in bundle["action_refinement"]["operations"] if item is not operation}
        self.assertEqual(others, {"api_call"})  # backend commands were never turned into clicks

    def test_unsupplied_range_and_a_gap_with_code_are_rejected_with_exact_repair_feedback(self):
        def gap(brief, attempt):
            reply = refine_reply(brief)
            if attempt == 1 and brief["stage"]["id"] == "checkout":
                reply["operations"][0]["refs"] = [{"path": CHECKOUT + "lines_add.py", "start_line": 6, "end_line": 19,
                                                   "symbol": "x"}]
            return reply
        business = Business(**handlers(map_refine=gap))
        bundle, _ = self.run_case(business)
        first, repair = business.of("TASK: map-refine", "checkout")
        self.assertEqual(repair["system"], first["system"] + "\n\n" + workflow._REPAIR_SYSTEM)
        feedback = repair["brief"].pop("validation_feedback")
        self.assertEqual(repair["brief"], first["brief"])
        [error] = feedback["errors"]
        self.assertEqual((error["code"], error["path"]), ("ref_outside_supplied", "operations[0].refs[0]"))
        # Lines 10-11 are blank but line 12 holds the unsupplied class line: the whole gap is reported.
        self.assertEqual(error["missing_ranges"], [{"start_line": 10, "end_line": 12}])
        self.assertIn({"start_line": 6, "end_line": 9}, error["supplied_ranges"])
        [request] = [item for item in bundle["action_refinement"]["requests"] if item["stage_id"] == "checkout"]
        self.assertEqual(request["outcome"], "accepted_after_repair")
        self.assertEqual([item["code"] for item in request["attempts"] if item["outcome"] == "rejected"],
                         ["ref_outside_supplied"])

    def test_accounting_failures_get_precise_numbers_and_a_failed_request_keeps_candidates_inspectable(self):
        def broken(brief, attempt):
            reply = refine_reply(brief)
            if brief["stage"]["id"] != "checkout":
                return reply
            if attempt == 1:
                reply["candidate_accounting"] = reply["candidate_accounting"][1:]
            else:
                reply["candidate_accounting"].append(copy.deepcopy(reply["candidate_accounting"][0]))
            return reply
        business = Business(**handlers(map_refine=broken))
        bundle, out = self.run_case(business)
        first, repair = business.of("TASK: map-refine", "checkout")
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual((error["code"], error["path"], error["missing_values"]),
                         ("missing_candidates", "candidate_accounting", ["1"]))
        record = bundle["action_refinement"]
        [request] = [item for item in record["requests"] if item["stage_id"] == "checkout"]
        self.assertEqual((request["outcome"], [item["code"] for item in request["attempts"]]),
                         ("rejected_after_repair", ["missing_candidates", "duplicate_candidate"]))
        checkout = [head for head in record["candidates"].values() if head["path"].startswith(CHECKOUT)]
        self.assertTrue(checkout)
        for head in checkout:
            self.assertEqual([(state["state"], state["request_id"]) for state in head["stages"]],
                             [("request_failed", request["id"])])
        self.assertEqual(record["status"], "partial")  # other stages were refined; no success from counts alone
        self.assertEqual(bundle["manifest"]["stages"]["map"]["status"], "completed")
        coarse = {op["id"] for op in coarse_map()["operations"]}
        self.assertTrue(coarse <= {op["id"] for op in bundle["process_map"]["operations"]})
        self.assertFalse([op for op in bundle["process_map"]["operations"] if op["stage_id"] == "checkout"
                          and op["id"] not in coarse])
        self.assertTrue(any("не получено" in text for text in bundle["process_map"]["unknowns"]))
        self.assertTrue((out / "action_catalog.json").is_file())

    def test_each_reference_rule_names_its_field_in_the_one_repair(self):
        def change(edit):
            def handler(brief, attempt):
                reply = refine_reply(brief)
                if attempt == 1 and brief["stage"]["id"] == "payment":
                    edit(reply, brief)
                return reply
            return handler

        def refund_site(brief):
            return next(site["number"] for site in brief["sites"] if site["event_key"] == "refund.completed")
        cases = {
            "unknown candidate": (lambda reply, brief: reply["candidate_accounting"][0].update(candidate=99),
                                  "unknown_candidate", "candidate_accounting[0].candidate"),
            "unknown operation": (lambda reply, brief: reply["candidate_accounting"][0].update(new_operations=[42]),
                                  "unknown_new_operation", "candidate_accounting[0].new_operations[0]"),
            "merge without reason": (lambda reply, brief: reply["candidate_accounting"][1].update(new_operations=[1]),
                                     "missing_reason", "candidate_accounting[0].reason"),
            "role without basis": (lambda reply, brief: [item.update(refs=[]) for item in reply["event_mappings"]
                                                         if item["lifecycle_role"] == "result"],
                                   "role_without_basis", None),
            "both operation fields": (lambda reply, brief: reply["event_mappings"][0].update(known_operation=1),
                                      "operation_choice", "event_mappings[0].new_operation"),
            "interaction without source": (lambda reply, brief: reply["operations"][0].update(initiation="user_interaction"),
                                           "interaction_without_source", "operations[0].initiation"),
            "transition without refs": (lambda reply, brief: reply["operation_transitions"].append(
                {"from_new": 1, "from_known": None, "to_new": 2, "to_known": None, "condition": None,
                 "basis": "source_condition", "basis_note": "Порядок вызовов", "refs": []}),
                                        "transition_basis", "operation_transitions[0].refs"),
            "business basis without text": (lambda reply, brief: reply["operation_transitions"].append(
                {"from_new": 1, "from_known": None, "to_new": 2, "to_known": None, "condition": None,
                 "basis": "business_text", "basis_note": "Так обычно", "refs": []}),
                                            "basis_not_supplied", "operation_transitions[0].basis"),
            "unknown parent": (lambda reply, brief: reply["operations"][0].update(parent_operation=77),
                               "unknown_parent_operation", "operations[0].parent_operation"),
        }
        for label, (edit, code, path) in cases.items():
            with self.subTest(case=label):
                business = Business(**handlers(map_refine=change(edit)))
                bundle, _ = self.run_case(business)
                first, repair = business.of("TASK: map-refine", "payment")
                [error] = repair["brief"]["validation_feedback"]["errors"]
                self.assertEqual(error["code"], code)
                if path is not None:
                    self.assertEqual(error["path"], path)
                self.assertEqual(error["reason"], workflow._REASONS[code])
                [request] = [item for item in bundle["action_refinement"]["requests"] if item["stage_id"] == "payment"]
                self.assertEqual(request["outcome"], "accepted_after_repair")

    def test_paging_keeps_every_candidate_with_its_sources_and_the_request_limit_defers_by_id(self):
        extra_files, extra_found = {}, []
        for index in range(12):
            name = f"bulk_{index:02d}.py"
            body = [f"checkout.note_{index}_{step:02d} = 'длинная строка для объёма шага {step:02d}' * 3"
                    for step in range(40)]
            extra_files[CHECKOUT + name] = mutation(f"BulkCommand{index:02d}", f"checkout.flag_{index} = value",
                                                    f"checkout.bulk_{index:02d}", body)
            extra_found.append(finding("entry_point", f"BulkCommand{index:02d}: отдельная команда корзины. " + "Подробно. " * 60,
                                       CHECKOUT + name, 7, 11 + len(body), f"BulkCommand{index:02d}.perform_mutation"))
        files, found = repository(extra_files, extra_found)
        trace = []
        from process_observability.action_refinement import _Refinement, REFINE_SYSTEM
        original_fits, checks = _Refinement.fits, []
        def tracked_fits(work, system, context):
            if system == REFINE_SYSTEM and context.get('stage', {}).get('id') == 'checkout':
                checks.append(len(context['candidates']))
            return original_fits(work, system, context)
        with patch.object(_Refinement, 'fits', tracked_fits):
            wide, _ = self.run_case(Business(**handlers()), files=files, found=found, digest_chars=8000,
                                    business_request_chars=400000, trace=lambda event, data: trace.append((event, data)))
        self.assertLessEqual(len(checks), 8)  # bounded prefix search, not a full re-encode per candidate
        [whole] = [item for item in wide["action_refinement"]["requests"] if item["stage_id"] == "checkout"]
        self.assertEqual(len(whole["candidate_ids"]), 18)
        [coarse] = [data["chars"] for event, data in trace if event == "model.request" and data.get("task") == "map"]
        budget = max(coarse + 2 * _RESERVE, whole["chars"] * 2 // 3)
        self.assertLess(budget, whole["chars"] + _RESERVE)  # precondition: one checkout page no longer fits
        business = Business(**handlers())
        full_business = "Полное описание самостоятельных действий. " * 150
        bundle, _ = self.run_case(business, files=files, found=found, digest_chars=8000, business_request_chars=budget,
                                  business_text=full_business)
        record = bundle["action_refinement"]
        pages = [item for item in record["requests"] if item["stage_id"] == "checkout"]
        self.assertGreater(len(pages), 1)
        for item in record["requests"]:
            self.assertLessEqual(item["chars"] + _RESERVE, budget)
        supplied = [cid for item in pages for cid in item["candidate_ids"]]
        self.assertEqual(sorted(supplied), sorted(whole["candidate_ids"]))  # nothing hidden, nothing twice
        later = business.of("TASK: map-refine", "checkout")[1]["brief"]
        self.assertIn("earlier_page", {row["origin"] for row in later["known_operations"]})
        for call in business.of("TASK: map-refine", "checkout"):
            self.assertEqual(call["brief"]["business_text"], full_business)
            for candidate in call["brief"]["candidates"]:
                self.assertTrue(candidate["excerpts"], candidate["symbol"])
                if candidate["symbol"].startswith("BulkCommand"):
                    self.assertTrue(candidate["findings"][0]["description"].endswith("Подробно. "))
        # With a request limit the rest is deferred explicitly by candidate ID, never silently dropped.
        limited, _ = self.run_case(Business(**handlers()), files=files, found=found, digest_chars=8000,
                                   business_request_chars=budget, refine_max_requests=2)
        heads = limited["action_refinement"]["candidates"]
        deferred = {cid for cid, head in heads.items() for state in head["stages"]
                    if state["state"] == "deferred" and state["reason"] == "request_limit_reached"}
        accounted = {cid for cid, head in heads.items() for state in head["stages"] if state["state"] == "accounted"}
        self.assertTrue(deferred)
        self.assertEqual(deferred | accounted, set(heads))
        self.assertEqual(limited["action_refinement"]["status"], "partial")
        self.assertTrue(any("лимит запросов" in text for text in limited["process_map"]["unknowns"]))

    def test_a_candidate_larger_than_one_request_is_deferred_never_clipped(self):
        body = [f"checkout.part_{step:03d} = '{'Ш' * 300}'" for step in range(150)]
        giant = {CHECKOUT + "giant.py": mutation("GiantCommand", "checkout.giant = value", "checkout.giant", body)}
        files, found = repository(giant, [finding("entry_point", "Огромная команда.", CHECKOUT + "giant.py", 7, 9,
                                                  "GiantCommand.perform_mutation")])
        options = ReviewOptions(request_chars=80000)
        trace = []
        self.run_case(Business(**handlers()), files=files, found=found, options=options, digest_chars=8000,
                      business_request_chars=400000, trace=lambda event, data: trace.append((event, data)))
        [coarse] = [data["chars"] for event, data in trace if event == "model.request" and data.get("task") == "map"]
        budget = coarse + 2 * _RESERVE
        business = Business(**handlers())
        bundle, _ = self.run_case(business, files=files, found=found, options=options, digest_chars=8000,
                                  business_request_chars=budget)
        identifier, head = self.candidate(bundle, "GiantCommand.perform_mutation")
        self.assertEqual([(state["state"], state["reason"]) for state in head["stages"]],
                         [("deferred", "candidate_exceeds_request_budget")])
        for call in business.calls:
            self.assertNotIn("Ш" * 150, call["user"])
            self.assertLessEqual(len(call["system"]) + len(call["user"]), budget)
        self.assertIsNotNone(self.operation_of(bundle, "CheckoutLinesDelete.perform_mutation"))

    def test_token_budget_applies_to_scope_and_refinement_with_full_business(self):
        from process_observability.token_budget import TokenBudget
        budget = TokenBudget(110000, 1000, 1000, 'a' * 64, lambda text: len(text.encode('utf-8')))
        business = Business(**handlers())
        text = 'Описание полного процесса. ' * 150
        bundle, _ = self.run_case(business, business_request_chars=999999, digest_chars=8000,
                                  business_token_budget=budget, business_text=text)
        for call in business.calls:
            self.assertTrue(budget.fits(call['system'], call['user']))
            if call['task'] in {'TASK: map-scope', 'TASK: map-refine'}:
                self.assertEqual(call['brief']['business_text'], text)
        self.assertTrue(business.of('TASK: map-refine'))
        self.assertTrue(bundle['action_refinement']['summary'])

    def test_transport_failure_is_not_repaired_and_keeps_the_catalog_and_coarse_map(self):
        business = Business(**handlers(map_refine=LLMError("rate limited", category="http", status=429)))
        bundle, out = self.run_case(business)
        # No repair, no retry and no further page after a 429: the remaining stages are deferred explicitly.
        self.assertEqual(business.tasks().count("TASK: map-refine"), 1)
        record = bundle["action_refinement"]
        self.assertEqual(record["status"], "failed")
        [failed] = [item for item in record["requests"] if item["kind"] == "refine"]
        self.assertEqual((failed["stage_id"], failed["outcome"]), ("checkout", "rejected"))
        self.assertEqual([(attempt["layer"], attempt["status"], attempt["retry"]) for attempt in failed["attempts"]],
                         [("transport", 429, "skipped_not_repairable")])
        later = {state["reason"] for head in record["candidates"].values() for state in head["stages"]
                 if state["stage_id"] != "checkout"}
        self.assertEqual(later, {"halted_after_transport_failure"})
        self.assertEqual(record["summary"]["failed_by_reason"], {"request_rejected": 6})
        self.assertEqual([op["id"] for op in bundle["process_map"]["operations"]],
                         [op["id"] for op in coarse_map()["operations"]])
        self.assertTrue(all(state["status"] == "completed" for state in bundle["manifest"]["stages"].values()))
        check_catalog(json.loads((out / "action_catalog.json").read_text(encoding="utf-8")))

    def test_expected_operation_without_code_survives_and_children_join_their_parent_scenario(self):
        missing = {"name": "Вернуть товар покупателем", "description": "Ожидается по бизнес-описанию; код не найден.",
                   "actor": "Покупатель", "kind": "operation", "initiation": "unknown", "initiation_basis": None,
                   "parent_operation": None, "refs": []}

        def reply(brief, attempt):
            parent = "Управление составом корзины" if brief["stage"]["id"] == "checkout" else None
            return refine_reply(brief, parent=parent, extra=[missing] if brief["stage"]["id"] == "payment" else [])
        bundle, _ = self.run_case(Business(**handlers(map_refine=reply)))
        [expected] = [op for op in bundle["process_map"]["operations"] if op["name"] == missing["name"]]
        self.assertEqual(expected["evidence_ids"], [])
        [scenario] = bundle["process_map"]["scenarios"]
        children = [op["id"] for op in bundle["process_map"]["operations"]
                    if op["stage_id"] == "checkout" and op["id"] != "cart-manage"]
        self.assertEqual(scenario["operation_ids"], ["cart-manage", *children])
        self.assertEqual(scenario["transition_ids"], [])

    def test_operator_stage_filter_defers_other_stages_and_disabled_refinement_sends_nothing(self):
        business = Business(**handlers())
        bundle, _ = self.run_case(business, refine_stages=("Оформление",))
        self.assertEqual([call["brief"]["stage"]["id"] for call in business.of("TASK: map-refine")], ["checkout"])
        reasons = {state["reason"] for head in bundle["action_refinement"]["candidates"].values()
                   for state in head["stages"] if state["state"] == "deferred"}
        self.assertEqual(reasons, {"stage_not_selected"})
        self.assertEqual(bundle["action_refinement"]["status"], "partial")
        off = Business(**handlers())
        bundle, out = self.run_case(off, action_refinement=False)
        self.assertEqual(off.tasks(), ["TASK: map", "TASK: instrumentation", "TASK: metrics"])
        self.assertEqual(bundle["action_refinement"]["status"], "disabled")
        self.assertTrue((out / "action_catalog.json").is_file())

    def test_scope_decisions_are_complete_and_reasoned(self):
        def scope(brief, attempt):
            reply = scope_reply(brief)
            if attempt == 1:
                reply["areas"] = reply["areas"][:-1]
            else:
                reply["areas"][-1].update(disposition="outside_process", stages=[], reason="Инфраструктура магазина")
            return reply
        business = Business(**handlers(map_scope=scope))
        bundle, _ = self.run_case(business)
        first, repair = business.of("TASK: map-scope")
        [error] = repair["brief"]["validation_feedback"]["errors"]
        self.assertEqual((error["code"], error["missing_values"]), ("missing_areas", [str(len(first["brief"]["areas"]))]))
        last = first["brief"]["areas"][-1]["area"]
        states = {head["scope"]["state"] for head in bundle["action_refinement"]["candidates"].values()
                  if head["area"] == last}
        self.assertEqual(states, {"outside_process"})
        self.assertEqual(bundle["action_refinement"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
