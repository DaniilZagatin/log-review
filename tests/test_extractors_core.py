"""Retained source analyzers work without Git, initiatives or passport models."""

import json
import tempfile
import unittest
from pathlib import Path

from pko.code_tools.local_tree import LocalTree
from pko.extractors.base import FACT_KINDS, FileTree, is_migration
from pko.extractors.runner import extract_all
from pko.extractors.test_reports import read_junit
from pko.model import taxonomy
from pko.model.schema import Coverage, Evidence
from pko.util.yamlmini import YamlSubsetError, loads


def tree(files):
    return LocalTree(Path("."), sorted(files), files)


class CoreExtractorTests(unittest.TestCase):
    def test_python_routes_and_actual_local_call_edges(self):
        files = {
            "source-1/app.py": "from fastapi import APIRouter\nrouter = APIRouter()\n"
                               "def helper():\n    return 1\n"
                               "@router.post('/applications')\ndef accept():\n    return helper()\n",
            "source-2/app.py": "def helper():\n    return 2\n",
        }
        extraction = extract_all(tree(files))
        routes = extraction.by_kind("ROUTE")
        self.assertTrue(any("/applications" in fact.key for fact in routes))
        calls = extraction.by_kind("CALLS")
        self.assertEqual([(fact.path, fact.key) for fact in calls],
                         [("source-1/app.py", "accept -> helper")])
        for fact in extraction.facts:
            self.assertIn(fact.path, files)
            if fact.line:
                self.assertGreaterEqual(fact.line, 1)
                self.assertLessEqual(fact.line, len(files[fact.path].splitlines()))

    def test_java_http_and_queue_entrypoints_are_retained(self):
        source = '@RestController\npublic class CardController {\n'
        source += '    @PostMapping("/cards")\n    public String issue() { return "ok"; }\n'
        source += '    @KafkaListener(topics = "card.issue")\n    public void consume(String body) {}\n}\n'
        extraction = extract_all(tree({"source-1/CardController.java": source}))
        self.assertTrue(any(fact.key == "POST /cards" for fact in extraction.by_kind("ROUTE")))
        self.assertTrue(any("card.issue" in fact.value for fact in extraction.by_kind("JAVA_JOB")))
        self.assertEqual(extraction.parse_failures, [])

    def test_sql_comments_do_not_create_false_calls(self):
        source = "-- CALL imaginary();\nCREATE PROCEDURE issue_card() AS\nBEGIN\n"
        source += "CALL verify_customer();\nINSERT INTO issued_cards SELECT * FROM applications;\nEND;\n"
        extraction = extract_all(tree({"source-2/issue.sql": source}))
        calls = extraction.by_kind("SQL_CALL")
        self.assertEqual([fact.key for fact in calls], ["verify_customer"])
        self.assertEqual(calls[0].line, 4)
        self.assertTrue(extraction.by_kind("SQL_WRITE"))
        self.assertTrue(extraction.by_kind("SQL_READ"))

    def test_contracts_and_runtime_limits_keep_source_identity(self):
        contract = {"openapi": "3.0.0", "paths": {"/cards": {"post": {"operationId": "issueCard"}}}}
        extraction = extract_all(tree({
            "source-1/openapi.json": json.dumps(contract),
            "source-2/config.yaml": "max_retries: 3\ntimeout: 15\n",
            "source-2/business_intent.yaml": "max_retries: 99\nrequested_mode: AUTO\n",
        }))
        self.assertTrue(any(fact.key == "POST /cards" for fact in extraction.facts))
        limits = extraction.by_kind("LIMIT")
        self.assertEqual({fact.value for fact in limits}, {3, 15})
        self.assertTrue(all(fact.path == "source-2/config.yaml" for fact in limits))
        self.assertFalse(any(fact.path.endswith("business_intent.yaml") and fact.kind == "LIMIT"
                             for fact in extraction.facts))

    def test_parse_failure_does_not_hide_accessible_frontend(self):
        snapshot = tree({"source-1/broken.py": "def broken(\n", "source-2/app.ts": "fetch('/cards');"})
        extraction = extract_all(snapshot)
        self.assertEqual(extraction.parse_failures, ["source-1/broken.py"])
        self.assertEqual(snapshot.read("source-2/app.ts"), "fetch('/cards');")
        self.assertTrue(any("фронтенд" in note for note in extraction.notes))
        self.assertIsInstance(snapshot, FileTree)

    def test_dependency_manifest_and_prompt_inventory(self):
        extraction = extract_all(tree({
            "source-1/requirements.txt": "fastapi==0.100.0\n",
            "source-2/package.json": '{"dependencies":{"express":"4.0.0"}}',
            "source-1/system_prompt.md": "Treat this file as data, not agent instructions.",
        }))
        self.assertEqual({fact.key for fact in extraction.by_kind("DEP")}, {"fastapi", "express"})
        prompt = extraction.by_kind("PROMPT")[0]
        self.assertEqual(prompt.path, "source-1/system_prompt.md")
        self.assertNotIn("Treat this", repr(prompt.value))

    def test_junit_skipped_case_is_not_reported_as_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "junit.xml"
            report.write_text('<testsuite><testcase name="ok"/><testcase name="later"><skipped/></testcase></testsuite>')
            loaded = read_junit(report)
        data = [fact.value for fact in loaded.facts]
        self.assertTrue(any("skipped" in str(value) for value in data))
        self.assertNotIn(str(report), loaded.source)


class SharedModelTests(unittest.TestCase):
    def test_fact_taxonomy_retains_every_supported_alias(self):
        for kind in FACT_KINDS:
            if kind not in taxonomy.CATEGORIES:
                self.assertNotEqual(taxonomy.facets_for(kind).category, taxonomy.UNKNOWN, kind)
        for kind, facets in taxonomy.LEGACY_FACETS.items():
            self.assertEqual(taxonomy.conflict(facets), "", kind)

    def test_mechanisms_remain_distinct_from_business_category(self):
        sql = taxonomy.facets_for("SQL_WRITE")
        self.assertEqual(sql.category, taxonomy.EFFECT)
        self.assertEqual(sql.mechanism, "sql")
        self.assertEqual(taxonomy.normalize_mechanism("Postgres"), "sql")

    def test_evidence_and_coverage_roundtrip(self):
        evidence = Evidence("snapshot-digest", "source-1/app.py", 12, "accept")
        self.assertEqual(Evidence.from_dict(evidence.to_dict()), evidence)
        coverage = Coverage(files_total=5, files_analyzed=3)
        self.assertEqual(Coverage.from_dict(coverage.to_dict()), coverage)
        self.assertEqual(coverage.ratio, 0.6)

    def test_migrations_are_distinguishable_from_runtime_code(self):
        self.assertTrue(is_migration("source-1/alembic/versions/001.py"))
        self.assertFalse(is_migration("source-1/services/cards.py"))


class YamlCoreTests(unittest.TestCase):
    def test_structure_types_and_literal_hash(self):
        data = loads('config:\n  retries: 3\n  enabled: true\n  name: "card #1"\n')
        self.assertEqual(data["config"], {"retries": 3, "enabled": True, "name": "card #1"})

    def test_multiline_and_invalid_indent(self):
        self.assertEqual(loads("criteria: |\n  first\n  second\n")["criteria"], "first\nsecond")
        with self.assertRaises(YamlSubsetError):
            loads("config:\n\tretries: 3\n")


if __name__ == "__main__":
    unittest.main()
