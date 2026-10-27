"""Regression coverage for node-only expansion and repeated rejected proposals."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pko.llm.client import ChatResult
from pko.scenario_map.agent import GraphFrontierGuide, ResearchTools, _failure_guidance, run_agent
from pko.scenario_map.schema import MapState
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, ScriptedClient, research_calls, sources, scripted_summary


def node(number, kind="action"):
    return {"id": f"step-{number}", "name": f"Действие {number}", "kind": kind,
            "system_id": "sys-1", "group_id": "group-1"}


class GraphFrontierGuideTests(unittest.TestCase):
    def state(self, count=0):
        return SimpleNamespace(model={"nodes": [node(index) for index in range(1, count + 1)],
                                     "edges": [], "gaps": []})

    def test_node_only_expansion_gets_rate_limited_guidance(self):
        state = self.state()
        guide = GraphFrontierGuide()
        nudges = []
        for index in range(1, 29):
            state.model["nodes"].append(node(index))
            message = guide.message(state)
            if message:
                nudges.append(index)
                self.assertIn("Не соединяй узлы ради связности", message)
                self.assertIn("inspect_map(section='frontier')", message)
            self.assertIsNone(guide.message(state), "Unchanged state must not receive another nudge")
        self.assertEqual(nudges, [4, 8, 12, 16, 20, 24, 28])
        self.assertEqual(len(state.model["edges"]), 0, "Guidance must never construct edges")

    def test_link_progress_restarts_expansion_allowance(self):
        state = self.state(4)
        guide = GraphFrontierGuide()
        self.assertTrue(guide.message(state))
        state.model["edges"].append({"source": "step-1", "target": "step-2", "kind": "retry"})
        self.assertIsNone(guide.message(state))
        for index in range(5, 8):
            state.model["nodes"].append(node(index))
            self.assertIsNone(guide.message(state))
        state.model["nodes"].append(node(8))
        self.assertTrue(guide.message(state))

    def test_explicit_unknown_fragments_are_not_forced_to_connect(self):
        state = self.state(8)
        state.model["gaps"] = [{"id": "gap-1", "node_ids": [f"step-{index}" for index in range(1, 9)],
                                "description": "Код внешнего обработчика не передан"}]
        self.assertIsNone(GraphFrontierGuide().message(state))

    def test_closed_cycle_is_not_a_frontier_even_with_data_edge(self):
        state = self.state(4)
        state.model["edges"] = [{"source": f"step-{index}", "target": f"step-{index % 4 + 1}",
                                 "kind": "retry"} for index in range(1, 5)]
        guide = GraphFrontierGuide()
        self.assertIsNone(guide.message(state))
        self.assertIsNone(guide.message(state))
        state = self.state(4)
        state.model["edges"] = [{"source": "step-1", "target": "step-2", "kind": "data"}]
        self.assertTrue(GraphFrontierGuide().message(state), "A data relation is not execution progress")

    def test_error_diagnostics_mask_secrets_and_do_not_include_arguments(self):
        message = _failure_guidance([{"tool": "upsert_step", "error": "api_key=secret-value; неверная цитата"}])
        self.assertNotIn("secret-value", message)
        self.assertIn("неверная цитата", message)
        self.assertIn("line_start/line_end", message)


class GraphFrontierLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = build_workspace(sources(), Path(self.tmp.name) / "sources")

    def test_loop_nudges_before_building_long_unlinked_catalog(self):
        calls = research_calls()
        fourth_action = [index for index, (name, args) in enumerate(calls)
                         if name == "upsert_step" and args["kind"] not in ("start", "end")][3]
        requests = []
        class ObservedClient(ScriptedClient):
            def chat(self, messages, **kwargs):
                if kwargs.get('tools'):
                    requests.append(copy.deepcopy(messages))
                return super().chat(messages, **kwargs)
        client = ObservedClient(calls=calls)
        # Isolate guidance delivery from independently tested compaction.
        with patch('pko.scenario_map.agent.context_budget', return_value=(200000, 1024)):
            model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=fourth_action + 2)
        self.assertEqual(model["run"]["status"], "partial", model["run"])
        reminders = list(dict.fromkeys(message["content"] for request in requests for message in request if message["role"] == "user"
                     and str(message.get("content", "")).startswith("[scenario-graph-frontier]")))
        self.assertEqual(len(reminders), 1)
        self.assertIn('"starts":["step-1"]', reminders[0])
        self.assertIn('"id":"step-1"', reminders[0])

    def test_varying_invalid_calls_receive_specific_diagnostic_and_remain_bounded(self):
        class InvalidClient:
            spec = SPEC

            def __init__(self):
                self.requests = []

            def chat(self, messages, **kwargs):
                if not kwargs.get('tools'):
                    return scripted_summary()
                self.requests.append(copy.deepcopy(messages))
                number = len(self.requests)
                return ChatResult(text="", usage={}, tool_calls=[{
                    "id": f"bad-{number}", "type": "function", "function": {
                        "name": "upsert_step", "arguments": json.dumps({"unexpected_field": number})}}])

        client = InvalidClient()
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=60)
        self.assertEqual(model["run"]["status"], "partial", model["run"])
        self.assertIn("повторение", model["run"]["reason"])
        self.assertLessEqual(len(client.requests), 50)
        fourth_request = json.dumps(client.requests[3], ensure_ascii=False)
        self.assertIn("Последние отклонённые вызовы", fourth_request)
        self.assertIn("unexpected_field", fourth_request)
        self.assertFalse(model["nodes"])

    def test_read_reply_provides_usable_range_without_retyping_quote(self):
        state = MapState(self.workspace, SCENARIO)
        response = ResearchTools(state, self.workspace).answer({"id": "read-1", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "source-1/app.py", "offset": 8, "limit": 4})}})
        payload = json.loads(response["content"])
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["evidence_reference"], {"path": "source-1/app.py", "line_start": 8, "line_end": 11})
        self.assertNotIn("quote", payload["evidence_reference"])
        self.assertIn("quote не нужно", payload["evidence_hint"])


if __name__ == "__main__":
    unittest.main()
