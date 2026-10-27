import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.scenario_map.agent import DeadlineClient, ResearchTools, _compact, run_agent
from pko.scenario_map.config import researcher_spec
from pko.scenario_map.runner import run_scenario
from pko.scenario_map.schema import MapState
from pko.scenario_map.sources import build_workspace
from scenario_script import EDGE_COUNT, NODE_COUNT, OPERATION_COUNT, SCENARIO, SPEC, ScriptedClient, research_calls, sources


class ScenarioAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = build_workspace(sources(), self.root / "sources")

    def test_one_session_builds_operation_map_across_sources(self):
        client = ScriptedClient()
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client)
        self.assertEqual(model["run"]["status"], "completed", model["run"])
        self.assertNotIn("systems", model)
        self.assertEqual(len(model["nodes"]), NODE_COUNT)
        self.assertEqual(len(model["edges"]), EDGE_COUNT)
        self.assertEqual(len(model["execution_reviews"]), 3)
        self.assertEqual(model["detail_review"]["operation_count"], OPERATION_COUNT)
        self.assertEqual(model["detail_review"]["status"], "covered")
        self.assertTrue(model["detail_review"]["registry_coverage_checked"])
        self.assertFalse(model["detail_review"]["semantic_completeness_verified"])
        operations = [operation for review in model["execution_reviews"] for operation in review["operations"]]
        self.assertEqual(len({operation["node_id"] for operation in operations}), OPERATION_COUNT)
        self.assertEqual({node["kind"] for node in model["nodes"]}, {"operation"})
        self.assertEqual(sum(node["continuation_mode"] == "choice" for node in model["nodes"]), 2)
        self.assertEqual(model["scenario"]["entry_operation_ids"], ["step-1"])
        self.assertEqual(model["scenario"]["terminal_operation_ids"], ["step-5", "step-10", "step-18"])
        self.assertEqual(sum(edge["kind"] == "branch" for edge in model["edges"]), 4)
        self.assertEqual(sum(edge["kind"] in ("call", "event") for edge in model["edges"]), 2)
        self.assertIn("Учебный детерминированный", model["summary"])
        self.assertNotIn("readiness", model)
        self.assertFalse(any("reissue" in node["name"] for node in model["nodes"]))
        self.assertEqual(len([m for m in client.seen if m["role"] == "system"]), 1)

    def test_fixture_operations_are_grounded_in_matching_statements(self):
        calls = research_calls()
        read_paths = {arguments["path"] for name, arguments in calls if name == "read_file"}
        reviews = [arguments for name, arguments in calls if name == "review_execution"]
        self.assertEqual(len(reviews), 3)
        for review in reviews:
            for operation in review["operations"]:
                for ref in operation["evidence"]:
                    self.assertIn(ref["path"], read_paths)
                    source_id, relative = ref["path"].split("/", 1)
                    lines = (sources()[int(source_id.split("-")[1]) - 1] / relative).read_text(encoding="utf-8").splitlines()
                    self.assertEqual(ref["quote"], "\n".join(lines[ref["line_start"] - 1:ref["line_end"]]))
        self.assertEqual(sum(len(review["operations"]) for review in reviews), OPERATION_COUNT)

    def test_step_budget_saves_partial_progress(self):
        saved = []
        model = run_agent(self.workspace, SCENARIO, SPEC, client=ScriptedClient(), max_steps=8,
                          checkpoint=lambda state: saved.append(state))
        self.assertEqual(model["run"]["status"], "partial")
        self.assertNotIn("systems", model)
        self.assertTrue(saved)
        self.assertEqual(saved[-1]["run"]["status"], "partial")

    def test_model_failure_and_interrupt_have_explicit_status(self):
        class Broken:
            spec = SPEC
            def chat(self, *a, **kw):
                raise RuntimeError("provider unavailable")
        model = run_agent(self.workspace, SCENARIO, SPEC, client=Broken())
        self.assertEqual(model["run"]["status"], "failed")
        class Interrupted:
            spec = SPEC
            def chat(self, *a, **kw):
                raise KeyboardInterrupt()
        model = run_agent(self.workspace, SCENARIO, SPEC, client=Interrupted())
        self.assertTrue(model["run"]["interrupted"])
        self.assertEqual(model["run"]["status"], "partial")

    def test_small_context_is_rejected_before_calling_model(self):
        from dataclasses import replace
        from unittest.mock import Mock
        client = Mock(spec=ScriptedClient)
        client.spec = SPEC
        model = run_agent(self.workspace, SCENARIO, replace(SPEC, context_window=2048), client=client)
        self.assertEqual(model["run"]["status"], "failed")
        client.chat.assert_not_called()
        self.assertIn("контекст", model["run"]["reason"])

    def test_deadline_client_disables_retries_and_limits_provider_timeout(self):
        from unittest.mock import Mock
        import time
        client = object.__new__(DeadlineClient)
        client.timeout = 180
        client.deadline = time.monotonic() + 5
        client._client = Mock()
        client._create({"model": "fixture", "messages": []})
        options = client._client.with_options.call_args.kwargs
        self.assertEqual(options["max_retries"], 0)
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 5)

    def test_cli_accepts_multiple_sources_and_only_variant_description(self):
        from pko.cli import _build_parser
        args = _build_parser().parse_args(["map-scenario", "--scenario-text", SCENARIO,
                                          "--source", "first", "--source", "second"])
        self.assertEqual(args.source, ["first", "second"])
        self.assertFalse(hasattr(args, "journey_text"))

    def test_cli_summary_settings_are_independent_of_research_mode(self):
        from pko.cli import _build_parser
        args = _build_parser().parse_args(["map-scenario", "--scenario-text", SCENARIO,
            "--source", "sample", "--thinking", "--summary-max-tokens", "12288", "--summary-mode", "standard"])
        self.assertTrue(args.thinking)
        self.assertEqual(args.summary_max_tokens, 12288)
        self.assertEqual(args.summary_mode, "standard")

    def test_invalid_summary_settings_fail_before_provider_request(self):
        from pko.errors import PkoError
        from unittest.mock import Mock
        for value in (True, 0, 511, 32769):
            client = Mock(spec=ScriptedClient)
            with self.subTest(value=value), self.assertRaises(PkoError):
                run_scenario(SCENARIO, sources(), self.root / "invalid", spec=SPEC,
                             client=client, summary_max_tokens=value)
            client.chat.assert_not_called()

    def test_compaction_preserves_state_and_does_not_leave_orphan_tools(self):
        state = MapState(self.workspace, SCENARIO)
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": SCENARIO},
                    {"role": "assistant", "content": "x" * 100000},
                    {"role": "tool", "content": "data", "tool_call_id": "previous"}]
        compact = _compact(messages, state, 24000)
        self.assertEqual(compact[:2], messages[:2])
        self.assertFalse(any(m["role"] == "tool" for m in compact))
        self.assertIn(SCENARIO, compact[-1]["content"])

    def test_unread_truncated_lines_cannot_be_cited(self):
        state = MapState(self.workspace, SCENARIO)
        tools = ResearchTools(state, self.workspace)
        with patch("pko.scenario_map.agent.MAX_TOOL_CHARS", 40):
            tools.answer({"id": "r", "function": {"name": "read_file", "arguments":
                         json.dumps({"path": "source-1/app.py"})}})
        coverage = state.snapshot()["read_coverage"]
        self.assertFalse(coverage, coverage)

    def test_compaction_fallback_fits_with_long_scenario_description(self):
        description = "Описание варианта. " * 1000
        state = MapState(self.workspace, description)
        state.model["scenario"]["title"] = description
        messages = [{"role": "system", "content": "rules"},
                    {"role": "user", "content": description},
                    {"role": "assistant", "content": "x" * 100000}]
        size = lambda items: sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) for item in items)
        budget = size(messages[:2]) + 1600
        compact = _compact(messages, state, budget)
        self.assertEqual(compact[:2], messages[:2])
        self.assertLessEqual(size(compact), budget)
        self.assertNotIn(description, compact[-1]["content"])
        self.assertIn("inspect_map", compact[-1]["content"])

    def test_cross_endpoint_override_does_not_forward_credentials(self):
        with patch.dict(os.environ, {
            "PKO_MATCHER_BASE_URL": SPEC.base_url, "PKO_MATCHER_API_KEY": "private-key",
            "PKO_MATCHER_CLIENT_CERT": "private.pem", "PKO_MATCHER_CLIENT_KEY": "",
            "PKO_SCENARIO_BASE_URL": "https://different.invalid/v1", "PKO_SCENARIO_API_KEY": "",
            "PKO_SCENARIO_MODEL": "fixture", "PKO_SCENARIO_CLIENT_CERT": "", "PKO_SCENARIO_CLIENT_KEY": ""}, clear=True):
            result = researcher_spec(thinking=False)
        self.assertEqual(result.api_key, "not-needed")
        self.assertEqual(result.client_cert, "")
        self.assertEqual(result.role, "scenario_researcher")
        self.assertIsNone(result.extra_body)

    def test_runner_writes_map_model_checkpoint_and_audit(self):
        output = self.root / "out"
        with patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            result = run_scenario(SCENARIO, sources(), output, spec=SPEC, client=ScriptedClient())
        self.assertEqual(result["run"]["status"], "completed", result["run"])
        self.assertTrue((output / "scenario_map.html").exists())
        self.assertTrue((output / "scenario_map.checkpoint.json").exists())
        self.assertEqual(json.loads((output / "scenario_map.json").read_text(encoding="utf-8"))["run"]["id"], result["run"]["id"])
        audit = next((output / "audit").glob("run_*.txt")).read_text(encoding="utf-8")
        self.assertIn("scenario_tool", audit)
        import re
        self.assertIsNone(re.search(r'"(?:thinking|reasoning_content|reasoning)"\s*:', audit),
                          "Audit must not contain private reasoning fields")
        final_event = json.loads(audit[audit.rfind('\n{')+1:])
        self.assertEqual((final_event['event'], final_event['result']), ('run_finished', 'completed'))

    def test_partial_run_is_reported_as_partial_in_audit_and_records_mode(self):
        from dataclasses import replace
        spec = replace(SPEC, extra_body={'chat_template_kwargs': {'enable_thinking':True}})
        output = self.root / 'partial'
        with patch.dict(os.environ, {'PKO_PHOENIX_ENABLED':'false'}):
            result = run_scenario(SCENARIO, sources(), output, spec=spec, client=ScriptedClient(), max_steps=2)
        self.assertEqual(result['run']['model_mode'], 'reasoning')
        audit = next((output/'audit').glob('run_*.txt')).read_text(encoding='utf-8')
        final_event = json.loads(audit[audit.rfind('\n{')+1:])
        self.assertEqual((final_event['event'], final_event['result']), ('run_finished', 'partial'))
        self.assertIn('"model_mode": "reasoning"', audit)

    def test_runner_rejects_output_that_would_exclude_its_sources(self):
        from pko.errors import PkoError
        from unittest.mock import Mock
        client = Mock(spec=ScriptedClient)
        with self.assertRaises(PkoError):
            run_scenario(SCENARIO, [self.root / "sources"], self.root,
                         spec=SPEC, client=client)
        client.chat.assert_not_called()

    def test_provider_failure_preserves_sanitized_hint(self):
        from pko.errors import LlmError
        class Broken:
            spec = SPEC
            def chat(self, *a, **kw):
                raise LlmError("Нет подключения", "Connection error. api_key=private-key")
        model = run_agent(self.workspace, SCENARIO, SPEC, client=Broken())
        self.assertIn("Connection error.", model["run"]["reason"])
        self.assertNotIn("private-key", model["run"]["reason"])

    def test_connection_exhaustion_keeps_existing_map_and_checkpoints(self):
        import httpx
        import openai
        first_node = next(index for index, (name, _) in enumerate(research_calls())
                          if name == 'upsert_step') + 1

        class Disconnected(ScriptedClient):
            requests = 0

            def chat(self, messages, **kwargs):
                if not kwargs.get('tools'):
                    return super().chat(messages, **kwargs)
                self.requests += 1
                if self.requests > first_node:
                    raise openai.APIConnectionError(request=httpx.Request('POST', 'http://model.invalid'))
                return super().chat(messages, **kwargs)

        client, saved, retries = Disconnected(), [], []
        with patch('pko.scenario_map.model_retry.time.sleep'):
            model = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                              checkpoint=saved.append,
                              on_event=lambda event, data: retries.append(data) if event == 'model_retry' else None)
        self.assertEqual(model['run']['status'], 'partial')
        self.assertEqual(len(model['nodes']), 1)
        self.assertEqual(client.requests, first_node + 4)
        self.assertEqual(len(retries), 3)
        self.assertIn('4 попыток', model['run']['reason'])
        self.assertEqual(saved[-1]['nodes'], model['nodes'])
        self.assertEqual(saved[-1]['run']['status'], 'partial')
        self.assertTrue(all(item['nodes'] == model['nodes'] for item in saved[-4:]))


if __name__ == "__main__":
    unittest.main()
