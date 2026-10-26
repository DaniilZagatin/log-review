"""Timeout contracts for queued model requests; no external endpoint is called."""
import contextlib
import inspect
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pko.cli import _build_parser
from pko.errors import PkoError
from pko.scenario_map.agent import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MODEL_TIMEOUT,
    DeadlineClient,
    run_agent,
)
from pko.scenario_map.runner import command_main, run_scenario
from pko.scenario_map.schema import MapState
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, sources


class ScenarioTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = build_workspace(sources(), self.root / "sources")
        environment = patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"})
        environment.start()
        self.addCleanup(environment.stop)

    def arguments(self, *extra):
        return _build_parser().parse_args([
            "map-scenario", "--scenario-text", SCENARIO,
            "--source", str(sources()[0]), "--out", str(self.root / "out"),
            "--quiet", *extra,
        ])

    def test_defaults_allow_fifteen_minute_request_and_two_hour_run(self):
        self.assertEqual(DEFAULT_MODEL_TIMEOUT, 900)
        self.assertEqual(DEFAULT_MAX_SECONDS, 7200)
        arguments = self.arguments()
        self.assertEqual(arguments.model_timeout, 900)
        self.assertEqual(arguments.max_seconds, 7200)
        for function in (run_agent, run_scenario):
            parameters = inspect.signature(function).parameters
            self.assertEqual(parameters["model_timeout"].default, 900)
            self.assertEqual(parameters["max_seconds"].default, 7200)

    def test_cli_forwards_custom_request_and_run_limits(self):
        arguments = self.arguments("--model-timeout", "1800", "--max-seconds", "14400")
        with patch("pko.scenario_map.runner.run_scenario", return_value={"run": {"status": "completed"}}) as runner:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(command_main(arguments), 0)
        self.assertEqual(runner.call_args.kwargs["model_timeout"], 1800)
        self.assertEqual(runner.call_args.kwargs["max_seconds"], 14400)

    def test_runner_forwards_custom_limits_and_preserves_run_metadata(self):
        model = MapState(self.workspace, SCENARIO).snapshot()
        model["run"].update(status="partial", model_timeout_seconds=1800, max_seconds=14400)
        with patch("pko.scenario_map.runner.run_agent", return_value=model) as agent:
            result = run_scenario(SCENARIO, sources(), self.root / "out", spec=SPEC,
                                  model_timeout=1800, max_seconds=14400)
        self.assertEqual(agent.call_args.kwargs["model_timeout"], 1800)
        self.assertEqual(agent.call_args.kwargs["max_seconds"], 14400)
        self.assertEqual(result["run"]["model_timeout_seconds"], 1800)
        self.assertEqual(result["run"]["max_seconds"], 14400)

    def test_agent_applies_timeout_to_its_real_client_without_network(self):
        for limits, expected_wait, expected_budget in (
            ({}, 900, 7200),
            ({"model_timeout": 1800, "max_seconds": 14400}, 1800, 14400),
            ({"model_timeout": 1800, "max_seconds": 120}, 120, 120),
        ):
            with self.subTest(limits=limits):
                sdk = Mock()
                sdk.with_options.return_value = sdk
                sdk.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                        content="Продолжаю исследование.", tool_calls=None))],
                    usage=None,
                )

                def initialize(client):
                    client._client = sdk

                with patch("pko.llm.client.ChatClient.__post_init__", initialize):
                    with patch("pko.scenario_map.agent.time.monotonic", return_value=1000):
                        model = run_agent(self.workspace, SCENARIO, SPEC, max_steps=1, **limits)
                self.assertEqual(model["run"]["status"], "partial", model["run"])
                self.assertEqual(model["run"]["model_timeout_seconds"], limits.get("model_timeout", 900))
                self.assertEqual(model["run"]["max_seconds"], expected_budget)
                sdk.with_options.assert_called_once_with(timeout=expected_wait, max_retries=0)
                sdk.chat.completions.create.assert_called_once()

    def test_wait_is_recalculated_against_remaining_deadline_before_each_request(self):
        client = object.__new__(DeadlineClient)
        client.timeout = 1800
        client.deadline = 5000
        client._client = Mock()
        with patch("pko.scenario_map.agent.time.monotonic", side_effect=[1000, 4900]):
            client._create({"model": "fixture", "messages": []})
            client._create({"model": "fixture", "messages": []})
        self.assertEqual([call.kwargs["timeout"] for call in client._client.with_options.call_args_list], [1800, 100])
        self.assertTrue(all(call.kwargs["max_retries"] == 0 for call in client._client.with_options.call_args_list))

    def test_expired_deadline_never_dispatches_request(self):
        client = object.__new__(DeadlineClient)
        client.timeout = 1800
        client.deadline = 1000
        client._client = Mock()
        with patch("pko.scenario_map.agent.time.monotonic", return_value=1000):
            with self.assertRaises(TimeoutError):
                client._create({"model": "fixture", "messages": []})
        client._client.with_options.assert_not_called()

    def test_invalid_agent_limits_are_rejected_before_state_or_client_creation(self):
        for parameter in ("model_timeout", "max_seconds"):
            for value in (0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(parameter=parameter, value=value):
                    with patch("pko.scenario_map.agent.MapState") as state:
                        with patch("pko.scenario_map.agent.DeadlineClient") as client:
                            with self.assertRaises(ValueError):
                                run_agent(object(), SCENARIO, SPEC, **{parameter: value})
                    state.assert_not_called()
                    client.assert_not_called()

    def test_invalid_runner_limits_are_rejected_before_reading_sources(self):
        for parameter in ("model_timeout", "max_seconds"):
            for value in (0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(parameter=parameter, value=value):
                    with patch("pko.scenario_map.runner.build_workspace") as reader:
                        with patch("pko.scenario_map.runner.run_agent") as agent:
                            with self.assertRaises(PkoError):
                                run_scenario(SCENARIO, sources(), self.root / "out", spec=SPEC,
                                             **{parameter: value})
                    reader.assert_not_called()
                    agent.assert_not_called()

    def test_cli_rejects_nonpositive_and_nonfinite_timeouts_before_source_read(self):
        for option in ("--model-timeout", "--max-seconds"):
            for value in ("0", "-1", "nan", "inf", "-inf"):
                with self.subTest(option=option, value=value):
                    arguments = self.arguments(f"{option}={value}")
                    with patch("pko.scenario_map.runner.build_workspace") as reader:
                        with patch("pko.scenario_map.runner.run_agent") as agent:
                            with self.assertRaises(PkoError):
                                command_main(arguments)
                    reader.assert_not_called()
                    agent.assert_not_called()

    def test_cli_rejects_non_numeric_timeout(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                self.arguments("--model-timeout", "forever")
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
