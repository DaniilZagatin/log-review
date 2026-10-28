"""Business stages: regions, grouping contract, publication and the projected view."""

import copy
import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.errors import PkoError
from pko.observability.contracts import map_revision
from pko.observability.model_stage import ReportBudget, StageModel
from pko.observability.pipeline import ReportOptions
from pko.render.scenario_map import _STAGE_PROJECTION_JS, render_scenario_map
from pko.scenario_map.runner import HIERARCHY_FILE, rebuild_from_map, run_scenario
from pko.stages import grouping
from pko.stages.contracts import HierarchyError, validate_hierarchy
from pko.stages.regions import collapse_check, immediate_dominators, operation_graph
from observability_script import ScriptedReportClient
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources

NAMES = ["Приём платежа", "Поиск договора по реквизитам платежа", "Проверка, найден ли договор",
         "Зачисление платежа на договор", "Уведомление клиента о зачислении", "Передача платежа специалисту"]
STAGES = {"stages": [
    {"id": "stage-1", "name": "Определение договора по платежу", "goal": "Установить, к какому договору относится платёж.",
     "result": "Договор найден либо не найден.", "operation_ids": ["step-1", "step-2", "step-3"]},
    {"id": "stage-2", "name": "Зачисление платежа", "goal": "Зачислить платёж на найденный договор.",
     "result": "Платёж зачислен, клиент уведомлён.", "operation_ids": ["step-4", "step-5"]},
    {"id": "stage-3", "name": "Передача случая специалисту", "goal": "Передать платёж без договора на ручной разбор.",
     "result": "Задача передана специалисту; её выполнение не показано.", "operation_ids": ["step-6"]}],
    "unassigned": []}


def payment_map():
    nodes = [{"id": f"step-{index}", "name": name, "kind": "operation", "output": "Результат операции"}
             for index, name in enumerate(NAMES, 1)]
    nodes[4]["outcome"], nodes[5]["outcome"] = "success", "deferred"
    pairs = [("step-1", "step-2", ""), ("step-2", "step-3", ""), ("step-3", "step-4", "Договор найден"),
             ("step-3", "step-6", "Договор не найден"), ("step-4", "step-5", "")]
    edges = [{"id": f"transition-{index}", "source": source, "target": target, "kind": "sequence",
              "condition": condition, "evidence_status": "observed"} for index, (source, target, condition) in enumerate(pairs, 1)]
    return {"schema_version": "2.0", "nodes": nodes, "edges": edges, "run": {"id": "run-1", "status": "completed"},
            "scenario": {"title": "Зачисление платежа", "description": "Платёж клиента по кредитному договору.",
                         "entry_operation_ids": ["step-1"], "terminal_operation_ids": ["step-5", "step-6"]}}


def graph_of(pairs, entries=()):
    nodes = sorted({node for pair in pairs for node in pair} | set(entries))
    return operation_graph({"nodes": [{"id": node} for node in nodes],
                            "edges": [{"source": a, "target": b} for a, b in pairs],
                            "scenario": {"entry_operation_ids": list(entries)}})


def stage_model(client):
    return StageModel(client, ReportBudget(max_requests=4, deadline=time.monotonic() + 60), {},
                      methodology_dir=grouping.METHODOLOGY)


class RegionTests(unittest.TestCase):
    def test_single_entry_region_collapses(self):
        graph = graph_of([("a", "b"), ("b", "c"), ("b", "d"), ("c", "e"), ("d", "e"), ("e", "f")], ["a"])
        self.assertEqual(collapse_check({"b", "c", "d", "e"}, graph), (True, "b", ""))
        # The scenario entry is an entry of its stage even without incoming transitions.
        self.assertEqual(collapse_check({"a", "b"}, graph)[:2], (True, "a"))

    def test_two_entries_or_disconnected_members_stay_operations(self):
        # x and y are reached from different places: one block would draw a -> stage -> d.
        graph = graph_of([("a", "x"), ("x", "c"), ("b", "y"), ("y", "d"), ("s", "a"), ("s", "b")], ["s"])
        collapsible, entry, reason = collapse_check({"x", "y"}, graph)
        self.assertFalse(collapsible)
        self.assertIsNone(entry)
        self.assertIn("несколько входов", reason)
        # One entry, but a member reachable only around the stage cannot be inside the block.
        graph = graph_of([("s", "b"), ("b", "c"), ("c", "d"), ("q", "q2")], ["s"])
        collapsible, entry, reason = collapse_check({"b", "q2"}, graph)
        self.assertFalse(collapsible)
        self.assertIn("несколько входов", reason)
        graph = graph_of([("s", "b"), ("b", "c")], ["s"])
        graph.nodes.append("lonely")
        graph.outgoing["lonely"], graph.incoming["lonely"] = [], []
        collapsible, entry, reason = collapse_check({"b", "lonely"}, graph)
        self.assertEqual((collapsible, entry), (False, "b"))
        self.assertIn("недостижимы", reason)
        graph = graph_of([("s", "b"), ("x", "y"), ("y", "x")], ["s"])
        self.assertIn("нет входа", collapse_check({"x", "y"}, graph)[2])

    def test_dominators_follow_every_path(self):
        graph = graph_of([("s", "a"), ("s", "b"), ("a", "c"), ("b", "c"), ("c", "d"), ("d", "c"), ("d", "e")], ["s"])
        graph.nodes.append("orphan")
        graph.outgoing["orphan"], graph.incoming["orphan"] = [], []
        dominators = immediate_dominators(graph)
        self.assertEqual(dominators, {"a": "s", "b": "s", "c": "s", "d": "c", "e": "d", "s": None, "orphan": None})


