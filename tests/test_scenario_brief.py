"""Orientation is durable working data, not authority to invent a graph."""
from copy import deepcopy
import json
import unittest

from pko.scenario_map.agent import ResearchTools, run_agent
from pko.scenario_map.schema import MapState
from scenario_script import SPEC, ScriptedClient
from test_scenario_schema import workspace


class ScenarioBriefTests(unittest.TestCase):
    def setUp(self):
        self.workspace = workspace(1)
        self.state = MapState(self.workspace, "Создание карты обработки заявки")
        self.tools = ResearchTools(self.state, self.workspace)
        self.ref = {"path": "source-1/channel.py", "line_start": 1, "line_end": 2}

    def arguments(self):
        return {"project_overview": "Проект обрабатывает заявки; исполнение клиента bank ещё нужно выяснить.",
                "scenario_alignment": "Вариант начинается с приёма заявки; выпуск требует проверки вызываемого кода.",
                "confirmed_facts": [{"finding": "Обработчик вызывает bank.post.", "evidence": [self.ref]}],
                "flow_hypothesis": ["Приём заявки, затем предполагаемая обработка выпуска."],
                "open_questions": ["Где выполняется запрос и какие данные он изменяет?"],
                "next_actions": ["Найти реализацию bank и проверить границы исполнения."], "evidence": [self.ref]}

    def call(self, name, args):
        return json.loads(self.tools.answer({"id": "test", "function": {"name": name, "arguments": args}})["content"])

    def read(self):
        result = self.call("read_file", {"path": self.ref["path"], "offset": 1, "limit": 2})
        self.assertTrue(result["ok"], result)

    def test_first_node_requires_saved_orientation_after_actual_read(self):
        node = {"name": "Приём заявки", "kind": "operation",
                "description": "Вход варианта", "evidence_status": "unknown", "evidence": []}
        rejected = self.call("upsert_step", node)
        self.assertFalse(rejected["ok"])
        self.assertIn("save_research_brief", rejected["error"])
        self.assertFalse(self.call("save_research_brief", self.arguments())["ok"])
        self.read()
        self.assertTrue(self.call("save_research_brief", self.arguments())["ok"])
        self.assertEqual(self.state.model["nodes"], [])
        self.assertEqual(self.state.model["edges"], [])
        self.assertTrue(self.call("upsert_step", node)["ok"])

    def test_brief_updates_are_atomic_idempotent_and_do_not_change_graph(self):
        self.read()
        args = self.arguments()
        self.assertEqual(self.call("save_research_brief", args)["revision"], 1)
        before = self.state.snapshot()
        unchanged = self.call("save_research_brief", args)
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["revision"], 1)
        invalid = deepcopy(args)
        invalid["confirmed_facts"][0]["evidence"][0]["line_end"] = 3
        self.assertFalse(self.call("save_research_brief", invalid)["ok"])
        self.assertEqual(self.state.snapshot(), before)
        args["flow_hypothesis"] = ["Новая проверяемая гипотеза после изучения вызываемого обработчика."]
        self.assertEqual(self.call("save_research_brief", args)["revision"], 2)
        self.assertEqual(self.state.model["nodes"], [])

    def test_inspect_and_checkpoint_preserve_long_meaning_not_only_preview(self):
        self.read()
        args = self.arguments()
        args["project_overview"] = "Назначение проекта. " * 75
        self.assertTrue(self.call("save_research_brief", args)["ok"])
        result = self.call("inspect_map", {"section": "research_brief"})["model"]
        self.assertEqual(result["project_overview"], args["project_overview"].strip())
        self.assertNotIn("quote", result["confirmed_facts"][0]["evidence"][0])
        snapshot = self.state.snapshot()
        self.assertIn("quote", snapshot["research_brief"]["confirmed_facts"][0]["evidence"][0])
        snapshot["research_brief"]["project_overview"] = "mutated copy"
        self.assertNotEqual(self.state.model["research_brief"]["project_overview"], "mutated copy")

    def test_initial_brief_needs_next_action_and_updates_cannot_drop_required_fields(self):
        self.read()
        args = self.arguments()
        args["next_actions"] = []
        self.assertFalse(self.call("save_research_brief", args)["ok"])
        args = self.arguments()
        args.pop("scenario_alignment")
        self.assertFalse(self.call("save_research_brief", args)["ok"])
        self.assertIsNone(self.state.model["research_brief"])

    def test_partial_run_saves_understanding_before_any_node(self):
        saved = []
        client = ScriptedClient(calls=[("read_file", {"path": self.ref["path"]}),
                                       ("save_research_brief", self.arguments())])
        model = run_agent(self.workspace, "Один вариант обработки заявки", SPEC, client=client,
                          max_steps=2, checkpoint=saved.append)
        self.assertEqual(model["run"]["status"], "partial")
        self.assertFalse(model["nodes"])
        self.assertEqual(saved[-1]["research_brief"]["next_actions"], self.arguments()["next_actions"])


if __name__ == "__main__":
    unittest.main()
