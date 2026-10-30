import json
from pathlib import Path
import tempfile
import unittest

from process_observability.log_discovery import discover_logging
from process_observability.source_tools import snapshot_repository
from process_observability.workflow import _emits


class LoggingDiscoveryTests(unittest.TestCase):
    def snapshot(self, text, filename="app.py"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / filename
        path.write_text(text, encoding="utf-8")
        return snapshot_repository(directory.name)

    def test_fixture_has_seven_multiline_sites_and_no_cancel_or_commit(self):
        snapshot = snapshot_repository(Path(__file__).parent / "fixtures/synthetic", includes=["app.py"])
        inventory = discover_logging(snapshot)
        self.assertEqual([(s["start_line"],s["end_line"]) for s in inventory["sites"]],
                         [(5,6),(10,11),(13,14),(16,17),(22,23),(25,26),(29,30)])
        for site in inventory["sites"]:
            self.assertEqual(site["fields"], ["attempt_id","case_id","occurrence_id"])
            evidence = snapshot.evidence({k:site[k] for k in ("path","start_line","end_line","symbol")})
            self.assertIn(site["event_key"], evidence.safe_quote)
        self.assertEqual(inventory, discover_logging(snapshot))
        self.assertNotIn('"verified"', json.dumps(inventory))

    def test_multiline_literals_apostrophes_constants_and_dynamic_keys(self):
        snapshot = self.snapshot('EVENT = "order.done"\n_logger.info(\n    "Can\'t approve",\n    case_id=case_id, extra={"order_id": value, **extra}\n)\n_logger.info(EVENT)\n_logger.info(f"order.{status}")\n')
        sites = discover_logging(snapshot)["sites"]
        self.assertEqual([s["event_key"] for s in sites], ["Can't approve", "order.done", None])
        self.assertEqual([s["key_status"] for s in sites], ["literal","resolved_constant","dynamic"])
        self.assertEqual(sites[0]["fields_status"], "partial")
        self.assertIn("order_id",sites[0]["fields"])
        anchor = snapshot.evidence({"path":"app.py","start_line":3,"end_line":3}).source_anchor
        self.assertTrue(_emits(anchor, "Can't approve",sites))
        self.assertFalse(_emits(anchor,"order.done",sites))

    def test_factory_logger_wrapper_hints_and_nonlogging_names(self):
        snapshot = self.snapshot('audit = logging.getLogger(__name__)\ndef log_business(name, **attrs):\n    audit.info(name, extra=attrs)\nlog_business("order.done", case_id=value)\ncatalog.info("not a log")\ndef emit(record):\n    return record\n')
        inventory = discover_logging(snapshot)
        self.assertEqual([s["kind"] for s in inventory["sites"]], ["log_call","wrapper_call"])
        self.assertEqual(inventory["wrappers"][0]["name"], "log_business")
        wrapper = inventory["sites"][1]
        anchor = snapshot.evidence({"path":"app.py","start_line":4,"end_line":4}).source_anchor
        self.assertFalse(_emits(anchor,"order.done",inventory["sites"]))
        self.assertNotIn("catalog.info",[s["callee"] for s in inventory["sites"]])

    def test_same_line_sites_keep_unique_ids_and_do_not_resolve_reassigned_constant(self):
        inventory = discover_logging(self.snapshot('EVENT="one"\nEVENT="two"\nlog.info(EVENT); log.info("done"); log.info("done")\n'))
        self.assertEqual(len(inventory["sites"]),3)
        self.assertEqual(len({s["id"] for s in inventory["sites"]}),3)
        self.assertEqual(inventory["sites"][0]["key_status"], "symbolic")

    def test_foreign_language_heuristic_and_config_are_hints(self):
        snapshot = self.snapshot('const audit = pino();\naudit.info(\n  "order.done", {orderId: id}\n);\nthis.emit("not.logging");\ncatalog.info("not.logging");\n// logger.info("comment");\n',"app.ts")
        inventory = discover_logging(snapshot)
        self.assertEqual(len(inventory["sites"]),1)
        self.assertEqual((inventory["sites"][0]["event_key"], inventory["sites"][0]["extractor"]),("order.done","heuristic"))
        self.assertEqual(inventory["sites"][0]["end_line"],4)
        config = discover_logging(self.snapshot('[handler_file]\nclass=FileHandler\npassword="CANARY"\n',"logging.conf"))
        self.assertTrue(config["config_hints"])
        self.assertNotIn("CANARY",json.dumps(config))

    def test_ast_fallback_keeps_correct_lines(self):
        for source in ['print "legacy"\n_logger.info("done")\n', 'broken !!!\n_logger.info("done")\n', 'x=1\f_logger.info("done")\n']:
            with self.subTest(source=source):
                snapshot = self.snapshot(source)
                inventory = discover_logging(snapshot)
                self.assertEqual(inventory["scope"]["extractors"],{"ast_fallback":1})
                [site] = inventory["sites"]
                self.assertEqual(site["start_line"],2)
                self.assertIn("done",snapshot.evidence({k:site[k] for k in ("path","start_line","end_line")}).safe_quote)

    def test_caps_and_explicit_custom_calls_preserve_omissions(self):
        snapshot = self.snapshot('audit_event("done")\nlog.info("one")\nlog.info("two")\n')
        inventory = discover_logging(snapshot,call_names=["audit_event"],max_sites=1)
        self.assertEqual((inventory["scope"]["sites_found"],inventory["scope"]["sites_returned"]),(3,1))
        self.assertTrue(inventory["scope"]["truncated"])
        self.assertEqual(inventory["scope"]["omitted_by_path"],{"app.py":2})
        self.assertEqual(inventory["sites"][0]["kind"],"configured_call")
        with self.assertRaises(ValueError):discover_logging(snapshot,call_names=["../../bad"])


if __name__ == "__main__":
    unittest.main()