class HierarchyTests(unittest.TestCase):
    def ask(self, *answers, model=None):
        client = ScriptedReportClient({"submit_business_stages": [lambda payload, messages, answer=answer: answer
                                                                  for answer in answers]})
        model = model or payment_map()
        before = copy.deepcopy(model)
        hierarchy = grouping.build_hierarchy(model, stage_model(client), run_id="run-2")
        self.assertEqual(model, before, "grouping never changes the atomic map")
        return hierarchy, client

    def test_complete_partition_with_collapse_flags(self):
        hierarchy, client = self.ask(STAGES)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(hierarchy["status"], {"result": "complete", "reasons": []})
        self.assertEqual(hierarchy["map_revision"], map_revision(payment_map()))
        self.assertEqual((hierarchy["run_id"], hierarchy["map_run_id"]), ("run-2", "run-1"))
        self.assertEqual(hierarchy["membership"]["step-6"], "stage-3")
        self.assertEqual([stage["entry_operation_id"] for stage in hierarchy["stages"]], ["step-1", "step-4", "step-6"])
        self.assertTrue(all(stage["collapsible"] for stage in hierarchy["stages"]))
        self.assertEqual((hierarchy["provenance"], hierarchy["authority_status"]), ("model_proposal", "proposed"))
        payload = client.requests[0][1]
        self.assertEqual({row["id"]: row["dominator"] for row in payload["operations"]}["step-4"], "step-3")
        self.assertNotIn("evidence", json.dumps(payload, ensure_ascii=False))

    def test_overlap_and_unknown_operations_get_one_addressed_repair(self):
        broken = copy.deepcopy(STAGES)
        broken["stages"][1]["operation_ids"] = ["step-3", "step-4", "step-5", "step-99"]
        hierarchy, client = self.ask(broken, STAGES)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertEqual(hierarchy["membership"]["step-3"], "stage-1")

    def test_unplaced_operations_stay_explicitly_unassigned(self):
        partial = copy.deepcopy(STAGES)
        partial["stages"].pop()
        hierarchy, client = self.ask(partial, partial)
        self.assertEqual(len(client.requests), 2, "exactly one repair turn")
        self.assertEqual(hierarchy["status"]["result"], "partial")
        self.assertEqual(hierarchy["unassigned"], [{"operation_id": "step-6", "reason": "Модель не отнесла операцию к этапу"}])
        explained = copy.deepcopy(partial)
        explained["unassigned"] = [{"operation_id": "step-6", "reason": "Ручной разбор вне сценария"}]
        hierarchy, _ = self.ask(explained)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertEqual(hierarchy["unassigned"][0]["reason"], "Ручной разбор вне сценария")

    def test_technical_stage_names_are_rejected(self):
        technical = copy.deepcopy(STAGES)
        technical["stages"][0]["name"] = "match_contract()"
        hierarchy, client = self.ask(technical, technical)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(hierarchy["status"]["result"], "partial")
        self.assertNotIn("stage-1", {stage["id"] for stage in hierarchy["stages"]})
        self.assertTrue(any("stage-1" in reason for reason in hierarchy["status"]["reasons"]))

    def test_compact_packet_is_used_when_the_full_one_does_not_fit(self):
        model = payment_map()
        model["scenario"]["description"] = "Описание процесса. " * 400
        full = len(json.dumps(grouping.build_payload(model), ensure_ascii=False))
        compact = len(json.dumps(grouping.build_payload(model, compact=True), ensure_ascii=False))
        self.assertLess(compact, full)
        client = ScriptedReportClient({"submit_business_stages": [lambda payload, messages: STAGES]})
        model_stage = stage_model(client)
        with patch.object(StageModel, "_limits", lambda self, schema: ((compact + full) // 2, 4000)):
            hierarchy = grouping.build_hierarchy(model, model_stage)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertNotIn("process_description", client.requests[0][1])

    def test_contract_rejects_foreign_or_incomplete_hierarchies(self):
        hierarchy, _ = self.ask(STAGES)
        model = payment_map()
        validate_hierarchy(hierarchy, model)
        cases = {
            "another map": lambda h, m: m["edges"].pop(),
            "accepted authority": lambda h, m: h.update(authority_status="accepted"),
            "missing operation": lambda h, m: h["membership"].pop("step-6"),
            "two places": lambda h, m: h["unassigned"].append({"operation_id": "step-1", "reason": "x"}),
            "empty stage": lambda h, m: h["stages"].append({"id": "stage-9", "name": "Пустой", "collapsible": False}),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                candidate, current = copy.deepcopy(hierarchy), payment_map()
                change(candidate, current)
                with self.assertRaises(HierarchyError):
                    validate_hierarchy(candidate, current)

    def test_research_groups_of_older_maps_are_vocabulary_only(self):
        model = payment_map()
        model["groups"] = [{"id": "group-1", "name": "Приём"}]
        model["nodes"][0]["group_id"] = "group-1"
        payload = grouping.build_payload(model)
        self.assertEqual(payload["research_groups"], [{"name": "Приём", "operation_ids": ["step-1"]}])
        html = render_scenario_map(model, hierarchy=self.ask(STAGES, model=model)[0])
        self.assertIn('id="map-hierarchy"', html)


class StagePublicationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.environment = patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_run_publishes_stages_next_to_an_unchanged_map(self):
        output = self.root / "run"
        model = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient())
        hierarchy = json.loads((output / HIERARCHY_FILE).read_text(encoding="utf-8"))
        saved = json.loads((output / "scenario_map.json").read_text(encoding="utf-8"))
        validate_hierarchy(hierarchy, saved)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertEqual((hierarchy["run_id"], hierarchy["map_run_id"]), (model["run"]["id"], model["run"]["id"]))
        self.assertNotIn("business_stages", json.dumps(saved, ensure_ascii=False))
        self.assertNotIn("groups", saved)
        self.assertIn('id="map-hierarchy"', (output / "scenario_map.html").read_text(encoding="utf-8"))
        audit = (output / "audit" / f"run_{model['run']['id']}.txt").read_text(encoding="utf-8")
        self.assertIn("business_stages", audit)

    def test_failed_grouping_keeps_the_map_and_records_the_reason(self):
        class FailingStages(ScriptedClient):
            def chat(self, messages, **kwargs):
                if kwargs.get("tools") and kwargs["tools"][0]["function"]["name"] == "submit_business_stages":
                    raise ValueError("stage provider broke")
                return super().chat(messages, **kwargs)

        output = self.root / "failed"
        model = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=FailingStages())
        self.assertEqual(model["run"]["status"], "completed")
        hierarchy = json.loads((output / HIERARCHY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(hierarchy["stages"], [])
        self.assertEqual(hierarchy["status"]["result"], "none")
        self.assertIn("stage provider broke", hierarchy["status"]["reasons"][0])
        self.assertEqual(len(hierarchy["unassigned"]), len(model["nodes"]))
        html = (output / "scenario_map.html").read_text(encoding="utf-8")
        self.assertIn('id="scenario-model"', html)
        self.assertIn('id="map-hierarchy"', html, "the reason reaches the reader of the map")

    def test_disabled_stages_remove_stale_ones(self):
        output = self.root / "plain"
        output.mkdir()
        (output / HIERARCHY_FILE).write_text("{}", encoding="utf-8")
        run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), stages=False)
        self.assertFalse((output / HIERARCHY_FILE).exists())
        self.assertNotIn('id="map-hierarchy"', (output / "scenario_map.html").read_text(encoding="utf-8"))

    def test_rebuilding_stages_keeps_the_map_report_and_handoff_intact(self):
        output = self.root / "full"
        run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient(), observability=True,
                     report_options=ReportOptions(client=ScriptedReportClient()))
        map_bytes = (output / "scenario_map.json").read_bytes()
        first = json.loads((output / HIERARCHY_FILE).read_text(encoding="utf-8"))
        client = ScriptedReportClient()
        rebuild_from_map(output / "scenario_map.json", [], output, spec=SPEC, client=client)
        self.assertEqual([tool for tool, _ in client.requests], ["submit_business_stages"])
        self.assertEqual((output / "scenario_map.json").read_bytes(), map_bytes, "stages never rewrite the map")
        manifest = json.loads((output / "handoff" / "manifest.json").read_text(encoding="utf-8"))
        checksum = next(row["sha256"] for row in manifest["files"] if row["name"] == "../scenario_map.json")
        self.assertEqual(checksum, hashlib.sha256(map_bytes).hexdigest())
        second = json.loads((output / HIERARCHY_FILE).read_text(encoding="utf-8"))
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["map_run_id"], first["map_run_id"])
        html = (output / "scenario_map.html").read_text(encoding="utf-8")
        self.assertIn('role="tablist"', html, "the saved report stays in the HTML")
        self.assertIn('id="map-hierarchy"', html)
        if shutil.which("node"):
            # Map, stage view and report scripts share one scope: names must not collide.
            script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
            result = subprocess.run([shutil.which("node"), "--check"], input=script, capture_output=True,
                                    text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        audit = (output / "audit" / f"run_{second['run_id']}.txt").read_text(encoding="utf-8")
        self.assertIn("scenario_rebuild_run", audit)

    def test_report_rebuild_without_stages_reuses_the_saved_ones(self):
        output = self.root / "reuse"
        run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient())
        saved = (output / HIERARCHY_FILE).read_bytes()
        stages_client = ScriptedReportClient()
        rebuild_from_map(output / "scenario_map.json", sources(), output, spec=SPEC, stages=False, observability=True,
                         client=stages_client, report_options=ReportOptions(client=ScriptedReportClient()))
        self.assertEqual(stages_client.requests, [])
        self.assertEqual((output / HIERARCHY_FILE).read_bytes(), saved)
        html = (output / "scenario_map.html").read_text(encoding="utf-8")
        self.assertIn('id="map-hierarchy"', html)
        self.assertIn('role="tablist"', html)
        with self.assertRaises(PkoError):
            rebuild_from_map(output / "scenario_map.json", [], output, spec=SPEC, stages=False)
        with self.assertRaisesRegex(PkoError, "источников"):
            rebuild_from_map(output / "scenario_map.json", [], output, spec=SPEC, observability=True)


@unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки проекции этапов")
class StageProjectionTests(unittest.TestCase):
    def project(self, cases):
        runner = _STAGE_PROJECTION_JS + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
console.log(JSON.stringify(input.map(c=>{const view=projectScenarioStages(c.nodes,c.edges,c.stages,c.membership,c.open||[]);
 return {items:view.items.map(item=>({id:item.id,type:item.type,members:item.members,open:item.open})),
  links:view.links.map(link=>({source:link.source,target:link.target,kind:link.kind,evidence_status:link.evidence_status,
   outer:[link.outerSource,link.outerTarget],inner:link.inner,transitions:link.transitions.map(edge=>edge.id)})),
  represent:c.nodes.map(n=>view.represent(n.id)),outer:c.nodes.map(n=>view.outer(n.id))};})));
"""
        result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps(cases),
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def case(pairs, stages, membership, statuses=None):
        nodes = sorted({node for pair in pairs for node in pair} | set(membership))
        return {"nodes": [{"id": node} for node in nodes],
                "edges": [{"id": f"{a}>{b}", "source": a, "target": b, "kind": "sequence",
                           "evidence_status": (statuses or {}).get(f"{a}>{b}", "observed")} for a, b in pairs],
                "stages": stages, "membership": membership}

    def test_blocks_replace_collapsible_stages_only(self):
        collapsible = self.case([("a", "b"), ("b", "c"), ("c", "d"), ("b", "d"), ("c", "b")],
                                [{"id": "stage-1", "collapsible": True}], {"b": "stage-1", "c": "stage-1"},
                                {"b>d": "inferred"})
        kept = self.case([("a", "x"), ("b", "y")], [{"id": "stage-1", "collapsible": False}], {"x": "stage-1", "y": "stage-1"})
        first, second = self.project([collapsible, kept])
        self.assertEqual([item["id"] for item in first["items"]], ["a", "stage:stage-1", "d"])
        self.assertEqual(first["items"][1]["members"], ["b", "c"])
        links = {(link["source"], link["target"]): link for link in first["links"]}
        self.assertEqual(set(links), {("a", "stage:stage-1"), ("stage:stage-1", "d")}, "inner transitions are not drawn")
        self.assertEqual(links[("stage:stage-1", "d")]["transitions"], ["c>d", "b>d"])
        self.assertEqual(links[("stage:stage-1", "d")]["evidence_status"], "inferred", "a link is as weak as its weakest transition")
        self.assertEqual(first["represent"], ["a", "stage:stage-1", "stage:stage-1", "d"])
        self.assertEqual([item["type"] for item in second["items"]], ["node"] * 4)
        self.assertEqual(len(second["links"]), 2)

    def test_an_open_stage_shows_its_operations_inside_its_frame(self):
        pairs = [("a", "b"), ("b", "c"), ("b", "e"), ("c", "d"), ("e", "f"), ("c", "b")]
        case = {**self.case(pairs, [{"id": "stage-1", "collapsible": True}], {"b": "stage-1", "c": "stage-1", "e": "stage-1"}),
                "open": ["stage-1"]}
        view, = self.project([case])
        self.assertEqual([(item["id"], item.get("open")) for item in view["items"]],
                         [("a", None), ("stage:stage-1", True), ("d", None), ("f", None)])
        self.assertEqual(view["represent"], ["a", "b", "c", "d", "e", "f"], "the frame shows each operation")
        self.assertEqual(view["outer"], ["a", "stage:stage-1", "stage:stage-1", "d", "stage:stage-1", "f"])
        links = {(link["source"], link["target"]): link for link in view["links"]}
        self.assertEqual({pair for pair, link in links.items() if link["inner"]}, {("b", "c"), ("c", "b"), ("b", "e")})
        # Each way out keeps its own operation, so two exits never merge at the frame border.
        self.assertEqual(links[("c", "d")]["outer"], ["stage:stage-1", "d"])
        self.assertEqual(links[("e", "f")]["outer"], ["stage:stage-1", "f"])
        self.assertEqual(links[("a", "b")]["outer"], ["a", "stage:stage-1"])
        self.assertEqual(links[("b", "e")]["inner"], "stage:stage-1")

    def test_projection_draws_no_path_the_map_does_not_have(self):
        # Random graphs and partitions; stages are collapsed only when the region check allows it.
        generator, cases, graphs = random.Random(7), [], []
        for _ in range(150):
            names = [f"n{index}" for index in range(generator.randint(4, 12))]
            pairs = sorted({(generator.choice(names), generator.choice(names)) for _ in range(generator.randint(3, 20))})
            graph = graph_of(pairs, [names[0]])
            for name in names:
                if name not in graph.outgoing:
                    graph.nodes.append(name)
                    graph.outgoing[name], graph.incoming[name] = [], []
            membership = {name: f"stage-{generator.randint(1, 3)}" for name in names if generator.random() < .7}
            stages = []
            for stage in sorted(set(membership.values())):
                members = {name for name, owner in membership.items() if owner == stage}
                stages.append({"id": stage, "collapsible": collapse_check(members, graph)[0]})
            cases.append({**self.case(pairs, stages, membership), "nodes": [{"id": name} for name in names]})
            graphs.append(graph)
        checked = 0
        for case, graph, view in zip(cases, graphs, self.project(cases)):
            members = {item["id"]: set(item.get("members") or [item["id"]]) for item in view["items"]}
            reach = {}

            def reachable(start, inside=None):
                seen, pending = {start}, [start]
                while pending:
                    for target in graph.outgoing.get(pending.pop(), []):
                        if target not in seen and (inside is None or target in inside):
                            seen.add(target)
                            pending.append(target)
                return seen

            incoming = {}
            for link in view["links"]:
                incoming.setdefault(link["target"], []).append(link)
            for link in view["links"]:
                if not link["source"].startswith("stage:"):
                    continue
                # Every way into a block continues to every way out of it inside the stage.
                block = members[link["source"]]
                for into in incoming.get(link["source"], []):
                    for entry_edge in into["transitions"]:
                        entry = entry_edge.split(">")[1]
                        inside = reach.setdefault(entry, reachable(entry, block))
                        self.assertTrue(any(edge.split(">")[0] in inside for edge in link["transitions"]),
                                        (case["membership"], into, link))
                        checked += 1
        self.assertGreater(checked, 50, "the generated graphs must exercise blocks with ways in and out")


if __name__ == "__main__":
    unittest.main()
