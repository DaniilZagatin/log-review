"""Declaration-to-business accounting: unanchored callables reach the atomic pages as host-owned primary work.

Tests-first. Synthetic Python and Java with arbitrary names (no repository or domain names); scripted models only.
A callable declaration with a body in an indexed or partially indexed file that no catalog anchor represents exactly
becomes one host unit (origin declaration_inventory) of declaration_coverage.json: it is supplied with its own
declaration text and no fabricated finding, decided on an ordinary atomic page by the business model (realized,
supporting, excluded, outside_process or unresolved) or kept explicitly deferred or failed, under the same request,
input, answer and one-repair limits. Exactly anchored candidate or context declarations get no second unit; every
source file whose declaration inventory is unknown (no index for its language, a partial or failed parse, including
a Python file the host parser rejects) is open scope and blocks a complete status. Export recomputes the inventory
from the saved catalog and index and refuses any change to it, to its aggregates or to the ledger. Status lines say
how many host units were found, sent in page requests, decided, deferred or failed, never that all were sent.
"""

from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from process_observability import declaration_index, direct_atomic
from process_observability.code_review import ReviewOptions
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import ExportError, _atomic_accounting, export_onboarding
from process_observability.redaction import redact_text
from process_observability.source_tools import MAX_CITATION_LINES, RepoSnapshot, SourceFile
from test_action_refinement import HEADER, Code, finding
from test_action_refinement import handlers as refine_handlers
from test_direct_atomic import (AtomicCase, Business, account, anchors, atomic_handlers, atomic_requests,
                                first_attempts)

ORIGIN = "declaration_inventory"
CALLABLE = {"function", "method", "constructor", "initializer"}
STATION = "pkg/relay/station.py"
DOCK = "svc/harbor/Dock.java"
STATION_TEXT = HEADER + (
    "def dispatch_parcel(parcel):\n"                                  # 6   finding: candidate
    "    stamp = _stamp(parcel)\n"                                    # 7
    "    parcel.save()\n"                                             # 8
    "    logger.info(\"relay.dispatched\", parcel_id=parcel.pk)\n"    # 9
    "    return stamp\n"                                              # 10
    "\n\n"
    "def _stamp(parcel):\n"                                           # 13  private helper, no finding
    "    return parcel.code.upper()\n"                                # 14
    "\n\n"
    "def archive_parcel(parcel):\n"                                   # 17  two effects, no finding, no log
    "    parcel.archived = True\n"
    "    parcel.save()\n"
    "    notify_hub(parcel)\n"                                        # 20
    "    return parcel\n"                                             # 21
    "\n\n"
    "def notify_hub(parcel):\n"                                       # 24  no finding, one log
    "    logger.info(\"relay.hub_notified\", parcel_id=parcel.pk)\n"  # 25
    "    return True\n"                                               # 26
    "\n\n"
    "def listen_queue(message):\n"                                    # 29  a queue handoff, no finding
    "    return dispatch_parcel(message.parcel)\n"                    # 30
    "\n\n"
    "class Gate:\n"                                                   # 33
    "    def open_gate(self, parcel):\n"                              # 34  finding of kind error: context
    "        if parcel.blocked:\n"                                    # 35
    "            raise ValueError(\"BLOCKED\")\n"                     # 36
    "        return parcel\n"                                         # 37
    "\n"
    "    def tally(self, parcel):\n"                                  # 39  read-only, no finding
    "        return len(parcel.items)\n")                             # 40
DOCK_TEXT = "\n".join((
    "package svc.harbor;",                                            # 1
    "",
    "public class Dock {",                                            # 3
    '    private final String label = "dock";',
    "",
    "    public void receive(String crate) {",                        # 6
    "        inspect(crate);",
    "        store(crate);",
    "    }",                                                          # 9
    "",
    "    private void inspect(String crate) {",                       # 11  overload 1
    "        if (crate.isEmpty()) {",
    '            throw new IllegalStateException("empty");',
    "        }",
    "    }",                                                          # 15
    "",
    "    private void inspect(int weight) {",                         # 17  overload 2
    "        if (weight < 0) {",
    '            throw new IllegalStateException("negative");',
    "        }",
    "    }",                                                          # 21
    "",
    "    void store(String crate) {",                                 # 23
    "        System.out.println(crate);",
    "    }",                                                          # 25
    "",
    "    public String getLabel() {",                                 # 27  a getter
    "        return label;",
    "    }",                                                          # 29
    "",
    "    public void onMessage(String body) {",                       # 31  a listener-like name
    "        receive(body);",
    "    }",                                                          # 33
    "",
    "    interface Hook {",                                           # 35
    "        void fire();",                                           # 36  abstract: nothing to review
    "    }",
    "}")) + "\n"
FILES = {STATION: STATION_TEXT, DOCK: DOCK_TEXT}
FOUND = [finding("entry_point", "Отправка посылки и запись relay.dispatched.", STATION, 6, 10, "dispatch_parcel"),
         finding("error", "Закрытый шлюз отклоняет заблокированную посылку.", STATION, 35, 36, "Gate.open_gate")]
