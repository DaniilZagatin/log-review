"""Public HTML contract: untrusted text, offline assets and partial results."""

import copy
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from pko.render.scenario_map import _GRAPH_LAYOUT_JS, render_scenario_map


class Document(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements = []
        self.scripts = []
        self.current_script = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "script":
            self.current_script = {"attributes": attributes, "text": ""}
            self.scripts.append(self.current_script)

    def handle_endtag(self, tag):
        if tag == "script":
            self.current_script = None

    def handle_data(self, value):
        if self.current_script is not None:
            self.current_script["text"] += value


class ScenarioRenderTests(unittest.TestCase):
    def model(self):
        return {
            "schema_version": "2.0",
            "scenario": {"title": "Оформление карты", "description": "Действующий клиент",
                         "entry_operation_ids": ["node-1"], "terminal_operation_ids": ["node-1"]},
            "sources": [{"id": "src-1", "name": "Заявки", "kind": "directory", "file_count": 2}],
            "nodes": [{"id": "node-1", "name": "Принять заявку", "kind": "operation", "evidence_status": "unknown", "evidence": []}],
            "edges": [], "gaps": [], "run": {"status": "partial", "steps": 3},
        }

    def test_script_termination_and_html_payload_are_only_data(self):
        value = '</script><script>alert("injected")</script><img src=x onerror="alert(1)"> & \u2028\u2029'
        model = self.model()
        model["scenario"]["title"] = value
        model["nodes"][0]["description"] = value
        document = Document(render_scenario_map(model))
        self.assertEqual(len(document.scripts), 2)
        self.assertFalse(any(tag == "img" for tag, _ in document.elements))
        data = next(script for script in document.scripts if script["attributes"].get("id") == "scenario-model")
        self.assertNotIn("</script>", data["text"])
        self.assertNotIn("<img", data["text"])
        self.assertEqual(json.loads(data["text"]), model)
        for _, attributes in document.elements:
            self.assertFalse(any(key.startswith("on") for key in attributes))

    def test_result_has_no_external_assets_or_network_calls(self):
        result = render_scenario_map(self.model())
        document = Document(result)
        for tag, attrs in document.elements:
            if tag in {"script", "link", "img", "iframe", "object", "embed"}:
                self.assertFalse(attrs.get("src") or attrs.get("href") or attrs.get("data"))
        self.assertEqual(result.count("data:font/woff2;base64,"), 3)
        script = document.scripts[-1]["text"]
        self.assertNotIn("fetch(", script)
        self.assertNotIn("XMLHttpRequest", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)

    def test_render_preserves_model_and_literal_template_names(self):
        model = self.model()
        model["scenario"]["title"] = "__STYLE__ __MODEL__ __TITLE__"
        previous = copy.deepcopy(model)
        result = render_scenario_map(model)
        self.assertEqual(model, previous)
        self.assertIn("<title>__STYLE__ __MODEL__ __TITLE__ · Белка</title>", result)
        data = Document(result).scripts[0]
        self.assertEqual(json.loads(data["text"]), model)

    def test_partial_and_unknown_state_remain_representable(self):
        model = self.model()
        model["run"] = {"status": "future-state", "reason": "Не получен ответ модели"}
        model["nodes"][0]["evidence_status"] = "unrecognized"
        result = render_scenario_map(model)
        data = Document(result).scripts[0]
        self.assertEqual(json.loads(data["text"])["run"], model["run"])
        self.assertIn("Статус исследования не установлен", result)
        self.assertIn("Не установлено", result)
        self.assertNotIn("GO / PIVOT", result)
        self.assertNotIn("Клиентский путь", result)

    def test_empty_map_and_accessible_navigation_are_present(self):
        result = render_scenario_map({"scenario": {"description": "Пока нет данных"}})
        document = Document(result)
        self.assertEqual(len(document.scripts), 2)
        self.assertTrue(any(attrs.get("id") == "empty-map" for _, attrs in document.elements))
        tabs = [attrs for _, attrs in document.elements if attrs.get("role") == "tab"]
        panels = {attrs["id"] for _, attrs in document.elements if attrs.get("role") == "tabpanel"}
        self.assertEqual(tabs, [])
        self.assertEqual(panels, set())
        self.assertTrue(any(attrs.get("id") == "stage-select" for _, attrs in document.elements))
        self.assertIn("prefers-reduced-motion:reduce", result)
        self.assertIn("focus-visible", result)
        self.assertIn("<noscript>", result)

    def test_demo_is_explicit_and_only_incomplete_runs_show_warning(self):
        model = self.model()
        for status, warning in (("completed", False), ("running", False),
                                ("future-state", False), ("partial", True), ("failed", True)):
            with self.subTest(status=status):
                model["run"] = {"status": status, "reason": "Причина результата", "demo": True}
                document = Document(render_scenario_map(model))
                elements = {attrs.get("id"): attrs for _, attrs in document.elements if "id" in attrs}
                self.assertNotIn("demo-badge", elements)
                self.assertTrue(json.loads(document.scripts[0]["text"])["run"]["demo"])
                self.assertEqual("hidden" not in elements["run-note"], warning)
        model["run"] = {"status": "partial", "demo": "true"}
        elements = {attrs.get("id"): attrs for _, attrs in Document(render_scenario_map(model)).elements if "id" in attrs}
        self.assertNotIn("demo-badge", elements)
        self.assertIn("hidden", elements["run-note"])

    def test_diagnostics_survive_partial_result_with_no_explicit_gaps(self):
        model = self.model()
        model.update(source_notes=["Исключены зависимости"],
                     source_omissions=["Превышен лимит исходников"],
                     validation={"issues": ["Не определено завершение"], "semantic_verification": False},
                     limitations=["Только переданные материалы"])
        result = render_scenario_map(model)
        self.assertEqual(json.loads(Document(result).scripts[0]["text"]), model)
        self.assertIn("Материалы, не вошедшие в исследование", result)
        self.assertIn("Примечания к источникам", result)
        self.assertIn("Незакрытые проверки карты", result)

    def test_map_uses_reference_style_with_closed_detail_drawer(self):
        result = render_scenario_map(self.model())
        elements = {attrs.get("id"): attrs for _, attrs in Document(result).elements if "id" in attrs}
        self.assertIn("hidden", elements["inspector"])
        self.assertEqual(elements["close-inspector"]["aria-label"], "Закрыть описание")
        self.assertIn("--mint:#F6FAF8", result)
        self.assertIn("--accent:#C17DF7", result)
        self.assertIn("height:100dvh", result)
        self.assertNotIn("Паспорт инициативы", result)

    def test_long_description_remains_in_data_with_compact_header(self):
        model = self.model()
        model["scenario"]["description"] = "Подробное описание выбранного варианта. " * 500
        result = render_scenario_map(model)
        self.assertEqual(json.loads(Document(result).scripts[0]["text"])["scenario"]["description"],
                         model["scenario"]["description"])
        self.assertIn("Описание варианта сценария", result)
        self.assertIn("-webkit-line-clamp:2", result)

    def test_single_canvas_preserves_only_supplied_semantic_edges(self):
        model = self.model()
        model["nodes"].append({"id": "node-2", "name": "Неизвестный участок",
                               "kind": "operation",
                               "evidence_status": "unknown", "evidence": []})
        model["gaps"] = [{"id": "gap-1", "description": "Переход не установлен",
                          "node_ids": ["node-1", "node-2"]}]
        result = render_scenario_map(model)
        document = Document(result)
        identifiers = [attrs.get("id") for _, attrs in document.elements]
        self.assertEqual(identifiers.count("viewport"), 1)
        self.assertNotIn("lanes", identifiers)
        self.assertEqual(json.loads(document.scripts[0]["text"])["edges"], [])
        self.assertNotIn("Шаги сгруппированы по АС", result)
        self.assertNotIn("Системы представлены отдельными дорожками", result)

    def test_renderer_import_does_not_load_legacy_progress_or_passport(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "backend")
        result = subprocess.run([sys.executable, "-B", "-c",
                                 "import json,sys;import pko.render.scenario_map;"
                                 "print(json.dumps(sorted(n for n in sys.modules "
                                 "if n.startswith('pko.progress') or 'passport' in n "
                                 "or n=='pko.render.progress_report')))"],
                                capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_new_methodology_has_no_legacy_warning_or_arrow_labels(self):
        model = self.model()
        result = render_scenario_map(model)
        document = Document(result)
        elements = {attrs.get("id"): attrs for _, attrs in document.elements}
        self.assertIn("hidden", elements["legacy-note"])
        self.assertNotIn("edge-label", result)
        self.assertIn("Условия выбора следующих операций указаны в карточке исходной операции", result)
        self.assertEqual(json.loads(document.scripts[0]["text"]), model)

    @unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки синтаксиса HTML")
    def test_generated_browser_script_has_valid_javascript_syntax(self):
        script = Document(render_scenario_map(self.model())).scripts[-1]["text"]
        result = subprocess.run([shutil.which("node"), "--check"], input=script,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки подсветки путей")
    def test_selected_operation_highlights_complete_paths_without_bypass(self):
        script = Document(render_scenario_map(self.model())).scripts[-1]["text"]
        helper = script[script.index("function scenarioNodeScope("):script.index("function updateHighlight(")]
        cases = [
            # Branches converge before and after the selection. The a->f
            # shortcut has highlighted endpoints but never visits s.
            ("branching", "s", ["a>b", "a>c", "b>s", "c>s", "s>d", "s>e", "d>f", "e>f",
                                 "a>f", "b>off", "off>other", "x>y"],
             {"a", "b", "c", "s", "d", "e", "f"},
             {"a>b", "a>c", "b>s", "c>s", "s>d", "s>e", "d>f", "e>f"}),
            # Cycles and a selected self-loop remain reachable; disconnected
            # cycles and self-loops must neither hang nor enter the scope.
            ("cycles", "s", ["a>s", "s>b", "b>s", "b>c", "c>d", "d>c", "s>s", "x>y", "y>x", "x>x"],
             {"a", "s", "b", "c", "d"}, {"a>s", "s>b", "b>s", "b>c", "c>d", "d>c", "s>s"}),
            ("isolated", "z", ["x>y", "x>x"], {"z"}, set()),
        ]
        # A long path also guards against a recursive traversal overflowing
        # the browser stack when either side of the selected operation grows.
        chain = [f"n{i}>n{i + 1}" for i in range(12000)]
        cases.append(("deep_path", "n6000", chain, {f"n{i}" for i in range(12001)}, set(chain)))
        payload = [{"selected": selected, "edges": [
            {"id": edge, "source": edge.split(">")[0], "target": edge.split(">")[1]} for edge in edges
        ]} for _, selected, edges, _, _ in cases]
        runner = helper + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
console.log(JSON.stringify(input.map(item=>{const scope=scenarioNodeScope(item.selected,item.edges);
 return {nodes:[...scope.nodes],edges:[...scope.edges]};})));
"""
        result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps(payload),
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        scopes = json.loads(result.stdout)
        self.assertEqual(len(scopes), len(cases))
        for (name, _, _, expected_nodes, expected_edges), scope in zip(cases, scopes):
            with self.subTest(name=name):
                self.assertEqual(set(scope["nodes"]), expected_nodes)
                self.assertEqual(set(scope["edges"]), expected_edges)

    @unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки карточки операции")
    def test_completion_condition_is_shown_only_when_supplied(self):
        # Execute the actual card renderer and text-field helper with a small
        # DOM double; no browser package is needed for this data contract.
        script = Document(render_scenario_map(self.model())).scripts[-1]["text"]
        helpers = "\n".join(line for line in script.splitlines() if line.startswith(
            ("const array =", "const text =", "const el =", "function field(")))
        card = script[script.index("function showNode(node){"):script.index("function showEdge(edge){")]
        runner = """
const element=tag=>({tag,children:[],textContent:'',append(...items){this.children.push(...items)},
 replaceChildren(){this.children=[]},classList:{add(){}},dataset:{}});
const document={createElement:element},panel=element('div'),$=()=>panel;
const legacy=false,scenario={terminal_operation_ids:['node-1']},operationsByNode=new Map(),
 byId=new Map(),validEdges=[],gaps=[],outcomeNames={};
const nodeKindLabel=()=> 'Атомарная операция',badge=()=>element('span'),nodeGroups=()=>[],
 continuations=()=>{},externalInteractions=()=>{},systemBlock=()=>{},codeExplanations=()=>{},evidenceBlock=()=>{};
""" + helpers + "\n" + card + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
console.log(JSON.stringify(input.map(node=>{showNode(node);return Object.fromEntries(
 panel.children.filter(e=>e.tag==='dl').map(e=>[e.children[0].textContent,e.children[1].textContent]));})));
"""
        operation = self.model()["nodes"][0]
        condition = "Если обращение отозвано клиентом, обработка завершается."
        result = subprocess.run([shutil.which("node"), "-e", runner],
                                input=json.dumps([{**operation, "completion_condition": condition}, operation]),
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        with_condition, without_condition = json.loads(result.stdout)
        self.assertEqual(with_condition.pop("Завершение сценария"), condition)
        self.assertNotIn("Завершение сценария", without_condition)
        self.assertEqual(with_condition, without_condition)

    def test_legacy_detail_warning_and_atomic_fields_preserve_source_data(self):
        model = self.model()
        elements = {attrs.get("id"): attrs for _, attrs in Document(render_scenario_map(model)).elements}
        self.assertNotIn("detail-badge", elements)
        model["schema_version"] = "1.1"
        model["nodes"][0].update(kind="start", system_id="as-1")
        model["systems"] = [{"id": "as-1", "name": "Архивная система"}]
        model["execution_reviews"] = [{"id": "review-1", "operations": [{
            "id": "op-1", "node_id": "node-1", "kind": "write",
            "name": "Сохранить заявку", "action": "Записывает заявку",
            "effect": "Заявке присвоен номер", "failure": "Транзакция откатывается",
            "condition": "Проверки пройдены", "evidence": [],
        }]}]
        document = Document(render_scenario_map(model))
        elements = {attrs.get("id"): attrs for _, attrs in document.elements}
        self.assertNotIn("detail-badge", elements)
        self.assertEqual(json.loads(document.scripts[0]["text"]), model)
        self.assertNotIn("hidden", elements["legacy-note"])
        self.assertIn("Архивная карта прежней методологии", render_scenario_map(model))


@unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки раскладки графа")
class ScenarioGraphLayoutTests(unittest.TestCase):
    """Exercise the actual browser layout algorithm without a browser or DOM."""

    def layout(self, nodes, edges, vertical=False, entry_ids=None):
        payload = {"nodes": nodes, "edges": edges, "vertical": vertical, "entry_ids": entry_ids or [],
                   "sizes": [[node["id"], {"w": 224, "h": 90 + index % 7 * 21}]
                             for index, node in enumerate(nodes)]}
        runner = _GRAPH_LAYOUT_JS + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const result=layoutScenarioGraph(input.nodes,input.edges,new Map(input.sizes),input.vertical,input.entry_ids);
console.log(JSON.stringify({width:result.width,height:result.height,
  positions:[...result.positions],rank:[...result.rank],outerEdges:[...result.outerEdges]}));
"""
        result = subprocess.run([shutil.which("node"), "-e", runner],
                                input=json.dumps(payload), capture_output=True,
                                text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_layout_valid(self, layout, expected_ids):
        positions = dict(layout["positions"])
        self.assertEqual(set(positions), set(expected_ids))
        for position in positions.values():
            self.assertTrue(all(math.isfinite(value) for value in position.values()))
            self.assertGreaterEqual(position["x"], 0)
            self.assertGreaterEqual(position["y"], 0)
            self.assertLessEqual(position["x"] + position["w"], layout["width"])
            self.assertLessEqual(position["y"] + position["h"], layout["height"])
        rows = list(positions.values())
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                overlaps = (left["x"] < right["x"] + right["w"]
                            and right["x"] < left["x"] + left["w"]
                            and left["y"] < right["y"] + right["h"]
                            and right["y"] < left["y"] + left["h"])
                self.assertFalse(overlaps, (left, right))

    def test_operation_entry_references_seed_cycle_layout_without_start_node(self):
        nodes = [{"id": key, "kind": "operation"} for key in ("a", "b", "c")]
        edges = [{"id": f"e-{i}", "source": left, "target": right, "kind": "sequence"}
                 for i, (left, right) in enumerate((("a", "b"), ("b", "c"), ("c", "a")))]
        layout = self.layout(nodes, edges, entry_ids=["b"])
        self.assert_layout_valid(layout, ["a", "b", "c"])
        ranks = dict(layout["rank"])
        self.assertLess(ranks["b"], ranks["c"])
        self.assertLess(ranks["c"], ranks["a"])

    def test_all_sixty_nested_operations_remain_individually_positioned(self):
        nodes = [{"id": f"step-{i}", "kind": "start" if i == 0 else "action",
                  "parent_id": "step-0" if i else None} for i in range(60)]
        edges = [{"id": f"edge-{i}", "source": f"step-{i}",
                  "target": f"step-{i+1}", "kind": "sequence"} for i in range(59)]
        layout = self.layout(nodes, edges)
        self.assert_layout_valid(layout, [node["id"] for node in nodes])
        self.assertEqual(len(set(dict(layout["rank"]).values())), 60)

    def test_cycles_parallel_edges_branches_and_long_jumps_in_both_directions(self):
        nodes = [{"id": key, "kind": "start" if key == "start" else "action"}
                 for key in ("start", "a", "b", "c", "d", "end")]
        pairs = [("start", "a", "sequence"), ("a", "b", "branch"),
                 ("a", "c", "branch"), ("b", "d", "sequence"),
                 ("c", "d", "sequence"), ("d", "end", "sequence"),
                 ("d", "a", "retry"), ("b", "b", "retry"),
                 ("a", "c", "data"), ("start", "end", "data")]
        edges = [{"id": f"edge-{i}", "source": left, "target": right, "kind": kind}
                 for i, (left, right, kind) in enumerate(pairs)]
        for vertical in (False, True):
            with self.subTest(vertical=vertical):
                layout = self.layout(nodes, edges, vertical)
                self.assert_layout_valid(layout, [node["id"] for node in nodes])
                self.assertIn("edge-9", layout["outerEdges"])
                self.assertIn("edge-6", layout["outerEdges"])
                self.assertIn("edge-7", layout["outerEdges"])

    def test_strongly_connected_component_and_disconnected_unknown_are_retained(self):
        nodes = [{"id": key, "kind": "action"} for key in ("a", "b", "c", "unknown")]
        edges = [{"id": f"e-{i}", "source": left, "target": right, "kind": "call"}
                 for i, (left, right) in enumerate((("a", "b"), ("b", "c"), ("c", "a")))]
        layout = self.layout(nodes, edges)
        self.assert_layout_valid(layout, [node["id"] for node in nodes])
        self.assertEqual(len({dict(layout["rank"])[key] for key in ("a", "b", "c")}), 3)

    def test_retry_and_long_jump_use_gutters_without_crossing_other_cards(self):
        # The lower branch used to exit from its top directly into a Bezier
        # channel, crossing the upper sibling. Check actual curve coordinates,
        # including unequal card heights and the narrow-screen orientation.
        nodes = [{"id": key, "kind": "start" if key == "start" else "action"}
                 for key in ("start", "fork", "top", "bottom", "join", "end")]
        pairs = [("start", "fork"), ("fork", "top"), ("fork", "bottom"),
                 ("top", "join"), ("bottom", "join"), ("join", "end"),
                 ("bottom", "fork"), ("start", "end")]
        edges = [{"id": f"edge-{i}", "source": left, "target": right,
                  "kind": "retry" if i == 6 else "data" if i == 7 else "sequence"}
                 for i, (left, right) in enumerate(pairs)]
        runner = _GRAPH_LAYOUT_JS + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const layout=layoutScenarioGraph(input.nodes,input.edges,new Map(input.sizes),input.vertical,input.entry_ids);
let index=0;const routes=input.edges.map(edge=>({
 id:edge.id,source:edge.source,target:edge.target,
 ...(layout.outerEdges.has(edge.id)?routeScenarioOuterEdge(edge,layout,input.vertical,index++):routeScenarioForwardEdge(edge,layout,input.vertical))}));
console.log(JSON.stringify({positions:[...layout.positions],routes,width:layout.width,height:layout.height}));
"""
        for vertical in (False, True):
            with self.subTest(vertical=vertical):
                payload = {"nodes": nodes, "edges": edges, "vertical": vertical,
                           "sizes": [[node["id"], {"w": 224, "h": 90 + i % 4 * 40}]
                                     for i, node in enumerate(nodes)]}
                result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps(payload),
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                routed = json.loads(result.stdout)
                positions = dict(routed["positions"])
                self.assertEqual({route["id"] for route in routed["routes"]}, {edge['id'] for edge in edges})
                for route in routed["routes"]:
                    self.assertNotIn(" L ", route["path"])
                    for left, right in zip(route['segments'], route['segments'][1:]):
                        for axis in ('x', 'y'):
                            # Adjacent cubic curves share both point and tangent.
                            self.assertEqual(left[3][axis], right[0][axis])
                            self.assertAlmostEqual(left[3][axis]-left[2][axis], right[1][axis]-right[0][axis])
                    for segment in route["segments"]:
                        for step in range(101):
                            t, u = step / 100, 1 - step / 100
                            point = {axis: sum(weight * control[axis] for weight, control in
                                              zip((u**3, 3*u*u*t, 3*u*t*t, t**3), segment))
                                     for axis in ("x", "y")}
                            self.assertGreaterEqual(point["x"], 0)
                            self.assertGreaterEqual(point["y"], 0)
                            self.assertLessEqual(point["x"], routed["width"])
                            self.assertLessEqual(point["y"], routed["height"])
                            for identifier, box in positions.items():
                                if identifier in (route["source"], route["target"]):
                                    continue
                                inside = (box["x"] < point["x"] < box["x"] + box["w"] and
                                          box["y"] < point["y"] < box["y"] + box["h"])
                                self.assertFalse(inside, (route["id"], identifier, point))

    def test_maximum_supported_five_hundred_nodes_have_finite_nonoverlapping_positions(self):
        nodes = [{"id": f"step-{i}", "kind": "action"} for i in range(500)]
        edges = [{"id": f"edge-{i}", "source": f"step-{i}",
                  "target": f"step-{i+1}", "kind": "sequence"} for i in range(499)]
        self.assert_layout_valid(self.layout(nodes, edges), [node["id"] for node in nodes])


if __name__ == "__main__":
    unittest.main()
