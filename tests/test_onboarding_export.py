"""Onboarding export of saved runs: strict inputs, no model or repository access, safe CSV/Markdown.

Runs are written by the real ``demo`` command; variants replace only the replayed model replies.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from process_observability import onboarding_export
from process_observability.cli import main
from process_observability.onboarding_export import ExportError, decode_cell, encode_cell, export_onboarding

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/synthetic"
_RECORDS = list(json.loads((EXAMPLE / "model-responses.json").read_text(encoding="utf-8"))["records"].values())
DEMO = {task: next(record for record in _RECORDS if key in record)
        for task, key in (("TASK: code", "findings"), ("TASK: map", "stages"),
                          ("TASK: instrumentation", "cards"), ("TASK: metrics", "metrics"))}
ARTIFACTS = ("process_map", "log_coverage", "instrumentation_plan", "metric_catalog")
COPIES = {"manifest.json": "manifest", **{f"{key}.json": key for key in ARTIFACTS}}
LISTS = {"operation_ids", "existing_fields", "proposed_fields", "fact_ids", "source_refs", "evidence_ids", "fields", "dependencies",
         "metric_ids", "required_fact_ids", "required_facts", "required_event_keys", "related_change_ids"}
RECEIVE = {"path": "app.py", "start_line": 4, "end_line": 6, "symbol": "receive_request"}
HOSTILE = ('=HYPERLINK("http://evil.example/x","открыть") <script>alert(1)</script> '
           '[ссылка](javascript:alert(1)) www.evil.example')
HOSTILE_KEY = "=cmd|' /C calc'!A0"
QUOTED = 'Группировать по "occurrence_id", затем\nпо attempt_id; повтор доставки — отдельно'
BIDI = "+приём‮текст\x07"


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(map(str, args)))
    return code, out.getvalue(), err.getvalue()


def demo_run(directory, name="run", replies=None):
    """A run saved by the demo command; replies by task line (e.g. "TASK: map") replace the strict replay."""
    output = Path(directory) / name
    if not replies:
        return output, invoke("demo", "--out", output)[0]
    answers = {**DEMO, **replies}

    def complete_json(self, system, user):
        reply = answers[system.split("\n", 1)[0]]
        if isinstance(reply, Exception):
            raise reply
        return copy.deepcopy(reply)

    with patch("process_observability.demo.RecordedModel.complete_json", complete_json):
        return output, invoke("demo", "--out", output)[0]


def extended_map():
    """Demo map plus a required stage/operation without code and hostile untrusted text."""
    payload = copy.deepcopy(DEMO["TASK: map"])
    payload["process"]["purpose"] = HOSTILE
    payload["stages"].append({"id": "archive", "name": "Архивирование заявки", "roles": ["архивариус"],
                              "systems": ["СЭД"], "refs": []})
    payload["operations"].append({"id": "archive-op", "name": "@SUM(1+1) Передать в архив", "stage_id": "archive",
                                  "kind": "operation", "description": "\tОбязательная операция; код не найден",
                                  "actor": None, "event_keys": [], "refs": []})
    payload["event_mappings"].append({"event_key": HOSTILE_KEY, "business_action": BIDI, "operation": "-5",
                                      "stage_id": "receive", "lifecycle_role": "ui", "occurrence_rule": QUOTED,
                                      "refs": [RECEIVE]})
    payload["facts"].append({"id": "archive.result", "stage_id": "archive", "meaning": "Заявка передана в архив",
                             "rationale": "Обязательный исход", "scope": "Все заявки", "condition": None,
                             "guidance_ids": ["FACT-QUESTION"], "metric_ids": ["archive-share"],
                             "evidence_alternatives": [], "event_keys": [], "refs": []})
    return payload


def extended_plan():
    payload = copy.deepcopy(DEMO["TASK: instrumentation"])
    payload["cards"] += [
        {"id": "join-archive", "guidance_ids": ["INST-REUSE"], "fact_id": "archive.result", "action": "join",
         "rationale": "'Архив хранит идентификатор заявки", "semantics": "Соединить запись архива с заявкой",
         "fields": {"archive_id": "Ключ соединения"}, "transaction_timing": None, "dependencies": [],
         "metric_ids": ["archive-share"], "validation_plan": "Сверить долю соединённых записей", "ref": None},
        {"id": "enrich-receive", "guidance_ids": ["INST-REUSE"], "fact_id": "receive.result", "action": "enrich",
         "rationale": "Канал приёма известен из запроса", "semantics": "Добавить канал при обогащении",
         "fields": {"channel": "Канал приёма"}, "transaction_timing": None, "dependencies": [],
         "metric_ids": [], "validation_plan": "Проверить заполненность канала", "ref": RECEIVE},
        {"id": "add-archive-event", "guidance_ids": ["INST-MINIMUM"], "fact_id": "archive.result",
         "action": "add_event", "rationale": "Записи о передаче в архив нет",
         "semantics": "Новое событие request.archived после commit",
         "fields": {"case_id": "Идентификатор заявки", "archived_at": "-5"}, "transaction_timing": "После commit",
         "dependencies": ["join-archive"], "metric_ids": ["archive-share"],
         "validation_plan": "=1+1 сверить с архивом", "ref": None},
    ]
    return payload


def extended_metrics():
    payload = copy.deepcopy(DEMO["TASK: metrics"])
    payload["metrics"].append({
        "id": "archive-share", "guidance_ids": ["METRIC-DEFINITION"], "purpose": "Доля заявок, переданных в архив",
        "formula_text": "=COUNT(DISTINCT case_id из archive.result) / COUNT(DISTINCT case_id из receive.result)",
        "counting_unit": "заявка", "cohort": None, "window": None, "time_semantics": None,
        "open_case_policy": None, "late_data_policy": None, "dependencies": ["archive.result", "receive.result"]})
    return payload


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def decoded(row):
    return {key: decode_cell(value, multiple=key in LISTS) for key, value in row.items()}


def files(directory):
    return {path.relative_to(directory).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(Path(directory).rglob("*")) if path.is_file()}


def rewrite(run, change):
    """Apply a change to bundle.json and keep its separate copies consistent, as the run writer does."""
    bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
    change(bundle)
    (run / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, key in COPIES.items():
        (run / name).write_text(json.dumps(bundle[key], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run / "evidence.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n"
                                                for item in bundle["evidence"]), encoding="utf-8")


def no_model_or_repository():
    stack = ExitStack()
    for target, label in (("process_observability.llm.urllib_transport", "model call"),
                          ("process_observability.llm.OpenAICompatibleModel", "model client"),
                          ("process_observability.demo.RecordedModel", "replay model"),
                          ("process_observability.source_tools.snapshot_repository", "repository read")):
        stack.enter_context(patch(target, side_effect=AssertionError(f"no {label} expected")))
    return stack


class OnboardingExportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def finished(self):
        run, code = demo_run(self.root)
        self.assertEqual(code, 0)
        return run

    def extended(self):
        run, code = demo_run(self.root, "extended", {"TASK: map": extended_map(),
                                                     "TASK: instrumentation": extended_plan(),
                                                     "TASK: metrics": extended_metrics()})
        self.assertEqual(code, 0)
        return run

    def test_finished_run_keeps_artifacts_ids_and_snapshot_without_model_repository_or_run_changes(self):
        run = self.finished()
        before = files(run)
        with no_model_or_repository():
            result = export_onboarding(run, self.root / "packet")
        self.assertEqual(files(run), before)
        out = Path(result["export"])
        self.assertEqual(result["status"], "all_stages_completed")
        self.assertEqual({path.name for path in out.iterdir()}, {"process.md", "onboarding.json", "events.csv",
                                                                 "logging-changes.csv", "metrics.csv", "SHA256SUMS"})
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        document = json.loads((out / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual((document["status"], document["partial_reasons"]), ("all_stages_completed", []))
        for key in ("manifest", *ARTIFACTS):
            self.assertEqual(document[key], bundle[key], key)
        self.assertEqual(document["evidence"], [{**item, "safe_quote": None} for item in bundle["evidence"]])
        self.assertNotIn("emit(", (out / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(document["source_run"]["files"]["bundle.json"],
                         hashlib.sha256((run / "bundle.json").read_bytes()).hexdigest())
        repo = {item["path"][5:]: item["sha256"] for item in bundle["manifest"]["input_fingerprints"]
                if item["path"].startswith("repo/")}
        self.assertEqual(document["source_snapshot"]["files"], repo)
        self.assertEqual({item["snapshot_id"] for item in bundle["evidence"]},
                         {document["source_snapshot"]["snapshot_id"]})
        listed = dict(reversed(line.split("  ", 1)) for line in (out / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(listed), {path.name for path in out.iterdir()} - {"SHA256SUMS"})
        for name, digest in listed.items():
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest, name)
        events = [decoded(row) for row in rows(out / "events.csv")]
        mapped = [row for row in events if row["row_kind"] == "mapped_key"]
        proposed = [row for row in events if row["row_kind"] == "proposed_new_event"]
        mappings = bundle["process_map"]["event_mappings"]
        self.assertEqual([row["event_key"] for row in mapped], [item["event_key"] for item in mappings])
        self.assertEqual([row["plan_card_id"] for row in proposed],
                         [card["id"] for card in bundle["instrumentation_plan"]["cards"] if card["action"] == "add_event"])
        for row, mapping in zip(mapped, mappings):
            self.assertEqual((row["row_kind"], row["lifecycle_role"], row["occurrence_rule"], row["stage_id"],
                              row["evidence_ids"], row["static_emit_in_cited_range"]),
                             ("mapped_key", mapping["lifecycle_role"], mapping["occurrence_rule"],
                              mapping["stage_id"], mapping["evidence_ids"], "found"))
        self.assertEqual(events[0]["source_refs"], ["app.py:4-6 receive_request"])
        self.assertEqual(events[0]["operation_ids"], ["intake"])
        self.assertEqual(set(events[0]["existing_fields"]), {"case_id", "occurrence_id", "attempt_id"})
        self.assertEqual(document["event_dictionary"][0]["existing_fields"], events[0]["existing_fields"])
        changes = [decoded(row) for row in rows(out / "logging-changes.csv")]
        self.assertEqual([row["card_id"] for row in changes], [card["id"] for card in bundle["instrumentation_plan"]["cards"]])
        [metric] = [decoded(row) for row in rows(out / "metrics.csv")]
        source = bundle["metric_catalog"]["metrics"][0]
        self.assertEqual((metric["formula_text"], metric["required_fact_ids"], metric["window"]),
                         (source["formula_text"], source["dependencies"], None))
        self.assertEqual(rows(out / "metrics.csv")[0].keys(), set(onboarding_export.METRIC_COLUMNS))
        self.assertFalse({"value", "readiness", "blockers"} & set(onboarding_export.METRIC_COLUMNS))
        page = (out / "process.md").read_text(encoding="utf-8")
        for stage in bundle["process_map"]["stages"]:
            self.assertIn(f"({stage['id']})", page)
        for operation in bundle["process_map"]["operations"]:
            self.assertIn(f"({operation['id']}, ", page)
        [revise] = [line for line in page.splitlines() if "(revise, " in line]
        self.assertIn("код: не указан", revise)
        self.assertIn("Метрика «Длительность от приёма заявки до исполнения или отмены» \\(lead-time\\): уточнить период",
                      page)

    def test_required_stage_and_operation_without_code_stay_visible(self):
        run = self.extended()
        out = Path(export_onboarding(run, self.root / "packet")["export"])
        document = json.loads((out / "onboarding.json").read_text(encoding="utf-8"))
        stages = {stage["id"]: stage for stage in document["process_map"]["stages"]}
        self.assertEqual((stages["archive"]["required"]["state"], stages["archive"]["implemented"]["state"],
                          stages["archive"]["implemented"]["evidence_ids"]), ("proposed", "unknown", []))
        [operation] = [op for op in document["process_map"]["operations"] if op["id"] == "archive-op"]
        self.assertEqual(operation["evidence_ids"], [])
        page = (out / "process.md").read_text(encoding="utf-8")
        section = page.split("### 5. Архивирование заявки (archive)", 1)[1].split("###", 1)[0]
        self.assertIn("Требуется: предложено; ссылок на код нет — реализация не установлена", section)
        self.assertIn("Роли: архивариус; системы: СЭД", section)
        [line] = [line for line in section.splitlines() if "(archive-op, операция)" in line]
        self.assertIn("код: не указан", line)
        self.assertIn("@SUM\\(1\\+1\\) Передать в архив", line)
        [proposed] = [decoded(row) for row in rows(out / "events.csv") if row["plan_card_id"] == "add-archive-event"]
        self.assertEqual((proposed["stage_id"], proposed["stage_name"]), ("archive", "Архивирование заявки"))
        [metric] = [decoded(row) for row in rows(out / "metrics.csv") if row["metric_id"] == "archive-share"]
        self.assertEqual(metric["required_fact_ids"], ["archive.result", "receive.result"])
        self.assertEqual(metric["related_change_ids"], ["join-archive", "add-archive-event"])

    def test_found_and_proposed_events_stay_distinct_and_other_actions_create_no_event(self):
        run = self.extended()
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        out = Path(export_onboarding(run, self.root / "packet")["export"])
        events = [decoded(row) for row in rows(out / "events.csv")]
        mapped = [row for row in events if row["row_kind"] == "mapped_key"]
        proposed = [row for row in events if row["row_kind"] == "proposed_new_event"]
        self.assertEqual(len(mapped) + len(proposed), len(events))
        self.assertEqual([row["event_key"] for row in mapped],
                         [item["event_key"] for item in bundle["process_map"]["event_mappings"]])
        self.assertEqual({row["event_key"]: row["static_emit_in_cited_range"] for row in mapped}[HOSTILE_KEY],
                         "not_established")
        hostile = next(row for row in mapped if row["event_key"] == HOSTILE_KEY)
        self.assertEqual((hostile["event_key_status"], hostile["existing_fields"]), ("from_process_map", []))
        cards = {card["id"]: card for card in bundle["instrumentation_plan"]["cards"]}
        self.assertEqual([row["plan_card_id"] for row in proposed],
                         [key for key, card in cards.items() if card["action"] == "add_event"])
        for row in proposed:
            self.assertEqual((row["event_key"], row["event_key_status"], row["lifecycle_role"], row["occurrence_rule"]),
                             (None, "not_specified_in_plan", None, None))
            self.assertEqual(row["proposed_fields"], list(cards[row["plan_card_id"]]["fields"]))
            self.assertEqual(row["fact_ids"], [cards[row["plan_card_id"]]["fact_id"]])
        # A key mentioned only in proposal prose is not parsed into the event dictionary.
        self.assertNotIn("request.archived", [row["event_key"] for row in events])
        self.assertIn("request.archived", next(row["semantics"] for row in proposed if row["plan_card_id"] == "add-archive-event"))
        self.assertEqual(proposed[1]["location"], "location_unresolved")
        changes = {row["card_id"]: decoded(row) for row in rows(out / "logging-changes.csv")}
        self.assertEqual({key: row["action"] for key, row in changes.items()},
                         {key: card["action"] for key, card in cards.items()})
        for key in ("join-archive", "enrich-receive", "map-fulfil-result"):
            self.assertNotIn(key, [row["plan_card_id"] for row in events])
        self.assertEqual(changes["enrich-receive"]["source_ref"], "app.py:4-6 receive_request")
        self.assertEqual(changes["add-archive-event"]["fields"], ["case_id: Идентификатор заявки", "archived_at: -5"])
        self.assertEqual(changes["join-archive"]["location"], "location_unresolved")

    def test_csv_cells_round_trip_and_never_start_a_spreadsheet_formula(self):
        for value in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\t=1", " =1", "'quoted", "＝1", "﻿=1", "\n=1",
                      "-5", "-0.25", "2024-01-01", "обычный, \"текст\"\nв две строки", BIDI, "'json:[1]", "json:x",
                      None, ["a", "=b"], ["-5"], ["x\ny", "z"], [], ["'json:"], ["a", "b"]):
            with self.subTest(value=value):
                cell = encode_cell(value)
                self.assertEqual(decode_cell(cell, multiple=isinstance(value, list)), value)
                if cell and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", cell):
                    self.assertNotIn(cell[0], "=+-@\t\r\n ＝﻿")
                self.assertFalse(any(char in cell for char in "\x07‮"))
        self.assertEqual((encode_cell("-5"), encode_cell("-2+3"), encode_cell(BIDI)[:6]), ("-5", "'-2+3", "'json:"))
        run = self.extended()
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        out = Path(export_onboarding(run, self.root / "packet")["export"])
        for name in ("events.csv", "logging-changes.csv", "metrics.csv"):
            self.assertTrue((out / name).read_bytes().startswith(b"\xef\xbb\xbf"), name)
            for row in rows(out / name):
                for column, cell in row.items():
                    if cell and not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", cell):
                        self.assertNotIn(cell[0], "=+-@\t\r\n ", (name, column))
        [hostile] = [row for row in rows(out / "events.csv") if decode_cell(row["event_key"]) == HOSTILE_KEY]
        mapping = bundle["process_map"]["event_mappings"][-1]
        self.assertEqual((hostile["event_key"], hostile["mapping_operation"]), ("'" + HOSTILE_KEY, "-5"))
        self.assertEqual((decode_cell(hostile["business_action"]), decode_cell(hostile["occurrence_rule"])),
                         (mapping["business_action"], mapping["occurrence_rule"]))
        [metric] = [row for row in rows(out / "metrics.csv") if row["metric_id"] == "archive-share"]
        self.assertTrue(metric["formula_text"].startswith("'="))
        self.assertEqual(decode_cell(metric["formula_text"]), bundle["metric_catalog"]["metrics"][-1]["formula_text"])
        changes = {row["card_id"]: row for row in rows(out / "logging-changes.csv")}
        self.assertEqual(decode_cell(changes["join-archive"]["rationale"]), "'Архив хранит идентификатор заявки")
        self.assertEqual(decode_cell(changes["add-archive-event"]["validation_plan"]), "=1+1 сверить с архивом")

    def test_markdown_escapes_untrusted_text_links_and_html(self):
        out = Path(export_onboarding(self.extended(), self.root / "packet")["export"])
        page = (out / "process.md").read_text(encoding="utf-8")
        for unsafe in ("<script", "](javascript:", "http://evil", "www.evil", "‮", "\x07"):
            self.assertNotIn(unsafe, page)
        for escaped in ("&lt;script&gt;", "\\[ссылка\\]\\(javascript:alert\\(1\\)\\)", "http\\://evil.example",
                        "www\\.evil.example"):
            self.assertIn(escaped, page)
        self.assertIn("[events.csv](events.csv)", page)
        self.assertEqual(len(re.findall(r"\]\(", page)), 5)  # only the packet's own file links

    def test_partial_runs_keep_stage_status_and_omit_views_of_unfinished_stages(self):
        run, code = demo_run(self.root, "no-plan", {"TASK: instrumentation": ValueError("provider down")})
        self.assertEqual(code, 1)
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        result = export_onboarding(run, self.root / "packet")
        out = Path(result["export"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual({path.name for path in out.iterdir()}, {"process.md", "onboarding.json", "events.csv", "SHA256SUMS"})
        document = json.loads((out / "onboarding.json").read_text(encoding="utf-8"))
        blockers = [*bundle["manifest"]["stages"]["instrumentation"]["blockers"],
                    *bundle["manifest"]["stages"]["metrics"]["blockers"]]
        self.assertEqual(document["partial_reasons"], blockers)
        self.assertEqual((document["instrumentation_plan"]["status"], document["instrumentation_plan"]["blockers"]),
                         ("incomplete", blockers[:1]))
        self.assertEqual([item["id"] for item in document["metric_catalog"]["metrics"]], ["lead-time"])
        self.assertIsNone(document["metric_catalog"]["metrics"][0]["value"])
        page = (out / "process.md").read_text(encoding="utf-8")
        self.assertIn("пакет: частичный", page)
        self.assertIn("| 3. Доработки логирования | не завершена | instrumentation stage incomplete", page)
        self.assertIn("logging-changes.csv — не сформирован", page)
        self.assertIn("предложенные новые события не установлены", page)
        self.assertIn("(cancel)", page)
        self.assertEqual({row["row_kind"] for row in rows(out / "events.csv")}, {"mapped_key"})

        failed, code = demo_run(self.root, "no-map", {"TASK: map": ValueError("provider down")})
        self.assertEqual(code, 1)
        out = Path(export_onboarding(failed, self.root / "packet-no-map")["export"])
        self.assertEqual({path.name for path in out.iterdir()}, {"process.md", "onboarding.json", "SHA256SUMS"})
        document = json.loads((out / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual([stage["id"] for stage in document["process_map"]["stages"]], ["unestablished"])
        self.assertEqual((document["status"], document["open_questions"], document["evidence"]), ("partial", [], []))
        page = (out / "process.md").read_text(encoding="utf-8")
        self.assertIn("Карта процесса не получена: map stage incomplete", page)
        self.assertNotIn("(unestablished)", page)

    def test_inconsistent_or_dangling_runs_are_refused_before_any_packet(self):
        def metric_value(bundle):
            evidence = bundle["evidence"][0]["id"]
            bundle["metric_catalog"]["metrics"][0].update(
                value=12.5, formula_id="lead-time-v1", computed_from=[evidence], window="2026-10",
                readiness={"primary": "ready", "blockers": [], "decision_id": None})

        def foreign_snapshot(bundle):
            for item in bundle["manifest"]["input_fingerprints"]:
                if item["path"] == "repo/app.py":
                    item["sha256"] = "0" * 64
            for key in ARTIFACTS:
                bundle[key]["input_fingerprints"]["repo/app.py"] = "0" * 64

        def stage_disagrees(bundle):
            bundle["manifest"]["stages"]["metrics"] = {"status": "incomplete", "blockers": ["metrics stage incomplete"]}

        def card_cycle(bundle):
            first, second = bundle["instrumentation_plan"]["cards"][:2]
            first["dependencies"], second["dependencies"] = [second["id"]], [first["id"]]

        cases = {
            "dangling card fact": lambda b: b["instrumentation_plan"]["cards"][0].update(fact_id="missing.fact"),
            "dangling card dependency": lambda b: b["instrumentation_plan"]["cards"][0]["dependencies"].append("missing"),
            "cyclic card dependencies": card_cycle,
            "dangling metric dependency": lambda b: b["metric_catalog"]["metrics"][0]["dependencies"].append("missing"),
            "dangling evidence": lambda b: b["process_map"]["stages"][0]["implemented"].update(evidence_ids=["src-missing"]),
            "fact on unknown stage": lambda b: b["log_coverage"]["facts"][0].update(stage_id="nowhere"),
            "unmapped fact key": lambda b: b["log_coverage"]["facts"][0]["event_keys"].append("never.mapped"),
            "undefined referenced metric": lambda b: b["log_coverage"]["facts"][0]["metric_ids"].append("ghost"),
            "unbacked card location": lambda b: b["instrumentation_plan"]["cards"][0].update(evidence_ids=[]),
            "fabricated metric value": metric_value,
            "foreign source snapshot": foreign_snapshot,
            "other run": lambda b: b["process_map"].update(run_id="other-run"),
            "stage status disagrees": stage_disagrees,
            "unknown field": lambda b: b["process_map"].update(approved=True),
            "no partial flag": lambda b: b["scope"].pop("partial_input"),
            "hidden partial source": lambda b: b["scope"]["source"].update(partial_input=True),
            "hidden incomplete review": lambda b: b.update(code_review={"complete": False}),
        }
        original = self.finished()
        for label, change in cases.items():
            with self.subTest(label):
                run = self.root / f"case-{len(list(self.root.iterdir()))}"
                shutil.copytree(original, run)
                rewrite(run, change)
                before = files(run)
                with self.assertRaises(ExportError):
                    export_onboarding(run, self.root / "packet")
                self.assertFalse(os.path.lexists(self.root / "packet"))
                self.assertEqual(files(run), before)

        def raw(name, edit):
            run = self.root / f"raw-{len(list(self.root.iterdir()))}"
            shutil.copytree(original, run)
            path = run / name
            if edit is None:
                path.unlink()
            else:
                path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
            return run

        broken = {
            "copy differs from bundle": raw("process_map.json", lambda text: text.replace("Обработка", "Изменённая", 1)),
            "evidence line dropped": raw("evidence.jsonl", lambda text: "".join(text.splitlines(True)[1:])),
            "duplicate JSON key": raw("bundle.json", lambda text: text.replace("{", '{"insights": [], ', 1)),
            "nonfinite number": raw("bundle.json", lambda text: text.replace('"insights": []', '"insights": [NaN]', 1)),
            "missing evidence file": raw("evidence.jsonl", None),
            "no bundle": raw("bundle.json", None),
        }
        for label, run in broken.items():
            with self.subTest(label):
                with self.assertRaises(ExportError) as caught:
                    export_onboarding(run, self.root / "packet")
                self.assertFalse(os.path.lexists(self.root / "packet"))
        self.assertIn("plan-only", str(caught.exception))

    def test_symlinks_aliases_and_existing_destinations_are_refused(self):
        run = self.finished()
        before = files(run)
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        for label, destination in {"inside run": run / "packet", "existing": outside,
                                   "missing parent": self.root / "absent" / "packet", "dot name": run / ".."}.items():
            with self.subTest(label):
                with self.assertRaises(ExportError):
                    export_onboarding(run, destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(files(run), before)
        try:
            os.symlink(run, self.root / "run-link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        linked_copy = self.root / "linked-bundle"
        shutil.copytree(run, linked_copy)
        (linked_copy / "bundle.json").unlink()
        os.symlink(run / "bundle.json", linked_copy / "bundle.json")
        cases = {"run symlink": (self.root / "run-link", self.root / "packet"),
                 "bundle symlink": (linked_copy, self.root / "packet"),
                 "destination through a link into the run": (run, self.root / "run-link" / "packet"),
                 "dangling destination link": (run, self.root / "dangling")}
        os.symlink(self.root / "nowhere", self.root / "dangling")
        for label, (source, destination) in cases.items():
            with self.subTest(label):
                with self.assertRaises(ExportError):
                    export_onboarding(source, destination)
                self.assertFalse(os.path.lexists(self.root / "packet"))
        self.assertEqual(files(run), before)

    def test_write_failure_removes_only_files_created_by_the_export(self):
        run = self.finished()
        real = onboarding_export._create
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                target = self.root / f"packet-{foreign}"
                calls = []

                def failing(path):
                    calls.append(path)
                    if len(calls) == 3:
                        if foreign:
                            (path.parent / "user-note.txt").write_text("keep", encoding="utf-8")
                        raise OSError("disk full")
                    return real(path)

                with patch("process_observability.onboarding_export._create", failing):
                    with self.assertRaises(OSError):
                        export_onboarding(run, target)
                if foreign:
                    self.assertEqual([path.name for path in target.iterdir()], ["user-note.txt"])
                else:
                    self.assertFalse(os.path.lexists(target))

    def test_export_is_deterministic(self):
        run = self.extended()
        left = Path(export_onboarding(run, self.root / "left")["export"])
        right = Path(export_onboarding(run, self.root / "right")["export"])
        self.assertEqual({path.name: path.read_bytes() for path in left.iterdir()},
                         {path.name: path.read_bytes() for path in right.iterdir()})

    def test_coverage_lifecycle_is_carried_and_a_disagreeing_record_is_refused(self):
        run = self.finished()
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        record = bundle["coverage_lifecycle"]
        self.assertEqual(record["schema"], "coverage-lifecycle/1")
        self.assertEqual({item["fact_id"] for item in record["facts"]},
                         {fact["id"] for fact in bundle["log_coverage"]["facts"]})
        document = json.loads((Path(export_onboarding(run, self.root / "packet")["export"]) / "onboarding.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(document["coverage_lifecycle"], record)
        rewrite(run, lambda data: data.pop("coverage_lifecycle"))  # a run saved before this record existed
        older = json.loads((Path(export_onboarding(run, self.root / "older")["export"]) / "onboarding.json")
                           .read_text(encoding="utf-8"))
        self.assertIsNone(older["coverage_lifecycle"])
        cases = {"schema": lambda data: data.update(schema="coverage-lifecycle/0"),
                 "check disagrees": lambda data: data["facts"][0].update(lifecycle_check="compatible",
                                                                        instrumentation="found")
                 if data["facts"][0]["instrumentation"] != "found" else data["facts"][0].update(instrumentation="unknown"),
                 "missing fact": lambda data: data["facts"].pop(),
                 "extra part": lambda data: data.update(note="x")}
        for index, (label, change) in enumerate(cases.items()):
            with self.subTest(label=label):
                def apply(data, change=change):
                    data["coverage_lifecycle"] = json.loads(json.dumps(record))
                    change(data["coverage_lifecycle"])
                rewrite(run, apply)
                with self.assertRaises(ExportError):
                    export_onboarding(run, self.root / f"refused-lifecycle{index}")

    def test_action_accounting_is_carried_and_altered_or_foreign_records_are_refused(self):
        run = self.finished()
        bundle = json.loads((run / "bundle.json").read_text(encoding="utf-8"))
        operation, snapshot = bundle["process_map"]["operations"][0], bundle["evidence"][0]["snapshot_id"]
        self.assertNotIn("action_refinement", bundle)  # a legacy run: no accounting, exported as None
        legacy = json.loads((Path(export_onboarding(run, self.root / "legacy")["export"]) / "onboarding.json")
                            .read_text(encoding="utf-8"))
        self.assertIsNone(legacy["action_accounting"])

        def head(state):
            return {"path": "app.py", "symbol": "receive_request", "start_line": 4, "end_line": 6, "area": ".",
                    "scope": {"state": "assigned", "stage_ids": [operation["stage_id"]]},
                    "stages": [{"stage_id": operation["stage_id"], **state}]}
        record = {"schema": "action-refinement/1", "status": "partial", "status_reasons": [],
                  "catalog": {"file": "action_catalog.json", "schema": "action-catalog/1", "catalog_sha256": "c" * 64,
                              "snapshot_id": snapshot, "review_run_fingerprint": "rf", "review_complete": True},
                  "settings": {}, "requests": [], "areas": [], "stages": [], "operations": [], "event_mappings": [],
                  "transitions": [], "scenario_inheritance": [], "basis": "host accounting",
                  "candidates": {"act-1": head({"state": "accounted", "disposition": "realized",
                                                "operation_ids": [operation["id"]]}),
                                 "act-2": head({"state": "deferred", "reason": "request_limit_reached"})},
                  "summary": {"candidates": 2, "stage_decisions": 1, "operations_added": 0,
                              "dispositions": {"realized": 1}, "deferred_by_reason": {"request_limit_reached": 1}}}

        def install(change=None):
            def apply(data):
                data["action_refinement"] = copy.deepcopy(record)
                data["action_catalog"] = {"catalog_sha256": "c" * 64}
                if change is not None:
                    change(data)
            rewrite(run, apply)
        install()
        result = export_onboarding(run, self.root / "packet")
        document = json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))
        self.assertEqual(document["action_accounting"], record)
        self.assertEqual(result["status"], "partial")
        self.assertIn("Action refinement partial: request_limit_reached: 1", document["partial_reasons"])
        self.assertIn("## Учёт кандидатов действий", (Path(result["export"]) / "process.md").read_text(encoding="utf-8"))
        cases = {
            "operation absent from the map": lambda data: data["action_refinement"]["candidates"]["act-1"]["stages"][0]
            .update(operation_ids=["invented"]),
            "foreign snapshot": lambda data: data["action_refinement"]["catalog"].update(snapshot_id="other"),
            "older or unknown schema": lambda data: data["action_refinement"].update(schema="action-refinement/0"),
            "missing part": lambda data: data["action_refinement"].pop("summary"),
            "catalog identity": lambda data: data["action_catalog"].update(catalog_sha256="d" * 64),
            "mapping absent from the map": lambda data: data["action_refinement"]["event_mappings"].append(
                {"operation_id": operation["id"], "event_key": "never.mapped"}),
            "unknown status": lambda data: data["action_refinement"].update(status="done"),
            "status shape": lambda data: data["action_refinement"].update(status=[]),
            "complete with deferred work": lambda data: data["action_refinement"].update(status="complete"),
            "settings shape": lambda data: data["action_refinement"].update(settings=[]),
            "scope shape": lambda data: data["action_refinement"]["candidates"]["act-1"].update(scope=[]),
            "stages shape": lambda data: data["action_refinement"]["candidates"]["act-1"].update(stages={}),
            "operation list shape": lambda data: data["action_refinement"]["candidates"]["act-1"]["stages"][0]
            .update(operation_ids=[["not-an-id"]]),
            "foreign stage operation": lambda data: data["action_refinement"]["candidates"]["act-1"]["stages"][0]
            .update(stage_id="check"),
            "reason count shape": lambda data: data["action_refinement"]["summary"].update(deferred_by_reason=[]),
        }
        for index, (label, change) in enumerate(cases.items()):
            with self.subTest(label=label):
                install(change)
                with self.assertRaises(ExportError):
                    export_onboarding(run, self.root / f"refused{index}")
                self.assertFalse((self.root / f"refused{index}").exists())


if __name__ == "__main__":
    unittest.main()
