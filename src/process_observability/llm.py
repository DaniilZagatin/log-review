"""OpenAI-compatible chat-completions adapter for an explicitly configured LLM endpoint.

Standard library only. ``OpenAICompatibleModel.complete_json`` sends one
non-streaming chat completion and returns the answer as a strictly validated
JSON object. Credentials are never stored: the API key is read at request time
from the environment variable named by ``ModelConfig.api_key_env`` and only
placed in the ``Authorization`` header. ``reasoning_content`` is ignored.

Transport contract: a transport is called as
``transport(url, payload, headers, ssl_context)`` with the endpoint URL, the
JSON-ready request dict, the HTTP header dict (may contain ``Authorization``)
and an ``ssl.SSLContext`` for https endpoints or ``None`` for http. It returns
``TransportResponse(status, body)`` with the final HTTP status and raw body
bytes; redirect and error statuses are returned, not raised. It may raise
``LLMError`` with a secret-free message; any other exception is reported only
by its class name. ``urllib_transport`` is the default: no timeout, no
redirects, at most ``MAX_RESPONSE_BYTES`` read, standard ``*_proxy``
environment variables honored by urllib.
"""

from __future__ import annotations

import http.client
import hashlib
import copy
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Mapping
from urllib.parse import SplitResult, urlsplit

