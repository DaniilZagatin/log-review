"""Retry policy checks without a provider, network traffic or real waiting."""

import copy
import ssl
import time
import unittest
from email.utils import formatdate
from unittest.mock import Mock, patch

import httpx
import openai

from pko.errors import LlmError
from pko.scenario_map import model_retry


def status_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    response = httpx.Response(
        status, headers=headers,
        request=httpx.Request("POST", "https://model.invalid/v1/chat/completions"),
    )
    return openai.APIStatusError("Provider request failed", response=response, body=None)


def connection_error():
    return openai.APIConnectionError(
        request=httpx.Request("POST", "https://model.invalid/v1/chat/completions")
    )


def wrapped(error):
    result = LlmError("Не удалось получить ответ модели.")
    result.__cause__ = error
    return result


class ModelRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.sleeps = []
        self.messages = [
            {"role": "system", "content": "Research the selected scenario"},
            {"role": "user", "content": "Issue a new debit card"},
            {"role": "assistant", "tool_calls": [{"id": "read-1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"app.py"}'}}]},
            {"role": "tool", "tool_call_id": "read-1", "content": "Existing evidence"},
        ]
        self.addCleanup(patch.stopall)
        patch.object(model_retry.time, "monotonic", side_effect=lambda: self.now).start()
        self.sleep = patch.object(model_retry.time, "sleep", side_effect=self.advance).start()
        patch.object(model_retry.random, "uniform", side_effect=lambda low, high: (low + high) / 2).start()

    def advance(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def test_real_transport_exception_types_are_retryable(self):
        request = httpx.Request("POST", "https://model.invalid")
        errors = [
            connection_error(), openai.APITimeoutError(request=request),
            httpx.ConnectError("connection refused", request=request),
            httpx.ReadError("connection reset", request=request),
            httpx.RemoteProtocolError("peer disconnected", request=request),
            httpx.ReadTimeout("response timed out", request=request),
            httpx.PoolTimeout("pool busy", request=request),
            TimeoutError("timed out"), ConnectionError("connection refused"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                delay = model_retry.retry_delay(wrapped(wrapped(error)), 1)
                self.assertIsNotNone(delay)
                self.assertGreater(delay, 0)
                self.assertLessEqual(delay, 60)

    def test_retryable_statuses_and_backoff(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                delays = [model_retry.retry_delay(wrapped(status_error(status)), attempt)
                          for attempt in (1, 2, 3)]
                self.assertTrue(all(delay is not None and 0 < delay <= 60 for delay in delays))
                self.assertLess(delays[0], delays[1])
                self.assertLess(delays[1], delays[2])

    def test_permanent_statuses_and_invalid_answers_are_not_retryable(self):
        errors = [status_error(status) for status in (400, 401, 403, 404, 422)]
        errors += [ValueError("invalid schema"), TypeError("invalid payload"),
                   RuntimeError("unexpected parser state"), LlmError("Пустой ответ модели.")]
        for error in errors:
            with self.subTest(error=repr(error)):
                self.assertIsNone(model_retry.retry_delay(error, 1))
                self.assertIsNone(model_retry.retry_delay(wrapped(error), 1))

    def test_certificate_failure_and_deadline_are_not_hidden_by_connection_wrapper(self):
        for cause in (ssl.SSLCertVerificationError("invalid certificate"),
                      model_retry.ModelDeadlineExceeded("budget exhausted")):
            with self.subTest(cause=type(cause).__name__):
                connection = connection_error()
                connection.__cause__ = cause
                self.assertIsNone(model_retry.retry_delay(wrapped(connection), 1))

    def test_httpx_status_errors_follow_the_same_policy(self):
        for status, retryable in ((429, True), (503, True), (401, False), (404, False)):
            with self.subTest(status=status):
                request = httpx.Request("POST", "https://model.invalid")
                error = httpx.HTTPStatusError("Provider rejected request", request=request,
                                              response=httpx.Response(status, request=request))
                self.assertEqual(model_retry.retry_delay(wrapped(error), 1) is not None, retryable)

    def test_cyclic_exception_chain_does_not_hang(self):
        first, second = LlmError("first"), LlmError("second")
        first.__cause__ = second
        second.__cause__ = first
        self.assertIsNone(model_retry.retry_delay(first, 1))

    def test_retry_after_numeric_is_not_shortened(self):
        self.assertEqual(model_retry.retry_delay(status_error(429, "30"), 1), 30)
        self.assertEqual(model_retry.retry_delay(status_error(503, "99999"), 1), 99999)

    def test_retry_after_http_date(self):
        retry_at = formatdate(time.time() + 45, usegmt=True)
        delay = model_retry.retry_delay(status_error(503, retry_at), 1)
        self.assertGreaterEqual(delay, 42)
        self.assertLessEqual(delay, 45)

    def test_invalid_retry_after_uses_bounded_backoff(self):
        for header in ("invalid", "-20", "NaN", "inf"):
            with self.subTest(header=header):
                delay = model_retry.retry_delay(status_error(429, header), 1)
                self.assertGreater(delay, 0)
                self.assertLessEqual(delay, 60)

    def test_success_after_two_failures_preserves_history_and_chat_arguments(self):
        answer = object()
        original = copy.deepcopy(self.messages)
        snapshots = []
        results = iter([wrapped(connection_error()), wrapped(status_error(503)), answer])

        def chat(messages, **kwargs):
            snapshots.append(copy.deepcopy(messages))
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        client = Mock()
        client.chat.side_effect = chat
        before_retry, on_event = Mock(), Mock()
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        actual = model_retry.call_model(
            client, self.messages, deadline=200, before_retry=before_retry, on_event=on_event,
            max_tokens=600, temperature=0.1, tools=tools,
        )
        self.assertIs(actual, answer)
        self.assertEqual(client.chat.call_count, 3)
        self.assertEqual(before_retry.call_count, 2)
        self.assertEqual(len(self.sleeps), 2)
        self.assertEqual(self.messages, original)
        self.assertEqual(snapshots, [original] * 3)
        for call in client.chat.call_args_list:
            self.assertIs(call.args[0], self.messages)
            self.assertEqual(call.kwargs, {"max_tokens": 600, "temperature": 0.1, "tools": tools})
        retry_events = [call for call in on_event.call_args_list if call.args[0] == "model_retry"]
        self.assertEqual(len(retry_events), 2)

    def test_exhaustion_has_four_attempts_and_preserves_cause(self):
        error = connection_error()
        client = Mock()
        client.chat.side_effect = error
        before_retry = Mock()
        with self.assertRaises(LlmError) as raised:
            model_retry.call_model(client, self.messages, deadline=200, before_retry=before_retry)
        self.assertEqual(client.chat.call_count, 4)
        self.assertEqual(before_retry.call_count, 3)
        self.assertEqual(len(self.sleeps), 3)
        self.assertIs(raised.exception.__cause__, error)

    def test_permanent_error_propagates_immediately(self):
        error = wrapped(status_error(401))
        client = Mock()
        client.chat.side_effect = error
        before_retry = Mock()
        with self.assertRaises(LlmError) as raised:
            model_retry.call_model(client, self.messages, deadline=200, before_retry=before_retry)
        self.assertIs(raised.exception, error)
        client.chat.assert_called_once()
        before_retry.assert_not_called()
        self.sleep.assert_not_called()

    def test_expired_deadline_prevents_first_request(self):
        client = Mock()
        with self.assertRaises(model_retry.ModelDeadlineExceeded):
            model_retry.call_model(client, self.messages, deadline=100)
        client.chat.assert_not_called()
        self.sleep.assert_not_called()

    def test_insufficient_time_prevents_wait_and_another_request(self):
        client = Mock()

        def failed_request(*args, **kwargs):
            self.now += 1.5
            raise connection_error()

        client.chat.side_effect = failed_request
        with self.assertRaises(model_retry.ModelDeadlineExceeded):
            model_retry.call_model(client, self.messages, deadline=101.8)
        client.chat.assert_called_once()
        self.sleep.assert_not_called()

    def test_long_server_pause_is_respected_or_stops_at_budget(self):
        answer = object()
        client = Mock()
        client.chat.side_effect = [status_error(429, '600'), answer]
        self.assertIs(model_retry.call_model(client, self.messages, deadline=1200), answer)
        self.sleep.assert_called_once_with(600)
        self.assertEqual(client.chat.call_count, 2)
        self.sleep.reset_mock()
        client.reset_mock()
        client.chat.side_effect = status_error(429, '600')
        with self.assertRaises(model_retry.ModelDeadlineExceeded):
            model_retry.call_model(client, self.messages, deadline=self.now + 100)
        client.chat.assert_called_once()
        self.sleep.assert_not_called()

    def test_deadline_rechecked_after_wait(self):
        client = Mock()
        client.chat.side_effect = connection_error()

        def oversleep(delay):
            self.now = 201

        self.sleep.side_effect = oversleep
        with self.assertRaises(model_retry.ModelDeadlineExceeded):
            model_retry.call_model(client, self.messages, deadline=200)
        client.chat.assert_called_once()
        self.sleep.assert_called_once()

    def test_checkpoint_time_is_included_in_deadline(self):
        client = Mock()
        client.chat.side_effect = connection_error()

        def save_checkpoint():
            self.now = 201

        with self.assertRaises(model_retry.ModelDeadlineExceeded):
            model_retry.call_model(client, self.messages, deadline=200, before_retry=save_checkpoint)
        client.chat.assert_called_once()
        self.sleep.assert_not_called()

    def test_keyboard_interrupt_during_request_is_not_retried(self):
        client = Mock()
        client.chat.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            model_retry.call_model(client, self.messages, deadline=200)
        client.chat.assert_called_once()
        self.sleep.assert_not_called()

    def test_keyboard_interrupt_during_wait_is_not_swallowed(self):
        client = Mock()
        client.chat.side_effect = connection_error()
        self.sleep.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            model_retry.call_model(client, self.messages, deadline=200)
        client.chat.assert_called_once()
        self.sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
