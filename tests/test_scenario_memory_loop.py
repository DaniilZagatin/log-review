"""End-to-end regressions for public research memory and bounded thinking loops."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.audit import audit_run
from pko.llm.client import ChatResult
from pko.scenario_map.agent import run_agent
from pko.scenario_map.sources import build_workspace
from scenario_script import NODE_COUNT, SCENARIO, SPEC, ScriptedClient, research_calls, sources, scripted_summary


PRIVATE_MARKER = "PRIVATE_THOUGHT_SENTINEL_92817"


class BatchClient(ScriptedClient):
    """Script public tool proposals, validating every observed tool response."""

    def __init__(self, batches, reasoning=None, public_text=None):
        self.batches = iter(batches)
        self.reasoning = reasoning or {}
        self.public_text = public_text or {}
        self.requests = []
        self.responses = {}
        self.emitted = []
        self.seen = []

    def chat(self, messages, **kwargs):
        if not kwargs.get("tools"):
            return scripted_summary()
        self.seen = messages
        self.requests.append(copy.deepcopy(messages))
        for message in messages:
            if message["role"] == "tool":
                payload = json.loads(message["content"])
                if not payload.get("ok"):
                    raise AssertionError(payload)
                self.responses[message["tool_call_id"]] = payload
        batch = next(self.batches)
        number = len(self.requests)
        self.emitted.extend(batch)
        return ChatResult(text=self.public_text.get(number, ""), usage={}, reasoning_content=self.reasoning.get(number, ""),
                          tool_calls=[{"id": f"request-{number}-tool-{index}", "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
                                      for index, (name, arguments) in enumerate(batch)])


class ScenarioMemoryLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = build_workspace(sources(), self.root / "sources")

    def test_repeated_reads_get_one_recovery_then_stop_without_false_progress(self):
        read = ("read_file", {"path": "source-1/app.py"})
        client = BatchClient([[read]] * 30)
        saved = []
        audit_path = self.root / "repeat-audit.txt"
        with audit_run(audit_path, "repeat-test"):
            model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=30,
                              checkpoint=saved.append)
        self.assertEqual(model["run"]["status"], "partial", model["run"])
        self.assertTrue(model["run"].get("loop_recovery_attempted"))
        self.assertIn("повторение", model["run"]["reason"])
        self.assertLess(len(client.requests), 15, "Repeat receipts must not reset the no-progress guard")
        self.assertEqual(len(model["research_history"]), 1)
        self.assertEqual(model["research_history"][0]["repeat_count"], len(client.requests) - 1)
        receipts = [response["memory"] for response in client.responses.values()]
        self.assertFalse(receipts[0]["seen_before"])
        self.assertTrue(all(receipt["seen_before"] for receipt in receipts[1:]))
        self.assertEqual(len({receipt["observation_id"] for receipt in receipts}), 1)
        self.assertEqual(saved[-1]["run"]["status"], "partial")
        audit = audit_path.read_text(encoding="utf-8")
        self.assertEqual(audit.count('"event": "research_loop_recovery"'), 1)

    def test_repeat_signature_does_not_stop_a_turn_that_adds_an_actual_map_node(self):
        repeated_read = ("read_file", {"path": "source-1/app.py"})
        batches = [[call] + ([repeated_read] * 3 if call[0] == "upsert_step" else [])
                   for call in research_calls()]
        client = BatchClient(batches)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client)
        self.assertEqual(model["run"]["status"], "completed", model["run"])
        self.assertEqual(len(model["nodes"]), NODE_COUNT)
        self.assertFalse(model["run"].get("loop_recovery_attempted", False))

    def test_new_progress_rearms_recovery_but_global_cap_stays_bounded(self):
        first = ('read_file', {'path':'source-1/app.py'})
        second = ('read_file', {'path':'source-2/app.py'})
        # Each new scope is genuine map progress. Four episodes may not create
        # an unlimited series of recoveries even with progress between them.
        batches = []
        for number in range(4):
            batches.append([('set_scope', {'title':f'Orientation {number}', 'start':'Input', 'end':'Output'})])
            batches.extend([[first if number % 2 == 0 else second]] * (4 if number < 2 else 3))
        client = BatchClient(batches + [[first]] * 20)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=60)
        self.assertEqual(model['run']['status'], 'partial', model['run'])
        self.assertEqual(model['run']['loop_recoveries'], 3)
        self.assertIn('повторение', model['run']['reason'])
        self.assertLess(len(client.requests), 24)

    def test_thinking_compactions_recover_pre_map_facts_and_cached_quotes_without_reread(self):
        calls = research_calls()
        first_node = next(arguments for name, arguments in calls if name == "upsert_step")
        original_ref = copy.deepcopy(first_node["evidence"][0])
        finding = "Обработчик новой дебетовой карты принимает запрос по /cards/debit/new."
        next_action = "Нанести вход в сценарий, затем проследить проверку типа клиента."
        preliminary = [
            ("read_file", {"path": "source-1/app.py"}),
            ("search", {"pattern": "/cards/debit/new", "glob": "source-1/*"}),
            ("save_research_note", {"kind": "fact", "finding": finding,
                                    "next_action": next_action, "evidence": [original_ref]}),
            ("inspect_map", {"section": "research_notes"}),
            ("inspect_map", {"section": "research_history"}),
            ("inspect_map", {"section": "read_fragments", "path": original_ref["path"],
                              "offset": original_ref["line_start"],
                              "limit": original_ref["line_end"] - original_ref["line_start"] + 1}),
        ]
        remaining = [copy.deepcopy(call) for call in calls
                     if call != ("read_file", {"path": "source-1/app.py"})]
        recovered_quote = []

        class RecoveringClient(BatchClient):
            def chat(inner, messages, **kwargs):
                # The next node is really submitted with the literal recovered
                # through inspect_map, not another read of the source file.
                for message in messages:
                    if message.get("tool_call_id") == "request-6-tool-0":
                        payload = json.loads(message["content"])
                        rows = payload["items"]
                        quote = "\n".join(row["text"] for row in rows)
                        recovered_quote.append(quote)
                        for name, arguments in remaining:
                            if name == "upsert_step":
                                arguments["evidence"] = [{**original_ref, "quote": quote}]
                                break
                return super().chat(messages, **kwargs)

        large_thinking = PRIVATE_MARKER + "x" * 50000
        client = RecoveringClient([[call] for call in preliminary + remaining],
                                  reasoning={1: large_thinking, 2: large_thinking, 3: large_thinking},
                                  public_text={n: "Публичный результат исследования. " * 1600 for n in (1, 2, 3)})
        saved = []
        audit_path = self.root / "compaction-audit.txt"
        with patch("pko.scenario_map.agent.context_budget", return_value=(30000, 1024)), \
                audit_run(audit_path, "compaction-test"):
            model = run_agent(self.workspace, SCENARIO, SPEC, client=client, checkpoint=saved.append)
        self.assertEqual(model["run"]["status"], "completed", model["run"])
        self.assertEqual(len(model["nodes"]), NODE_COUNT)
        self.assertGreaterEqual(model["run"]["context_compactions"], 3)
        self.assertTrue(recovered_quote)
        self.assertEqual(recovered_quote[0], original_ref["quote"])
        self.assertEqual(client.responses["request-4-tool-0"]["items"][0]["finding"], finding)
        self.assertEqual(client.responses["request-4-tool-0"]["items"][0]["next_action"], next_action)
        history = client.responses["request-5-tool-0"]["items"]
        search = next(item for item in history if item["name"] == "search")
        self.assertEqual(search["arguments"]["pattern"], "/cards/debit/new")
        self.assertIn("/cards/debit/new", json.dumps(search["payload"], ensure_ascii=False))
        self.assertEqual(sum(name == "read_file" and arguments.get("path") == "source-1/app.py"
                             for name, arguments in client.emitted), 1)
        self.assertTrue(any(checkpoint["run"].get("context_compactions", 0) >= 2 and not checkpoint["nodes"]
                            for checkpoint in saved))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(model))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(client.requests))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(client.seen))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(saved))
        self.assertNotIn(PRIVATE_MARKER, audit_path.read_text(encoding="utf-8"))
        self.assertFalse(model["run"].get("loop_recovery_attempted", False))

    def test_thinking_is_never_queued_replayed_or_used_to_trigger_compaction(self):
        client = BatchClient([[call] for call in research_calls()],
                             reasoning={1: PRIVATE_MARKER + "x" * 50000})
        saved = []
        audit_path = self.root / "thinking-audit.txt"
        with patch("pko.scenario_map.agent.context_budget", return_value=(30000, 1024)), \
                audit_run(audit_path, "thinking-test"):
            model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=2,
                              checkpoint=saved.append)
        self.assertEqual(model["run"]["status"], "partial")
        assistant = next(message for message in client.requests[1] if message["role"] == "assistant")
        self.assertNotIn("reasoning_content", assistant)
        self.assertTrue(assistant["tool_calls"])
        self.assertNotIn(PRIVATE_MARKER, json.dumps(client.requests))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(client.seen))
        self.assertFalse(model["run"].get("context_summary_calls"))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(saved))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(model))
        self.assertNotIn(PRIVATE_MARKER, audit_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
