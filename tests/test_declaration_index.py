"""Business-only declaration index and host-verified command units (direct-atomic plan D1).

The index parses the ORIGINAL frozen text with ast.parse only: nothing is imported or executed and no source
text is stored. Units group catalog anchors only by the innermost indexed declaration of the same file
revision; the model's symbol string is a consistency check, never a grouping key. Sources are synthetic files
written by the test; findings imitate validated code-review output. These checks say nothing about live model
quality.
"""

import ast
import copy
import json
from pathlib import Path
import random
import tempfile
import unittest

from process_observability.action_catalog import build_catalog
from process_observability.code_graph import build_graph
from process_observability.log_discovery import discover_logging
from process_observability.source_tools import snapshot_repository
from process_observability.workflow import save_json


def api():
    """The module under test, imported per test so that each check reports its own result."""
    from process_observability import declaration_index
    return declaration_index


HEADER = "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"
CHECKOUT = "shop/checkout/mutations/"

# An assignment to a credential-like name continued on the next line: redaction masks the rest of the first
# line, so the redacted text raises IndentationError while the original parses (as on the public pilot files).
LINES_ADD = HEADER + (
    "def add_variants(checkout, lines):\n"                                       # 6
    "    for line in lines:\n"
    "        checkout.lines.append(line)\n"
    "    return checkout\n"                                                      # 9
    "\n\n"
    "class CheckoutLinesAdd:\n"                                                  # 12
    "    def perform_mutation(self, checkout, lines):\n"                         # 13
    "        token = checkout.get_token(\n"
    "            lines)\n"
    "        if not lines:\n"                                                    # 16
    "            raise ValueError(\"no lines\")\n"
    "        checkout = add_variants(checkout, lines)\n"                         # 18
    "        checkout.save()\n"                                                  # 19
    "        logger.info(\"checkout.lines_added\", checkout_id=checkout.pk)\n"   # 20
    "        return checkout\n")                                                 # 21
ADDRESSES = HEADER + (
    "class CheckoutShippingAddressUpdate:\n"                                     # 6
    "    def perform_mutation(self, checkout, address):\n"                       # 7
    "        token = checkout.get_token(\n"
    "            address)\n"                                                     # 9
    "        checkout.shipping_address = address\n"                              # 10
    "        checkout.save()\n"                                                  # 11
    "        logger.info(\"checkout.shipping_address_updated\", checkout_id=checkout.pk)\n"
    "        return checkout\n"                                                  # 13
    "\n\n"
    "class CheckoutBillingAddressUpdate:\n"                                      # 16
    "    def perform_mutation(self, checkout, address):\n"                       # 17
    "        token = checkout.get_token(\n"
    "            address)\n"                                                     # 19
    "        checkout.billing_address = address\n"                               # 20
    "        checkout.save()\n"                                                  # 21
    "        logger.info(\"checkout.billing_address_updated\", checkout_id=checkout.pk)\n"
    "        return checkout\n")                                                 # 23
ORDER_CONFIRM = HEADER + (
    "class OrderConfirm:\n"                                                      # 6
    "    def get_instance(self, order):\n"                                       # 7
    "        if order.status != \"unconfirmed\":\n"
    "            raise ValueError(\"INVALID\")\n"                                # 9
    "        return order\n"
    "\n"
    "    def clean_note(self, order):\n"                                         # 12
    "        if order.note is None:\n"
    "            raise ValueError(\"NOTE\")\n"                                   # 14
    "\n"
    "    def perform_mutation(self, order):\n"                                   # 16
    "        order = self.get_instance(order)\n"
    "        order.status = \"unfulfilled\"\n"                                   # 18
    "        order.save()\n"                                                     # 19
    "        logger.info(\"order.confirmed\", order_id=order.pk)\n"
    "        return order\n")                                                    # 21
