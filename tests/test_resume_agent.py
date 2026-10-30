"""Continuation uses committed public state, never a replayed provider transcript."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from pko.llm.client import ChatResult
from pko.scenario_map.agent import run_agent
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, ScriptedClient, research_calls, sources


def tool(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class BatchClient:
    """One provider batch; a second model call would be a test error."""
    spec = SPEC

    def __init__(self, calls):
        self.calls = list(calls)
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        if len(self.requests) > 1:
            raise AssertionError("Unexpected additional provider dispatch")
        return ChatResult(text="PUBLIC_DISCARDED_TRANSCRIPT", usage={},
                          reasoning_content="PRIVATE_RESUME_SENTINEL",
                          tool_calls=[tool(name, args, f"original-call-{i}")
                                      for i, (name, args) in enumerate(self.calls)])


class CapturingScriptedClient(ScriptedClient):
    def __init__(self, calls=None):
        super().__init__(calls)
        self.first_messages = None
        self.request_count = 0

    def chat(self, messages, **kwargs):
        self.request_count += 1
        if self.first_messages is None:
            self.first_messages = deepcopy(messages)
        return super().chat(messages, **kwargs)


class ResumeAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = build_workspace(sources(), Path(self.tmp.name) / "snapshot")
        self.saved = None

    def save(self, checkpoint):
        self.saved = deepcopy(checkpoint)

    def scope(self, title="Принятая область"):
        return {"title": title, "start": "Получено обращение", "end": "Получен результат"}

    def checkpoint_after_first_action(self):
        calls = research_calls()
        stop = next(i for i, (name, _) in enumerate(calls) if name == "upsert_step")
        client = CapturingScriptedClient(calls[:stop + 1])
        result = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                           max_steps=stop + 1, checkpoint=self.save)
        self.assertEqual(result["run"]["stop_kind"], "step_limit")
        self.assertEqual(len(result["nodes"]), 1)
        self.assertIn("_resume", self.saved)
        return deepcopy(self.saved), calls[stop + 1:]

    def test_abrupt_stop_between_tools_commits_first_and_discards_pending_batch(self):
        original = BatchClient([("set_scope", self.scope()),
                                ("set_scope", self.scope("НЕПРИМЕНЁННАЯ ОБЛАСТЬ"))])

        def crash(event, data):
            if event == "tool":
                raise SystemExit("Simulated process termination after one tool")

        with self.assertRaises(SystemExit):
            run_agent(self.workspace, SCENARIO, SPEC, client=original, max_steps=1,
                      checkpoint=self.save, on_event=crash)
        checkpoint = deepcopy(self.saved)
        self.assertEqual(checkpoint["scenario"]["title"], "Принятая область")
        self.assertNotIn("PRIVATE_RESUME_SENTINEL", json.dumps(checkpoint))
        self.assertNotIn("original-call-1", json.dumps(checkpoint))

        continuation = BatchClient([("save_research_note", {
            "kind": "question", "finding": "Уточнить условие продолжения",
            "next_action": "Прочитать обработчик"})])
        result = run_agent(self.workspace, SCENARIO, SPEC, client=continuation,
                           resume_checkpoint=checkpoint, max_steps=1, checkpoint=self.save)
        self.assertEqual(result["scenario"]["title"], "Принятая область")
        first = continuation.requests[0]
        self.assertEqual(first[0]["role"], "system")
        self.assertFalse(any(message.get("role") == "tool" or message.get("tool_calls") for message in first))
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertIn("Принятая область", serialized)
        self.assertNotIn("PRIVATE_RESUME_SENTINEL", serialized)
        self.assertNotIn("PUBLIC_DISCARDED_TRANSCRIPT", serialized)
        self.assertNotIn("НЕПРИМЕНЁННАЯ ОБЛАСТЬ", serialized)
        self.assertNotIn("original-call-", serialized)

    def test_real_script_continues_with_same_nodes_and_existing_read_authority(self):
        checkpoint, remaining = self.checkpoint_after_first_action()
        original = deepcopy(checkpoint["nodes"][0])
        client = CapturingScriptedClient(remaining)
        result = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                           resume_checkpoint=checkpoint, checkpoint=self.save)
        self.assertEqual(result["run"]["status"], "completed", result["run"])
        self.assertEqual(len(result["nodes"]), 18)
        self.assertEqual(len({node["id"] for node in result["nodes"]}), 18)
        self.assertEqual(result["nodes"][0], original)
        self.assertEqual(result["read_coverage"], checkpoint["read_coverage"])
        self.assertFalse(any(m.get("role") == "tool" or m.get("tool_calls") for m in client.first_messages))
        self.assertNotIn("_resume", result, "Resume internals must not leak into the public map")

    def test_deleted_identifier_is_not_reused_after_resume(self):
        calls = research_calls()
        first = next(i for i, (name, _) in enumerate(calls) if name == "upsert_step")
        prefix = deepcopy(calls[:first + 2])
        prefix[0][1]["entry_operation_ids"] = []
        prefix[0][1]["terminal_operation_ids"] = []
        prefix.append(("remove_element", {"kind": "step", "id": "step-2"}))
        result = run_agent(self.workspace, SCENARIO, SPEC,
                           client=CapturingScriptedClient(prefix), max_steps=len(prefix), checkpoint=self.save)
        self.assertEqual([node["id"] for node in result["nodes"]], ["step-1"])
        next_node = deepcopy(calls[first][1])
        next_node["name"] = "Следующая самостоятельная работа"
        next_node.pop("id", None)
        resumed = run_agent(self.workspace, SCENARIO, SPEC,
                            client=BatchClient([("upsert_step", next_node)]),
                            resume_checkpoint=deepcopy(self.saved), max_steps=1)
        self.assertEqual([node["id"] for node in resumed["nodes"]], ["step-1", "step-3"])

    def test_resume_gets_new_attempt_budget_and_keeps_cumulative_steps(self):
        first = run_agent(self.workspace, SCENARIO, SPEC,
                          client=BatchClient([("set_scope", self.scope())]),
                          max_steps=1, max_seconds=30, checkpoint=self.save)
        self.assertEqual(first["run"]["stop_kind"], "step_limit")
        resumed = run_agent(self.workspace, SCENARIO, SPEC,
                            client=BatchClient([("set_scope", self.scope("Продолженный вариант"))]),
                            resume_checkpoint=deepcopy(self.saved), max_steps=1, max_seconds=60)
        self.assertEqual(resumed["run"]["steps"], 1)
        self.assertEqual(resumed["run"]["total_steps"], 2)
        self.assertEqual(resumed["run"]["max_seconds"], 60)
        self.assertEqual(resumed["scenario"]["title"], "Продолженный вариант")

    def test_checkpoint_write_failure_prevents_next_tool_and_provider_call(self):
        client = BatchClient([("set_scope", self.scope()),
                              ("set_scope", self.scope("Не должно исполниться"))])

        def failing_save(checkpoint):
            if checkpoint["scenario"]["title"]:
                raise OSError("checkpoint storage unavailable")
            self.save(checkpoint)

        result = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                           max_steps=3, checkpoint=failing_save)
        self.assertEqual(result["run"]["stop_kind"], "checkpoint_error")
        self.assertEqual(result["scenario"]["title"], "Принятая область")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(self.saved["scenario"]["title"], "")

    def test_committed_finish_resumes_without_research_provider_call(self):
        complete = run_agent(self.workspace, SCENARIO, SPEC,
                             client=ScriptedClient(), checkpoint=self.save)
        self.assertEqual(complete["run"]["status"], "completed")
        client = Mock(spec=ScriptedClient)
        client.spec = SPEC
        client.chat.side_effect = AssertionError("Finished research must not run again")
        resumed = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                            resume_checkpoint=deepcopy(self.saved))
        client.chat.assert_not_called()
        self.assertEqual(resumed["nodes"], complete["nodes"])
        self.assertEqual(resumed["edges"], complete["edges"])
        self.assertEqual(resumed["run"]["status"], "completed")

    def test_interrupt_after_accepted_finish_preserves_research_completion(self):
        def interrupt(event, data):
            if event == "tool" and data.get("name") == "finish":
                raise KeyboardInterrupt()

        stopped = run_agent(self.workspace, SCENARIO, SPEC, client=ScriptedClient(),
                            checkpoint=self.save, on_event=interrupt)
        self.assertEqual(stopped["run"]["stop_kind"], "cancelled")
        self.assertEqual(self.saved["run"]["research_completion"]["status"], "completed")
        client = Mock(spec=ScriptedClient)
        client.spec = SPEC
        client.chat.side_effect = AssertionError("Accepted research finish must survive later cancellation")
        resumed = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                            resume_checkpoint=deepcopy(self.saved))
        client.chat.assert_not_called()
        self.assertEqual(resumed["run"]["status"], "completed")
        self.assertEqual(resumed["nodes"], stopped["nodes"])

    def test_loading_source_snapshot_does_not_grant_unread_citation_authority(self):
        run_agent(self.workspace, SCENARIO, SPEC,
                  client=BatchClient([("set_scope", self.scope())]),
                  max_steps=1, checkpoint=self.save)
        result = run_agent(self.workspace, SCENARIO, SPEC,
            client=BatchClient([("save_research_note", {
                "kind": "fact", "finding": "Непрочитанное утверждение",
                "evidence": [{"path": "source-1/app.py", "line_start": 1, "line_end": 1}]})]),
            resume_checkpoint=deepcopy(self.saved), max_steps=1)
        self.assertEqual(result["research_notes"], [])
        self.assertEqual(result["read_coverage"], [])
        self.assertTrue(result["run"].get("recent_rejections"))
        self.assertEqual(result["run"]["recent_rejections"][-1]["code"], "invalid_evidence")

    def test_corrupt_checkpoint_is_rejected_before_provider_dispatch(self):
        checkpoint, _ = self.checkpoint_after_first_action()
        checkpoint["nodes"][0]["description"] = "Altered without updating the receipt"
        client = Mock(spec=ScriptedClient)
        client.spec = SPEC
        with self.assertRaises(Exception):
            run_agent(self.workspace, SCENARIO, SPEC, client=client, resume_checkpoint=checkpoint)
        client.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
