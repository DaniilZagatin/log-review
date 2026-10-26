"""Recover ongoing research across compactions, including thinking tool turns."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from pko.scenario_map.agent import _brief, _compact, _json
from pko.scenario_map.context import COMPACTION_MARKER, compact_context, context_budget


class ContextCompactionTests(unittest.TestCase):
    @staticmethod
    def size(messages):
        return sum(len(_json(message)) for message in messages)

    @staticmethod
    def prefix():
        return [{"role": "system", "content": "rules"}, {"role": "user", "content": "scenario"}]

    @staticmethod
    def exchange(identifier, *, reasoning="", result="observed data", count=1):
        calls = [{"id": f"{identifier}-{index}", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": f"source-1/{identifier}.py"})}}
                 for index in range(count)]
        assistant = {"role": "assistant", "content": "", "tool_calls": calls}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        return [assistant] + [{"role": "tool", "tool_call_id": call["id"], "content": result} for call in calls]

    @staticmethod
    def state():
        state = Mock()
        state.snapshot.return_value = {
            "scenario": {"title": "Выпуск карты"}, "run": {"status": "running"},
            "read_coverage": [{"path": "source-1/handler.py", "ranges": [[5, 17]], "line_count": 13}],
            "research_history": [{"observation_id": "obs-3", "name": "search",
                                  "arguments": {"query": "CardIssued"}, "preview": "publisher found",
                                  "repeat_count": 1}],
            "research_notes": [{"id": "note-1", "kind": "question", "status": "open",
                                "finding": "Есть отправитель события", "next_action": "Найти получателя CardIssued"}],
            "nodes": [{"id": "step-1", "name": "Сохранить заявку"}], "edges": [],
            "groups": [], "systems": [], "gaps": [],
        }
        return state

    def assert_paired_tools(self, messages):
        pending = []
        for message in messages:
            if message["role"] == "assistant":
                self.assertFalse(pending)
                pending = [call["id"] for call in message.get("tool_calls", [])]
            elif message["role"] == "tool":
                self.assertIn(message["tool_call_id"], pending)
                pending.remove(message["tool_call_id"])
            else:
                self.assertFalse(pending)
        self.assertFalse(pending)

    def test_serialized_summary_escaping_is_included_in_budget(self):
        state = Mock()
        state.snapshot.return_value = {"scenario": {"title": "Сценарий"}, "run": {"status": "running"}}
        messages = [{"role": "system", "content": "rules"},
                    {"role": "user", "content": "scenario"},
                    {"role": "assistant", "content": "x" * 10000}]
        with patch("pko.scenario_map.agent._brief", return_value='"\\\n' * 400):
            result = _compact(messages, state, 2000)
        self.assertLessEqual(sum(len(_json(m)) for m in result), 2000)
        self.assertEqual(result[:2], messages[:2])
        self.assertIn("inspect_map", result[-1]["content"])

    def test_nested_operation_quotes_stay_in_state_but_not_compact_summary(self):
        citation = {"path": "source-1/app.py", "line_start": 1, "quote": "large literal source"}
        original = {"execution_reviews": [{"operations": [{"evidence": [citation]}],
                    "checks": {"branches": {"evidence": [citation]}},
                    "node_fingerprints": {"step-1": "digest"}}]}
        state = Mock()
        state.snapshot.return_value = original
        compact = json.loads(_brief(state))
        ref = compact["execution_reviews"][0]["operations"][0]["evidence"][0]
        self.assertEqual(ref, {"path": "source-1/app.py", "line_start": 1})
        self.assertNotIn("large literal source", _brief(state))
        self.assertNotIn("node_fingerprints", compact["execution_reviews"][0])
        self.assertEqual(citation["quote"], "large literal source")

    def test_configured_long_model_window_is_used_with_response_reserve(self):
        small, small_response = context_budget(64000, [])
        large, large_response = context_budget(300000, [])
        self.assertGreater(large, 180000)
        self.assertGreater(large, small)
        self.assertGreater(large_response, 0)
        self.assertGreater(small_response, 0)
        self.assertLess(large, int((300000 - large_response) * 1.6))
        schemas = [{"function": {"description": '"\\\n' * 100}}]
        with_tools, _ = context_budget(300000, schemas)
        self.assertLess(with_tools, large)

    def test_compaction_retains_recent_complete_thinking_exchanges(self):
        first = self.exchange("first", reasoning="private-first", count=2)
        second = self.exchange("second", reasoning="private-second")
        messages = self.prefix() + [{"role": "assistant", "content": "x" * 30000}] + first + second
        result = compact_context(messages, self.state(), 12000, brief=_brief)
        self.assertEqual(result[:2], self.prefix())
        self.assertEqual(result[-len(first + second):], first + second)
        self.assert_paired_tools(result)
        self.assertLessEqual(self.size(result), int(12000 * .6))

    def test_memory_survives_repeated_compaction_before_nodes_are_built(self):
        state = self.state()
        state.snapshot.return_value["nodes"] = []
        messages = self.prefix()
        for number in range(4):
            messages += [{"role": "assistant", "content": "x" * 30000}]
            latest = self.exchange(f"turn-{number}", reasoning=f"private-{number}")
            messages += latest
            messages = compact_context(messages, state, 12000, brief=_brief)
            recovery = messages[2]["content"]
            self.assertIn("source-1/handler.py", recovery)
            self.assertIn("CardIssued", recovery)
            self.assertIn("Найти получателя", recovery)
            self.assertIn("obs-3", recovery)
            self.assertEqual(messages[-len(latest):], latest)
            self.assertEqual(sum(COMPACTION_MARKER in str(m.get("content", "")) for m in messages), 1)
            self.assert_paired_tools(messages)
            # A compacted history does not immediately need another reset.
            self.assertIs(compact_context(messages, state, 12000, brief=_brief), messages)

    def test_large_map_uses_research_manifest_not_counts_only(self):
        state = self.state()
        state.snapshot.return_value["nodes"] = [{"id": "step-9", "name": "Long " * 10000}]
        messages = self.prefix() + [{"role": "assistant", "content": "x" * 30000}]
        result = compact_context(messages, state, 12000, brief=_brief)
        recovery = result[2]["content"]
        self.assertIn("source-1/handler.py", recovery)
        self.assertIn("obs-3", recovery)
        self.assertIn("CardIssued", recovery)
        self.assertIn("step-9", recovery)
        self.assertIn("read_fragments", recovery)
        self.assertLessEqual(self.size(result), int(12000 * .6))

    def test_oversized_latest_thinking_drops_whole_exchange_not_reasoning_alone(self):
        state = self.state()
        old = self.exchange("older")
        huge = self.exchange("huge", reasoning="private-thinking " * 3000)
        messages = self.prefix() + old + huge
        result = compact_context(messages, state, 6000, brief=_brief)
        self.assertEqual(len(result), 3)
        self.assertNotIn("private-thinking", _json(result))
        self.assertIn("research_history", result[2]["content"])
        self.assertIn("read_fragments", result[2]["content"])
        self.assertIn("Найти получателя", result[2]["content"])
        self.assert_paired_tools(result)

    def test_small_fallback_keeps_all_recovery_routes(self):
        state = self.state()
        messages = self.prefix() + [{"role": "assistant", "content": "x" * 10000}]
        result = compact_context(messages, state, 1800, brief=lambda _: "x" * 10000)
        recovery = result[-1]["content"]
        for section in ("research_history", "research_notes", "read_coverage", "read_fragments", "nodes", "edges", "gaps"):
            self.assertIn(section, recovery)
        self.assertIn("inspect_map", recovery)
        self.assertLessEqual(self.size(result), 1800)

    def test_incomplete_and_orphan_tool_blocks_are_not_preserved(self):
        incomplete = self.exchange("unfinished", count=2)[:-1]
        latest = self.exchange("valid")
        messages = self.prefix() + [{"role": "assistant", "content": "x" * 10000}] + incomplete + latest
        result = compact_context(messages, self.state(), 6000, brief=_brief)
        self.assertNotIn("unfinished-0", _json(result))
        self.assertEqual(result[-len(latest):], latest)
        self.assert_paired_tools(result)
        orphan = {"role": "tool", "tool_call_id": "absent-call", "content": "oops"}
        messages = self.prefix() + [{"role": "assistant", "content": "x" * 10000}] + latest + [orphan]
        result = compact_context(messages, self.state(), 6000, brief=_brief)
        self.assertEqual(len(result), 3)
        self.assert_paired_tools(result)

    def test_retained_suffix_does_not_skip_an_oversized_middle_exchange(self):
        oldest = self.exchange("oldest", result="small result")
        middle = self.exchange("middle", result="x" * 10000)
        latest = self.exchange("latest")
        result = compact_context(self.prefix() + oldest + middle + latest, self.state(), 6000, brief=_brief)
        self.assertEqual(result[3:], latest)
        self.assert_paired_tools(result)

    def test_live_reasoning_never_enters_recovery_state_or_compaction_audit(self):
        state = self.state()
        original = copy.deepcopy(state.snapshot.return_value)
        latest = self.exchange("latest", reasoning="SECRET-PRIVATE-THINKING")
        messages = self.prefix() + [{"role": "assistant", "content": "x" * 30000}] + latest
        with patch("pko.scenario_map.context.record") as record:
            result = compact_context(messages, state, 12000, brief=_brief)
        self.assertNotIn("SECRET-PRIVATE-THINKING", result[2]["content"])
        self.assertNotIn("SECRET-PRIVATE-THINKING", str(record.call_args))
        self.assertEqual(state.snapshot.return_value, original)
        self.assertEqual(result[-len(latest)]["reasoning_content"], "SECRET-PRIVATE-THINKING")

    def test_recovery_marker_is_replaced_instead_of_recursively_summarized(self):
        old_marker = {"role": "user", "content": COMPACTION_MARKER + "\nold memory " + "x" * 20000}
        latest = self.exchange("latest")
        result = compact_context(self.prefix() + [old_marker] + latest, self.state(), 6000, brief=_brief)
        self.assertNotIn("old memory", _json(result))
        self.assertEqual(sum(COMPACTION_MARKER in str(m.get("content", "")) for m in result), 1)
        self.assertEqual(result[-len(latest):], latest)
