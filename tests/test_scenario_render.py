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
            "schema_version": "1.0",
            "scenario": {"title": "Оформление карты", "description": "Действующий клиент"},
            "sources": [{"id": "src-1", "name": "Заявки", "kind": "directory", "file_count": 2}],
            "systems": [{"id": "as-1", "name": "Заявки", "source_ids": ["src-1"]}],
            "nodes": [{"id": "node-1", "name": "Принять заявку", "kind": "start", "system_id": "as-1", "evidence_status": "unknown", "evidence": []}],
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
                               "kind": "external", "system_id": "as-2",
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

    def test_legacy_detail_warning_and_atomic_fields_preserve_source_data(self):
        model = self.model()
        elements = {attrs.get("id"): attrs for _, attrs in Document(render_scenario_map(model)).elements}
        self.assertNotIn("detail-badge", elements)
        model["schema_version"] = "1.1"
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


@unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки раскладки графа")
class ScenarioGraphLayoutTests(unittest.TestCase):
    """Exercise the actual browser layout algorithm without a browser or DOM."""

    def layout(self, nodes, edges, vertical=False):
        payload = {"nodes": nodes, "edges": edges, "vertical": vertical,
                   "sizes": [[node["id"], {"w": 224, "h": 90 + index % 7 * 21}]
                             for index, node in enumerate(nodes)]}
        runner = _GRAPH_LAYOUT_JS + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const result=layoutScenarioGraph(input.nodes,input.edges,new Map(input.sizes),input.vertical);
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
const layout=layoutScenarioGraph(input.nodes,input.edges,new Map(input.sizes),input.vertical);
let index=0;const routes=input.edges.filter(edge=>layout.outerEdges.has(edge.id)).map(edge=>({
 id:edge.id,source:edge.source,target:edge.target,
 ...routeScenarioOuterEdge(edge,layout,input.vertical,index++)}));
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
                self.assertEqual({route["id"] for route in routed["routes"]}, {"edge-6", "edge-7"})
                for route in routed["routes"]:
                    self.assertNotIn(" L ", route["path"])
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