# Overloads, a nested declaration, sibling methods and module-level code; line 1-2 break the redacted parse,
# so every catalog anchor of this file is a finding_ref fragment that only the index can place.
TRICKY = (
    "token = load_settings(\n"                                                   # 1
    "    \"default\")\n"
    "ROUTES = [\"checkout\", \"order\"]\n"                                       # 3
    "\n\n"
    "def process(value: int) -> int:\n"                                          # 6
    "    return value + 1\n"                                                     # 7
    "\n\n"
    "def process(value: str) -> str:\n"                                          # 10
    "    return value.strip()\n"                                                 # 11
    "\n\n"
    "def outer(items):\n"                                                        # 14
    "    def helper(item):\n"                                                    # 15
    "        return item.total\n"                                                # 16
    "    result = [helper(item) for item in items]\n"                            # 17
    "    return sum(result)\n"                                                   # 18
    "\n\n"
    "class Pair:\n"                                                              # 21
    "    def first(self):\n"                                                     # 22
    "        return 1\n"                                                         # 23
    "\n"
    "    def second(self):\n"                                                    # 25
    "        return 2\n")                                                        # 26
ORDER_TASKS = HEADER + (
    "def task(function):\n"                                                      # 6
    "    return function\n"
    "\n\n"
    "@task\n"                                                                    # 10
    "def send_order_email(order):\n"                                             # 11
    "    order.mail()\n"                                                         # 12
    "    logger.info(\"order.email_sent\", order_id=order.pk)\n")                # 13
BUTTON = ("export function CartButton({ onAdd }) {\n  function handleClick() {\n    onAdd();\n  }\n"
          "  return <button onClick={handleClick}>Add</button>;\n}\n")
# Fails to parse even as original text; the failing line holds a credential-like literal.
BROKEN = "def broken(order):\n    api_key = \"RAW-SECRET-77\" +\n    return order\n"


def command(name, effect, key):
    """Lines 6-11: class, perform_mutation 7-11 with its own effect, save and one literal log key."""
    return HEADER + (f"class {name}:\n    def perform_mutation(self, checkout, value):\n        {effect}\n"
                     f"        checkout.save()\n        logger.info(\"{key}\", checkout_id=checkout.pk)\n"
                     "        return checkout\n")


def line_of(text, needle, occurrence=1):
    return [number for number, line in enumerate(text.splitlines(), 1) if needle in line][occurrence - 1]


def sources(extra=None):
    files = {CHECKOUT + "lines_add.py": LINES_ADD, CHECKOUT + "addresses.py": ADDRESSES,
             CHECKOUT + "lines_delete.py": command("CheckoutLinesDelete", "checkout.lines.remove(value)",
                                                   "checkout.lines_deleted"),
             CHECKOUT + "lines_update.py": command("CheckoutLinesUpdate", "checkout.lines.update(value)",
                                                   "checkout.lines_updated"),
             "shop/order/mutations/order_confirm.py": ORDER_CONFIRM, "shop/core/tricky.py": TRICKY,
             "shop/order/tasks.py": ORDER_TASKS, "web/src/CartButton.tsx": BUTTON, "shop/broken.py": BROKEN}
    files.update(extra or {})
    return files


def finding(identifier, kind, description, path, start, end, symbol):
    return {"id": identifier, "kind": kind, "description": description, "task_id": "task-1", "task_kind": "base",
            "refs": [{"path": path, "start_line": start, "end_line": end, "symbol": symbol}]}


