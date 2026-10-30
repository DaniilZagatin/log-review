"""Offline tests for the OpenAI-compatible LLM adapter.

No test opens a socket: the adapter is driven through an injected transport,
and the default urllib transport runs against an in-process fake HTTP handler
or a mocked opener.
"""

from __future__ import annotations

import email.message
import io
import json
import os
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

from process_observability import llm
from process_observability.llm import (
    LLMError,
    ModelConfig,
    OpenAICompatibleModel,
    TransportResponse,
)

KEY_ENV = "PO_TEST_LLM_API_KEY"
SECRET = "sk-test-0123456789-SECRET"
PROMPT = "PROMPT-MARKER confidential process description"
HTTP_URL = "http://llm.corp.example:8000/v1/"
HTTPS_URL = "https://llm.corp.example/v1"


def envelope(content, *, finish_reason="stop", **extra) -> bytes:
    message = {"role": "assistant", "content": content, **extra}
    choice = {"index": 0, "message": message, "finish_reason": finish_reason}
    return json.dumps({"id": "chatcmpl-test", "choices": [choice]}).encode()


def ok(content, **extra) -> TransportResponse:
    return TransportResponse(status=200, body=envelope(content, **extra))


class FakeTransport:
    """Injected transport that records calls and replays canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, ssl_context):
        self.calls.append(
            {"url": url, "payload": payload, "headers": headers, "ssl_context": ssl_context}
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeHTTPHandler(urllib.request.HTTPHandler):
    """Replaces urllib's HTTP handler so the real opener chain runs without sockets."""

    def __init__(self, status=200, body=b"", response_headers=None, error=None):
        super().__init__()
        self.status = status
        self.body = body
        self.response_headers = response_headers or {}
        self.error = error
        self.requests = []

    def http_open(self, req):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        headers = email.message.Message()
        for name, value in self.response_headers.items():
            headers[name] = value
        response = urllib.response.addinfourl(
            io.BytesIO(self.body), headers, req.full_url, code=self.status
        )
        response.msg = "fake"
        return response


def make_model(transport, **overrides) -> OpenAICompatibleModel:
    config = {"base_url": HTTP_URL, "model": "corp-chat", **overrides}
    return OpenAICompatibleModel(ModelConfig.from_dict(config), transport=transport)


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop(KEY_ENV, None)

    def assertMasked(self, error, *markers):
        message = str(error)
        for marker in (SECRET, PROMPT, *markers):
            self.assertNotIn(marker, message)