# (qualname, first line) of every declaration that must become host primary work, and nothing else.
HOST = [("_stamp", 13), ("archive_parcel", 17), ("notify_hub", 24), ("listen_queue", 29), ("Gate.tally", 39),
        ("Dock.receive", 6), ("Dock.inspect", 11), ("Dock.inspect", 17), ("Dock.store", 23), ("Dock.getLabel", 27),
        ("Dock.onMessage", 31)]
REALIZED = {"dispatch_parcel": [("Отправить посылку", "intake", "api_call")],
            "archive_parcel": [("Архивировать посылку", "handling", "api_call"),
                               ("Уведомить узел об архивации", "handling", "api_call")],
            "listen_queue": [("Принять сообщение очереди", "intake", "external_message")],
            "Dock.receive": [("Принять груз", "intake", "api_call")],
            "Dock.store": [("Сохранить груз", "handling", "api_call")],
            "Dock.onMessage": [("Принять сообщение о грузе", "intake", "external_message")]}
SUPPORTING = {"_stamp": "dispatch_parcel", "notify_hub": "archive_parcel", "Dock.getLabel": None}
TWO_EFFECTS = "Две независимые записи: архивация посылки и уведомление узла"
TALLY = "Только подсчёт позиций без изменения состояния"
QUESTION = "Какая проверка груза обязательна перед хранением?"
WEIGHT = "Проверка веса не относится к описанному процессу"


# Working groups of the code-led pages (provisional; composition derives the stages): key -> (name, contract).
GROUPS = {"intake": ("Приём", "Посылка или груз принимаются и отправляются дальше."),
          "handling": ("Обработка", "Посылка или груз хранятся, архивируются и сообщаются узлу.")}


def decide(brief, attempt, *, drop=None, drop_on=(1, 2)):
    """Code-led page decisions read from the page by symbol: each action is terminal and joins the working group of
    its kind of work (proposed once by name, reused by number on later pages); emit sites carry no fact (facts are
    derived by composition). drop leaves one unit undecided on the given attempts."""
    groups = {item["name"]: item["number"] for item in brief["working_groups"]}
    numbers = {unit["symbol"]: unit["number"] for unit in brief["units"]}
    operations, accounting, positions, new_groups = [], [], {}, []

    def operation(name, group, initiation):
        title, contract = GROUPS[group]
        if title not in groups:
            new_groups.append({"name": title, "contract": contract})
            groups[title] = len(brief["working_groups"]) + len(new_groups)
        operations.append({"name": name, "description": None, "actor": None, "kind": "operation",
                           "group": groups[title], "initiation": initiation, "initiation_basis": None,
                           "exit": "terminal", "exit_question": None, "refs": []})
        return len(operations)
    for unit in brief["units"]:
        symbol, number = unit["symbol"], unit["number"]
        if symbol == drop and attempt in drop_on:
            continue
        if symbol in REALIZED:
            positions[symbol] = [operation(*spec) for spec in REALIZED[symbol]]
            reason = TWO_EFFECTS if len(positions[symbol]) > 1 else None
            accounting.append(account(number, "realized", positions[symbol], reason=reason))
        elif symbol in SUPPORTING:
            served = numbers.get(SUPPORTING[symbol])
            accounting.append(account(number, "supporting", serves=[served] if served else [],
                                      reason="Вспомогательный код вызывающей единицы"))
        elif symbol == "Gate.tally":
            accounting.append(account(number, "excluded", exclusion="read_only", reason=TALLY))
        elif symbol == "Dock.inspect":
            accounting.append(account(number, "unresolved", question=QUESTION) if unit["start_line"] == 11
                              else account(number, "outside_process", reason=WEIGHT))
        else:
            accounting.append(account(number, "realized", [operation("Действие " + symbol, "intake", "api_call")]))
    mappings = []
    for site in brief["sites"]:
        target = {"relay.dispatched": ("dispatch_parcel", 0),
                  "relay.hub_notified": ("archive_parcel", 1)}.get(site["event_key"])
        if target and target[0] in positions:
            mappings.append({"site": site["number"], "new_operation": positions[target[0]][target[1]],
                             "known_operation": None, "lifecycle_role": "result",
                             "role_basis": "Запись после выполненного действия", "occurrence_rule": "Одна запись",
                             "ambiguity": None,
                             "refs": [{"path": site["path"], "start_line": site["start_line"],
                                       "end_line": site["end_line"]}]})
    return {"operations": operations, "new_groups": new_groups, "unit_accounting": accounting,
            "event_mappings": mappings, "operation_transitions": [], "unknowns": []}


def handlers(**overrides):
    """Code-led handlers: decide() on the pages, the shared composition of test_direct_atomic after them."""
    return atomic_handlers(**{"map_atomic": decide, **overrides})


HOST_LINE = "Объявления без находок технического разбора:"
PANEL = "export function showPanel() {\n  return 1;\n}\n"


def sent_units(bundle):
    """Units of atomic page requests that this run actually sent to the model, failed attempts included."""
    return {uid for item in atomic_requests(bundle)
            if any(attempt["attempt"] >= 1 and attempt.get("api_request_sha256") for attempt in item["attempts"])
            for uid in item["unit_ids"]}


def host_line(bundle):
    [line] = [text for text in bundle["process_map"]["unknowns"] if text.startswith(HOST_LINE)]
    return line


