"""Semantic compression cannot alter evidence, graph state or tool protocol."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from pko.llm.client import ChatResult
from pko.scenario_map.agent import _brief
from pko.scenario_map.compaction import compact_research_context, summary_request, validate_summary
from pko.scenario_map.context import COMPACTION_MARKER, compact_context, _size, graph_identity
from pko.scenario_map.schema import MapState
from scenario_script import scripted_summary
from test_scenario_schema import workspace


class ScenarioCompactionTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState(workspace(1), "Сценарий обработки заявки")
        self.state.observe_read("source-1/channel.py", 1, 2)
        self.state.model["run"]["steps"] = 9
        self.prefix = [{"role": "system", "content": "immutable rules"},
                       {"role": "user", "content": "original request"}]

    def exchange(self, number, result="read data", private=""):
        call_id = f"read-{number}"
        call = {"id": call_id, "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"source-1/channel.py"}'}}
        assistant = {"role": "assistant", "content": "", "tool_calls": [call]}
        if private:
            assistant["reasoning_content"] = private
        return [assistant,
                {"role": "tool", "tool_call_id": call_id, "content": result}]

    def history(self):
        return self.prefix + self.exchange(1, "source text " * 4000, "PRIVATE_SENTINEL") + self.exchange(2)

    def compress(self, callback, messages=None, budget=16000):
        return compact_research_context(messages or self.history(), self.state, budget,
                                        summarize=callback, brief=_brief)

    def test_semantic_summary_is_persisted_and_previous_summary_is_separate_on_repeat(self):
        seen = []
        def summarize(request):
            seen.append(deepcopy(request))
            return scripted_summary()
        current = self.compress(summarize)
        self.assertEqual(current[:2], self.prefix)
        self.assertEqual(current[-2:], self.exchange(2))
        summary = self.state.model["context_summary"]
        self.assertEqual(summary["revision"], 1)
        self.assertEqual(summary["through_step"], 8)
        self.assertGreater(summary["input_truncated_fields"], 0)
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(seen))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(current))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.state.snapshot()))
        self.assertLessEqual(_size(current), 16000)
        original = deepcopy(self.state.model["nodes"])
        current += self.exchange(3, "new material " * 4000)
        after = self.compress(summarize, current)
        second = json.loads(seen[1][1]["content"])
        self.assertEqual(second["previous_summary"]["revision"], 1)
        self.assertNotIn(COMPACTION_MARKER, json.dumps(second["completed_exchanges"]))
        self.assertEqual(self.state.model["context_summary"]["revision"], 2)
        self.assertEqual(self.state.model["nodes"], original)
        self.assertEqual(sum(COMPACTION_MARKER in row.get("content", "") for row in after), 1)

    def test_registry_preserves_terminal_business_outcome_and_conditional_stop(self):
        self.state.model['nodes'] = [{'id': 'step-1', 'name': 'Отказать в оформлении', 'kind': 'operation',
            'outcome': 'refusal', 'effect_kind': 'result', 'continuation_mode': 'choice',
            'completion_condition': 'Заявка отклонена', 'join_mode': 'any', 'group_id': None}]
        self.state.model['scenario']['terminal_operation_ids'] = ['step-1']
        registry = graph_identity(self.state.model)
        row = registry['nodes'][0]
        self.assertEqual((row['outcome'], row['effect_kind'], row['completion_condition']),
                         ('refusal', 'result', 'Заявка отклонена'))
        self.assertEqual(registry['terminal_operation_ids'], ['step-1'])

    def test_bad_summary_and_provider_error_fall_back_once_without_losing_existing_summary(self):
        self.compress(lambda _: scripted_summary())
        previous = deepcopy(self.state.model["context_summary"])
        for bad in (ChatResult(text="", usage={}), ChatResult(text="invalid", usage={}),
                    ChatResult(text=scripted_summary().text, usage={"completion_tokens": 4096}, finish_reason="length"),
                    ChatResult(text="{}", usage={}, tool_calls=[{"function": {"name": "finish"}}]),
                    RuntimeError("provider offline")):
            with self.subTest(bad=type(bad).__name__):
                callback = Mock(side_effect=bad) if isinstance(bad, Exception) else Mock(return_value=bad)
                result = self.compress(callback)
                callback.assert_called_once()
                self.assertEqual(self.state.model["context_summary"], previous)
                self.assertLess(_size(result), _size(self.history()))
                self.assertEqual(result[-2:], self.exchange(2))
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_open_task_truncation_is_explicit_and_durable_across_summaries(self):
        ref = {'path':'source-1/channel.py', 'line_start':1, 'line_end':2}
        for number in range(15):
            result = self.state.dispatch('save_research_task', {'kind':'function', 'target':f'work-{number}',
                'evidence':[ref], 'status':'open'})
            self.assertTrue(result['ok'], result)
        requests = []
        def summarize(request):
            requests.append(json.loads(request[1]['content']))
            return scripted_summary()
        current = self.history()
        for revision in (1, 2):
            current = self.compress(summarize, current, budget=24000)
            self.assertEqual(self.state.model['context_summary']['revision'], revision)
            self.assertFalse(self.state.model['context_summary']['input_open_tasks_complete'])
            self.assertEqual(requests[-1]['current_state']['open_task_count'], 15)
            self.assertEqual(len(requests[-1]['current_state']['open_tasks']), 12)
            self.assertEqual(len(self.state.dispatch('inspect_map', {'section':'research_tasks','limit':30})['items']), 15)
            current += self.exchange(10+revision, 'new data '*6000)

    def test_summary_cannot_cite_unread_lines_or_replace_evidence_cache(self):
        value = json.loads(scripted_summary().text)
        value["references"] = [{"path": "source-1/channel.py", "line_start": 3, "line_end": 3}]
        before = deepcopy(self.state._reads)
        with self.assertRaises(ValueError):
            validate_summary(json.dumps(value), self.state)
        self.assertEqual(self.state._reads, before)
        value["references"][0].update(line_start=1, line_end=2)
        self.assertEqual(validate_summary(json.dumps(value), self.state)["references"], value["references"])

    def test_summary_input_bounds_large_history_and_excludes_unpaired_calls(self):
        messages = self.prefix
        for number in range(100):
            messages += self.exchange(number, "data" * 3000)
        messages += self.exchange(101)[:-1]
        request = summary_request(messages, self.state, 9000)
        self.assertLessEqual(_size(request), 9000)
        payload = json.loads(request[1]["content"])
        self.assertNotIn("read-101", json.dumps(payload))
        # An incomplete tail invalidates the eligible suffix; it must not be
        # disguised as a completed observation by the summarizer.
        self.assertEqual(payload["completed_exchanges"], [])
        request = summary_request(messages[:-1], self.state, 9000)
        payload = json.loads(request[1]["content"])
        self.assertGreater(payload["omitted_exchange_count"], 0)
        self.assertGreater(payload["truncated_field_count"], 0)

    def test_interrupt_propagates_and_no_new_summary_is_committed(self):
        with self.assertRaises(KeyboardInterrupt):
            self.compress(Mock(side_effect=KeyboardInterrupt()))
        self.assertIsNone(self.state.model["context_summary"])

    def test_nonshrinking_replacement_is_rejected_without_committing_new_summary(self):
        messages = self.prefix + self.exchange(1)
        original = deepcopy(messages)
        result = self.compress(lambda _: scripted_summary(), messages, budget=16000)
        self.assertIs(result, messages)
        self.assertEqual(result, original)
        self.assertIsNone(self.state.model["context_summary"])

    def test_rejected_compaction_does_not_return_legacy_thinking(self):
        for callback in (Mock(return_value=scripted_summary()), Mock(side_effect=RuntimeError("offline"))):
            with self.subTest(callback=callback):
                messages = self.prefix + self.exchange(1, private="PRIVATE_SENTINEL")
                result = self.compress(callback, messages)
                self.assertEqual(result, self.prefix + self.exchange(1))
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(callback.call_args))
                self.assertIsNone(self.state.model["context_summary"])

    def test_summary_inspection_is_complete_and_snapshot_is_detached(self):
        self.compress(lambda _: scripted_summary())
        response = self.state.dispatch("inspect_map", {"section": "context_summary"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["model"], self.state.model["context_summary"])
        response["model"]["summary"]["next_actions"].clear()
        self.assertTrue(self.state.model["context_summary"]["summary"]["next_actions"])

    def test_graph_ids_and_brief_have_priority_and_full_recovery_when_shortened(self):
        state = Mock()
        state.snapshot.return_value = {"nodes": [
            {"id": f"step-{n}", "name": f"Действие {n}", "kind": "operation"} for n in range(28)],
            "research_brief": {"revision": 3, "status": "working_hypothesis",
                               "project_overview": "Назначение " * 150,
                               "scenario_alignment": "Признаки варианта " * 100,
                               "flow_hypothesis": ["Проверяемая гипотеза " * 15] * 10,
                               "next_actions": ["Изучить обработчик"], "open_questions": []}}
        messages = self.prefix + self.exchange(1, "data " * 20000)
        for budget in (16000, 6000, 2400):
            result = compact_context(messages, state, budget, brief=lambda _: "x" * 30000)
            payload = json.loads(result[2]["content"].split("\n")[-1])
            self.assertEqual(payload["research_brief"]["revision"], 3)
            self.assertIn("research_brief", payload["research_brief"]["recover"])
            identity = payload["graph_identity"]
            if identity["registry_complete"]:
                ids = identity.get("all_node_ids") or [row["id"] for row in identity["nodes"]]
                self.assertEqual(len(ids), 28)
            self.assertLessEqual(_size(result), budget)

    def test_partially_investigated_tasks_remain_open_and_keep_child_dependencies(self):
        self.state.model['research_tasks'] = [
            {'id': 'task-1', 'kind': 'executor', 'target': 'Dispatcher.createAgent',
             'status': 'analyzed', 'child_task_ids': ['task-2'], 'evidence': [], 'node_ids': []},
            {'id': 'task-2', 'kind': 'prompt', 'target': 'Prompt.instructions',
             'status': 'read', 'child_task_ids': [], 'evidence': [], 'node_ids': []},
        ]
        payload = json.loads(summary_request(self.history(), self.state, 16000)[1]['content'])
        self.assertEqual(payload['current_state']['open_task_count'], 2)
        self.assertEqual(payload['current_state']['open_tasks'][0]['child_task_ids'], ['task-2'])
        self.assertNotIn('systems', payload['current_state']['counts'])


if __name__ == "__main__":
    unittest.main()
