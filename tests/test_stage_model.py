"""The bounded grouping request: one addressed repair, every physical attempt counted."""
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pko.errors import LlmError
from pko.llm.client import ChatResult
from pko.stages.model import BudgetExhausted, PacketTooLarge, RequestBudget, StageModel
from scenario_script import SPEC
from stage_script import ScriptedStageClient

TOOL = {"name": "submit_items", "parameters": {"type": "object", "properties": {
    "items": {"type": "array", "items": {"type": "object"}}}}}


def validate_items(arguments):
    """Accept named items and reject the rest separately, like the grouping check."""
    accepted, errors = [], []
    for item in arguments["items"]:
        if not item.get("name"):
            errors.append(f"{item['id']}: нужно название")
        else:
            accepted.append(dict(item))
    return accepted, errors


def unavailable(*_):
    raise LlmError("Модель недоступна", "400 Bad Request")


class StageModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rules = Path(temporary.name)
        (self.rules / "common.md").write_text("Общие правила.", encoding="utf-8")
        (self.rules / "items.md").write_text("Верни элементы.", encoding="utf-8")

    def engine(self, client, requests=20, deadline=None):
        budget = RequestBudget(requests, time.monotonic() + 60 if deadline is None else deadline)
        return StageModel(client, budget, self.rules)

    def ask(self, stage):
        return stage.ask("items", {"purpose": "test"}, TOOL, validate_items, key=lambda row: row["id"])

    def test_repair_fault_keeps_accepted_items_and_public_reason(self):
        client = ScriptedStageClient({"submit_items": [
            lambda *_: {"items": [{"id": "i-1", "name": "Принято"}, {"id": "i-2", "name": ""}]}, unavailable]})
        stage = self.engine(client)
        accepted, errors = self.ask(stage)
        self.assertEqual([row["id"] for row in accepted], ["i-1"])
        self.assertTrue(any(error.startswith("i-2:") for error in errors))
        self.assertTrue(any("LlmError" in error for error in errors))
        self.assertEqual(stage.budget.used, 2)

    def test_empty_repair_keeps_items_accepted_first_and_reports_the_rest(self):
        client = ScriptedStageClient({"submit_items": [
            lambda *_: {"items": [{"id": "i-1", "name": "Принято"}, {"id": "i-2", "name": ""}]},
            lambda *_: {"items": []}]})
        accepted, errors = self.ask(self.engine(client))
        self.assertEqual([row["id"] for row in accepted], ["i-1"])
        self.assertTrue(any(error.startswith("i-2:") for error in errors))

    def test_repair_sees_exactly_the_rejected_items(self):
        seen = []

        def repaired(payload, messages):
            seen.append(messages[-1]["content"])
            return {"items": [{"id": "i-2", "name": "Исправлено"}]}
        client = ScriptedStageClient({"submit_items": [
            lambda *_: {"items": [{"id": "i-1", "name": "Принято"}, {"id": "i-2", "name": ""}]}, repaired]})
        accepted, errors = self.ask(self.engine(client))
        self.assertEqual(errors, [])
        self.assertEqual({row["id"] for row in accepted}, {"i-1", "i-2"})
        self.assertIn("i-2: нужно название", seen[0])
        self.assertNotIn("i-1:", seen[0])

    def test_transport_retries_count_every_physical_request(self):
        calls = []

        class Failing:
            spec = SPEC

            def chat(self, *_args, **_kwargs):
                calls.append(1)
                raise ConnectionError("connection reset")

        stage = self.engine(Failing(), requests=2)
        with patch("pko.scenario_map.model_retry.retry_delay", return_value=0), \
                patch("pko.scenario_map.model_retry.time.sleep"), self.assertRaises(BudgetExhausted):
            self.ask(stage)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stage.budget.snapshot(), {"max_requests": 2, "used_requests": 2, "by_stage": {"items": 2}})

    def test_expired_deadline_does_not_dispatch(self):
        client = ScriptedStageClient()
        stage = self.engine(client, deadline=time.monotonic() - 1)
        with self.assertRaises(BudgetExhausted):
            self.ask(stage)
        self.assertFalse(client.requests)
        self.assertEqual(stage.budget.used, 0)

    def test_budget_exhausted_during_repair_retains_accepted_items(self):
        client = ScriptedStageClient({"submit_items": lambda *_: {
            "items": [{"id": "i-1", "name": "Принято"}, {"id": "i-2", "name": ""}]}})
        result, errors = self.ask(self.engine(client, requests=1))
        self.assertEqual([item["id"] for item in result], ["i-1"])
        self.assertTrue(any("Исчерпан" in error for error in errors))
        self.assertEqual(len(client.requests), 1)

    def test_answer_as_fenced_json_text_is_accepted(self):
        class Fenced:
            spec = SPEC

            def chat(self, messages, **kwargs):
                return ChatResult(text='```json\n{"items": [{"id": "i-1", "name": "Принято"}]}\n```', usage={})
        accepted, errors = self.ask(self.engine(Fenced()))
        self.assertEqual((accepted, errors), ([{"id": "i-1", "name": "Принято"}], []))

    def test_grouping_answer_is_requested_without_reasoning_and_with_a_large_limit(self):
        # Both runs of 28 October lost their stages: reasoning used all 8192 answer tokens.
        seen = []

        class Recording(ScriptedStageClient):
            spec = replace(SPEC, context_window=524288)

            def chat(self, messages, **kwargs):
                seen.append(kwargs)
                return super().chat(messages, **kwargs)

        stage = self.engine(Recording({"submit_items": lambda *_: {"items": [{"id": "i-1", "name": "Принято"}]}}))
        self.ask(stage)
        self.assertIs(seen[0]["enable_thinking"], False)
        self.assertGreaterEqual(seen[0]["max_tokens"], 32768)

    def test_packet_that_does_not_fit_the_window_is_refused_before_any_request(self):
        client = ScriptedStageClient()
        stage = self.engine(client)
        with patch.object(StageModel, "_limits", lambda self, schema: (100, 50)), self.assertRaises(PacketTooLarge):
            stage.ask("items", {"purpose": "х" * 500}, TOOL, validate_items)
        self.assertFalse(client.requests)
        self.assertEqual(stage.budget.used, 0)


if __name__ == "__main__":
    unittest.main()