def canonical_sha(document, key):
    data = {name: value for name, value in document.items() if name != key}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


class CompletionCase(AtomicCase):
    def completed(self, business=None, files=None, **settings):
        self.business = business or Business(**handlers())
        bundle, self.out = self.run_case(self.business, files=dict(files or FILES), found=copy.deepcopy(FOUND),
                                         **settings)
        return bundle

    def saved(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def host_by_line(self):
        """(qualname, first line) -> host unit of the saved coverage document."""
        return {(unit["declaration"]["qualname"], unit["declaration"]["start_line"]): unit
                for unit in self.saved("declaration_coverage.json")["primary_units"]}

    def exported(self, bundle, name):
        from process_observability.cli import _write_bundle
        bundle["diagnostics"] = {}
        _write_bundle(bundle, self.out)
        result = export_onboarding(self.out, self.out.parent / name)
        return result, json.loads((Path(result["export"]) / "onboarding.json").read_text(encoding="utf-8"))


class HostPrimaryWorkTests(CompletionCase):
    def test_unanchored_python_and_java_callables_become_accounted_host_primary_work(self):
        bundle = self.completed()
        coverage, index, units = (self.saved(name) for name in ("declaration_coverage.json", "declaration_index.json",
                                                                 "command_units.json"))
        catalog = self.saved("action_catalog.json")
        self.assertEqual((coverage["schema"], coverage["snapshot_id"], coverage["index_sha256"],
                          coverage["catalog_sha256"], coverage["units_sha256"]),
                         ("declaration-coverage/1", index["snapshot_id"], index["index_sha256"],
                          catalog["catalog_sha256"], units["units_sha256"]))
        hosts = self.host_by_line()
        self.assertEqual(sorted(hosts), sorted(HOST))
        for unit in hosts.values():
            self.assertEqual((unit["origin"], unit["status"], unit["role"], unit["candidate_ids"], unit["finding_ids"]),
                             (ORIGIN, "verified", "declaration", [], []))
        # Every callable with a body is either exactly anchored or host work, never both.
        exact = {unit["declaration_id"] for unit in units["units"] if unit["status"] == "verified"}
        hosted = {unit["declaration_id"] for unit in hosts.values()}
        self.assertFalse(exact & hosted)
        for item in index["declarations"]:
            if item["kind"] in CALLABLE and item.get("body", "block") != "none":
                self.assertIn(item["id"], exact | hosted, item["qualname"])
        self.assertNotIn(("Dock.Hook.fire", 36), hosts)  # an abstract method has no code to review
        record = bundle["atomic_map"]
        self.assertEqual((record["status"], record["status_reasons"]), ("complete", []))
        self.assertEqual({state["state"] for state in record["unit_states"].values()}, {"accounted"})
        host_states = {key: state for key, state in record["unit_states"].items() if state.get("origin") == ORIGIN}
        self.assertEqual(set(host_states), {unit["id"] for unit in hosts.values()})
        for key, state in host_states.items():
            self.assertEqual((state["candidate_ids"], state["finding_ids"]), ([], []))
        # No fabricated candidate: the candidate partition is exactly the catalog's.
        self.assertEqual(set(record["candidates"]), {item["id"] for item in catalog["candidates"]})
        self.assertEqual(record["summary"]["declaration_units"], len(HOST))
        self.assertEqual(record["declaration_coverage"]["coverage_sha256"], coverage["coverage_sha256"])

        def state(qualname, line):
            return record["unit_states"][hosts[(qualname, line)]["id"]]
        self.assertEqual((state("Gate.tally", 39)["disposition"], state("Gate.tally", 39)["exclusion"],
                          state("Gate.tally", 39)["reason"]), ("excluded", "read_only", TALLY))
        self.assertEqual((state("Dock.inspect", 11)["disposition"], state("Dock.inspect", 11)["question"]),
                         ("unresolved", QUESTION))
        self.assertEqual((state("Dock.inspect", 17)["disposition"], state("Dock.inspect", 17)["reason"]),
                         ("outside_process", WEIGHT))
        self.assertNotEqual(hosts[("Dock.inspect", 11)]["id"], hosts[("Dock.inspect", 17)]["id"])
        self.assertEqual(state("Dock.getLabel", 27)["disposition"], "supporting")
        for qualname, line in (("listen_queue", 29), ("Dock.onMessage", 31), ("Dock.receive", 6), ("Dock.store", 23)):
            self.assertEqual(state(qualname, line)["disposition"], "realized", qualname)
        # One declaration with two independent effects keeps both actions.
        operations = {op["name"]: op for op in bundle["process_map"]["operations"]}
        archive = state("archive_parcel", 17)
        self.assertEqual((archive["disposition"], archive["reason"], len(archive["operation_ids"])),
                         ("realized", TWO_EFFECTS, 2))
        for name in ("Архивировать посылку", "Уведомить узел об архивации"):
            self.assertIn(operations[name]["id"], archive["operation_ids"])
        self.assertEqual(sum(op["name"] == "Отправить посылку" for op in bundle["process_map"]["operations"]), 1)

    def test_host_rows_carry_their_own_source_no_finding_and_their_log_sites(self):
        run_bundle = self.completed()
        hosts = self.host_by_line()
        pages = first_attempts(self.business)
        rows = {(row["symbol"], row["start_line"]): (call, row) for call in pages for row in call["brief"]["units"]}
        for key, unit in hosts.items():
            call, row = rows[key]
            self.assertEqual((row["origin"], row["findings"], row["declaration_supplied"], row["status"]),
                             (ORIGIN, [], "whole", "verified"), key)
            first, last = unit["declaration"]["start_line"], unit["declaration"]["end_line"]
            excerpts = {item["number"]: item for item in call["brief"]["excerpts"]}
            self.assertTrue(any(excerpts[number]["path"] == unit["path"] and excerpts[number]["start_line"] <= first
                                and last <= excerpts[number]["end_line"] for number in row["excerpts"]), key)
            # The lines the host links on its own are labelled as a structural anchor that shows no effect.
            self.assertEqual(row["declaration_anchor"],
                             {"start_line": first, "end_line": min(last, first + MAX_CITATION_LINES - 1),
                              "use": "structural_navigation_only"}, key)
        self.assertRegex(direct_atomic.ATOMIC_SYSTEM, r"(?i)exact supplied lines of each separate effect")
        record = self.saved("atomic_map.json")
        [store] = [op for op in record["operations"] if op["name"] == "Сохранить груз"]
        self.assertEqual(anchors(run_bundle, store["unit_evidence_ids"]), {(DOCK, 23, 25)})  # only that anchor
        _, dispatch = rows[("dispatch_parcel", 6)]
        self.assertNotIn("origin", dispatch)  # a finding candidate row keeps its exact earlier shape
        # The unanchored private helper is planned on its caller's page as supporting code.
        stamp_call, stamp = rows[("_stamp", 13)]
        self.assertIs(stamp_call, rows[("dispatch_parcel", 6)][0])
        self.assertEqual(stamp["role"], "supporting")
        self.assertIn(stamp["number"], dispatch["called_units"])
        # A host unit without any finding still brings its emit site, which supporting code lends to the action.
        notify_call, notify = rows[("notify_hub", 24)]
        [site] = [item for item in notify_call["brief"]["sites"] if item["event_key"] == "relay.hub_notified"]
        self.assertIn(notify["number"], site["units"])
        bundle = json.loads(json.dumps(self.saved("atomic_map.json")))
        [mapping] = [item for item in bundle["event_mappings"] if item["event_key"] == "relay.hub_notified"]
        self.assertEqual(mapping["role_support"], "emit_within_operation_sources")
        # Units without any log are reviewed like all others.
        for key in (("archive_parcel", 17), ("Gate.tally", 39), ("Dock.store", 23)):
            self.assertEqual(rows[key][1]["sites"], [], key)

    def test_names_never_decide_relevance_and_triage_never_judges_host_units_without_source(self):
        def triage(brief, attempt):  # the code-led triage shape has no stage hint: no stage exists before code
            return {"units": [{"unit": unit["number"], "decision": "atomic_review", "reason": None,
                               "question": None} for unit in brief["units"]], "unknowns": []}
        bundle = self.completed(Business(**handlers(map_triage=triage)), triage="on")
        triaged = [unit["symbol"] for call in self.business.calls if call["task"] == "TASK: map-triage"
                   for unit in call["brief"]["units"]]
        self.assertEqual(triaged, ["dispatch_parcel"])
        hosts = self.host_by_line()
        for key in (("listen_queue", 29), ("Dock.onMessage", 31), ("Dock.getLabel", 27), ("Gate.tally", 39)):
            self.assertEqual(bundle["atomic_map"]["unit_states"][hosts[key]["id"]]["state"], "accounted", key)
        guidance = direct_atomic.ATOMIC_SYSTEM
        self.assertIn(ORIGIN, guidance)
        self.assertRegex(guidance, r"(?i)never glue")
        self.assertRegex(guidance, r"(?i)getter")
        self.assertRegex(guidance, r"(?i)queue")


class HostLimitTests(CompletionCase):
    def test_the_request_limit_defers_host_units_explicitly_and_keeps_the_map_partial(self):
        bundle = self.completed(refine_max_requests=1, atomic_page_units=3)
        self.assertEqual(len(first_attempts(self.business)), 1)
        record = bundle["atomic_map"]
        deferred = [state for state in record["unit_states"].values() if state["state"] == "deferred"]
        # Intentional expectation change (context-callable completion): the error-anchored Gate.open_gate is primary
        # work now and is deferred with the host units; the host counts below are unchanged.
        self.assertEqual(sorted(state["symbol"] for state in deferred),
                         sorted(["Gate.open_gate", "Gate.tally", "Dock.receive", "Dock.inspect", "Dock.inspect",
                                 "Dock.store", "Dock.getLabel", "Dock.onMessage"]))
        self.assertEqual({(state.get("origin"), state["reason"]) for state in deferred},
                         {(ORIGIN, "request_limit_reached"), ("context_anchor", "request_limit_reached")})
        self.assertEqual(record["summary"]["deferred_by_reason"], {"request_limit_reached": 8})
        self.assertEqual(record["status"], "partial")
        # Found, sent, decided and deferred are counted apart: 4 of 11 host units were in the one sent page.
        line = host_line(bundle)
        for part in ("найдено 11;", "в попытках модельных запросов страниц (включая неудачные) — 4;",
                     "из сохранённых ответов — 0;", "решение получено — 4;", "отложено — 7;",
                     "без решения из-за ошибки запроса — 0."):
            self.assertIn(part, line)
        self.assertEqual(len(sent_units(bundle) & {key for key, state in record["unit_states"].items()
                                                   if state.get("origin") == ORIGIN}), 4)
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")
        self.assertIn("Atomic map partial: request_limit_reached: 8", document["partial_reasons"])
        page = (Path(result["export"]) / "process.md").read_text(encoding="utf-8")
        self.assertIn("Объявлений без находок технического разбора найдено: 11; состояния: accounted — 4, "
                      "deferred — 7", page)
        for text in (page, *bundle["process_map"]["unknowns"]):
            self.assertNotRegex(text, r"переданы на атомарные страницы|переданных на страницы")

    def test_input_budget_deferrals_are_never_counted_as_sent(self):
        body = "".join("    item.part_%03d = '%s'\n" % (step, "Ш" * 800) for step in range(150))
        bulky = "def bulky_transfer(item):\n" + body + "    return item\n"
        bundle = self.completed(files={**FILES, "pkg/relay/bulky.py": bulky}, default_bounds=True,
                                options=ReviewOptions(request_chars=400000))
        self.assertGreater(len(bulky.encode("utf-8")), 80000)  # precondition: above the default hard input bound
        record, hosts = bundle["atomic_map"], self.host_by_line()
        bulky_id = hosts[("bulky_transfer", 1)]["id"]
        self.assertEqual((record["unit_states"][bulky_id]["state"], record["unit_states"][bulky_id]["reason"]),
                         ("deferred", "unit_exceeds_request_budget"))
        host_ids = {unit["id"] for unit in hosts.values()}
        sent = sent_units(bundle) & host_ids
        self.assertNotIn(bulky_id, sent)
        for call in self.business.calls:
            self.assertNotIn("Ш" * 150, call["user"])  # the oversized declaration is never cut into a request
        counts = Counter(record["unit_states"][key]["state"] for key in host_ids)
        line = host_line(bundle)
        self.assertIn(f"найдено {len(host_ids)};", line)
        self.assertIn(f"в попытках модельных запросов страниц (включая неудачные) — {len(sent)};", line)
        self.assertIn(f"решение получено — {counts['accounted']};", line)
        self.assertIn(f"отложено — {counts['deferred']};", line)
        self.assertLess(len(sent), len(host_ids))
        self.assertEqual(record["status"], "partial")

    def test_one_repair_then_explicit_failure_covers_host_units_too(self):
        # Intentional expectation change (context-callable completion): Gate.open_gate is primary work now, so
        # Dock.onMessage is planned on a second page; the checks follow the page that holds it.
        def message_page(call):
            return call["task"] == "TASK: map-atomic" and any(unit["symbol"] == "Dock.onMessage"
                                                               for unit in call["brief"]["units"])
        repaired = self.completed(Business(**handlers(
            map_atomic=lambda brief, attempt: decide(brief, attempt, drop="Dock.onMessage", drop_on=(1,)))))
        calls = [call for call in self.business.calls if message_page(call)]
        self.assertEqual(len(calls), 2)
        [error] = calls[1]["brief"]["validation_feedback"]["errors"]
        self.assertEqual(error["code"], "missing_units")
        self.assertEqual({item["outcome"] for item in atomic_requests(repaired)}, {"accepted", "accepted_after_repair"})
        failed = self.completed(Business(**handlers(
            map_atomic=lambda brief, attempt: decide(brief, attempt, drop="Dock.onMessage"))))
        self.assertEqual(len([call for call in self.business.calls if message_page(call)]), 2)
        record = failed["atomic_map"]
        self.assertEqual(record["status"], "partial")
        [rejected] = [item for item in atomic_requests(failed) if item["outcome"] == "rejected_after_repair"]
        hosts = [state for state in record["unit_states"].values() if state.get("origin") == ORIGIN]
        self.assertEqual(len(hosts), len(HOST))
        self.assertEqual({(state["state"], state["reason"]) for state in hosts if state["request_id"] == rejected["id"]},
                         {("request_failed", "request_rejected_after_repair")})
        self.assertIn(("request_failed", "Dock.onMessage"), {(state["state"], state["symbol"]) for state in hosts})
        _atomic_accounting(failed, ProcessMap.from_dict(failed["process_map"]), record["catalog"]["snapshot_id"])


class OpenScopeTests(CompletionCase):
    def test_unknown_declaration_inventory_is_open_scope_and_blocks_a_complete_map(self):
        files = {**FILES, "web/panel.ts": PANEL,
                 "svc/harbor/Escape.java": "class Escape {\n    // \\u000a void hidden() {}\n}\n",
                 "pkg/relay/old_syntax.py": "def broken(:\n    pass\n"}
        bundle = self.completed(files=files)
        coverage = self.saved("declaration_coverage.json")
        rows = {row["path"]: row for row in coverage["open_scope"]["files"]}
        self.assertEqual({path: (row["language"], row["status"], row["blocking"]) for path, row in rows.items()},
                         {"web/panel.ts": ("typescript", "unsupported_language", True),
                          "svc/harbor/Escape.java": ("java", "parse_failed", True),
                          "pkg/relay/old_syntax.py": ("python", "parse_failed", True)})
        record = bundle["atomic_map"]
        self.assertEqual(record["declaration_coverage"]["open_scope"], {"files": 3, "blocking": 3})
        self.assertEqual({state["state"] for state in record["unit_states"].values()}, {"accounted"})
        self.assertEqual(record["status"], "partial")
        self.assertIn("declaration_inventory_unknown", record["status_reasons"])
        self.assertTrue(any("declaration_coverage.json" in line for line in bundle["process_map"]["unknowns"]))
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")
        self.assertIn("Atomic map partial: declaration_inventory_unknown", document["partial_reasons"])
        forged = copy.deepcopy(bundle)
        forged["atomic_map"].update(status="complete", status_reasons=[])
        with self.assertRaises(ExportError):
            _atomic_accounting(forged, ProcessMap.from_dict(forged["process_map"]),
                               record["catalog"]["snapshot_id"])

    def test_a_file_the_host_python_parser_rejects_keeps_an_unknown_blocking_inventory(self):
        # Valid in another Python dialect or runtime: the host parser says nothing about the target's runtime.
        dialect = "def forward(item):\n    print item\n    publish(item)\n"
        bundle = self.completed(files={**FILES, "pkg/relay/forward.py": dialect})
        coverage = self.saved("declaration_coverage.json")
        [row] = coverage["open_scope"]["files"]
        self.assertEqual((row["path"], row["language"], row["status"], row["blocking"]),
                         ("pkg/relay/forward.py", "python", "parse_failed", True))
        record = bundle["atomic_map"]
        self.assertEqual(record["declaration_coverage"]["open_scope"], {"files": 1, "blocking": 1})
        self.assertEqual({state["state"] for state in record["unit_states"].values()}, {"accounted"})
        self.assertEqual((record["status"], record["status_reasons"]), ("partial", ["declaration_inventory_unknown"]))
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")
        self.assertIn("Atomic map partial: declaration_inventory_unknown", document["partial_reasons"])
        # No artifact claims anything about whether the target can run the file.
        texts = [json.dumps(coverage, ensure_ascii=False), json.dumps(record["settings"], ensure_ascii=False),
                 *bundle["process_map"]["unknowns"], (Path(result["export"]) / "process.md").read_text(encoding="utf-8")]
        for text in texts:
            self.assertNotRegex(text, r"(?i)cannot run|run as written|в таком виде (он|они) не выполня|"
                                      r"не разбираются интерпретатором|non_blocking")


class IdentityAndLegacyTests(CompletionCase):
    def test_business_identity_is_new_while_the_technical_review_is_reused(self):
        with patch.object(direct_atomic, "VALIDATOR_VERSION", "direct-atomic-validator/4"):
            earlier = self.completed()
        earlier_out, code = self.out, Code(copy.deepcopy(FOUND))
        current = self.completed(code=code, resume_from=earlier_out)
        self.assertEqual(code.calls, [])  # technical review and reduction reused under the same technical requests
        self.assertIn("TASK: map-atomic", self.business.tasks())  # a page of the earlier identity is no hit
        self.assertNotEqual(earlier["manifest"]["provenance"]["config_sha256"],
                            current["manifest"]["provenance"]["config_sha256"])
        settings = current["atomic_map"]["settings"]
        self.assertEqual((settings["validator"], settings["declaration_completion"]["unit_origin"]),
                         (direct_atomic.VALIDATOR_VERSION, ORIGIN))
        again = self.completed(resume_from=self.out)
        self.assertNotIn("TASK: map-atomic", self.business.tasks())  # the exact identity reuses host pages too
        self.assertEqual({item["outcome"] for item in atomic_requests(again)}, {"accepted_from_checkpoint"})
        self.assertEqual(again["atomic_map"]["declaration_coverage"], current["atomic_map"]["declaration_coverage"])

    def test_runs_without_unanchored_callables_send_the_earlier_unit_rows(self):
        self.business = Business(**atomic_handlers())
        bundle, self.out = self.run_case(self.business)
        coverage = self.saved("declaration_coverage.json")
        self.assertEqual((coverage["primary_units"], coverage["open_scope"]["files"]), ([], []))
        # Intentional expectation change (context-callable completion): the two error-anchored helpers of this
        # fixture are context units now and say so; every other row keeps its earlier shape.
        contexts = {view["qualname"] for view in coverage["context_units"]}
        self.assertEqual(contexts, {"clean_order_cancel", "OrderConfirm.get_instance"})
        for call in first_attempts(self.business):
            for row in call["brief"]["units"]:
                self.assertEqual(row.get("origin"), "context_anchor" if row["symbol"] in contexts else None)
        record = bundle["atomic_map"]
        self.assertEqual((record["summary"]["declaration_units"], record["declaration_coverage"]["primary_units"]),
                         (0, 0))
        self.assertEqual({state["symbol"]: state["origin"] for state in record["unit_states"].values()
                          if "origin" in state}, dict.fromkeys(contexts, "context_anchor"))
        # A record saved before declaration accounting existed is still accepted as it was. Such a run never had
        # context units, so it is reproduced under the original inventory before the later keys are removed.
        original = declaration_index.declaration_coverage
        with patch.object(declaration_index, "declaration_coverage",
                          lambda catalog, index: original(catalog, index, None)):
            earlier, _ = self.run_case(Business(**atomic_handlers()))
        legacy = copy.deepcopy(earlier)
        old = legacy["atomic_map"]
        old.pop("declaration_coverage")
        old["settings"].pop("declaration_completion")
        old["settings"]["validator"] = "direct-atomic-validator/4"
        for key in ("declaration_units", "declaration_units_by_state"):
            old["summary"].pop(key)
        _atomic_accounting(legacy, ProcessMap.from_dict(legacy["process_map"]), old["catalog"]["snapshot_id"])

    def test_the_explicit_coarse_mode_builds_no_declaration_inventory(self):
        self.business = Business(**refine_handlers())
        bundle, out = self.run_case(self.business, map_mode="coarse_refine", action_refinement=True)
        self.assertNotIn("atomic_map", bundle)
        self.assertFalse((out / "declaration_coverage.json").exists())


class ExportTamperTests(CompletionCase):
    def test_export_recomputes_the_inventory_and_refuses_ledger_or_document_changes(self):
        bundle = self.completed()
        result, document = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "all_stages_completed" if not document["partial_reasons"] else "partial")
        self.assertEqual(document["atomic_accounting"]["declaration_coverage"],
                         bundle["atomic_map"]["declaration_coverage"])
        record = bundle["atomic_map"]
        process, snapshot = ProcessMap.from_dict(bundle["process_map"]), record["catalog"]["snapshot_id"]
        hosts = self.host_by_line()
        tally = hosts[("Gate.tally", 39)]["id"]

        def host(data):
            return data["unit_states"][tally]

        def drop_state(data):
            data["unit_states"].pop(tally)
            data["summary"]["units"] -= 1
        edits = {"host unit with a candidate": lambda data: host(data).update(candidate_ids=["act-0000000000000000"]),
                 "unknown origin": lambda data: host(data).update(origin="model_guess"),
                 "candidate unit claims an origin": lambda data: next(
                     state for state in data["unit_states"].values() if "origin" not in state).update(origin=ORIGIN),
                 "count of host units": lambda data: data["declaration_coverage"].update(primary_units=99),
                 "summary count": lambda data: data["summary"].update(declaration_units=0),
                 "binding dropped": lambda data: data.pop("declaration_coverage"),
                 "descriptor dropped": lambda data: data["settings"].pop("declaration_completion"),
                 "foreign units": lambda data: data["declaration_coverage"].update(units_sha256="0" * 64),
                 "foreign snapshot": lambda data: data["declaration_coverage"].update(snapshot_id="f" * 64),
                 "host state omitted": drop_state}
        for label, edit in edits.items():
            with self.subTest(edit=label):
                changed = copy.deepcopy(bundle)
                edit(changed["atomic_map"])
                with self.assertRaises(ExportError):
                    _atomic_accounting(changed, process, snapshot)
        # Saved documents: a resealed inventory or a renamed host unit fails against the recomputed one.
        path = self.out / "declaration_coverage.json"
        original = path.read_bytes()

        def reseal(data):
            data["coverage_sha256"] = canonical_sha(data, "coverage_sha256")

        def omit(data):
            data["primary_units"].pop(0)
            data["summary"]["primary_units"] -= 1

        def moved(data):
            data["primary_units"][0]["declaration"]["end_line"] += 1

        def foreign(data):
            data["snapshot_id"] = "e" * 64
        for index, change in enumerate((omit, moved, foreign)):
            with self.subTest(document=change.__name__):
                data = json.loads(original)
                change(data)
                reseal(data)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ExportError):
                    export_onboarding(self.out, self.out.parent / f"refused-{index}")
        path.write_bytes(original)
        saved = {name: (self.out / name).read_bytes() for name in ("bundle.json", "atomic_map.json")}

        def rename(data):
            data["unit_states"]["hdecl-" + "0" * 24] = data["unit_states"].pop(tally)
        bundle_data = json.loads(saved["bundle.json"])
        rename(bundle_data["atomic_map"])
        (self.out / "bundle.json").write_text(json.dumps(bundle_data, ensure_ascii=False), encoding="utf-8")
        (self.out / "atomic_map.json").write_text(json.dumps(bundle_data["atomic_map"], ensure_ascii=False),
                                                  encoding="utf-8")
        with self.assertRaises(ExportError):
            export_onboarding(self.out, self.out.parent / "refused-rename")
        for name, data in saved.items():
            (self.out / name).write_bytes(data)
        export_onboarding(self.out, self.out.parent / "packet-again")

    def test_a_consistent_two_copy_forgery_of_the_open_scope_is_refused(self):
        bundle = self.completed(files={**FILES, "web/panel.ts": PANEL})
        self.assertEqual(bundle["atomic_map"]["declaration_coverage"]["open_scope"], {"files": 1, "blocking": 1})
        result, _ = self.exported(bundle, "packet")
        self.assertEqual(result["status"], "partial")
        names = ("bundle.json", "atomic_map.json", "declaration_coverage.json", "declaration_index.json",
                 "action_catalog.json", "command_units.json")
        saved = {name: (self.out / name).read_bytes() for name in names}

        def forge(scope):
            """The same false binding and a complete status in bundle.json and atomic_map.json; sources untouched."""
            data = json.loads(saved["bundle.json"])
            data["atomic_map"]["declaration_coverage"]["open_scope"] = scope
            data["atomic_map"].update(status="complete", status_reasons=[])
            return data
        # The reported forgery claims a non-blocking file; the second hides the file with self-consistent counts,
        # which the record alone cannot tell from a real run without unknown scope.
        forgeries = {"non-blocking claim": {"files": 1, "blocking": 0, "non_blocking": 1},
                     "consistent zero counts": {"files": 0, "blocking": 0}}
        consistent = forge(forgeries["consistent zero counts"])
        _atomic_accounting(consistent, ProcessMap.from_dict(consistent["process_map"]),
                           consistent["atomic_map"]["catalog"]["snapshot_id"])
        for index, (label, scope) in enumerate(forgeries.items()):
            with self.subTest(forgery=label):
                data = forge(scope)
                for name, value in (("bundle.json", data), ("atomic_map.json", data["atomic_map"])):
                    (self.out / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ExportError):
                    export_onboarding(self.out, self.out.parent / f"forged-{index}")
                self.assertFalse((self.out.parent / f"forged-{index}").exists())
                for name in names[2:]:
                    self.assertEqual((self.out / name).read_bytes(), saved[name])
        for name, data in saved.items():
            (self.out / name).write_bytes(data)
        self.assertEqual(export_onboarding(self.out, self.out.parent / "packet-again")["status"], "partial")