class ModelConfigTests(unittest.TestCase):
    def test_minimal_config_defaults_and_round_trip(self):
        config = ModelConfig.from_dict({"base_url": HTTPS_URL, "model": "corp-chat"})
        expected = {
            "base_url": HTTPS_URL,
            "model": "corp-chat",
            "api_key_env": None,
            "thinking": None,
            "ca_bundle": None,
            "client_cert": None,
            "client_key": None,
            "max_output_tokens": 8192,
        }
        self.assertEqual(config.to_dict(), expected)
        self.assertEqual(ModelConfig.from_dict(config.to_dict()), config)

    def test_config_records_variable_name_not_key(self):
        with mock.patch.dict(os.environ, {KEY_ENV: SECRET}):
            config = ModelConfig.from_dict(
                {"base_url": HTTPS_URL, "model": "m", "api_key_env": KEY_ENV}
            )
        self.assertEqual(config.to_dict()["api_key_env"], KEY_ENV)
        self.assertNotIn(SECRET, repr(config))
        self.assertNotIn(SECRET, json.dumps(config.to_dict()))

    def test_rejects_unknown_fields_without_echoing_values(self):
        with self.assertRaises(LLMError) as caught:
            ModelConfig.from_dict({"base_url": HTTPS_URL, "model": "m", "api_key": SECRET})
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.category, "config")
        self.assertIn("api_key", str(caught.exception))
        self.assertIn("api_key_env", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_rejects_missing_fields_and_non_objects(self):
        for data in ({"model": "m"}, {"base_url": HTTPS_URL}, [("base_url", HTTPS_URL)], None):
            with self.subTest(data=data):
                with self.assertRaises(LLMError) as caught:
                    ModelConfig.from_dict(data)
                self.assertEqual(caught.exception.category, "config")

    def test_rejects_wrong_field_types(self):
        cases = [
            {"base_url": 42},
            {"model": None},
            {"model": "  "},
            {"model": "corp\nchat"},
            {"api_key_env": 5},
            {"thinking": "true"},
            {"thinking": 1},
            {"ca_bundle": 3},
            {"ca_bundle": ""},
            {"client_cert": "c.pem", "client_key": b"k.pem"},
        ]
        for override in cases:
            with self.subTest(override=override):
                with self.assertRaises(LLMError) as caught:
                    ModelConfig.from_dict({"base_url": HTTPS_URL, "model": "m", **override})
                self.assertEqual(caught.exception.category, "config")

    def test_rejects_unsafe_or_ambiguous_base_urls(self):
        urls = [
            "https://alice:hunter2@llm.corp.example/v1",
            "https://token@llm.corp.example/v1",
            "https://llm.corp.example/v1?api_key=abc123",
            "https://llm.corp.example/v1#section",
            "ftp://llm.corp.example/v1",
            "llm.corp.example/v1",
            "https:///v1",
            "https://llm.corp.example:99999/v1",
            "https://llm.corp.example /v1",
            "https://llm.corp.example/v1/chat/completions/",
            "",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(LLMError) as caught:
                    ModelConfig.from_dict({"base_url": url, "model": "m"})
                self.assertEqual(caught.exception.category, "config")
                if url:
                    self.assertNotIn(url, str(caught.exception))
                for fragment in ("hunter2", "abc123", "token@"):
                    self.assertNotIn(fragment, str(caught.exception))

    def test_direct_construction_is_validated(self):
        with self.assertRaises(LLMError):
            ModelConfig(base_url="https://u:p@llm.corp.example/v1", model="m")

    def test_accepts_explicit_http_and_https_endpoints(self):
        for url in ("http://10.0.0.5:8000/v1", "https://llm.corp.example/v1/", "http://localhost"):
            with self.subTest(url=url):
                self.assertEqual(ModelConfig.from_dict({"base_url": url, "model": "m"}).base_url, url)

    def test_rejects_incomplete_client_certificate_pair(self):
        for pair in ({"client_cert": "client.pem"}, {"client_key": "client.key"}):
            with self.subTest(pair=pair):
                with self.assertRaises(LLMError) as caught:
                    ModelConfig.from_dict({"base_url": HTTPS_URL, "model": "m", **pair})
                self.assertIn("together", str(caught.exception))

    def test_rejects_tls_files_for_http_endpoint(self):
        for tls in ({"ca_bundle": "ca.pem"}, {"client_cert": "c.pem", "client_key": "k.pem"}):
            with self.subTest(tls=tls):
                with self.assertRaises(LLMError) as caught:
                    ModelConfig.from_dict({"base_url": HTTP_URL, "model": "m", **tls})
                self.assertIn("https", str(caught.exception))

    def test_rejects_api_key_env_that_is_not_a_variable_name(self):
        for value in ("", "MY-KEY", "sk-live abc", "1KEY"):
            with self.subTest(value=value):
                with self.assertRaises(LLMError) as caught:
                    ModelConfig.from_dict({"base_url": HTTPS_URL, "model": "m", "api_key_env": value})
                if value:
                    self.assertNotIn(value, str(caught.exception))


class RequestTests(AdapterTestCase):
    def test_posts_chat_completion_without_forced_extensions(self):
        transport = FakeTransport(ok('{"ok": true}'))
        result = make_model(transport).complete_json("SYSTEM", PROMPT)
        self.assertEqual(result, {"ok": True})
        [call] = transport.calls
        self.assertEqual(call["url"], "http://llm.corp.example:8000/v1/chat/completions")
        self.assertEqual(
            call["payload"],
            {
                "model": "corp-chat",
                "messages": [
                    {"role": "system", "content": "SYSTEM"},
                    {"role": "user", "content": PROMPT},
                ],
                "temperature": 0,
                "stream": False,
                "max_tokens": 8192,
            },
        )
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertNotIn("Authorization", call["headers"])
        self.assertIsNone(call["ssl_context"])

    def test_thinking_flag_uses_top_level_chat_template_kwargs(self):
        for thinking in (True, False):
            with self.subTest(thinking=thinking):
                transport = FakeTransport(ok("{}"))
                make_model(transport, thinking=thinking).complete_json("s", "u")
                payload = transport.calls[0]["payload"]
                self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": thinking})
                self.assertNotIn("response_format", payload)

    def test_api_key_comes_only_from_named_environment_variable(self):
        os.environ[KEY_ENV] = SECRET
        transport = FakeTransport(ok("{}"), ok("{}"))
        model = make_model(transport, api_key_env=KEY_ENV)
        model.complete_json("s", "u")
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, repr(vars(model)))
        os.environ[KEY_ENV] = "rotated-key"
        model.complete_json("s", "u")
        self.assertEqual(transport.calls[1]["headers"]["Authorization"], "Bearer rotated-key")

    def test_missing_or_empty_api_key_variable_fails_before_sending(self):
        for value in (None, ""):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop(KEY_ENV, None)
                else:
                    os.environ[KEY_ENV] = value
                transport = FakeTransport()
                with self.assertRaises(LLMError) as caught:
                    make_model(transport, api_key_env=KEY_ENV).complete_json("s", PROMPT)
                self.assertEqual(caught.exception.category, "credentials")
                self.assertIn(KEY_ENV, str(caught.exception))
                self.assertMasked(caught.exception)
                self.assertEqual(transport.calls, [])

    def test_api_key_with_header_breaking_characters_is_rejected_without_echo(self):
        os.environ[KEY_ENV] = SECRET + "\r\nX-Injected: 1"
        transport = FakeTransport()
        with self.assertRaises(LLMError) as caught:
            make_model(transport, api_key_env=KEY_ENV).complete_json("s", "u")
        self.assertEqual(caught.exception.category, "credentials")
        self.assertMasked(caught.exception, "X-Injected")
        self.assertEqual(transport.calls, [])

    def test_https_endpoint_gets_verifying_tls_context(self):
        transport = FakeTransport(ok("{}"))
        make_model(transport, base_url=HTTPS_URL).complete_json("s", "u")
        context = transport.calls[0]["ssl_context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_prompts_must_be_strings(self):
        for system, user in ((None, "u"), ("s", b"u")):
            with self.subTest(system=system, user=user):
                transport = FakeTransport()
                with self.assertRaises(LLMError) as caught:
                    make_model(transport).complete_json(system, user)
                self.assertEqual(caught.exception.category, "request")
                self.assertEqual(transport.calls, [])


class ResponseTests(AdapterTestCase):
    def complete(self, response):
        return make_model(FakeTransport(response)).complete_json("s", PROMPT)

    def assertRejected(self, response, category, *markers):
        with self.assertRaises(LLMError) as caught:
            self.complete(response)
        self.assertEqual(caught.exception.category, category)
        self.assertMasked(caught.exception, *markers)
        return caught.exception

    def test_returns_content_object_and_ignores_reasoning(self):
        result = self.complete(
            ok('{"stage": "intake", "items": [1, 2.5]}', reasoning_content="REASONING-TRACE")
        )
        self.assertEqual(result, {"stage": "intake", "items": [1, 2.5]})
        self.assertNotIn("REASONING-TRACE", json.dumps(result))

    def test_observer_omits_reasoning_fields_and_tagged_content(self):
        for content in ('<think>PRIVATE-THOUGHT</think>{"ok":true}', '<think>PRIVATE-THOUGHT'):
            events=[]
            model=OpenAICompatibleModel(ModelConfig(base_url=HTTP_URL,model='test',thinking=True),
                transport=FakeTransport(ok(content,reasoning_content='PRIVATE-FIELD')),
                observer=lambda event,data:events.append((event,data)))
            with self.assertRaises(LLMError):model.complete_json('s','u')
            trace=json.dumps(events)
            self.assertNotIn('PRIVATE-THOUGHT',trace)
            self.assertNotIn('PRIVATE-FIELD',trace)
            self.assertIn('omitted',trace)

    def test_malformed_envelopes_are_invalid_responses(self):
        bodies = {
            "not json": b"<html>proxy login</html>",
            "not utf-8": b"\xff\xfe{}",
            "array": b"[]",
            "no choices": b'{"object": "chat.completion"}',
            "empty choices": b'{"choices": []}',
            "error object": json.dumps({"error": {"message": SECRET}}).encode(),
            "choice not object": b'{"choices": ["x"]}',
            "no message": b'{"choices": [{"index": 0}]}',
            "duplicate envelope key": b'{"choices": [], "choices": []}',
        }
        for label, body in bodies.items():
            with self.subTest(label):
                self.assertRejected(TransportResponse(200, body), "invalid_response", "proxy login")

    def test_missing_content_reports_finish_reason_without_reasoning(self):
        for content in (None, "", "   ", [{"type": "text", "text": "{}"}], 42):
            with self.subTest(content=content):
                response = TransportResponse(
                    200,
                    envelope(content, finish_reason="length", reasoning_content="REASONING-TRACE"),
                )
                error = self.assertRejected(response, "missing_content", "REASONING-TRACE")
                self.assertIn("finish_reason=length", str(error))

    def test_model_content_must_be_json_object(self):
        for content in ("[1, 2]", '"text"', "42", "null", "true"):
            with self.subTest(content=content):
                self.assertRejected(ok(content), "invalid_json")

    def test_truncated_content_is_not_accepted_as_complete_json(self):
        response = TransportResponse(200, envelope('{"ok":true}', finish_reason="length"))
        self.assertRejected(response, "invalid_response", "REASONING-TRACE")

    def test_rejects_non_finite_numbers(self):
        for content in (
            '{"a": NaN}',
            '{"a": Infinity}',
            '{"a": -Infinity}',
            '{"a": 1e999}',
            '{"a": [1, NaN]}',
        ):
            with self.subTest(content=content):
                error = self.assertRejected(ok(content), "invalid_json")
                self.assertIn("non-finite", str(error))

    def test_rejects_duplicate_keys(self):
        for content in ('{"a": 1, "a": 2}', '{"outer": {"b": 1, "b": 1}}'):
            with self.subTest(content=content):
                error = self.assertRejected(ok(content), "invalid_json")
                self.assertIn("duplicate", str(error))

    def test_rejects_invalid_or_prose_wrapped_json_without_echo(self):
        for content in (
            '{"a": 1',
            "{'a': 1}",
            'Sure! Here it is: {"a": 1}',
            '{"a": 1}\nHope this helps.',
        ):
            with self.subTest(content=content):
                self.assertRejected(ok(content), "invalid_json", content, "Hope this helps")

    def test_accepts_one_exact_json_fence(self):
        for content in (
            '```json\n{"a": 1}\n```',
            '\n  ```json\n{"a": 1}\n```  \n',
            '```json\r\n{"a": 1}\r\n```',
            '```json\n{\n  "a": 1\n}\n```',
        ):
            with self.subTest(content=content):
                self.assertEqual(self.complete(ok(content)), {"a": 1})

    def test_rejects_other_fences_and_text_around_fence(self):
        for content in (
            '```\n{"a": 1}\n```',
            '```JSON\n{"a": 1}\n```',
            '```python\n{"a": 1}\n```',
            'Result:\n```json\n{"a": 1}\n```',
            '```json\n{"a": 1}\n```\nDone.',
            '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```',
            '```json {"a": 1} ```',
        ):
            with self.subTest(content=content):
                self.assertRejected(ok(content), "invalid_json")


class ErrorMaskingTests(AdapterTestCase):
    def test_http_errors_report_status_without_body_or_secrets(self):
        os.environ[KEY_ENV] = SECRET
        body = json.dumps({"error": f"bad key {SECRET}", "echo": PROMPT}).encode()
        for status in (400, 401, 403, 404, 413, 418, 429, 500, 502, 503):
            with self.subTest(status=status):
                transport = FakeTransport(TransportResponse(status, body))
                with self.assertRaises(LLMError) as caught:
                    make_model(transport, api_key_env=KEY_ENV).complete_json("s", PROMPT)
                self.assertEqual(caught.exception.category, "http")
                self.assertEqual(caught.exception.status, status)
                self.assertIn(f"HTTP {status}", str(caught.exception))
                self.assertMasked(caught.exception, "bad key", "Bearer")

    def test_redirect_statuses_are_rejected(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                transport = FakeTransport(TransportResponse(status, b""))
                with self.assertRaises(LLMError) as caught:
                    make_model(transport).complete_json("s", "u")
                self.assertEqual(caught.exception.category, "redirect")
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(transport.calls), 1)

    def test_transport_exceptions_are_masked_and_not_chained(self):
        os.environ[KEY_ENV] = SECRET
        transport = FakeTransport(RuntimeError(f"failed sending Bearer {SECRET} with {PROMPT}"))
        with self.assertRaises(LLMError) as caught:
            make_model(transport, api_key_env=KEY_ENV).complete_json("s", PROMPT)
        error = caught.exception
        self.assertEqual(error.category, "connection")
        self.assertIn("RuntimeError", str(error))
        self.assertMasked(error, "Bearer")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_llm_errors_from_transport_pass_through(self):
        original = LLMError("custom transport failure", category="connection")
        with self.assertRaises(LLMError) as caught:
            make_model(FakeTransport(original)).complete_json("s", "u")
        self.assertIs(caught.exception, original)

    def test_transport_must_return_transport_response(self):
        for response in (
            {"status": 200, "body": envelope("{}")},
            TransportResponse("200", envelope("{}")),
            TransportResponse(True, envelope("{}")),
            TransportResponse(200, envelope("{}").decode()),
        ):
            with self.subTest(response=type(response).__name__):
                with self.assertRaises(LLMError) as caught:
                    make_model(FakeTransport(response)).complete_json("s", "u")
                self.assertEqual(caught.exception.category, "invalid_response")

    def test_injected_transport_body_is_size_limited(self):
        with mock.patch.object(llm, "MAX_RESPONSE_BYTES", 64):
            with self.assertRaises(LLMError) as caught:
                make_model(FakeTransport(ok('{"a": "' + "x" * 200 + '"}'))).complete_json("s", "u")
        self.assertEqual(caught.exception.category, "response_too_large")

    def test_transport_response_repr_hides_body(self):
        self.assertNotIn(SECRET, repr(TransportResponse(200, SECRET.encode())))


class TLSConfigTests(unittest.TestCase):
    def https_config(self, **tls) -> ModelConfig:
        return ModelConfig.from_dict({"base_url": HTTPS_URL, "model": "m", **tls})

    def test_missing_ca_bundle_fails_at_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.https_config(ca_bundle=str(Path(tmp) / "missing.pem"))
            with self.assertRaises(LLMError) as caught:
                OpenAICompatibleModel(config, transport=FakeTransport())
        self.assertEqual(caught.exception.category, "tls")
        self.assertIn("ca_bundle", str(caught.exception))

    def test_unparseable_ca_bundle_fails_at_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "corp-ca.pem"
            path.write_text("not a certificate\n", encoding="utf-8")
            with self.assertRaises(LLMError) as caught:
                OpenAICompatibleModel(self.https_config(ca_bundle=str(path)), transport=FakeTransport())
        self.assertEqual(caught.exception.category, "tls")

    def test_client_certificate_pair_is_loaded_into_verifying_context(self):
        context = mock.Mock(spec=ssl.SSLContext)
        config = self.https_config(
            ca_bundle="corp-ca.pem", client_cert="client.pem", client_key="client.key"
        )
        transport = FakeTransport(ok("{}"))
        with mock.patch.object(ssl, "create_default_context", return_value=context) as create:
            model = OpenAICompatibleModel(config, transport=transport)
        create.assert_called_once_with(cafile="corp-ca.pem")
        args, kwargs = context.load_cert_chain.call_args
        self.assertEqual(args, ("client.pem", "client.key"))
        self.assertTrue(callable(kwargs["password"]))
        model.complete_json("s", "u")
        self.assertIs(transport.calls[0]["ssl_context"], context)

    def test_encrypted_client_key_is_refused_without_prompting(self):
        context = mock.Mock(spec=ssl.SSLContext)
        context.load_cert_chain.side_effect = lambda certfile, keyfile, password: password()
        config = self.https_config(client_cert="client.pem", client_key="client.key")
        with mock.patch.object(ssl, "create_default_context", return_value=context):
            with self.assertRaises(LLMError) as caught:
                OpenAICompatibleModel(config, transport=FakeTransport())
        self.assertEqual(caught.exception.category, "tls")
        self.assertIn("encrypted", str(caught.exception))


class DefaultTransportTests(AdapterTestCase):
    def run_default(self, handler, **overrides):
        """Run complete_json through urllib_transport with ``handler`` serving HTTP."""
        real_build_opener = urllib.request.build_opener

        def build_opener(*handlers):
            # An empty ProxyHandler keeps host proxy settings out of the test.
            return real_build_opener(*handlers, urllib.request.ProxyHandler({}), handler)

        with mock.patch.object(urllib.request, "build_opener", side_effect=build_opener):
            return make_model(None, **overrides).complete_json("SYSTEM", PROMPT)

    def test_posts_json_without_timeout_and_returns_content(self):
        os.environ[KEY_ENV] = SECRET
        handler = FakeHTTPHandler(body=envelope('{"a": 1}'))
        self.assertEqual(self.run_default(handler, api_key_env=KEY_ENV), {"a": 1})
        [request] = handler.requests
        self.assertEqual(request.full_url, "http://llm.corp.example:8000/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertIsNone(request.timeout)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {SECRET}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data)["messages"][1]["content"], PROMPT)

    def test_redirects_are_not_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                handler = FakeHTTPHandler(
                    status=status,
                    response_headers={"Location": "http://elsewhere.example/v1/chat/completions"},
                )
                with self.assertRaises(LLMError) as caught:
                    self.run_default(handler)
                self.assertEqual(caught.exception.category, "redirect")
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(handler.requests), 1)
                self.assertNotIn("elsewhere.example", str(caught.exception))

    def test_http_error_body_is_not_reported(self):
        os.environ[KEY_ENV] = SECRET
        handler = FakeHTTPHandler(status=401, body=f"invalid key {SECRET} for {PROMPT}".encode())
        with self.assertRaises(LLMError) as caught:
            self.run_default(handler, api_key_env=KEY_ENV)
        self.assertEqual(caught.exception.category, "http")
        self.assertEqual(caught.exception.status, 401)
        self.assertMasked(caught.exception, "invalid key")

    def test_streamed_body_over_limit_is_rejected(self):
        handler = FakeHTTPHandler(body=envelope('{"a": "' + "x" * 200 + '"}'))
        with mock.patch.object(llm, "MAX_RESPONSE_BYTES", 64):
            with self.assertRaises(LLMError) as caught:
                self.run_default(handler)
        self.assertEqual(caught.exception.category, "response_too_large")

    def test_declared_content_length_over_limit_is_rejected(self):
        handler = FakeHTTPHandler(body=envelope("{}"), response_headers={"Content-Length": "999999"})
        with mock.patch.object(llm, "MAX_RESPONSE_BYTES", 1024):
            with self.assertRaises(LLMError) as caught:
                self.run_default(handler)
        self.assertEqual(caught.exception.category, "response_too_large")

    def test_body_exactly_at_limit_is_accepted(self):
        body = envelope('{"a": 1}')
        with mock.patch.object(llm, "MAX_RESPONSE_BYTES", len(body)):
            self.assertEqual(self.run_default(FakeHTTPHandler(body=body)), {"a": 1})

    def test_tls_verification_failure_is_not_retried_insecurely(self):
        failure = ssl.SSLCertVerificationError(1, "certificate verify failed")
        handler = FakeHTTPHandler(error=urllib.error.URLError(failure))
        with self.assertRaises(LLMError) as caught:
            self.run_default(handler)
        self.assertEqual(caught.exception.category, "tls")
        self.assertEqual(len(handler.requests), 1)

    def test_connection_errors_are_masked_and_not_chained(self):
        failure = ConnectionRefusedError(61, f"refused for {SECRET}")
        handler = FakeHTTPHandler(error=urllib.error.URLError(failure))
        with self.assertRaises(LLMError) as caught:
            self.run_default(handler)
        error = caught.exception
        self.assertEqual(error.category, "connection")
        self.assertIn("ConnectionRefusedError", str(error))
        self.assertMasked(error)
        self.assertIsNone(error.__context__)

    def test_https_opener_uses_adapter_tls_context_and_rejects_redirects(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError())
        with mock.patch.object(urllib.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(LLMError):
                make_model(None, base_url=HTTPS_URL).complete_json("s", "u")
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers))
        [https] = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
        context = https._context  # CPython attribute; no public accessor exists
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        opener.open.assert_called_once()
        self.assertIsNone(opener.open.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
