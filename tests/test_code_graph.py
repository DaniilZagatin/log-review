import json
from pathlib import Path
import tempfile
import unittest

from process_observability.code_graph import CALL_STATUSES, build_graph
from process_observability.log_discovery import discover_logging
from process_observability.source_tools import SourceFile, snapshot_repository


class CodeGraphTests(unittest.TestCase):
    """Synthetic fixtures are written to temporary directories and only parsed, never executed."""

    def snapshot(self, files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return snapshot_repository(root)

    def build(self, files, **options):
        snapshot = self.snapshot(files)
        return snapshot, build_graph(snapshot, **options)

    @staticmethod
    def node(graph, path, qualname):
        [found] = [n for n in graph.to_dict()["nodes"] if n["path"] == path and n["qualname"] == qualname]
        return found

    @staticmethod
    def call(graph, path, line, callee):
        [found] = [c for c in graph.to_dict()["calls"]
                   if (c["path"], c["start_line"], c["callee"]) == (path, line, callee)]
        return found

    def test_cross_file_alias_resolves_to_logging_wrapper_hint_only(self):
        snapshot, graph = self.build({
            "src/shop/__init__.py": "",
            "src/shop/audit.py": ("import logging\n_logger = logging.getLogger(__name__)\n\n"
                                  "def log_business(name, **attrs):\n    _logger.info(name, extra=attrs)\n"),
            "src/shop/orders.py": ("from shop.audit import log_business as record\nfrom . import audit\n\n"
                                   "def complete(order):\n    record(\"order.completed\", order_id=order)\n"
                                   "    audit.log_business(\"order.audited\")\n"),
        })
        wrapper = self.node(graph, "src/shop/audit.py", "log_business")
        complete = self.node(graph, "src/shop/orders.py", "complete")
        [site] = [s for s in discover_logging(snapshot)["sites"] if s["path"] == "src/shop/audit.py"]
        self.assertTrue(wrapper["wrapper_candidate"])
        self.assertEqual(wrapper["log_sites"], [site["id"]])
        self.assertEqual(complete["log_sites"], [])
        aliased = self.call(graph, "src/shop/orders.py", 5, "record")
        relative = self.call(graph, "src/shop/orders.py", 6, "audit.log_business")
        for call in (aliased, relative):
            self.assertEqual((call["status"], call["target_id"], call["target_path"], call["owner_id"]),
                             ("resolved_import", wrapper["id"], "src/shop/audit.py", complete["id"]))
            self.assertEqual((call["wrapper_hint"]["wrapper_id"], call["wrapper_hint"]["relation"]),
                             (wrapper["id"], "candidate_logging_wrapper"))
        self.assertEqual([c["id"] for c in graph.incoming(wrapper["id"])], [aliased["id"], relative["id"]])
        self.assertEqual([c["id"] for c in graph.outgoing(complete["id"])], [aliased["id"], relative["id"]])
        self.assertEqual(self.call(graph, "src/shop/audit.py", 2, "logging.getLogger")["status"], "external_or_builtin")
        inner = self.call(graph, "src/shop/audit.py", 5, "_logger.info")
        self.assertEqual((inner["status"], inner["target_id"], inner["owner_id"]),
                         ("unresolved_receiver", None, wrapper["id"]))
        self.assertNotIn("wrapper_hint", inner)
        data = graph.to_dict()
        [alias] = [i for i in data["imports"] if i["bound_name"] == "record"]
        self.assertEqual((alias["module"], alias["name"], alias["status"], alias["module_paths"]),
                         ("shop.audit", "log_business", "resolved", ["src/shop/audit.py"]))
        self.assertLessEqual({c["status"] for c in data["calls"]}, set(CALL_STATUSES))
        self.assertEqual(data["source_roots"], [".", "src"])

    def test_nested_owner_default_and_decorator_scope(self):
        source = ("def trace(label):\n    return lambda fn: fn\n\n"
                  "def default():\n    return 1\n\n"
                  "def helper():\n    return 2\n\n"
                  "def outer():\n"
                  "    def inner(value=default()):\n        return helper()\n"
                  "    @trace(default())\n    def decorated():\n        return inner()\n"
                  "    return decorated()\n\n"
                  "class Service:\n    registry = default()\n\n"
                  "    def build(self, x=helper()):\n        return x\n")
        _, graph = self.build({"app.py": source})
        names = ("trace", "default", "helper", "outer", "outer.inner", "outer.decorated", "Service", "Service.build")
        ids = {name: self.node(graph, "app.py", name)["id"] for name in names}
        inner, decorated = self.node(graph, "app.py", "outer.inner"), self.node(graph, "app.py", "outer.decorated")
        self.assertEqual((inner["parent_id"], inner["kind"], inner["start_line"], inner["end_line"]),
                         (ids["outer"], "function", 11, 12))
        self.assertEqual((decorated["start_line"], decorated["end_line"]), (13, 15))
        self.assertEqual(self.node(graph, "app.py", "Service.build")["kind"], "method")
        expected = {(11, "default"): ("outer", "default"), (12, "helper"): ("outer.inner", "helper"),
                    (13, "trace"): ("outer", "trace"), (13, "default"): ("outer", "default"),
                    (15, "inner"): ("outer.decorated", "outer.inner"), (16, "decorated"): ("outer", "outer.decorated"),
                    (19, "default"): ("Service", "default"), (21, "helper"): ("Service", "helper")}
        for (line, callee), (owner, target) in expected.items():
            with self.subTest(line=line, callee=callee):
                call = self.call(graph, "app.py", line, callee)
                self.assertEqual((call["owner_id"], call["status"], call["target_id"]),
                                 (ids[owner], "local_def", ids[target]))
        self.assertEqual([(c["start_line"], c["callee"]) for c in graph.outgoing(ids["outer"])],
                         [(11, "default"), (13, "trace"), (13, "default"), (16, "decorated")])
        self.assertEqual([c["callee"] for c in graph.outgoing(ids["outer.inner"])], ["helper"])
        self.assertEqual(graph.outgoing(ids["Service.build"]), [])
        self.assertEqual([n["qualname"] for n in graph.decls_in("app.py", 12, 12)], ["<module>", "outer", "outer.inner"])

    def test_duplicate_method_names_are_never_joined(self):
        source = ("class Orders:\n    def save(self):\n        return 1\n\n    def complete(self):\n        self.save()\n\n\n"
                  "class Invoices:\n    def save(self):\n        return 2\n\n"
                  "    def finish(self, obj):\n        obj.save()\n        self.archive()\n\n\n"
                  "class Dup:\n    def save(self):\n        return 3\n\n    def save(self):\n        return 4\n\n"
                  "    def run(self):\n        self.save()\n")
        _, graph = self.build({"app.py": source})
        saves = sorted(n["id"] for n in graph.to_dict()["nodes"] if n["name"] == "save")
        orders_save = self.node(graph, "app.py", "Orders.save")["id"]
        invoices_save = self.node(graph, "app.py", "Invoices.save")["id"]
        self.assertEqual(len(saves), 4)
        lexical = self.call(graph, "app.py", 6, "self.save")
        self.assertEqual((lexical["status"], lexical["target_id"]), ("same_class_lexical", orders_save))
        receiver = self.call(graph, "app.py", 14, "obj.save")
        self.assertEqual((receiver["status"], receiver["target_id"], receiver["target_path"]),
                         ("unresolved_receiver", None, None))
        self.assertEqual(sorted(h["target_id"] for h in receiver["not_edges"]), saves)
        self.assertEqual(({h["relation"] for h in receiver["not_edges"]}, receiver["not_edges_total"]),
                         ({"name_match_only"}, 4))
        duplicate = self.call(graph, "app.py", 26, "self.save")
        self.assertEqual((duplicate["status"], duplicate["target_id"]), ("ambiguous_binding", None))
        self.assertEqual(self.call(graph, "app.py", 15, "self.archive")["status"], "unresolved_receiver")
        self.assertEqual([c["id"] for c in graph.incoming(orders_save)], [lexical["id"]])
        self.assertEqual(graph.incoming(invoices_save), [])

    def test_shadowed_and_rebound_imports_stay_unresolved(self):
        _, graph = self.build({
            "helpers.py": "def notify():\n    pass\n\ndef save():\n    pass\n",
            "app.py": ("from helpers import notify, save\nnotify = wrap(notify)\n\n"
                       "def wrap(fn):\n    return fn\n\n"
                       "def run(save):\n    save()\n    notify()\n\n"
                       "def other():\n    save()\n"),
        })
        param = self.call(graph, "app.py", 8, "save")
        self.assertEqual((param["status"], param["target_id"], param["owner_id"]),
                         ("dynamic", None, self.node(graph, "app.py", "run")["id"]))
        rebound = self.call(graph, "app.py", 9, "notify")
        self.assertEqual((rebound["status"], rebound["target_id"]), ("ambiguous_binding", None))
        imported = self.call(graph, "app.py", 12, "save")
        self.assertEqual((imported["status"], imported["target_id"], imported["target_path"]),
                         ("resolved_import", self.node(graph, "helpers.py", "save")["id"], "helpers.py"))
        self.assertEqual(graph.incoming(self.node(graph, "helpers.py", "notify")["id"]), [])
        self.assertEqual(self.call(graph, "app.py", 2, "wrap")["status"], "local_def")
        self.assertEqual(graph.to_dict()["references"], [])

    def test_relative_imports_and_exact_source_roots(self):
        files = {"src/pkg/__init__.py": "from .impl import run\n",
                 "src/pkg/impl.py": "def run():\n    pass\n",
                 "src/pkg/sub/__init__.py": "",
                 "src/pkg/sub/user.py": ("from ..impl import run\nfrom .. import run as exported\nimport pkg.impl as impl\n"
                                         "import impl as wrong\n\ndef go():\n    run()\n    exported()\n"
                                         "    impl.run()\n    wrong.run()\n")}
        user = "src/pkg/sub/user.py"
        _, graph = self.build(files)
        run = self.node(graph, "src/pkg/impl.py", "run")["id"]
        expected = {(7, "run"): ("resolved_import", run, "src/pkg/impl.py"),
                    (8, "exported"): ("module_only", None, "src/pkg/__init__.py"),  # re-export is not followed
                    (9, "impl.run"): ("resolved_import", run, "src/pkg/impl.py"),
                    (10, "wrong.run"): ("external_or_builtin", None, None)}  # no suffix matching to src/pkg/impl.py
        for (line, callee), outcome in expected.items():
            with self.subTest(callee=callee):
                call = self.call(graph, user, line, callee)
                self.assertEqual((call["status"], call["target_id"], call["target_path"]), outcome)
        [relative] = [i for i in graph.to_dict()["imports"] if i["path"] == user and i["start_line"] == 1]
        self.assertEqual((relative["level"], relative["module"], relative["status"], relative["module_paths"]),
                         (2, "impl", "resolved", ["src/pkg/impl.py"]))
        duplicate = {**files, "pkg/impl.py": "def run():\n    pass\n"}
        _, both = self.build(duplicate)
        ambiguous = self.call(both, user, 9, "impl.run")
        self.assertEqual((ambiguous["status"], ambiguous["target_id"]), ("ambiguous_binding", None))
        self.assertEqual(self.call(both, user, 7, "run")["target_id"], self.node(both, "src/pkg/impl.py", "run")["id"])
        _, narrow = self.build(duplicate, source_roots=("src",))
        unique = self.call(narrow, user, 9, "impl.run")
        self.assertEqual((unique["status"], unique["target_id"]),
                         ("resolved_import", self.node(narrow, "src/pkg/impl.py", "run")["id"]))
        self.assertEqual(narrow.to_dict()["source_roots"], ["src"])

    def test_wildcard_may_shadow_existing_declarations_and_imported_members(self):
        _,graph=self.build({
            'producer.py':'def work(): pass\nfrom unavailable import *\nwork()\n',
            'consumer.py':'from producer import work\nwork()\n',
            'local.py':'from unavailable import *\ndef run():\n    def local(): pass\n    local()\n'})
        for path,line in [('producer.py',3),('consumer.py',2)]:
            call=self.call(graph,path,line,'work')
            self.assertEqual(call['status'],'ambiguous_binding')
            self.assertIsNone(call['target_id'])
        # A nested local binding still takes precedence over the module's star.
        self.assertEqual(self.call(graph,'local.py',4,'local')['status'],'local_def')

    def test_callback_reference_is_not_a_call_edge_and_dynamic_targets_stay_dynamic(self):
        source = ("from tasks import scheduler\n\ndef callback(event):\n    return event\n\n"
                  "class Worker:\n    def handle(self):\n        return 1\n\n    def start(self):\n"
                  "        scheduler.submit(callback)\n        scheduler.submit(fn=self.handle)\n"
                  "        getattr(self, \"handle\")()\n        handlers = {}\n        handlers[\"x\"]()\n")
        _, graph = self.build({"app.py": source})
        callback, handle, start = (self.node(graph, "app.py", name)["id"]
                                   for name in ("callback", "Worker.handle", "Worker.start"))
        references = graph.to_dict()["references"]
        self.assertEqual([(r["kind"], r["name"], r["arg_index"], r["keyword"], r["status"], r["target_id"], r["owner_id"])
                          for r in references],
                         [("reference_arg", "callback", 0, None, "local_def", callback, start),
                          ("reference_arg", "self.handle", None, "fn", "same_class_lexical", handle, start)])
        submit = self.call(graph, "app.py", 11, "scheduler.submit")
        self.assertEqual((references[0]["call_id"], submit["status"], submit["target_id"]),
                         (submit["id"], "external_or_builtin", None))
        self.assertEqual((graph.incoming(callback), graph.incoming(handle)), ([], []))
        self.assertEqual(self.call(graph, "app.py", 13, "getattr(...)")["status"], "dynamic")
        self.assertEqual(self.call(graph, "app.py", 13, "getattr")["status"], "external_or_builtin")
        self.assertEqual(self.call(graph, "app.py", 15, "handlers[...]")["status"], "dynamic")

    def test_identical_queue_topics_are_literals_without_edges(self):
        _, graph = self.build({
            "producer.py": "def place(bus):\n    bus.publish(\"orders.created\", {\"id\": 1})\n",
            "consumer.py": ("from broker import subscribe\n\n@subscribe(\"orders.created\")\ndef on_created(message):\n"
                            "    return message\n\ndef poll(client):\n    client.consume(topic=\"orders.created\")\n"),
        })
        module = self.node(graph, "consumer.py", "<module>")["id"]
        on_created = self.node(graph, "consumer.py", "on_created")["id"]
        poll, place = self.node(graph, "consumer.py", "poll")["id"], self.node(graph, "producer.py", "place")["id"]
        self.assertEqual([(s["path"], s["start_line"], s["owner_id"], s["role"], s["decorated_id"], s["relation"])
                          for s in graph.literal_sites("orders.created")],
                         [("consumer.py", 3, module, "first_arg", on_created, "literal_only"),
                          ("consumer.py", 8, poll, "keyword:topic", None, "literal_only"),
                          ("producer.py", 2, place, "first_arg", None, "literal_only")])
        self.assertEqual(graph.literal_sites("orders"), [])
        data = graph.to_dict()
        self.assertEqual([(c["callee"], c["target_id"]) for c in data["calls"]],
                         [("subscribe", None), ("client.consume", None), ("bus.publish", None)])
        self.assertEqual(data["references"], [])
        for node in data["nodes"]:
            self.assertEqual(graph.incoming(node["id"]), [])

    def test_ids_bind_source_hash_and_graph_is_deterministic(self):
        source = "def run():\n    helper()\n\ndef helper():\n    pass\n"
        snapshot, first = self.build({"app.py": source})
        _, same = self.build({"app.py": source})
        self.assertEqual(first.to_dict(), build_graph(snapshot).to_dict())
        self.assertEqual(first.sha256, same.sha256)
        _, changed = self.build({"app.py": source.replace("pass", "pass  # revised")})
        old, new = self.node(first, "app.py", "run"), self.node(changed, "app.py", "run")
        self.assertEqual((old["start_line"], old["end_line"]), (new["start_line"], new["end_line"]))
        self.assertNotEqual(old["id"], new["id"])
        self.assertNotEqual(self.call(first, "app.py", 2, "helper")["id"], self.call(changed, "app.py", 2, "helper")["id"])
        self.assertNotEqual(first.sha256, changed.sha256)
        with self.assertRaises(ValueError):
            build_graph(snapshot, discover_logging(self.snapshot({"other.py": "x = 1\n"})))

    def test_parse_error_and_odd_separators_fall_back_without_edges(self):
        for source in ["def broken(:\n    helper()\n", "x = 1\fhelper()\n"]:
            with self.subTest(source=source):
                _, graph = self.build({"app.py": source, "helper.py": "def helper():\n    pass\n"})
                data = graph.to_dict()
                [meta] = [f for f in data["files"] if f["path"] == "app.py"]
                self.assertEqual(meta["extractor"], "python_fallback")
                self.assertTrue(meta["fallback"])
                self.assertEqual([(n["kind"], n["start_line"], n["end_line"]) for n in data["nodes"] if n["path"] == "app.py"],
                                 [("module", 1, 2), ("window", 1, 2)])
                self.assertEqual([r for r in data["calls"] + data["imports"] if r["path"] == "app.py"], [])
                self.assertEqual(graph.incoming(self.node(graph, "helper.py", "helper")["id"]), [])

    def test_non_python_heuristic_boundaries_and_text_windows(self):
        java = ("package demo;\n\npublic class OrderService {\n    @Transactional\n    public void complete(String id) {\n"
                "        if (id != null) {\n            publisher.send(\"orders.completed\", id);\n        }\n    }\n\n"
                "    private int count() { return 1; }\n}\n")
        typescript = ("export class Cart {\n  add(item: string): void {\n    this.items.push(item);\n  }\n}\n\n"
                      "export const total = (items: number[]) => {\n  return items.length;\n};\n\n"
                      "function helper() {\n  return 1;\n}\n")
        go = "package main\n\ntype Server struct {\n\tname string\n}\n\nfunc (s *Server) Handle(id string) error {\n\treturn nil\n}\n"
        kotlin = ("class Greeter {\n    fun greet(name: String): String = \"hi $name\"\n    fun run() {\n"
                  "        println(greet(\"x\"))\n    }\n}\n")
        config = "".join(f"key{number}: value\n" for number in range(85))
        _, graph = self.build({"Service.java": java, "app.ts": typescript, "main.go": go, "Greeter.kt": kotlin,
                               "settings.yaml": config})
        data = graph.to_dict()

        def spans(path):
            return [(n["qualname"], n["kind"], n["start_line"], n["end_line"])
                    for n in data["nodes"] if n["path"] == path and n["kind"] != "module"]
        self.assertEqual(spans("Service.java"), [("OrderService", "heuristic_type", 3, 12),
                                                 ("OrderService.complete", "heuristic_function", 4, 9),
                                                 ("OrderService.count", "heuristic_function", 11, 11)])
        self.assertEqual(spans("app.ts"), [("Cart", "heuristic_type", 1, 5), ("Cart.add", "heuristic_function", 2, 4),
                                           ("total", "heuristic_function", 7, 9), ("helper", "heuristic_function", 11, 13)])
        self.assertEqual(spans("main.go"), [("Server", "heuristic_type", 3, 5),
                                            ("Server.Handle", "heuristic_function", 7, 9)])
        self.assertEqual(spans("Greeter.kt"), [("Greeter", "heuristic_type", 1, 6),
                                               ("Greeter.greet", "heuristic_function", 2, 2),
                                               ("Greeter.run", "heuristic_function", 3, 5)])
        self.assertEqual(spans("settings.yaml"), [("<window:1-80>", "window", 1, 80), ("<window:81-85>", "window", 81, 85)])
        self.assertEqual({n["extractor"] for n in data["nodes"] if n["kind"].startswith("heuristic")}, {"heuristic"})
        self.assertEqual((data["calls"], data["imports"], data["references"]), ([], [], []))
        self.assertEqual({f["path"]: f["extractor"] for f in data["files"]},
                         {"Greeter.kt": "heuristic", "Service.java": "heuristic", "app.ts": "heuristic",
                          "main.go": "heuristic", "settings.yaml": "text_window"})
        [literal] = graph.literal_sites("orders.completed")
        self.assertEqual((literal["owner_id"], literal["extractor"], literal["call_id"]),
                         (self.node(graph, "Service.java", "OrderService.complete")["id"], "heuristic", None))

    def test_uses_only_frozen_redacted_text(self):
        class Snapshot:
            snapshot_id = "stub"
            scope = {"partial_input": False}
            logging_calls = ()
            files = {"app.py": SourceFile("app.py", "0" * 64, "def visible():\n    pass\n", "def hidden_raw():\n    pass\n")}
        text = json.dumps(build_graph(Snapshot()).to_dict())
        self.assertIn("visible", text)
        self.assertNotIn("hidden_raw", text)


if __name__ == "__main__":
    unittest.main()