def snapshot(files):
    return RepoSnapshot({path: SourceFile(path, hashlib.sha256(text.encode("utf-8")).hexdigest(), redact_text(text),
                                          text) for path, text in files.items()}, {})


class CoverageFunctionTests(unittest.TestCase):
    FLOW = ("def entry(item):\n"            # 1  exact candidate
            "    _helper(item)\n"
            "    return item\n"             # 3
            "\n\n"
            "def _helper(item):\n"          # 6  unanchored, called by entry
            "    return item.value\n"       # 7
            "\n\n"
            "def outer(rows):\n"            # 10 unanchored
            "    def inner(row):\n"         # 11 nested, unanchored
            "        return row\n"          # 12
            "    return [inner(r) for r in rows]\n"   # 13
            "\n\n"
            "class Board:\n"                # 16
            "    def pin(self, note):\n"    # 17 exact context
            "        return note\n"         # 18
            "\n"
            "    def unpin(self, note):\n"  # 20 only overlapped by an anchor across declarations
            "        return None\n")        # 21

    def build(self):
        from process_observability import declaration_index as module
        built = snapshot({"lib/flow.py": self.FLOW})
        index = module.build_declaration_index(built)
        sha = built.files["lib/flow.py"].sha256

        def anchor(identifier, first, last, symbol, role):
            return {"id": identifier, "role": role, "path": "lib/flow.py", "start_line": first, "end_line": last,
                    "symbol": symbol, "file_sha256": sha, "finding_ids": [identifier + "-f"],
                    "findings": [{"id": identifier + "-f"}]}
        catalog = {"snapshot_id": built.snapshot_id, "catalog_sha256": "a" * 64,
                   "candidates": [anchor("c-entry", 1, 3, "entry", "candidate"),
                                  anchor("c-cross", 18, 21, "Board", "candidate")],
                   "context_anchors": [anchor("x-pin", 17, 18, "Board.pin", "context")]}
        return module, index, catalog

    def test_only_exact_anchors_represent_and_every_other_callable_body_is_host_work(self):
        module, index, catalog = self.build()
        coverage = module.declaration_coverage(catalog, index)
        hosts = {unit["declaration"]["qualname"]: unit for unit in coverage["primary_units"]}
        self.assertEqual(sorted(hosts), ["Board.unpin", "_helper", "outer", "outer.inner"])
        self.assertEqual(hosts["Board.unpin"]["coverage"], "overlap_only")
        self.assertEqual(hosts["_helper"]["coverage"], "none")
        units = module.build_command_units(catalog, index)
        [entry_unit] = [unit for unit in units["units"] if "c-entry" in unit["candidate_ids"]]
        self.assertEqual(hosts["_helper"]["navigation"]["caller_unit_ids"], [entry_unit["id"]])
        self.assertEqual(hosts["outer.inner"]["owner"]["qualname"], "outer")
        self.assertEqual(len({unit["id"] for unit in hosts.values()}), 4)
        self.assertEqual(module.declaration_coverage(catalog, index), coverage)
        self.assertEqual(coverage["summary"]["primary_units"], 4)
        module.check_coverage(coverage, catalog=catalog, index=index)
        tampered = copy.deepcopy(coverage)
        tampered["primary_units"].pop()
        tampered["coverage_sha256"] = canonical_sha(tampered, "coverage_sha256")
        with self.assertRaises(module.DeclarationError) as caught:
            module.check_coverage(tampered, catalog=catalog, index=index)
        self.assertEqual(caught.exception.code, "coverage_mismatch")


if __name__ == "__main__":
    unittest.main()