__all__ = [
    "LLMError",
    "MAX_RESPONSE_BYTES",
    "ModelConfig",
    "OpenAICompatibleModel",
    "Transport",
    "TransportResponse",
    "urllib_transport",
]

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 64 * 1024
_REQUIRED_FIELDS = ("base_url", "model")
_TLS_FIELDS = ("ca_bundle", "client_cert", "client_key")
_CREDENTIAL_LIKE_FIELDS = frozenset(
    {"api_key", "apikey", "key", "token", "password", "secret", "authorization"}
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FENCED_JSON = re.compile(r"```json[ \t]*\r?\n(.*)\r?\n[ \t]*```", re.DOTALL)
_FINISH_REASON = re.compile(r"[a-z_]{1,32}")
_STATUS_HINTS = {
    400: "bad request",
    401: "authentication failed",
    403: "access denied",
    404: "endpoint or model not found",
    405: "method not allowed",
    408: "request timeout",
    413: "request too large",
    422: "request rejected",
    429: "rate limited",
    500: "server error",
    502: "bad gateway",
    503: "service unavailable",
    504: "gateway timeout",
}


class LLMError(ValueError):
    """Adapter failure with a masked message.

    Messages never contain response bodies, header values, credentials, URL
    userinfo or prompts. ``category`` is one of ``config``, ``request``,
    ``credentials``, ``tls``, ``connection``, ``redirect``, ``http``,
    ``response_too_large``, ``invalid_response``, ``missing_content`` or
    ``invalid_json``; ``status`` is the HTTP status when known.
    """

    def __init__(self, message: str, *, category: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status = status


@dataclass(frozen=True)
class TransportResponse:
    """Final HTTP status and raw response body returned by a transport."""

    status: int
    body: bytes = field(repr=False)


Transport = Callable[
    [str, dict[str, Any], dict[str, str], ssl.SSLContext | None], TransportResponse
]


def _request_options(value):
    """Freeze explicit endpoint extensions as bounded JSON, without accepting credentials/core overrides."""
    if value is None:
        return None
    if type(value) is not dict or set(value) - {"reasoning", "provider", "response_format"}:
        raise _config_error("extra_body accepts only reasoning, provider and response_format objects")
    forbidden = _CREDENTIAL_LIKE_FIELDS | {
        "headers", "api_key_env", "client_key", "client_cert", "x-api-key", "api-key",
        "access_token", "access-token", "cookie", "set-cookie", "client_secret"}

    def check(item, depth=0):
        if depth > 10:
            raise _config_error("extra_body exceeds the nested JSON limit")
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or not key.isprintable() or key.lower() in forbidden:
                    raise _config_error("extra_body contains an invalid or credential-like field")
                check(child, depth+1)
        elif type(item) is list:
            for child in item:
                check(child, depth+1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise _config_error("extra_body must contain JSON values only")
        elif type(item) is float and not math.isfinite(item):
            raise _config_error("extra_body must contain finite JSON numbers")

    if any(type(item) is not dict for item in value.values()):
        raise _config_error("extra_body extension values must be objects")
    check(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 32768:
        raise _config_error("extra_body exceeds the 32768-byte configuration limit")
    return encoded if value else None


@dataclass(frozen=True)
class ModelConfig:
    """Endpoint settings; holds no credentials.

    ``api_key_env`` names the environment variable holding the API key (no
    ``Authorization`` header when unset). ``thinking`` sends
    ``chat_template_kwargs.enable_thinking`` unless None. ``ca_bundle``
    replaces the system trust store; ``client_cert``/``client_key`` enable
    mutual TLS with an unencrypted key. TLS settings require an https
    ``base_url``; certificate verification is always on.
    """

    base_url: str
    model: str
    api_key_env: str | None = None
    thinking: bool | None = None
    ca_bundle: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    max_output_tokens: int = 8192
    extra_body: dict | None = field(default=None, repr=False, compare=False)
    rate_limit_retries: int = 0
    _request_options_json: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        _check_base_url(self.base_url)
        if not (isinstance(self.model, str) and self.model.strip() and self.model.isprintable()):
            raise _config_error("model must be a non-empty string without control characters")
        if self.api_key_env is not None and not (
            isinstance(self.api_key_env, str) and _ENV_NAME.fullmatch(self.api_key_env)
        ):
            raise _config_error(
                "api_key_env must be an environment variable name such as LLM_API_KEY"
            )
        if self.thinking is not None and not isinstance(self.thinking, bool):
            raise _config_error("thinking must be true, false or null")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 131072:
            raise _config_error("max_output_tokens must be an integer between 1 and 131072")
        if type(self.rate_limit_retries) is not int or not 0 <= self.rate_limit_retries <= 3:
            raise _config_error("rate_limit_retries must be an integer between 0 and 3")
        encoded = _request_options(self.extra_body)
        object.__setattr__(self, "_request_options_json", encoded)
        object.__setattr__(self, "extra_body", json.loads(encoded) if encoded else None)
        for name in _TLS_FIELDS:
            value = getattr(self, name)
            if value is not None and not (isinstance(value, str) and value and value.isprintable()):
                raise _config_error(f"{name} must be a non-empty path string")
        if (self.client_cert is None) != (self.client_key is None):
            raise _config_error("client_cert and client_key must be set together")
        if urlsplit(self.base_url).scheme != "https" and any(
            getattr(self, name) is not None for name in _TLS_FIELDS
        ):
            raise _config_error("ca_bundle, client_cert and client_key require an https base_url")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelConfig:
        """Build from a parsed config object, rejecting unknown or missing fields."""
        if not isinstance(data, Mapping):
            raise _config_error("expected an object")
        known = {item.name for item in fields(cls) if item.init}
        unknown = sorted(str(key) for key in data if key not in known)
        if unknown:
            hint = ""
            if _CREDENTIAL_LIKE_FIELDS & {name.lower() for name in unknown}:
                hint = "; store credentials in an environment variable named by api_key_env"
            raise _config_error(f"unknown field(s) {', '.join(unknown)}{hint}")
        missing = [name for name in _REQUIRED_FIELDS if name not in data]
        if missing:
            raise _config_error(f"missing field(s) {', '.join(missing)}")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict for run records; contains no credential values."""
        result = {item.name: getattr(self, item.name) for item in fields(self)
                  if item.init and item.name not in {"extra_body", "rate_limit_retries"}}
        # Preserve historical default serialization/cache identity. Use the validated snapshot,
        # so mutation of the caller's dict or the public dict cannot change a configured request.
        if self._request_options_json is not None:
            result["extra_body"] = json.loads(self._request_options_json)
        if self.rate_limit_retries:
            result["rate_limit_retries"] = self.rate_limit_retries
        return result


class OpenAICompatibleModel:
    """Chat-completions client that returns the model answer as a JSON object."""

    def __init__(self, config: ModelConfig, *, transport: Transport | None = None, observer=None, sleeper=None) -> None:
        if not isinstance(config, ModelConfig):
            raise LLMError("config must be a ModelConfig", category="config")
        if transport is not None and not callable(transport):
            raise LLMError("transport must be callable", category="config")
        if sleeper is not None and not callable(sleeper):
            raise LLMError("sleeper must be callable", category="config")
        self.config = config
        self._transport = urllib_transport if transport is None else transport
        self._observer = observer
        self._sleeper = time.sleep if sleeper is None else sleeper
        self._url = config.base_url.rstrip("/") + "/chat/completions"
        self._ssl_context = _build_ssl_context(config)

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """Send one chat completion and return its content parsed as a JSON object."""
        if not isinstance(system, str) or not isinstance(user, str):
            raise LLMError("system and user prompts must be strings", category="request")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": self.config.max_output_tokens,
        }
        if self.config.thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": self.config.thinking}
        options = self.config.to_dict().get("extra_body")
        if options:
            payload.update(options)
        response = self._send(payload, self._headers())
        if self._observer and isinstance(response,TransportResponse):
            # SHA-256(system+"\n"+user); review model.request events carry the same value as api_request_sha256.
            digest=hashlib.sha256((system+'\n'+user).encode()).hexdigest()
            meta = {"http_status":response.status,"response_bytes":len(response.body),
                    "request_sha256":digest,"api_request_sha256":digest}
            if 200 <= response.status < 300 and len(response.body) <= MAX_RESPONSE_BYTES:
                # Capture only answer content and numeric usage, never headers or private reasoning fields.
                try:
                    envelope=json.loads(response.body)
                    message=envelope.get("choices",[{}])[0].get("message",{})
                    content=message.get("content")
                    if isinstance(content,str) and re.search(r"</?(?:think|analysis)\b",content,re.IGNORECASE):
                        # Keep the strict JSON failure; never copy embedded reasoning to the trace.
                        meta["content"]="[omitted: reasoning markers in answer content]"
                    else:
                        meta["content"]=content
                    meta["finish_reason"]=envelope.get("choices",[{}])[0].get("finish_reason")
                    meta["usage"]={key:value for key,value in envelope.get("usage",{}).items() if type(value) is int}
                except (ValueError,AttributeError,IndexError,TypeError):
                    meta["envelope_status"]="unparseable"
            self._observer("api.response",meta)
        return _parse_json_object(_message_content(_successful_body(response)))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        name = self.config.api_key_env
        if name is None:
            return headers
        key = os.environ.get(name)
        if not key:
            raise LLMError(
                f"API key environment variable {name} is not set or is empty",
                category="credentials",
            )
        if not all(33 <= ord(ch) <= 126 for ch in key):
            raise LLMError(
                f"API key environment variable {name} contains whitespace, control "
                "or non-ASCII characters",
                category="credentials",
            )
        headers["Authorization"] = f"Bearer {key}"
        return headers

    def _send(self, payload: dict[str, Any], headers: dict[str, str]) -> object:
        delays = (30, 90, 270)
        digest = hashlib.sha256((payload["messages"][0]["content"] + "\n"
                                 + payload["messages"][1]["content"]).encode()).hexdigest()

        def observed(attempt, status, retry=False):
            if self.config.rate_limit_retries and self._observer:
                self._observer("api.transport_attempt", {
                    "attempt": attempt, "max_attempts": self.config.rate_limit_retries + 1,
                    "request_sha256": digest, "api_request_sha256": digest,
                    "http_status": status, "retry": retry,
                    "wait_seconds": delays[attempt-1] if retry else 0})

        for attempt in range(1, self.config.rate_limit_retries + 2):
            try:
                # Each transport invocation remains a separate accounting/reservation boundary.
                response = self._transport(self._url, copy.deepcopy(payload), headers, self._ssl_context)
            except LLMError:
                observed(attempt, None)
                raise
            except Exception as exc:  # messages may echo headers, URLs or prompts
                failure = f"LLM transport failed ({type(exc).__name__})"
            else:
                retry = (isinstance(response, TransportResponse) and response.status == 429
                         and attempt <= self.config.rate_limit_retries)
                observed(attempt, response.status if isinstance(response, TransportResponse) else None, retry)
                if not retry:
                    return response
                self._sleeper(delays[attempt-1])
                continue
            # Raised outside the handler so the original exception is not chained.
            observed(attempt, None)
            raise LLMError(failure, category="connection")


def urllib_transport(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    ssl_context: ssl.SSLContext | None,
) -> TransportResponse:
    """Default transport: POST JSON with no timeout, no redirects and a bounded body."""
    data = json.dumps(payload, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(
        _RejectRedirects(), urllib.request.HTTPSHandler(context=ssl_context)
    )
    try:
        with opener.open(request, timeout=None) as response:
            return TransportResponse(status=response.status, body=_read_limited(response))
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()  # error bodies are never read
        return TransportResponse(status=status, body=b"")
    except LLMError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        failure = _transport_failure(exc)
    raise failure


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Stop urllib from following 3xx; the status then surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_limited(response: Any) -> bytes:
    limit = MAX_RESPONSE_BYTES
    if _declared_length(response.headers.get("Content-Length")) > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise _too_large(limit)
        chunks.append(chunk)


def _declared_length(value: str | None) -> int:
    try:
        return int(value) if value is not None else -1
    except ValueError:
        return -1


def _too_large(limit: int) -> LLMError:
    return LLMError(f"LLM response exceeds {limit} bytes", category="response_too_large")


def _transport_failure(exc: BaseException) -> LLMError:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if not isinstance(reason, BaseException):
        reason = exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return LLMError("TLS certificate verification failed", category="tls")
    if isinstance(reason, ssl.SSLError):
        return LLMError(f"TLS connection failed ({type(reason).__name__})", category="tls")
    if isinstance(reason, TimeoutError):
        return LLMError("connection to LLM endpoint timed out", category="connection")
    if isinstance(reason, socket.gaierror):
        return LLMError("could not resolve LLM endpoint host", category="connection")
    return LLMError(
        f"connection to LLM endpoint failed ({type(reason).__name__})", category="connection"
    )


class _EncryptedKeyError(Exception):
    pass


def _refuse_key_password() -> str:
    # OpenSSL calls this only for encrypted keys; it replaces the interactive prompt.
    raise _EncryptedKeyError


def _build_ssl_context(config: ModelConfig) -> ssl.SSLContext | None:
    if urlsplit(config.base_url).scheme != "https":
        return None
    source = "ca_bundle" if config.ca_bundle is not None else "system trust store"
    try:
        context = ssl.create_default_context(cafile=config.ca_bundle)
    except (OSError, ValueError) as exc:
        failure = f"could not load {source} ({type(exc).__name__})"
    else:
        if config.client_cert is None:
            return context
        try:
            context.load_cert_chain(
                config.client_cert, config.client_key, password=_refuse_key_password
            )
            return context
        except _EncryptedKeyError:
            failure = "client_key is encrypted; passphrase-protected keys are not supported"
        except (OSError, ValueError) as exc:
            failure = f"could not load client_cert/client_key ({type(exc).__name__})"
    raise LLMError(failure, category="tls")


def _successful_body(response: object) -> bytes:
    if not (
        isinstance(response, TransportResponse)
        and type(response.status) is int
        and isinstance(response.body, bytes)
    ):
        raise LLMError(
            "transport must return TransportResponse(status: int, body: bytes)",
            category="invalid_response",
        )
    status = response.status
    if 300 <= status < 400:
        raise LLMError(
            f"LLM endpoint returned HTTP {status}; redirects are not followed",
            category="redirect",
            status=status,
        )
    if not 200 <= status < 300:
        raise LLMError(
            f"LLM endpoint returned HTTP {status} ({_status_hint(status)})",
            category="http",
            status=status,
        )
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise _too_large(MAX_RESPONSE_BYTES)
    return response.body


def _status_hint(status: int) -> str:
    if status in _STATUS_HINTS:
        return _STATUS_HINTS[status]
    if 400 <= status < 500:
        return "client error"
    if 500 <= status < 600:
        return "server error"
    return "unexpected status"


def _message_content(body: bytes) -> str:
    text = _decode_utf8(body)
    if text is None:
        raise LLMError("LLM response body is not valid UTF-8", category="invalid_response")
    envelope = _strict_json(text, "LLM response body", "invalid_response")
    if not isinstance(envelope, dict):
        raise LLMError("LLM response body is not a JSON object", category="invalid_response")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        detail = "an error object" if "error" in envelope else "no choices"
        raise LLMError(f"LLM response contains {detail}", category="invalid_response")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise LLMError("LLM response choice has no message", category="invalid_response")
    content = message.get("content")  # reasoning_content is deliberately never read
    if not isinstance(content, str) or not content.strip():
        reason = choice.get("finish_reason")
        hint = ""
        if isinstance(reason, str) and _FINISH_REASON.fullmatch(reason):
            hint = f" (finish_reason={reason})"
        raise LLMError(
            f"LLM response message has no text content{hint}", category="missing_content"
        )
    if choice.get("finish_reason") == "length":
        raise LLMError("LLM response was truncated; increase max_output_tokens", category="invalid_response")
    return content


def _decode_utf8(body: bytes) -> str | None:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = _FENCED_JSON.fullmatch(text)
    if fenced:
        text = fenced.group(1)
    elif text.startswith("```"):
        raise LLMError(
            "model content uses an unsupported code fence; only one enclosing ```json "
            "block is accepted",
            category="invalid_json",
        )
    value = _strict_json(text, "model content", "invalid_json")
    if not isinstance(value, dict):
        raise LLMError(
            f"model content must be a JSON object, not {_json_type(value)}",
            category="invalid_json",
        )
    return value


class _JSONPolicyError(ValueError):
    pass


def _strict_json(text: str, what: str, category: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except _JSONPolicyError as exc:
        problem = str(exc)
    except json.JSONDecodeError as exc:
        problem = f"invalid JSON at line {exc.lineno}, column {exc.colno}"
    except RecursionError:
        problem = "JSON nesting is too deep"
    except ValueError:
        problem = "unsupported JSON value"
    raise LLMError(f"{what}: {problem}", category=category)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _JSONPolicyError("duplicate object key")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise _JSONPolicyError("non-finite number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _JSONPolicyError("non-finite number")
    return value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array"


def _config_error(message: str) -> LLMError:
    return LLMError(f"invalid model config: {message}", category="config")


def _check_base_url(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise _config_error("base_url must be a non-empty string")
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        raise _config_error("base_url must not contain whitespace or control characters")
    if "?" in value or "#" in value:
        raise _config_error("base_url must not contain a query string or fragment")
    try:
        parts = urlsplit(value)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise _config_error("base_url must be an absolute http:// or https:// URL")
    if "@" in parts.netloc:
        raise _config_error("base_url must not contain user credentials; use api_key_env")
    if not parts.hostname or not _has_valid_port(parts):
        raise _config_error("base_url has an invalid host or port")
    if parts.path.rstrip("/").endswith("/chat/completions"):
        raise _config_error(
            "base_url must be the API base (for example https://host/v1), "
            "not the /chat/completions endpoint"
        )


def _has_valid_port(parts: SplitResult) -> bool:
    try:
        parts.port
    except ValueError:
        return False
    return True
