"""Action catalog (plan A1): real frozen snapshot, code graph and logging inventory; no model, nothing executed.

Sources are synthetic files written by the test. Findings imitate validated code-review output, including kinds
that are not entry points, a helper the code model called an entry point and refs that do not resolve.
"""

import copy
import json
from pathlib import Path
import random
import tempfile
import unittest

from process_observability.action_catalog import (SCHEMA, CatalogError, build_catalog, candidate_areas,
                                                  check_catalog)
from process_observability.code_graph import build_graph
from process_observability.log_discovery import discover_logging
from process_observability.source_tools import snapshot_repository
from process_observability.workflow import save_json

HEADER = "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"


def mutation(name, effect, key):
    """Lines 6-13: class, perform_mutation 7-13 with its effect, save, an early return and one literal log key."""
    return (HEADER + f"class {name}:\n    def perform_mutation(self, checkout, value):\n        {effect}\n"
            "        checkout.save()\n        if value is None:\n            return checkout\n"
            f"        logger.info(\"{key}\", checkout_id=checkout.pk)\n        return checkout\n")


LINES_ADD = (HEADER + "def add_variants(checkout, lines):\n    for line in lines:\n        checkout.lines.append(line)\n"
             "    return checkout\n\n\nclass CheckoutLinesAdd:\n    def perform_mutation(self, checkout, lines):\n"
             "        if not lines:\n            raise ValueError(\"no lines\")\n"
             "        checkout = add_variants(checkout, lines)\n        checkout.save()\n"
             "        logger.info(\"checkout.lines_added\", checkout_id=checkout.pk)\n        return checkout\n")
ORDER_CONFIRM = (HEADER + "class OrderConfirm:\n    def get_instance(self, order):\n"
                 "        if order.status != \"unconfirmed\":\n            raise ValueError(\"INVALID\")\n"
                 "        return order\n\n    def perform_mutation(self, order):\n        order = self.get_instance(order)\n"
                 "        order.status = \"unfulfilled\"\n        order.save()\n"
                 "        logger.info(\"order.confirmed\", order_id=order.pk)\n        return order\n")
ORDER_CANCEL = (HEADER + "def clean_order_cancel(order):\n    if order is None or not order.can_cancel():\n"
                "        raise ValueError(\"CANNOT_CANCEL_ORDER\")\n\n\nclass OrderCancel:\n"
                "    def perform_mutation(self, order):\n        clean_order_cancel(order)\n"
                "        order.status = \"canceled\"\n        order.save()\n"
                "        logger.info(\"order.canceled\", order_id=order.pk)\n        return order\n")
RELEASE = (HEADER + "def release_funds_task(transactions):\n"
           "    pending = [item for item in transactions if item.order_id is None]\n    if not pending:\n"
           "        logger.warning(\"payment.release_funds_noop\")\n        return 0\n    for item in pending:\n"
           "        logger.info(\"payment.release_funds_canceling\", transaction_id=item.pk)\n        item.cancel()\n"
           "    return len(pending)\n")
BUTTON = ("export function CartButton({ onAdd }) {\n  function handleClick() {\n    onAdd();\n  }\n"
          "  return <button onClick={handleClick}>Add</button>;\n}\n")
MUTATIONS = {"lines_delete.py": ("CheckoutLinesDelete", "checkout.lines.remove(value)", "checkout.lines_deleted"),
             "lines_update.py": ("CheckoutLinesUpdate", "checkout.lines.update(value)", "checkout.lines_updated"),
             "shipping_address_update.py": ("CheckoutShippingAddressUpdate", "checkout.shipping_address = value",
                                            "checkout.shipping_address_updated"),
             "billing_address_update.py": ("CheckoutBillingAddressUpdate", "checkout.billing_address = value",
                                           "checkout.billing_address_updated")}


def sources(extra=None):
    files = {"shop/checkout/mutations/lines_add.py": LINES_ADD, "shop/order/mutations/order_confirm.py": ORDER_CONFIRM,
             "shop/order/mutations/order_cancel.py": ORDER_CANCEL, "shop/payment/tasks.py": RELEASE,
             "web/src/CartButton.tsx": BUTTON}
    files.update({f"shop/checkout/mutations/{name}": mutation(*spec) for name, spec in MUTATIONS.items()})
    files.update(extra or {})
    return files


def finding(identifier, kind, description, path, start, end, symbol):
    return {"id": identifier, "kind": kind, "description": description, "task_id": "task-1", "task_kind": "base",
            "refs": [{"path": path, "start_line": start, "end_line": end, "symbol": symbol}]}


