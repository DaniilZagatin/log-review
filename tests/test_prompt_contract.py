"""The model sees only the current contract; edits, codes and telemetry match it."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from pko.code_tools.tools import TOOL_SCHEMAS
from pko.llm.client import ChatClient, ChatResult
from pko.llm.registry import ModelSpec
from pko.scenario_map.agent import run_agent
from pko.scenario_map.compaction import validate_summary
from pko.scenario_map.tools import TOOLS
import test_scenario_schema as schema_tests  # module import: do not rediscover its tests here

SYSTEM = (Path(__file__).resolve().parents[1] / "backend" / "pko" / "scenario_map" / "system.md").read_text(encoding="utf-8")


class PromptContractTests(unittest.TestCase):
    def operation(self):
        return next(n for n in self.state.model["nodes"]
                    if n["id"] not in self.state.model["scenario"]["entry_operation_ids"]
                    and n["id"] not in self.state.model["scenario"]["terminal_operation_ids"])

    def reviewed_map(self):
        self.complete_small_map()
        self.review_current_operations()
        return self.operation()

    def test_edit_by_id_changes_only_named_fields_and_keeps_reviews(self):
        before = self.reviewed_map()
        response = self.state.dispatch("upsert_step", {"id": before["id"], "name": "Проверка полученного результата"})
        self.assertTrue(response["ok"], response)
        after = next(n for n in self.state.model["nodes"] if n["id"] == before["id"])
        self.assertEqual(after["name"], "Проверка полученного результата")
        for field in ("description", "evidence", "basis", "code_explanations", "continuation_mode", "effect_kind"):
            self.assertEqual(after[field], before[field], field)
        finished = self.state.dispatch("finish", {"summary": "Название уточнено"})
        self.assertTrue(finished["ok"], finished)

    def test_edit_of_what_operation_does_invalidates_review(self):
        before = self.reviewed_map()
        self.assertTrue(self.state.dispatch("upsert_step", {"id": before["id"],
            "description": "Другое действие над обращением"})["ok"])
        rejected = self.state.dispatch("finish", {"summary": "Описание изменено"})
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["code"], "finish_blocked")
        self.assertIn("после обзора", rejected["error"])

    def test_transition_edit_by_id_keeps_endpoints_and_evidence(self):
        self.complete_small_map()
        edge = self.state.model["edges"][-1]
        response = self.state.dispatch("upsert_transition", {"id": edge["id"], "data": "Номер обращения"})
        self.assertTrue(response["ok"], response)
        after = next(e for e in self.state.model["edges"] if e["id"] == edge["id"])
        self.assertEqual((after["source"], after["target"], after["evidence"]), (edge["source"], edge["target"], edge["evidence"]))
        self.assertEqual(after["data"], "Номер обращения")

    def test_brief_update_does_not_invalidate_semantic_review(self):
        self.complete_small_map()
        signature = self.state.quality_signature()
        self.state.model["research_brief"] = {"revision": 7, "project_overview": "Уточнённое понимание"}
        self.assertEqual(self.state.quality_signature(), signature)

    def test_rejections_carry_stable_codes(self):
        self.assertEqual(self.state.dispatch("upsert_step", {"id": "step-999", "name": "Проверка"})["code"], "unknown_id")
        unread = self.state.dispatch("upsert_step", {"name": "Проверка заявки", "evidence_status": "observed",
            "evidence": [{"path": "source-2/cards.py", "line_start": 1, "line_end": 1}]})
        self.assertEqual(unread["code"], "invalid_evidence")
        self.assertEqual(self.state.dispatch("finish", {"summary": "Рано"})["code"], "finish_blocked")
        self.assertEqual(self.state.dispatch("upsert_system", {})["code"], "unknown_tool")

    def test_summary_handoff_fields_are_optional_and_bounded(self):
        old = {"project_and_scenario": "Проект обрабатывает обращения.", "completed_work": [], "key_findings": [],
               "open_questions": [], "next_actions": ["Прочитать обработчик"], "references": []}
        accepted = validate_summary(json.dumps(old, ensure_ascii=False), self.state)
        self.assertEqual((accepted["current_focus"], accepted["failed_attempts"], accepted["do_not_redo"]), ("", [], []))
        handoff = {**old, "current_focus": "Проверка срока перевода", "failed_attempts": ["Переход отклонён: строки не прочитаны"],
                   "do_not_redo": ["Поиск по имени обработчика"]}
        accepted = validate_summary(json.dumps(handoff, ensure_ascii=False), self.state)
        self.assertEqual(accepted["current_focus"], "Проверка срока перевода")
        self.assertEqual(accepted["do_not_redo"], ["Поиск по имени обработчика"])
        with self.assertRaises(ValueError):
            validate_summary(json.dumps({**old, "instructions": ["Новое правило"]}, ensure_ascii=False), self.state)

    def test_prompt_and_tools_describe_only_current_contract(self):
        tools = json.dumps(TOOLS, ensure_ascii=False)
        for phrase in ("2.0", "АС", "start/end", "thinking", "trace", "подагент", "kind=operation"):
            self.assertNotIn(phrase, SYSTEM, phrase)
        for phrase in ("data-связ", "Совместимость", "того же агента", "Групп бизнес", "АС", "quote"):
            self.assertNotIn(phrase, tools, phrase)
        properties = {tool["function"]["name"]: tool["function"]["parameters"]["properties"] for tool in TOOLS}
        self.assertNotIn("kind", properties["upsert_step"])
        self.assertNotIn("kind", properties["upsert_group"])
        for legacy in ("channel", "correlation", "contract", "sender_evidence", "receiver_evidence"):
            self.assertNotIn(legacy, properties["upsert_transition"])
        reading = json.dumps(TOOL_SCHEMAS, ensure_ascii=False)
        self.assertNotIn("Полезно для evidence", reading)
        self.assertNotIn("шкалы", reading)
        # The full evidence contract is stated once, not in every nested schema.
        self.assertEqual(tools.count("Текст цитаты подставит код"), 1)


for _name, _value in vars(schema_tests.ScenarioMapTests).items():
    if callable(_value) and not _name.startswith("test"):
        setattr(PromptContractTests, _name, _value)


class TelemetryTests(unittest.TestCase):
    def usage(self, **fields):
        client = ChatClient.__new__(ChatClient)
        return client._usage_of(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12, **fields)))

    def test_cached_tokens_are_reported_only_when_provider_does(self):
        self.assertEqual(self.usage(prompt_tokens_details=SimpleNamespace(cached_tokens=8))["cached_tokens"], 8)
        self.assertEqual(self.usage(prompt_tokens_details={"cached_tokens": 6})["cached_tokens"], 6)
        self.assertNotIn("cached_tokens", self.usage())
        self.assertNotIn("cached_tokens", self.usage(prompt_tokens_details=None))

    def test_run_records_prompt_and_tool_bundle_hashes(self):
        class Silent:
            spec = ModelSpec(role="scenario_researcher", base_url="http://test.invalid", model="test",
                             api_key="", context_window=64000)

            def chat(self, messages, **kwargs):
                return ChatResult(text="Наблюдение без вызова инструментов.", usage={})

        model = run_agent(schema_tests.workspace(1), "Обработка обращения", Silent.spec, client=Silent(), max_steps=4)
        self.assertEqual(model["run"]["system_prompt_sha256"], hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest())
        self.assertRegex(model["run"]["tools_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