def findings():
    add, addresses, tricky = CHECKOUT + "lines_add.py", CHECKOUT + "addresses.py", "shop/core/tricky.py"
    return [
        finding("f-add-entry", "entry_point", "Добавление строк корзины: проверка входа.", add, 13, 16,
                "CheckoutLinesAdd.perform_mutation"),
        finding("f-add-save", "state_change", "Добавление строк и сохранение корзины.", add, 18, 19,
                "CheckoutLinesAdd.perform_mutation"),
        finding("f-add-log", "logging", "Лог checkout.lines_added после сохранения.", add, 20, 20, "perform_mutation"),
        finding("f-helper", "entry_point", "add_variants добавляет строки в корзину.", add, 6, 9, "add_variants"),
        finding("f-ship-entry", "entry_point", "Изменение адреса доставки.", addresses, 7, 9,
                "CheckoutShippingAddressUpdate.perform_mutation"),
        finding("f-ship-save", "state_change", "Сохранение адреса доставки.", addresses, 10, 11, "perform_mutation"),
        finding("f-bill-entry", "entry_point", "Изменение платёжного адреса.", addresses, 17, 19,
                "CheckoutBillingAddressUpdate.perform_mutation"),
        finding("f-bill-save", "state_change", "Сохранение платёжного адреса.", addresses, 20, 21, "perform_mutation"),
        finding("f-delete", "entry_point", "Удаление строк корзины.", CHECKOUT + "lines_delete.py", 7, 11,
                "CheckoutLinesDelete.perform_mutation"),
        finding("f-update", "entry_point", "Изменение количества строк корзины.", CHECKOUT + "lines_update.py", 7, 11,
                "CheckoutLinesUpdate.perform_mutation"),
        finding("f-confirm-check", "error", "Подтверждение только неподтверждённого заказа.",
                "shop/order/mutations/order_confirm.py", 8, 9, "OrderConfirm.get_instance"),
        finding("f-note", "error", "Проверка примечания заказа.", "shop/order/mutations/order_confirm.py", 13, 14,
                "OrderConfirm.clean_note"),
        finding("f-confirm", "state_change", "Подтверждение заказа и сохранение.",
                "shop/order/mutations/order_confirm.py", 18, 19, "OrderConfirm.perform_mutation"),
        finding("f-process-int", "entry_point", "Обработка числа.", tricky, 7, 7, "process"),
        finding("f-process-str", "entry_point", "Обработка строки.", tricky, 11, 11, "process"),
        finding("f-inner", "state_change", "Сумма позиции.", tricky, 16, 16, "outer.helper"),
        finding("f-outer-a", "state_change", "Список сумм.", tricky, 17, 17, "outer"),
        finding("f-outer-b", "outcome", "Итоговая сумма.", tricky, 18, 18, "outer"),
        finding("f-cross", "entry_point", "Диапазон через границу методов.", tricky, 23, 25, "Pair.first"),
        finding("f-stale", "state_change", "Второе значение пары.", tricky, 26, 26, "Pair.first"),
        finding("f-module", "integration", "Маршруты модуля.", tricky, 3, 3, "<module>"),
        finding("f-email", "entry_point", "Отправка письма о заказе.", "shop/order/tasks.py", 12, 13, "send_order_email"),
        finding("f-button", "entry_point", "Обработчик нажатия кнопки вызывает onAdd.", "web/src/CartButton.tsx", 2, 4,
                "CartButton.handleClick"),
        finding("f-broken-a", "entry_point", "Повреждённый модуль: начало.", "shop/broken.py", 1, 2, "broken"),
        finding("f-broken-b", "outcome", "Повреждённый модуль: возврат.", "shop/broken.py", 3, 3, "broken"),
    ]


class Built:
    def __init__(self, **values):
        self.__dict__.update(values)


class IndexCase(unittest.TestCase):
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

    def build(self, files=None, items=None):
        root, snapshot, graph, inventory = self.scope(files or sources())
        review = {"findings": findings() if items is None else items, "unknowns": [], "complete": True,
                  "snapshot_id": snapshot.snapshot_id, "run_fingerprint": "rf-1"}
        catalog = build_catalog(review, snapshot, graph=graph, inventory=inventory)
        index = api().build_index(snapshot)
        units = api().build_units(catalog, index, snapshot)
        return Built(root=root, snapshot=snapshot, graph=graph, catalog=catalog, index=index, units=units)

    @staticmethod
    def unit_of(units, finding_id):
        [unit] = [item for item in units["units"] if finding_id in item["finding_ids"]]
        return unit

    @staticmethod
    def anchors(catalog):
        return [*catalog["candidates"], *catalog["context_anchors"]]


