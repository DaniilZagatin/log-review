"""Public summary requests have independent output budgets and no replayed thinking."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from pko.audit import AuditedClient, audit_run
from pko.errors import LlmError
from pko.llm.client import ChatClient, ChatResult
from pko.llm.registry import ModelSpec
from pko.scenario_map.agent import run_agent
from test_scenario_schema import workspace


PRIVATE = "PRIVATE_PROVIDER_THOUGHT_61933"


def public_summary():
    return ChatResult(text=json.dumps({
        "project_and_scenario": "Проект обрабатывает обращения.",
        "completed_work": ["Прочитан обработчик."], "key_findings": [],
        "open_questions": ["Нужно исследовать продолжение."],
        "next_actions": ["Проследить следующий вызов."], "references": []}, ensure_ascii=False),
        usage={"completion_tokens": 100}, finish_reason="stop")


class SummaryRuntimeTests(unittest.TestCase):
    def spec(self, configured=True):
        return ModelSpec(role="scenario_researcher", base_url="http://test.invalid", model="test",
                         api_key="", context_window=64000,
                         extra_body={"chat_template_kwargs": {"enable_thinking": True, "other": 1}} if configured else None)

    def client(self, text="{}", finish="stop", configured=True):
        client = ChatClient.__new__(ChatClient)
        client.spec = self.spec(configured)
        message = SimpleNamespace(content=text, tool_calls=None, reasoning_content=PRIVATE)
        response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                                   usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34, total_tokens=46))
        client._create = Mock(return_value=response)
        return client

    def test_summary_mode_changes_only_configured_flag_and_never_mutates_main_mode(self):
        client = self.client()
        original = deepcopy(client.spec.extra_body)
        result = client.chat([{"role": "user", "content": "public facts"}], enable_thinking=False, max_tokens=8192)
        self.assertEqual(result.finish_reason, "stop")
        self.assertFalse(client._create.call_args.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(client.spec.extra_body, original)
        client.chat([{"role": "user", "content": "continue research"}])
        self.assertTrue(client._create.call_args.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        plain = self.client(configured=False)
        plain.chat([{"role": "user", "content": "facts"}], enable_thinking=False)
        self.assertIsNone(plain._create.call_args.kwargs["extra_body"])
        for malformed in (None, "provider-value", False):
            plain.spec = replace(plain.spec, extra_body={"chat_template_kwargs": malformed})
            plain.chat([{"role": "user", "content": "facts"}], enable_thinking=False)
            self.assertEqual(plain._create.call_args.kwargs["extra_body"], {"chat_template_kwargs": malformed})

    def test_empty_reply_retains_safe_finish_and_usage_diagnostics(self):
        client = self.client(text="", finish="length")
        with self.assertRaises(LlmError) as caught:
            client.chat([{"role": "user", "content": "public facts"}])
        self.assertEqual(caught.exception.finish_reason, "length")
        self.assertEqual(caught.exception.usage["completion_tokens"], 34)
        self.assertNotIn(PRIVATE, str(caught.exception.__dict__))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.txt"
            with audit_run(path, "summary-error"):
                with self.assertRaises(LlmError):
                    AuditedClient(client).chat([{"role": "user", "content": "public facts"}])
            audit = path.read_text(encoding="utf-8")
        self.assertIn('"finish_reason": "length"', audit)
        self.assertIn('"completion_tokens": 34', audit)
        self.assertNotIn(PRIVATE, audit)

    def test_summary_budget_is_independent_and_request_is_bounded_once(self):
        for mode in ("standard", "inherit"):
            with self.subTest(mode=mode):
                spec = self.spec()
                class Client:
                    def __init__(self):
                        self.spec = spec
                        self.summary_options = []
                        self.main_count = 0
                    def chat(inner, messages, **kwargs):
                        if not kwargs.get("tools"):
                            inner.summary_options.append(deepcopy(kwargs))
                            self.assertNotIn(PRIVATE, json.dumps(messages))
                            return public_summary()
                        inner.main_count += 1
                        return ChatResult(text="Публичное наблюдение. " * 4000 if inner.main_count == 1 else "",
                            usage={}, reasoning_content=PRIVATE, tool_calls=[{
                                "id": f"read-{inner.main_count}", "type": "function", "function": {
                                    "name": "read_file", "arguments": '{"path":"source-1/channel.py"}'}}])
                client = Client()
                with patch("pko.scenario_map.agent.context_budget", return_value=(30000, 1024)):
                    model = run_agent(workspace(1), "Обработка обращения", spec, client=client,
                                      max_steps=2, summary_max_tokens=8192, summary_mode=mode)
                self.assertEqual(len(client.summary_options), 1, model["run"])
                options = client.summary_options[0]
                self.assertEqual(options["max_tokens"], 8192)
                self.assertIsNone(options["tools"])
                if mode == "standard":
                    self.assertIs(options["enable_thinking"], False)
                else:
                    self.assertNotIn("enable_thinking", options)
                self.assertEqual(model["run"]["context_summaries"], 1)
                self.assertEqual(model["run"]["context_summary_calls"], 1)
                self.assertEqual(model["run"]["summary_effective_mode"], "standard" if mode == "standard" else "reasoning")
                self.assertNotIn(PRIVATE, json.dumps(model))

    def test_invalid_summary_configuration_is_rejected_before_dispatch(self):
        for options in ({"summary_max_tokens": 0}, {"summary_max_tokens": True},
                        {"summary_max_tokens": 32769}, {"summary_mode": "retry_forever"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                run_agent(workspace(1), "Сценарий", self.spec(), client=Mock(), **options)


if __name__ == "__main__":
    unittest.main()
