"""Canonical paths and execution semantics survive the business-stage view.

``stage_render_fixture`` is also an offline browser fixture. It never calls a
provider or changes source files.
"""
import copy
import json
import shutil
import subprocess
import unittest

from pko.render.scenario_map import _SCRIPT, _STAGE_PROJECTION_JS, render_scenario_map
from pko.stages.contracts import HIERARCHY_VERSION
from pko.stages.inputs import METHODOLOGY, STAGE, input_fingerprint, map_revision, methodology
from pko.stages.regions import collapse_check, operation_graph


def stage_render_fixture():
    names = {"a": "Принять обращение", "b": "Согласовать обслуживание", "c": "Проверить причину отказа",
             "d": "Подготовить документы", "x": "Зарегистрировать решение", "y": "Отменить обращение",
             "u": "Отложить обработку", "v": "Зафиксировать отсутствие решения", "w": "Передать результат проверки",
             "r": "Отклонить обращение", "f": "Зафиксировать сбой обработки"}
    nodes = [{"id": key, "name": value, "kind": "operation", "description": "Синтетическая операция для проверки интерфейса.",
              "output": "Результат операции", "evidence_status": "unknown"} for key, value in names.items()]
    by_id = {node["id"]: node for node in nodes}
    for key, mode in (("a", "choice"), ("b", "all"), ("c", "choice"), ("x", "choice")):
        by_id[key]["continuation_mode"] = mode
    by_id["x"]["join_mode"], by_id["v"]["join_mode"] = "all", "any"
    for key, outcome in (("x", "success"), ("y", "cancelled"), ("u", "deferred"), ("v", "no_decision"),
                         ("w", "unknown"), ("r", "refusal"), ("f", "failure")):
        by_id[key]["outcome"] = outcome
    by_id["x"]["completion_condition"] = "Обслуживание согласовано окончательно"
    by_id["u"]["completion_condition"] = "Ожидается новое обращение клиента"
    pairs = [("a", "b"), ("a", "c"), ("b", "d"), ("b", "x"), ("d", "x"), ("c", "y"),
             ("c", "w"), ("c", "r"), ("c", "f"), ("x", "u"), ("x", "v"), ("u", "v")]
    edges = [{"id": f"e{i}", "source": source, "target": target, "kind": "sequence", "condition": "При выполнении условия продолжения", "evidence_status": "unknown"}
             for i, (source, target) in enumerate(pairs)]
    model = {"schema_version": "2.0", "scenario": {"title": "ДЕМО · Проверка двух видов карты",
             "description": "Синтетическая карта, не результат исследования реального проекта.",
             "entry_operation_ids": ["a"], "terminal_operation_ids": ["x", "y", "u", "v", "w", "r", "f"]},
             "nodes": nodes, "edges": edges, "run": {"id": "synthetic-render", "status": "partial", "demo": True},
             "gaps": [{"id": "gap-1", "node_ids": ["w"], "description": "Итог синтетической проверки не установлен."}]}
    graph = operation_graph(model)
    partitions = [("Рассмотрение обращения", ["a", "b", "c", "d"]), ("Оформление решения", ["x", "u", "v"]),
                  ("Отмена обращения", ["y"]), ("Передача результата", ["w"]),
                  ("Отказ в обслуживании", ["r"]), ("Регистрация сбоя", ["f"])]
    stages, membership = [], {}
    for index, (name, members) in enumerate(partitions, 1):
        stage_id = f"stage-{index}"
        collapsible, entry, reason = collapse_check(set(members), graph)
        stages.append({"id": stage_id, "name": name, "goal": "Проверить представление этапа.",
                       "result": "Результаты зависят от выполненной ветви.", "collapsible": collapsible,
                       "entry_operation_id": entry, "collapse_reason": reason})
        membership.update({member: stage_id for member in members})
    hierarchy = {"hierarchy_version": HIERARCHY_VERSION, "map_revision": map_revision(model),
                 "input_mode": "full", "input_sha256": input_fingerprint(model, "full"),
                 "methodology_sha256": methodology(STAGE, METHODOLOGY)["sha256"],
                 "authority_status": "proposed", "stages": stages, "membership": membership, "unassigned": []}
    return model, hierarchy


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer graph checks")
class StageRenderFixTests(unittest.TestCase):
    def js(self, source, payload):
        result = subprocess.run([shutil.which("node"), "-e", source], input=json.dumps(payload),
                                encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_atomic_scope_is_projected_without_another_members_exits(self):
        helpers = _SCRIPT[_SCRIPT.index("function scenarioNodeScope("):_SCRIPT.index("function updateHighlight(")]
        model, hierarchy = stage_render_fixture()
        source = _STAGE_PROJECTION_JS + helpers + """
const data=JSON.parse(require('fs').readFileSync(0,'utf8'));
const scope=scenarioNodeScope('b',data.model.edges);
console.log(JSON.stringify([[],['stage-1'],['stage-1','stage-2']].map(open=>{
 const view=projectScenarioStages(data.model.nodes,data.model.edges,data.hierarchy.stages,data.hierarchy.membership,open);
 const projected=projectScenarioScope(scope,view);
 return {nodes:[...projected.nodes],edgeIds:view.links.filter(e=>projected.edges.has(e.id)).flatMap(e=>e.transitions.map(t=>t.id))};
})));
"""
        results = self.js(source, {"model": model, "hierarchy": hierarchy})
        for result in results:
            # c -> y/w/r/f bypass b despite the common collapsed stage.
            self.assertFalse(set(result["edgeIds"]) & {"e1", "e5", "e6", "e7", "e8"})
            self.assertTrue({"e3", "e4"} <= set(result["edgeIds"]))
            self.assertNotIn("stage-3", result["nodes"])

    def test_projection_keeps_cycles_and_only_highlights_matching_parallel_links(self):
        helpers = _SCRIPT[_SCRIPT.index("function scenarioNodeScope("):_SCRIPT.index("function updateHighlight(")]
        source = _STAGE_PROJECTION_JS + helpers + """
const data=JSON.parse(require('fs').readFileSync(0,'utf8'));
const view=projectScenarioStages(data.nodes,data.edges,data.stages,data.membership,['stage-1']);
const scope=projectScenarioScope(scenarioNodeScope('b',data.edges),view);
console.log(JSON.stringify(view.links.filter(e=>scope.edges.has(e.id)).flatMap(e=>e.transitions.map(t=>t.id))));
"""
        payload = {"nodes": [{"id": key} for key in "abcxy"],
                   "edges": [{"id": str(i), "source": a, "target": b} for i, (a, b) in enumerate(
                       [("a", "b"), ("a", "c"), ("b", "x"), ("b", "x"), ("x", "b"), ("c", "y")])],
                   "stages": [{"id": "stage-1", "collapsible": True}], "membership": {key: "stage-1" for key in "abc"}}
        self.assertEqual(set(self.js(source, payload)), {"0", "2", "3", "4"})

    def test_focus_reference_uses_transition_identity_when_projection_ids_change(self):
        helper = _SCRIPT[_SCRIPT.index("function focusReference("):_SCRIPT.index("function restoreMapFocus(")]
        source = _STAGE_PROJECTION_JS + helper + """
const text=String,data=JSON.parse(require('fs').readFileSync(0,'utf8'));
const closed=projectScenarioStages(data.nodes,data.edges,data.stages,data.membership);
const link=closed.links.find(link=>link.transitions.some(edge=>edge.id==='cd'));
const display={linkById:new Map(closed.links.map(link=>[link.id,link]))},selected={type:'link',id:link.id};
const owner={dataset:{edgeId:link.id,transitionIds:JSON.stringify(link.transitions.map(edge=>edge.id))}};
const references=[focusReference({closest:()=>owner}).reference,focusReference({closest:()=>null}).reference];
const opened=projectScenarioStages(data.nodes,data.edges,data.stages,data.membership,['stage-1']);
console.log(JSON.stringify(references.map(ref=>({ref,restored:opened.links.find(link=>link.transitions.some(edge=>edge.id===ref.id)).transitions.map(edge=>edge.id),old:opened.links.find(other=>other.id===link.id).transitions.map(edge=>edge.id)}))));
"""
        payload = {"nodes": [{"id": key} for key in "abcd"],
                   "edges": [{"id": a + b, "source": a, "target": b} for a, b in [("a", "b"), ("b", "c"), ("c", "d"), ("b", "d")]],
                   "stages": [{"id": "stage-1", "collapsible": True}], "membership": {"b": "stage-1", "c": "stage-1"}}
        for result in self.js(source, payload):
            self.assertEqual(result["ref"], {"type": "edge", "id": "cd"})
            self.assertEqual(result["restored"], ["cd"])
            self.assertEqual(result["old"], ["bc"], "the same projection id would focus a different transition")

    def terminal_helpers(self):
        return "const text=String,array=value=>Array.isArray(value)?value:[],legacy=false;\n" + _SCRIPT[
            _SCRIPT.index("const outcomeNames="):_SCRIPT.index("const nodeKindLabel=")]

    def test_every_exact_outcome_and_conditional_completion_survive_badges(self):
        model, _ = stage_render_fixture()
        model["nodes"].append({"id": "extra-success", "outcome": "success"})
        model["scenario"]["terminal_operation_ids"].append("extra-success")
        source = "const data=JSON.parse(require('fs').readFileSync(0,'utf8')),scenario=data.scenario;\n" + self.terminal_helpers() + "console.log(JSON.stringify(stageOutcomes(data.nodes)));"
        outcomes = self.js(source, model)
        self.assertEqual({row["outcome"] for row in outcomes}, {"success", "cancelled", "deferred", "no_decision", "unknown", "refusal", "failure"})
        successes = [row for row in outcomes if row["outcome"] == "success"]
        self.assertEqual({row["conditional"] for row in successes}, {False, True})
        self.assertTrue(next(row for row in successes if row["conditional"])["label"].startswith("При завершении:"))
        for outcome in ("deferred", "no_decision", "unknown"):
            self.assertEqual(next(row for row in outcomes if row["outcome"] == outcome)["tone"], "neutral")
        self.assertIn("отмена", next(row for row in outcomes if row["outcome"] == "cancelled")["label"])

    def test_continuation_and_join_semantics_keep_their_own_operation(self):
        model, _ = stage_render_fixture()
        source = "const data=JSON.parse(require('fs').readFileSync(0,'utf8')),scenario=data.scenario;\n" + self.terminal_helpers() + "console.log(JSON.stringify(stageSemantics(data.nodes,data.edges).map(row=>[row.node.id,row.dimension,row.mode])));"
        self.assertEqual(self.js(source, model), [["a", "continuation", "choice"], ["b", "continuation", "all"],
            ["c", "continuation", "choice"], ["x", "continuation", "choice"], ["x", "join", "all"], ["v", "join", "any"]])

    def test_data_links_do_not_invent_execution_forks_or_joins(self):
        model = {"scenario": {}, "nodes": [{"id": "a", "continuation_mode": "all"}, {"id": "b"}, {"id": "c", "join_mode": "all"}],
                 "edges": [{"source": "a", "target": "b", "kind": "sequence"}, {"source": "b", "target": "c", "kind": "sequence"},
                           {"source": "a", "target": "c", "kind": "data"}]}
        source = "const data=JSON.parse(require('fs').readFileSync(0,'utf8')),scenario=data.scenario;\n" + self.terminal_helpers() + "console.log(JSON.stringify(stageSemantics(data.nodes,data.edges)));"
        self.assertEqual(self.js(source, model), [])

    def test_fixture_page_is_valid_and_does_not_mutate_the_map(self):
        model, hierarchy = stage_render_fixture()
        original = copy.deepcopy((model, hierarchy))
        html = render_scenario_map(model, hierarchy)
        self.assertEqual((model, hierarchy), original)
        self.assertIn('id="map-hierarchy"', html)
        script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
        result = subprocess.run([shutil.which("node"), "--check"], input=script, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