def findings():
    checkout = "shop/checkout/mutations/"
    result = [
        finding("f-add", "entry_point", "Точка входа добавления строк корзины: проверка, add_variants, сохранение.",
                checkout + "lines_add.py", 13, 19, "CheckoutLinesAdd.perform_mutation"),
        finding("f-add-save", "state_change", "Сохранение корзины после добавления строк.",
                checkout + "lines_add.py", 16, 17, "CheckoutLinesAdd.perform_mutation"),
        finding("f-add-log", "logging", "Лог checkout.lines_added после сохранения.",
                checkout + "lines_add.py", 18, 18, "CheckoutLinesAdd.perform_mutation"),
        finding("f-helper", "entry_point", "add_variants добавляет строки в корзину.",
                checkout + "lines_add.py", 6, 9, "add_variants"),
        finding("f-confirm-check", "error", "Подтверждение только неподтверждённого заказа, иначе INVALID.",
                "shop/order/mutations/order_confirm.py", 7, 10, "OrderConfirm.get_instance"),
        finding("f-confirm", "state_change", "Статус заказа unfulfilled и сохранение.",
                "shop/order/mutations/order_confirm.py", 14, 15, "OrderConfirm.perform_mutation"),
        finding("f-confirm-tx", "transaction_boundary", "Сохранение подтверждения и лог order.confirmed.",
                "shop/order/mutations/order_confirm.py", 12, 17, "OrderConfirm.perform_mutation"),
        finding("f-cancel-check", "error", "Отмена невозможна: CANNOT_CANCEL_ORDER.",
                "shop/order/mutations/order_cancel.py", 6, 8, "clean_order_cancel"),
        finding("f-cancel", "transaction_boundary", "Отмена заказа: статус canceled и сохранение.",
                "shop/order/mutations/order_cancel.py", 12, 17, "OrderCancel.perform_mutation"),
        finding("f-release", "entry_point", "Задача освобождения средств по транзакциям без заказа (order_id None).",
                "shop/payment/tasks.py", 6, 14, "release_funds_task"),
        finding("f-release-noop", "branch", "Нет подходящих транзакций: предупреждение и выход.",
                "shop/payment/tasks.py", 8, 10, "release_funds_task"),
        finding("f-button", "entry_point", "Обработчик нажатия кнопки добавления вызывает onAdd.",
                "web/src/CartButton.tsx", 2, 4, "CartButton.handleClick"),
    ]
    for index, (name, (cls, _, _)) in enumerate(sorted(MUTATIONS.items())):
        result.append(finding(f"f-{name}", "entry_point", f"Точка входа {cls}: изменение корзины и сохранение.",
                              checkout + name, 7, 13, f"{cls}.perform_mutation"))
    return result