class DeclarationIndexTests(IndexCase):
    def test_original_text_is_indexed_where_redaction_broke_the_graph_and_fragments_join_one_unit(self):
        built = self.build()
        path = CHECKOUT + "lines_add.py"
        source = built.snapshot.files[path]
        # Preconditions reproduce the pilot condition: the original parses, the redacted text does not, so the
        # technical graph fell back to windows and the catalog holds several finding_ref fragments of one method.
        ast.parse(source._raw_text)
        with self.assertRaises(IndentationError):
            ast.parse(source.text)
        self.assertEqual(next(item for item in built.graph.to_dict()['files'] if item['path'] == path)['extractor'], "python_fallback")
        fragments = sorted((item for item in self.anchors(built.catalog)
                            if item["path"] == path and item["start_line"] >= 13), key=lambda item: item["start_line"])
        self.assertEqual([(item["start_line"], item["end_line"]) for item in fragments], [(13, 16), (18, 19), (20, 20)])
        self.assertEqual({item["symbol_source"] for item in fragments}, {"finding_ref"})

        index = built.index
        self.assertEqual((index["schema"], index["snapshot_id"]), ("declaration-index/1", built.snapshot.snapshot_id))
        entry = index["files"][path]
        self.assertEqual((entry["parse"], entry["redacted_parse"], entry["sha256"], entry["error"]),
                         ("ok", "failed", source.sha256, None))
        declarations = {(item["qualname"], item["kind"]): (item["start_line"], item["end_line"])
                        for item in entry["declarations"]}
        self.assertEqual(declarations[("CheckoutLinesAdd", "class")], (12, 21))
        self.assertEqual(declarations[("CheckoutLinesAdd.perform_mutation", "method")], (13, 21))
        self.assertEqual(declarations[("add_variants", "function")], (6, 9))

        units = built.units
        self.assertEqual((units["schema"], units["snapshot_id"], units["catalog_sha256"], units["index_sha256"]),
                         ("command-units/1", built.snapshot.snapshot_id, built.catalog["catalog_sha256"],
                          index["index_sha256"]))
        unit = self.unit_of(units, "f-add-entry")
        self.assertEqual((unit["status"], unit["reason"], unit["role"], unit["path"], unit["file_sha256"]),
                         ("verified", None, "candidate", path, source.sha256))
        self.assertEqual({key: unit["declaration"][key] for key in ("qualname", "kind", "start_line", "end_line")},
                         {"qualname": "CheckoutLinesAdd.perform_mutation", "kind": "method", "start_line": 13,
                          "end_line": 21})
        # Every fragment is a member, in line order, with every finding kept; the logging-only one is context.
        self.assertEqual(unit["member_ids"], [item["id"] for item in fragments])
        self.assertEqual(unit["finding_ids"], ["f-add-entry", "f-add-save", "f-add-log"])
        self.assertEqual(sorted(unit["candidate_ids"]),
                         sorted(item["id"] for item in fragments if item["role"] == "candidate"))
        self.assertEqual(len(unit["candidate_ids"]), 2)
        sites = {site["id"]: site for item in fragments for site in item["event_sites"]}
        self.assertEqual([sites[identifier]["event_key"] for identifier in unit["event_site_ids"]],
                         ["checkout.lines_added"])
        # The short symbol "perform_mutation" written for one fragment agrees with the host declaration.
        self.assertEqual({member["symbol_check"] for member in unit["members"]}, {"consistent"})
        self.assertNotEqual(self.unit_of(units, "f-helper")["id"], unit["id"])

    def test_distinct_commands_with_alike_names_or_bodies_stay_separate_units(self):
        units = self.build().units
        shipping, billing = self.unit_of(units, "f-ship-entry"), self.unit_of(units, "f-bill-entry")
        self.assertEqual(self.unit_of(units, "f-ship-save")["id"], shipping["id"])
        self.assertEqual(self.unit_of(units, "f-bill-save")["id"], billing["id"])
        self.assertNotEqual(shipping["id"], billing["id"])
        self.assertEqual((shipping["declaration"]["qualname"], billing["declaration"]["qualname"]),
                         ("CheckoutShippingAddressUpdate.perform_mutation", "CheckoutBillingAddressUpdate.perform_mutation"))
        self.assertEqual((len(shipping["member_ids"]), len(billing["member_ids"])), (2, 2))
        self.assertFalse(set(shipping["member_ids"]) & set(billing["member_ids"]))
        commands = {self.unit_of(units, key)["id"] for key in ("f-add-entry", "f-delete", "f-update")}
        self.assertEqual(len(commands), 3)  # add, delete and update are three units, never one "cart management"

    def test_overloads_nested_declarations_wrong_symbols_and_cross_declaration_refs_are_never_grouped_by_name(self):
        built = self.build()
        units = built.units
        first, second = self.unit_of(units, "f-process-int"), self.unit_of(units, "f-process-str")
        # Two declarations with one qualified name are separate by span; the shared name groups nothing.
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((first["declaration"]["qualname"], second["declaration"]["qualname"]), ("process", "process"))
        self.assertEqual((first["declaration"]["start_line"], second["declaration"]["start_line"]), (6, 10))
        self.assertNotEqual(first["declaration"]["id"], second["declaration"]["id"])
        inner, outer = self.unit_of(units, "f-inner"), self.unit_of(units, "f-outer-a")
        self.assertEqual((inner["declaration"]["qualname"], outer["declaration"]["qualname"]), ("outer.helper", "outer"))
        self.assertEqual(self.unit_of(units, "f-outer-b")["id"], outer["id"])  # two fragments of outer itself
        self.assertNotEqual(inner["id"], outer["id"])                          # the nested declaration stays apart
        self.assertEqual(outer["finding_ids"], ["f-outer-a", "f-outer-b"])
        # A wrong model symbol is recorded as stale; the host declaration decides the unit.
        stale = self.unit_of(units, "f-stale")
        self.assertEqual((stale["status"], stale["declaration"]["qualname"]), ("verified", "Pair.second"))
        self.assertEqual([(member["symbol"], member["symbol_check"]) for member in stale["members"]],
                         [("Pair.first", "stale_symbol")])
        # A ref that crosses a declaration boundary is a tentative single-anchor unit, joined to neither method.
        cross = self.unit_of(units, "f-cross")
        self.assertEqual((cross["status"], cross["reason"], cross["declaration"]), ("tentative", "spans_declarations", None))
        self.assertEqual(len(cross["member_ids"]), 1)
        self.assertNotEqual(cross["id"], stale["id"])
        module = self.unit_of(units, "f-module")
        self.assertEqual((module["status"], module["reason"], len(module["member_ids"])), ("tentative", "module_level", 1))
        self.assertGreaterEqual(units["accounting"]["stale_symbols"], 1)
        self.assertEqual(units["accounting"]["tentative_by_reason"]["spans_declarations"], 1)

    def test_a_decorated_graph_declaration_is_verified_with_the_graph_line_convention(self):
        built = self.build()
        [anchor] = [item for item in built.catalog["candidates"] if item["path"] == "shop/order/tasks.py"]
        self.assertEqual((anchor["symbol_source"], anchor["start_line"], anchor["end_line"]), ("graph_declaration", 10, 13))
        unit = self.unit_of(built.units, "f-email")
        self.assertEqual((unit["status"], unit["declaration"]["qualname"], unit["declaration"]["start_line"],
                          unit["declaration"]["end_line"]), ("verified", "send_order_email", 10, 13))
        self.assertEqual(unit["member_ids"], [anchor["id"]])

    def test_files_without_a_verified_declaration_give_tentative_single_anchor_units(self):
        built = self.build()
        button = self.unit_of(built.units, "f-button")
        self.assertEqual((button["status"], button["reason"], button["declaration"]), ("tentative", "not_indexed", None))
        broken = built.index["files"]["shop/broken.py"]
        self.assertEqual((broken["parse"], broken["declarations"]), ("failed", []))
        self.assertEqual(set(broken["error"]), {"type", "line"})
        self.assertEqual((broken["error"]["type"], broken["error"]["line"]), ("SyntaxError", 2))
        # Even two fragments citing one symbol are not grouped in a file that fails as original text.
        first, second = self.unit_of(built.units, "f-broken-a"), self.unit_of(built.units, "f-broken-b")
        self.assertNotEqual(first["id"], second["id"])
        for unit in (first, second):
            self.assertEqual((unit["status"], unit["reason"], len(unit["member_ids"])), ("tentative", "parse_failed", 1))
        self.assertEqual(built.index["files"]["web/src/CartButton.tsx"]["parse"], "not_indexed")

    def test_every_anchor_candidate_and_finding_is_kept_in_exactly_one_unit(self):
        built = self.build()
        units, anchors = built.units["units"], self.anchors(built.catalog)
        members = [identifier for unit in units for identifier in unit["member_ids"]]
        self.assertEqual(sorted(members), sorted(item["id"] for item in anchors))
        self.assertEqual(len(members), len(set(members)))
        candidates = [identifier for unit in units for identifier in unit["candidate_ids"]]
        self.assertEqual(sorted(candidates), sorted(item["id"] for item in built.catalog["candidates"]))
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertEqual(sorted(identifier for unit in units for identifier in unit["finding_ids"]),
                         sorted({identifier for item in anchors for identifier in item["finding_ids"]}))
        for unit in units:
            self.assertEqual(unit["role"], "candidate" if unit["candidate_ids"] else "context")
            if unit["status"] == "tentative":
                self.assertEqual(len(unit["member_ids"]), 1)
        accounting = built.units["accounting"]
        self.assertEqual((accounting["anchors"], accounting["candidates"], accounting["units"]),
                         (len(anchors), len(built.catalog["candidates"]), len(units)))
        self.assertEqual(accounting["verified_units"] + accounting["tentative_units"], len(units))
        self.assertEqual(accounting["candidate_units"], sum(unit["role"] == "candidate" for unit in units))
        self.assertEqual(sum(accounting["tentative_by_reason"].values()), accounting["tentative_units"])
        # Grouping reduced the lines_add fragments (3), the two address commands (2 + 2) and outer (2).
        self.assertGreaterEqual(accounting["fragments_grouped"], 9)

    def test_navigation_uses_resolved_or_indexed_calls_and_never_the_same_owner(self):
        built = self.build()
        by_finding = lambda key: self.unit_of(built.units, key)
        confirm = by_finding("f-confirm")
        # Graph-resolved self call: get_instance is a dependency; the sibling clean_note only shares an owner.
        self.assertEqual(confirm["navigation"]["callee_unit_ids"], [by_finding("f-confirm-check")["id"]])
        self.assertIn(by_finding("f-note")["id"], confirm["navigation"]["same_owner_unit_ids"])
        self.assertNotIn(by_finding("f-note")["id"], confirm["navigation"]["callee_unit_ids"])
        # In a file whose redacted text has no graph calls, the original-text index still resolves the plain
        # same-file call of add_variants; the reverse link is recorded on the callee.
        add, helper = by_finding("f-add-entry"), by_finding("f-helper")
        self.assertEqual(add["navigation"]["callee_unit_ids"], [helper["id"]])
        self.assertEqual(helper["navigation"]["caller_unit_ids"], [add["id"]])

    def test_index_and_units_hold_no_source_text_or_raw_secret_and_nothing_is_executed(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        marker = Path(directory.name).resolve() / "executed"
        loader = ("from pathlib import Path\n\n" f"Path({str(marker)!r}).write_text(\"ran\")\n\n\n"
                  "def connect(password=\"RAW-SECRET-88\"):\n    return password\n")
        extra = [finding("f-connect", "integration", "Подключение к сервису.", "shop/loader.py", 6, 7, "connect")]
        built = self.build(sources({"shop/loader.py": loader}), findings() + extra)
        self.assertFalse(marker.exists())
        self.assertIn("RAW-SECRET-88", built.snapshot.files["shop/loader.py"]._raw_text)  # precondition
        out = Path(directory.name).resolve() / "saved"
        out.mkdir()
        save_json(out / "declaration_index.json", built.index)
        save_json(out / "command_units.json", built.units)
        for document in (built.index, built.units):
            text = json.dumps(document, ensure_ascii=False)
            for secret in ("RAW-SECRET-77", "RAW-SECRET-88", "get_token(", "write_text("):
                self.assertNotIn(secret, text)
        for path in out.iterdir():
            self.assertNotIn(b"RAW-SECRET", path.read_bytes())
        self.assertEqual(self.unit_of(built.units, "f-connect")["status"], "verified")

    def test_a_changed_file_revision_is_rejected_and_never_becomes_a_tentative_unit(self):
        old = self.build()
        edited = sources({CHECKOUT + "lines_delete.py": command("CheckoutLinesDelete", "checkout.lines.discard(value)",
                                                                "checkout.lines_deleted")})
        new = self.build(edited)
        self.assertNotEqual(old.snapshot.snapshot_id, new.snapshot.snapshot_id)  # precondition: same paths, new text
        module = api()
        with self.assertRaises(module.DeclarationIndexError) as caught:
            module.build_units(new.catalog, old.index, new.snapshot)
        self.assertIn(caught.exception.code, {"snapshot_mismatch", "file_revision_mismatch"})
        with self.assertRaises(module.DeclarationIndexError):
            module.check_index(old.index, new.snapshot)
        # A forged per-file revision or declaration span is refused even when the document hash is recomputed.
        forged = copy.deepcopy(new.index)
        forged["files"][CHECKOUT + "lines_delete.py"]["sha256"] = old.snapshot.files[CHECKOUT + "lines_delete.py"].sha256
        with self.assertRaises(module.DeclarationIndexError) as caught:
            module.check_index(module.seal(forged), new.snapshot)
        self.assertEqual(caught.exception.code, "file_revision_mismatch")
        forged = copy.deepcopy(new.index)
        declaration = next(item for item in forged["files"][CHECKOUT + "lines_add.py"]["declarations"]
                           if item["qualname"] == "CheckoutLinesAdd.perform_mutation")
        declaration["end_line"] = 19
        with self.assertRaises(module.DeclarationIndexError) as caught:
            module.check_index(module.seal(forged), new.snapshot)
        self.assertEqual(caught.exception.code, "declaration_span_mismatch")

    def test_saved_documents_verify_identity_is_deterministic_and_altered_units_are_refused(self):
        built = self.build()
        module = api()
        out = built.root / "saved"
        out.mkdir()
        save_json(out / "declaration_index.json", built.index)
        save_json(out / "command_units.json", built.units)
        index = json.loads((out / "declaration_index.json").read_text(encoding="utf-8"))
        units = json.loads((out / "command_units.json").read_text(encoding="utf-8"))
        self.assertEqual(module.check_index(index, built.snapshot), index)
        self.assertEqual(module.check_units(units, catalog=built.catalog, index=index), units)
        shuffled = findings()
        random.Random(11).shuffle(shuffled)
        again = self.build(items=shuffled)
        self.assertEqual(again.units["units_sha256"], built.units["units_sha256"])
        self.assertEqual(again.index["index_sha256"], built.index["index_sha256"])
        # (change, recompute the document hash): resealed changes must fail on content, not only on the hash.
        changes = {
            "digest": (lambda data: data["units"][0]["finding_ids"].append("invented"), False),
            "member moved": (_move_member, True),
            "unknown anchor": (lambda data: data["units"][0]["member_ids"].append("act-0000000000000000"), True),
            "other catalog": (lambda data: data.update(catalog_sha256="0" * 64), True),
            "older schema": (lambda data: data.update(schema="command-units/0"), False),
        }
        for label, (change, reseal) in changes.items():
            with self.subTest(change=label):
                altered = copy.deepcopy(units)
                change(altered)
                if reseal:
                    altered = module.seal(altered)
                with self.assertRaises(module.DeclarationIndexError):
                    module.check_units(altered, catalog=built.catalog, index=index)


def _move_member(units):
    """One member taken from a multi-member unit and given to another unit: the partition no longer matches."""
    source = next(unit for unit in units["units"] if len(unit["member_ids"]) > 1)
    target = next(unit for unit in units["units"] if unit is not source)
    target["member_ids"].append(source["member_ids"].pop())


if __name__ == "__main__":
    unittest.main()
