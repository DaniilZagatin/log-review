"""Process report over a saved map: static logging catalog, linkage and tabs."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from pko.observability.contracts import ReportError, map_revision, validate_report
from pko.observability.log_discovery import discover_logging, logging_config_paths
from pko.observability.mapping import link_sites
from pko.observability.export import safe_cell
from pko.observability.pipeline import ReportOptions, run_observability
from pko.render.scenario_map import render_scenario_map
from pko.errors import PkoError
from pko.scenario_map.runner import load_saved_map, run_report_from_map, run_scenario
from observability_script import ScriptedReportClient
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources

PYTHON = '''import logging
logger = logging.getLogger(__name__)
ACCEPTED = "application.accepted"


def accept(request):
    logger.info(ACCEPTED, extra={"request_id": request["id"], "channel": "mobile"})
    store.save(request["id"])
    return request["id"]


def reject(request, reason):
    logger.warning("application.rejected: %s", reason)
    return None


def audit(message):
    logger.info(message)
'''

JAVA = '''class Issuance {
    private static final Logger LOG = LoggerFactory.getLogger(Issuance.class);
    void issue(Card card) {
        LOG.info("card.issued", card.getId());
        // LOG.debug("not a call");
    }
}
'''

LOGBACK = '<configuration><appender name="KAFKA" class="LogstashTcpSocketAppender"/></configuration>\n'


class Tree:
    def __init__(self, files):
        self.content = files
        self.files = sorted(files)

    def read(self, path):
        return self.content.get(path)


def ref(path, start, end):
    return {"path": path, "line_start": start, "line_end": end, "quote": ""}


def model():
    return {
        "schema_version": "2.0",
        "scenario": {"title": "Заявка", "entry_operation_ids": ["step-1"], "terminal_operation_ids": ["step-2"]},
        "nodes": [
            {"id": "step-1", "kind": "operation", "name": "Принять заявку", "evidence": [ref("src/app.py", 6, 9)],
             "code_explanations": [{"kind": "function", "symbol": "accept", "explanation": "",
                                    "evidence": [ref("src/app.py", 6, 9)]}]},
            {"id": "step-2", "kind": "operation", "name": "Отклонить заявку", "evidence": [ref("src/app.py", 12, 14)]},
            {"id": "step-3", "kind": "operation", "name": "Сверить заявку", "evidence": [ref("src/app.py", 13, 13)]},
        ],
        "edges": [{"id": "edge-1", "source": "step-1", "target": "step-2"}],
        "read_coverage": [{"path": "src/app.py", "ranges": [[1, 14]]}, {"path": "src/Issuance.java", "ranges": [[1, 7]]}],
        "run": {"id": "run-1", "status": "completed"},
    }


def tree():
    return Tree({"src/app.py": PYTHON, "src/Issuance.java": JAVA, "config/logback.xml": LOGBACK, "README.md": "# docs\n"})


class Elements(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements, self.scripts = [], []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == "script":
            self.scripts.append(dict(attrs))


class LogDiscoveryTests(unittest.TestCase):
    def test_python_and_java_sites_keep_key_status_and_never_execute_code(self):
        inventory = discover_logging(tree(), ["src/app.py", "src/Issuance.java"],
                                     config_paths=logging_config_paths(tree()))
        by_key = {(site["path"], site["line_start"]): site for site in inventory["sites"]}
        accepted = by_key[("src/app.py", 7)]
        self.assertEqual((accepted["event_key"], accepted["key_status"]), ("application.accepted", "resolved_constant"))
        self.assertEqual(accepted["fields"], ["channel", "extra", "request_id"])
        self.assertEqual(accepted["symbol"], "accept")
        self.assertEqual(by_key[("src/app.py", 18)]["key_status"], "symbolic")
        self.assertEqual(by_key[("src/Issuance.java", 4)]["event_key"], "card.issued")
        self.assertNotIn(("src/Issuance.java", 5), by_key, "Commented calls are not sites")
        self.assertEqual([w["name"] for w in inventory["wrappers"]], ["audit"])
        self.assertEqual(inventory["scope"]["config_files"], ["config/logback.xml"])
        self.assertEqual(inventory["config_hints"][0]["kind"], "configuration_hint")

    def test_unreadable_files_and_truncation_are_reported(self):
        inventory = discover_logging(tree(), ["src/app.py", "src/missing.py"], max_sites=1)
        self.assertEqual(inventory["scope"]["files_unreadable"], ["src/missing.py"])
        self.assertTrue(inventory["scope"]["truncated"])
        self.assertEqual(inventory["scope"]["sites_returned"], 1)
        self.assertTrue(any("missing.py" in item for item in inventory["limitations"]))


class LinkageTests(unittest.TestCase):
    def test_location_candidates_are_proposed_and_ambiguity_is_kept(self):
        data = model()
        inventory = discover_logging(tree(), ["src/app.py", "src/Issuance.java"])
        linked = link_sites(data, inventory, map_revision(data))
        status = {(site["path"], site["line_start"]): site for site in linked["sites"]}
        self.assertEqual(status[("src/app.py", 7)]["operation_ids"], ["step-1"])
        self.assertEqual(status[("src/app.py", 13)]["link_status"], "ambiguous")
        self.assertEqual(sorted(status[("src/app.py", 13)]["operation_ids"]), ["step-2", "step-3"])
        self.assertEqual(status[("src/app.py", 18)]["link_status"], "unlinked")
        self.assertFalse(status[("src/app.py", 18)]["read_by_researcher"])
        self.assertTrue(status[("src/Issuance.java", 4)]["read_by_researcher"])
        [binding] = linked["bindings"]
        self.assertEqual((binding["operation_id"], binding["authority_status"], binding["event_role"]),
                         ("step-1", "proposed", "unknown"))
        self.assertEqual(binding["map_revision"], map_revision(data))

    def test_operation_evidence_outranks_shared_function_explanations(self):
        data = model()
        explanation = [{"kind": "function", "symbol": "accept", "explanation": "", "evidence": [ref("src/app.py", 6, 9)]}]
        data["nodes"] = [
            {"id": "step-1", "kind": "operation", "evidence": [ref("src/app.py", 7, 7)], "code_explanations": explanation},
            {"id": "step-2", "kind": "operation", "evidence": [ref("src/app.py", 8, 8)], "code_explanations": explanation},
        ]
        inventory = {"sites": [{"id": "log-a", "path": "src/app.py", "line_start": 7, "line_end": 7},
                               {"id": "log-b", "path": "src/app.py", "line_start": 9, "line_end": 9}]}
        linked = {site["id"]: site for site in link_sites(data, inventory, "rev")["sites"]}
        self.assertEqual((linked["log-a"]["link_status"], linked["log-a"]["link_basis"], linked["log-a"]["operation_ids"]),
                         ("linked", "operation_evidence", ["step-1"]))
        self.assertEqual((linked["log-b"]["link_status"], linked["log-b"]["link_basis"], linked["log-b"]["operation_ids"]),
                         ("ambiguous", "code_explanation", ["step-1", "step-2"]))


class ReportContractTests(unittest.TestCase):
    def test_pipeline_report_is_valid_and_does_not_touch_operations(self):
        data = model()
        before = copy.deepcopy(data)
        report = run_observability(data, tree())
        self.assertEqual(data, before)
        self.assertEqual([(s["id"], s["execution_status"], s["result_status"]) for s in report["stages"]],
                         [("logging_inventory", "completed", "complete"), ("event_operation_mapping", "completed", "complete"),
                          ("event_binding_review", "not_run", "none"), ("process_context", "not_run", "none"),
                          ("fact_requirements", "blocked", "none"), ("logging_coverage", "blocked", "none"),
                          ("instrumentation_plan", "blocked", "none"), ("metric_catalog", "blocked", "none")])
        self.assertEqual(report["logging"]["scope"]["files_scanned"], 2)

    def test_partial_map_gives_partial_mapping_result(self):
        data = model()
        data["run"]["status"] = "partial"
        stages = {s["id"]: s for s in run_observability(data, tree())["stages"]}
        self.assertEqual(stages["event_operation_mapping"]["result_status"], "partial")
        self.assertTrue(stages["event_operation_mapping"]["reasons"])

    def test_failed_inventory_blocks_only_its_dependants(self):
        with patch("pko.observability.pipeline.discover_logging", side_effect=RuntimeError("boom")):
            report = run_observability(model(), tree())
        self.assertEqual([(s["execution_status"], s["result_status"]) for s in report["stages"]],
                         [("failed", "none"), ("blocked", "none"), ("blocked", "none"), ("not_run", "none")]
                         + [("blocked", "none")] * 4)
        self.assertEqual(report["logging"], {})

    def test_report_cannot_refer_to_another_revision_or_claim_acceptance(self):
        data = model()
        report = run_observability(data, tree())
        changed = copy.deepcopy(data)
        changed["nodes"][0]["name"] = "Принять заявку клиента"
        with self.assertRaisesRegex(ReportError, "another map revision"):
            validate_report(report, changed)
        accepted = copy.deepcopy(report)
        accepted["logging"]["bindings"][0]["authority_status"] = "accepted"
        with self.assertRaisesRegex(ReportError, "proposed"):
            validate_report(accepted, data)
        foreign = copy.deepcopy(report)
        foreign["logging"]["bindings"][0]["operation_id"] = "step-99"
        with self.assertRaises(ReportError):
            validate_report(foreign, data)
        complete_failure = copy.deepcopy(report)
        complete_failure["stages"][0].update(execution_status="failed", result_status="complete")
        with self.assertRaises(ReportError):
            validate_report(complete_failure, data)


class ReportRenderTests(unittest.TestCase):
    def test_tabs_appear_only_with_a_validated_report_and_text_stays_data(self):
        data = model()
        plain = Elements(render_scenario_map(data))
        self.assertFalse(any(attrs.get("role") == "tab" for _, attrs in plain.elements))
        self.assertNotIn("process-report", [attrs.get("id") for attrs in plain.scripts])
        report = run_observability(data, tree())
        report["logging"]["sites"][0]["event_key"] = '</script><img src=x onerror="alert(1)">'
        document = Elements(render_scenario_map(data, report))
        tabs = [attrs for _, attrs in document.elements if attrs.get("role") == "tab"]
        self.assertEqual([tab["data-tab"] for tab in tabs], ["map", "logging", "changes", "metrics", "basis"])
        panels = {attrs["id"] for _, attrs in document.elements if attrs.get("role") == "tabpanel"}
        self.assertEqual(panels, {"panel-logging", "panel-changes", "panel-metrics", "panel-basis"})
        self.assertIn("process-report", [attrs.get("id") for attrs in document.scripts])
        self.assertFalse(any(tag == "img" for tag, _ in document.elements))
        with self.assertRaises(ReportError):
            render_scenario_map({**data, "edges": []}, report)


class RunnerReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_observability_run_publishes_report_next_to_unchanged_map(self):
        output = self.root / "out"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            result = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                                  report_options=ReportOptions(client=ScriptedReportClient()))
        self.assertEqual(result["run"]["status"], "completed")
        saved = json.loads((output / "scenario_map.json").read_text(encoding="utf-8"))
        report = json.loads((output / "process_report.json").read_text(encoding="utf-8"))
        validate_report(report, saved)
        self.assertEqual(report["run_id"], result["run"]["id"])
        self.assertEqual({row["execution_status"] for row in report["stages"]}, {"completed"})
        self.assertIn('role="tablist"', (output / "scenario_map.html").read_text(encoding="utf-8"))
        audit = next((output / "audit").glob("run_*.txt")).read_text(encoding="utf-8")
        for span in ("logging_inventory", "event_operation_mapping", "process_context", "metric_catalog",
                     "publish_report", "export_handoff"):
            self.assertIn(span, audit)
        manifest = json.loads((output / "handoff" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["map_revision"], report["map_revision"])
        for row in manifest["files"]:
            content = (output / "handoff" / row["name"]).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), row["sha256"], row["name"])
        self.assertTrue((output / "handoff" / "metrics.csv").read_text(encoding="utf-8-sig").startswith("metric_id,"))

    def test_local_journal_keeps_one_span_tree_with_terminal_statuses(self):
        output = self.root / "traced"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            result = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                                  report_options=ReportOptions(client=ScriptedReportClient()))
        self.assertEqual(result["run"]["audit_status"], "complete")
        text = next((output / "audit").glob("run_*.txt")).read_text(encoding="utf-8")
        entries = [json.loads(part.split("\n", 1)[1]) for part in text.split("\n=== ")[1:]]
        started = {row["span_id"]: row for row in entries if row["event"] == "span_started"}
        finished = {row["span_id"]: row for row in entries if row["event"] == "span_finished"}
        self.assertEqual(set(started), set(finished), "every started span ends")
        self.assertTrue(all(row["status"] in ("ok", "error", "interrupted") for row in finished.values()))
        roots = [row for row in started.values() if row["parent_span_id"] is None]
        self.assertEqual([row["name"] for row in roots], ["scenario_map_run"])
        by_name = {row["name"]: row for row in started.values()}
        self.assertEqual(started[by_name["event_binding_review"]["parent_span_id"]]["name"], "process_observability")
        self.assertEqual(started[by_name["process_observability"]["parent_span_id"]]["name"], "scenario_map_run")
        self.assertNotIn("reasoning_chars", text)

    def test_csv_cells_cannot_start_a_formula(self):
        self.assertEqual(safe_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(safe_cell("-1"), "'-1")
        self.assertEqual(safe_cell(["a", "b"]), "a; b")
        self.assertEqual(safe_cell(None), "")

    def test_report_failure_keeps_the_published_map(self):
        output = self.root / "failed"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}), \
                patch("pko.scenario_map.runner.run_observability", side_effect=RuntimeError("report broke")):
            result = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True)
        self.assertEqual(result["run"]["status"], "completed")
        self.assertIn("report broke", result["run"]["process_report_error"])
        self.assertFalse((output / "process_report.json").exists())
        self.assertNotIn('role="tablist"', (output / "scenario_map.html").read_text(encoding="utf-8"))

    def test_report_reruns_from_a_saved_map_with_the_same_sources_only(self):
        output = self.root / "first"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            first = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                                 report_options=ReportOptions(client=ScriptedReportClient()))
            earlier = json.loads((output / "process_report.json").read_text(encoding="utf-8"))
            rerun = run_report_from_map(output / "scenario_map.json", sources(), output, spec=SPEC,
                                        report_options=ReportOptions(client=ScriptedReportClient()))
            with self.assertRaisesRegex(PkoError, "не совпадают"):
                run_report_from_map(output / "scenario_map.json", sources()[:2], self.root / "other", spec=SPEC,
                                    report_options=ReportOptions(client=ScriptedReportClient()))
        report = json.loads((output / "process_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["map_revision"], earlier["map_revision"])
        self.assertNotEqual(report["run_id"], earlier["run_id"])
        self.assertEqual(rerun["run"]["id"], first["run"]["id"], "the map keeps the run that built it")
        self.assertEqual(rerun["run"]["report_runs"][-1]["id"], report["run_id"])
        audit = (output / "audit" / f"run_{report['run_id']}.txt").read_text(encoding="utf-8")
        self.assertIn("scenario_report_run", audit)
        self.assertIn(first["run"]["id"], audit)

    def test_saved_map_must_be_a_finished_current_map(self):
        path = self.root / "map.json"
        for content in ({"schema_version": "1.0", "run": {"status": "completed"}, "nodes": [{}]},
                        {"schema_version": "2.0", "run": {"status": "running"}, "nodes": [{}]}):
            path.write_text(json.dumps(content), encoding="utf-8")
            with self.assertRaises(PkoError):
                load_saved_map(path)

    def test_map_only_run_has_no_report(self):
        output = self.root / "plain"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient())
        self.assertFalse((output / "process_report.json").exists())


if __name__ == "__main__":
    unittest.main()