class CatalogCase(unittest.TestCase):
    def scope(self, files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        for relative, text in files.items():
            path = root / "repo" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        snapshot = snapshot_repository(root / "repo", max_files=None, max_chars=None)
        inventory = discover_logging(snapshot, max_sites=None, max_per_file=None, max_hints=None)
        return root, snapshot, build_graph(snapshot, inventory), inventory

    def catalog(self, files=None, items=None, unknowns=(), **review):
        _, snapshot, graph, inventory = self.scope(files or sources())
        data = {"findings": findings() if items is None else items, "unknowns": list(unknowns),
                "snapshot_id": snapshot.snapshot_id, "complete": True, "run_fingerprint": "rf-1", **review}
        return build_catalog(data, snapshot, graph=graph, inventory=inventory), snapshot

    @staticmethod
    def by_symbol(catalog, role="candidate"):
        collection = catalog["candidates"] if role == "candidate" else catalog["context_anchors"]
        return {item["symbol"]: item for item in collection}


class ActionCatalogTests(CatalogCase):
    def test_distinct_cart_and_address_commands_stay_separate_with_exact_ranges(self):
        catalog, _ = self.catalog()
        self.assertEqual(catalog["schema"], SCHEMA)
        candidates = self.by_symbol(catalog)
        for symbol, path in (("CheckoutLinesAdd.perform_mutation", "lines_add.py"),
                             ("CheckoutLinesDelete.perform_mutation", "lines_delete.py"),
                             ("CheckoutLinesUpdate.perform_mutation", "lines_update.py"),
                             ("CheckoutShippingAddressUpdate.perform_mutation", "shipping_address_update.py"),
                             ("CheckoutBillingAddressUpdate.perform_mutation", "billing_address_update.py")):
            with self.subTest(symbol=symbol):
                item = candidates[symbol]
                self.assertEqual(item["path"], "shop/checkout/mutations/" + path)
                self.assertEqual(item["symbol_source"], "graph_declaration")
        add = candidates["CheckoutLinesAdd.perform_mutation"]
        self.assertEqual((add["start_line"], add["end_line"]), (13, 19))
        # Several findings of one declaration are one candidate with each finding kept separately.
        self.assertEqual(add["finding_ids"], ["f-add", "f-add-save", "f-add-log"])
        self.assertEqual(add["signals"], {"entry_point": 1, "logging": 1, "state_change": 1})
        self.assertEqual([site["event_key"] for site in add["event_sites"]], ["checkout.lines_added"])
        ids = [item["id"] for item in catalog["candidates"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_identity_follows_the_relevant_source_not_finding_or_file_order(self):
        catalog, _ = self.catalog()
        shuffled = findings()
        random.Random(7).shuffle(shuffled)
        again, _ = self.catalog(items=shuffled)
        self.assertEqual(again["catalog_sha256"], catalog["catalog_sha256"])
        # Editing one command's body (same line count) changes that candidate's identity only.
        edited = sources({"shop/checkout/mutations/lines_delete.py":
                          mutation("CheckoutLinesDelete", "checkout.lines.discard(value)", "checkout.lines_deleted")})
        changed, _ = self.catalog(files=edited)
        before = {item["symbol"]: item["id"] for item in catalog["candidates"]}
        after = {item["symbol"]: item["id"] for item in changed["candidates"]}
        self.assertNotEqual(before["CheckoutLinesDelete.perform_mutation"], after["CheckoutLinesDelete.perform_mutation"])
        self.assertEqual({key: value for key, value in before.items() if key != "CheckoutLinesDelete.perform_mutation"},
                         {key: value for key, value in after.items() if key != "CheckoutLinesDelete.perform_mutation"})

    def test_more_than_eight_commands_of_one_area_are_all_candidates(self):
        extra, items = {}, []
        for index in range(12):
            name = f"bulk_{index:02d}.py"
            extra["shop/checkout/mutations/" + name] = mutation(f"BulkCommand{index:02d}", f"checkout.flag_{index} = value",
                                                                f"checkout.bulk_{index:02d}")
            items.append(finding(f"f-bulk-{index}", "entry_point", f"Команда {index}: своя мутация корзины.",
                                 "shop/checkout/mutations/" + name, 7, 13, f"BulkCommand{index:02d}.perform_mutation"))
        catalog, _ = self.catalog(files=sources(extra), items=findings() + list(reversed(items)))
        [area] = [group for group in candidate_areas(catalog) if group["area"] == "shop/checkout/mutations"]
        symbols = {item["symbol"] for item in catalog["candidates"] if item["area"] == "shop/checkout/mutations"}
        self.assertEqual(len(area["candidate_ids"]), 18)  # 12 bulk + add, helper, delete, update, two addresses
        self.assertTrue({f"BulkCommand{index:02d}.perform_mutation" for index in range(12)} <= symbols)

    def test_non_entry_point_commands_and_automatic_actions_are_candidates_with_context(self):
        catalog, _ = self.catalog()
        candidates, context = self.by_symbol(catalog), self.by_symbol(catalog, "context")
        confirm, cancel = candidates["OrderConfirm.perform_mutation"], candidates["OrderCancel.perform_mutation"]
        self.assertNotIn("entry_point", confirm["signals"])
        self.assertNotIn("entry_point", cancel["signals"])
        self.assertIn("release_funds_task", candidates)
        # Validations are context, linked by a resolved static call or the same owner, not separate candidates.
        self.assertIn(context["clean_order_cancel"]["id"], cancel["navigation"]["callee_anchor_ids"])
        self.assertIn(context["OrderConfirm.get_instance"]["id"], confirm["navigation"]["same_owner_anchor_ids"])
        self.assertIn(context["OrderConfirm.get_instance"]["id"], confirm["navigation"]["callee_anchor_ids"])
        self.assertNotIn("clean_order_cancel", candidates)

    def test_helper_and_ui_handler_are_signals_only_and_nothing_decides_initiation(self):
        catalog, _ = self.catalog()
        candidates = self.by_symbol(catalog)
        helper = candidates["add_variants"]
        self.assertEqual(helper["navigation"]["callers_in_snapshot"], 1)
        self.assertEqual(helper["navigation"]["caller_symbols"], ["CheckoutLinesAdd.perform_mutation"])
        self.assertEqual(candidates["CheckoutLinesAdd.perform_mutation"]["navigation"]["callers_in_snapshot"], 0)
        [button] = [item for item in catalog["candidates"] if item["path"] == "web/src/CartButton.tsx"]
        self.assertEqual(button["finding_ids"], ["f-button"])
        text = json.dumps(catalog, ensure_ascii=False)
        for decided in ('"initiation"', '"actor"', '"user_interaction"', '"click"', '"business_action"'):
            self.assertNotIn(decided, text)

    def test_ui_source_without_a_signal_finding_creates_no_candidate_by_extension(self):
        items = [item for item in findings() if item["id"] != "f-button"]
        items.append(finding("f-button-log", "logging", "Кнопка ничего не логирует.", "web/src/CartButton.tsx",
                             5, 5, "CartButton"))
        catalog, _ = self.catalog(items=items)
        self.assertFalse([item for item in catalog["candidates"] if item["path"].endswith(".tsx")])
        self.assertTrue([item for item in catalog["context_anchors"] if item["path"].endswith(".tsx")])

    def test_unsafe_or_unresolvable_refs_are_listed_not_dropped(self):
        items = findings() + [
            finding("f-missing", "entry_point", "Файл вне снимка.", "shop/missing.py", 1, 2, "missing"),
            finding("f-range", "state_change", "Строки за концом файла.", "shop/payment/tasks.py", 90, 95, "x")]
        bad_shape = finding("f-shape", "outcome", "Ссылка без строк.", "shop/payment/tasks.py", 1, 1, "x")
        bad_shape["refs"] = [{"path": "shop/payment/tasks.py"}]
        catalog, _ = self.catalog(items=items + [bad_shape])
        self.assertEqual(sorted((item["finding_id"], item["reason"]) for item in catalog["rejected_refs"]),
                         [("f-missing", "path_outside_snapshot"), ("f-range", "range_outside_file"),
                          ("f-shape", "range_outside_file")])
        self.assertEqual(catalog["accounting"]["findings_without_valid_ref_ids"], ["f-missing", "f-range", "f-shape"])
        self.assertEqual(catalog["accounting"]["findings"], len(items) + 1)

    def test_full_descriptions_partial_review_and_failures_stay_on_disk(self):
        long = "Очень подробное описание ветви. " * 300
        items = findings()
        items[0] = {**items[0], "description": long}
        gap = {"id": "cs-gap", "description": "The worker marked a range partial.", "refs": [], "origin": "interval_status",
               "model_status": "partial", "range": {"path": "shop/checkout/mutations/lines_delete.py",
                                                    "start_line": 7, "end_line": 13, "symbol": "x"}}
        loose = {"id": "cu-1", "description": "Где задаётся order.status, не видно.", "refs": [],
                 "origin": "worker_unknown", "interval_ids": ["iv-1"]}
        catalog, _ = self.catalog(items=items, unknowns=[gap, loose], complete=False,
                                  incomplete_reasons=["followup_tasks_failed"], errors=[{"task_id": "t"}] * 6)
        add = self.by_symbol(catalog)["CheckoutLinesAdd.perform_mutation"]
        self.assertEqual(add["findings"][0]["description"], long)
        delete = self.by_symbol(catalog)["CheckoutLinesDelete.perform_mutation"]
        self.assertEqual([gap["model_status"] for gap in delete["review_gaps"]], ["partial"])
        self.assertEqual([item["unknown_id"] for item in catalog["unanchored_unknowns"]], ["cu-1"])
        self.assertEqual(catalog["review"], {"run_fingerprint": "rf-1", "complete": False, "coverage_complete": None,
                                             "incomplete_reasons": ["followup_tasks_failed"], "failed_tasks": 6})

    def test_saved_catalog_verifies_and_altered_or_foreign_data_is_refused(self):
        catalog, snapshot = self.catalog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "action_catalog.json"
            save_json(path, catalog)
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(check_catalog(loaded, snapshot_id=snapshot.snapshot_id), catalog)
        with self.assertRaisesRegex(CatalogError, "different source snapshot"):
            check_catalog(loaded, snapshot_id="other")
        altered = copy.deepcopy(loaded)
        altered["candidates"][0]["findings"][0]["description"] = "Изменённое описание"
        with self.assertRaisesRegex(CatalogError, "catalog_sha256"):
            check_catalog(altered)
        for change in (lambda data: data.update(schema="action-catalog/0"), lambda data: data.pop("rejected_refs"),
                       lambda data: data.update(extra=True)):
            broken = copy.deepcopy(loaded)
            change(broken)
            with self.assertRaises(CatalogError):
                check_catalog(broken)
        relabeled = copy.deepcopy(loaded)
        relabeled["candidates"][0]["start_line"] += 1
        from process_observability.action_catalog import _digest
        relabeled["catalog_sha256"] = _digest(relabeled)  # even a self-consistent hash cannot hide a moved range
        with self.assertRaisesRegex(CatalogError, "anchor ID"):
            check_catalog(relabeled)

    def test_foreign_review_or_graph_is_refused(self):
        _, snapshot, graph, inventory = self.scope(sources())
        with self.assertRaisesRegex(CatalogError, "different snapshot"):
            build_catalog({"findings": [], "unknowns": [], "snapshot_id": "other"}, snapshot, graph=graph)
        with self.assertRaisesRegex(CatalogError, "different snapshot"):
            build_catalog({"findings": [], "unknowns": []}, snapshot, graph={"snapshot_id": "other", "nodes": []})


if __name__ == "__main__":
    unittest.main()
