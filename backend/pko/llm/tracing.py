"""One optional Phoenix trace: scenario run -> researcher -> steps -> model and tools.

Only observable inputs/results are exported; credentials and hidden reasoning are masked.
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Optional

from pko.audit import audit_span, current_audit, record, sanitize

logger = logging.getLogger(__name__)

# Подписи проходов в дереве Phoenix. Внутреннее имя остаётся атрибутом
# `pko.span`: по нему спан находят журнал анализа и мониторинг.
_SESSION_LABELS = {"scenario_research": "Исследование сценария"}
SESSION_PASSES = frozenset(_SESSION_LABELS)
STEP_PASSES = {"scenario_step"}
_KIND = "openinference.span.kind"
_JSON = "application/json"
_TEXT = "text/plain"
# LLM-спан несёт всю очередь сообщений — сотни килобайт на шаге. Пачка по
# умолчанию (512 спанов) превращалась бы в запрос на сотню мегабайт.
_EXPORT_BATCH = 32
# Сообщение очереди — до десятка плоских атрибутов, а за сессию их сотни.
# Лимит OTel по умолчанию (128 атрибутов) молча выбрасывал самые старые —
# вместе с видом спана, и LLM-спан переставал быть LLM-спаном.
_MAX_SPAN_ATTRIBUTES = 20_000
_FLUSH_TIMEOUT_MS = 30_000
_PROBE_TIMEOUT_SECONDS = 3


class _NoopSpan:
    """Заглушка для режима без трассировки: методы ничего не делают.

    Даёт коду пайплайна единый интерфейс `set_attribute`/`add_event`/
    `record_error` независимо от того, включён ли Phoenix — не нужно
    ветвить логику.
    """

    trace_id = ""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_input(self, value: Any) -> None:
        pass

    def set_output(self, value: Any) -> None:
        pass

    def add_event(self, name: str, **attributes: Any) -> None:
        pass

    def record_error(self, exc: BaseException) -> None:
        pass


class _SpanHandle:
    """Тонкая обёртка над OTel-спаном: глотает ошибки наблюдаемости.

    Атрибуты, события и статусы — метаданные; ронять прогон ради них нельзя.
    Атрибуты, заданные при открытии фазы, становятся её входом в Phoenix,
    заданные потом — выходом, если вход и выход не заданы явно.
    """

    def __init__(self, span: Any, session: bool = False, step: Any = None) -> None:
        self._span = span
        self._session = session
        self.step = step
        self._opened = False
        self._ended = False
        self._inputs: dict[str, Any] = {}
        self._outputs: dict[str, Any] = {}
        self._explicit: set[str] = set()

    @property
    def trace_id(self) -> str:
        try:
            return f"{self._span.get_span_context().trace_id:032x}"
        except Exception:
            return ""

    def set_attribute(self, key: str, value: Any) -> None:
        if value is None:
            return
        (self._outputs if self._opened else self._inputs)[key] = value
        self._raw(key, value)

    def set_input(self, value: Any) -> None:
        self._explicit.add("input")
        self._set_value("input", value)

    def set_output(self, value: Any) -> None:
        self._explicit.add("output")
        self._set_value("output", value)

    def update_name(self, name: str) -> None:
        try:
            self._span.update_name(name)
        except Exception:
            pass

    def add_event(self, name: str, **attributes: Any) -> None:
        # Гейт паспорта пишет события в спан сессии; пока выполняется
        # инструмент, событие — его результат и принадлежит его спану.
        tool = _ACTIVE_TOOL.get() if self._session else None
        target = tool._span if tool is not None else self._span
        try:
            if attributes:
                target.add_event(name, attributes={k: _attribute_value(sanitize(v)) for k, v in attributes.items()})
            else:
                target.add_event(name)
        except Exception:
            pass

    def record_error(self, exc: BaseException) -> None:
        try:
            from opentelemetry.trace import Status, StatusCode
            message = str(sanitize(str(exc))) or type(exc).__name__
            # A raw exception carries its original text and traceback; handing it
            # to an exporter can expose credentials despite sanitizing attributes.
            self._span.add_event("exception", attributes={
                "exception.type": type(exc).__name__, "exception.message": message,
            })
            self._span.set_status(Status(StatusCode.ERROR, message))
        except Exception:
            pass

    def finish(self) -> None:
        if self._ended:
            return
        self._ended = True
        if "input" not in self._explicit and self._inputs:
            self._set_value("input", self._inputs)
        if "output" not in self._explicit and self._outputs:
            self._set_value("output", self._outputs)
        try:
            self._span.end()
        except Exception:
            pass

    def _set_value(self, kind: str, value: Any) -> None:
        if isinstance(value, str):
            self._raw(f"{kind}.value", value)
            self._raw(f"{kind}.mime_type", _TEXT)
        else:
            self._raw(f"{kind}.value", _dumps(value))
            self._raw(f"{kind}.mime_type", _JSON)

    def _raw(self, key: str, value: Any) -> None:
        if value is None:
            return
        try:
            self._span.set_attribute(key, _attribute_value(sanitize(value)))
        except Exception:
            pass


# Tracer из Phoenix/OTel. None, если трассировка выключена или не
# инициализирована — в этом режиме `trace_span` работает как no-op,
# и код пайплайна не замечает отсутствия наблюдаемости.
_tracer: Optional[Any] = None
_provider: Optional[Any] = None
# True после любой попытки инициализации (успех/отказ/выключено).
_initialized: bool = False

# Текущая сессия агента, её открытый шаг и выполняемый инструмент.
_SESSION: ContextVar[Optional["_SessionState"]] = ContextVar("pko_trace_session", default=None)
_STEP: ContextVar[Optional[_SpanHandle]] = ContextVar("pko_trace_step", default=None)
_ACTIVE_TOOL: ContextVar[Optional[_SpanHandle]] = ContextVar("pko_trace_tool", default=None)


class _SessionState:
    """Какую часть очереди сообщений модель уже видела в этой сессии.

    Цикл агента дописывает сообщения в один и тот же список, а ужимает их на
    месте, не меняя длины. Поэтому новое для шага — хвост после прошлого
    вызова модели.
    """

    def __init__(self, handle: _SpanHandle) -> None:
        self.handle = handle
        self.messages: Any = None
        self.seen = 0

    def fresh(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        if messages is not self.messages:
            self.messages, self.seen = messages, 0
        first = self.seen == 0
        new = list(messages[self.seen:])
        self.seen = len(messages)
        return new, first


def tracing_enabled() -> bool:
    return _tracer is not None


def init_phoenix_tracing() -> Optional[object]:
    """Включить экспорт трейсов в Phoenix, если задан `PKO_PHOENIX_ENABLED`.

    Возвращает TracerProvider или None, если трассировка выключена, пакет
    `arize-phoenix-otel` не установлен или сервер недоступен. Прогон в любом
    случае продолжается: наблюдаемость не должна его останавливать.
    """
    global _tracer, _provider, _initialized

    if _initialized:
        return _provider
    _initialized = True

    enabled = os.environ.get("PKO_PHOENIX_ENABLED", "").lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("Phoenix tracing disabled via PKO_PHOENIX_ENABLED")
        return None

    host = os.environ.get("PKO_PHOENIX_HOST", "localhost")
    port = os.environ.get("PKO_PHOENIX_PORT", "6006")
    endpoint = os.environ.get("PKO_PHOENIX_ENDPOINT") or f"http://{host}:{port}/v1/traces"
    project = os.environ.get("PKO_PHOENIX_PROJECT", "belka-scenario")
    try:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from phoenix.otel import PROJECT_NAME, HTTPSpanExporter, Resource, TracerProvider
    except ImportError:
        print("Phoenix: PKO_PHOENIX_ENABLED=true, но пакет arize-phoenix-otel не установлен — "
              "трассировка выключена. Установите: pip install -r requirements-phoenix.txt",
              file=sys.stderr)
        return None
    if not _reachable(endpoint):
        print(f"Phoenix недоступен по адресу {endpoint}: трассировка этого запуска выключена. "
              "Запустите `phoenix serve` или проверьте PKO_PHOENIX_ENDPOINT.", file=sys.stderr)
        return None
    try:
        api_key = os.environ.get("PHOENIX_API_KEY")
        headers = {"authorization": f"Bearer {api_key}"} if api_key else None
        provider = TracerProvider(resource=Resource.create({PROJECT_NAME: project}),
                                  span_limits=span_limits(), endpoint=endpoint,
                                  protocol="http/protobuf", verbose=False)
        provider.add_span_processor(BatchSpanProcessor(
            HTTPSpanExporter(endpoint=endpoint, headers=headers),
            max_export_batch_size=_EXPORT_BATCH,
        ))
        _tracer = provider.get_tracer("pko")
        _provider = provider
    except Exception as exc:
        print(f"Phoenix: не удалось включить трассировку ({sanitize(str(exc))})", file=sys.stderr)
        return None
    print(f"Phoenix: {_base_url(endpoint)} · проект {project}", flush=True)
    return provider


def span_limits() -> Any:
    """Лимиты спанов под очередь сообщений агента; общие для CLI и тестов."""
    from opentelemetry.sdk.trace import SpanLimits
    return SpanLimits(max_span_attributes=_MAX_SPAN_ATTRIBUTES)


def flush_tracing() -> None:
    """Дождаться отправки спанов: CLI-процесс завершается сразу после прогона."""
    if _provider is None:
        return
    try:
        if not _provider.force_flush(_FLUSH_TIMEOUT_MS):
            print("Phoenix: не все спаны успели уйти за "
                  f"{_FLUSH_TIMEOUT_MS // 1000} с — трейс может быть неполным", file=sys.stderr)
    except Exception as exc:
        print(f"Phoenix: ошибка отправки спанов ({sanitize(str(exc))})", file=sys.stderr)


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[Any]:
    """Открыть именованный спан с атрибутами. No-op, если трассировка выключена.

    Все LLM-вызовы, инструменты и дочерние `trace_span` внутри блока
    становятся дочерними к этому спану — так выстраивается иерархия одного
    trace. Атрибуты из `**attributes` ставятся при открытии и считаются входом
    фазы; донастроить спан после выполнения можно через возвращаемый хэндл:

        with trace_span("load_plan", format=".pptx") as span:
            slides = parse(...)
            span.set_attribute("slide_count", len(slides))

    Исключение внутри блока помечается на спане (`record_error`) и
    пробрасывается дальше — трассировка не глотает ошибки.
    """
    with audit_span(name, attributes) as local:
        if _tracer is None:
            yield _AuditedSpan(_NoopSpan(), local)
            return

        from opentelemetry import trace as otel_trace

        is_session = name in SESSION_PASSES
        step = attributes.get("step") if name in STEP_PASSES else None
        span = _tracer.start_span(_display_name(name, step))
        handle = _SpanHandle(span, session=is_session, step=step)
        handle._raw(_KIND, "AGENT" if is_session else "CHAIN")
        handle._raw("pko.span", name)
        # One correlation ID connects the local journal to Phoenix.
        journal = current_audit()
        if journal is not None:
            handle._raw("pko.run_id", journal.run_id)
            handle._raw("session.id", journal.run_id)
        try:
            context = span.get_span_context()
            record("phoenix_span", name=name, trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}")
        except Exception:
            pass
        observed = _AuditedSpan(handle, local)
        for key, value in attributes.items():
            observed.set_attribute(key, value)
        handle._opened = True

        tokens: list[tuple[ContextVar, Any]] = []
        if is_session:
            tokens.append((_SESSION, _SESSION.set(_SessionState(handle))))
        if name in STEP_PASSES:
            tokens.append((_STEP, _STEP.set(handle)))
        try:
            with otel_trace.use_span(span, end_on_exit=False, record_exception=False,
                                     set_status_on_exception=False):
                yield observed
        except BaseException as exc:
            handle.record_error(exc)
            raise
        finally:
            for var, token in reversed(tokens):
                var.reset(token)
            handle.finish()


def trace_tool_call(answer: Callable[[Any, dict], dict], state: Any, call: dict) -> dict:
    """Выполнить вызов инструмента агента в TOOL-спане текущего шага.

    `answer` — `_answer_call` прохода: протокол или инструмент репозитория.
    Ответ возвращается без изменений; спан получает аргументы и результат
    ровно в том виде, в каком результат уходит модели.
    """
    if _tracer is None:
        return answer(state, call)

    from opentelemetry import trace as otel_trace

    function = call.get("function") or {}
    name = str(function.get("name") or "tool")
    span = _tracer.start_span(name)
    handle = _SpanHandle(span)
    handle._raw(_KIND, "TOOL")
    handle._raw("tool.name", name)
    handle._raw("pko.tool_call.id", str(call.get("id") or ""))
    journal = current_audit()
    if journal is not None:
        handle._raw("session.id", journal.run_id)
    handle.set_input(_arguments(function.get("arguments")))
    token = _ACTIVE_TOOL.set(handle)
    try:
        with otel_trace.use_span(span, end_on_exit=False, record_exception=False,
                                 set_status_on_exception=False):
            reply = answer(state, call)
    except BaseException as exc:
        handle.record_error(exc)
        handle.finish()
        raise
    finally:
        _ACTIVE_TOOL.reset(token)
    content = reply.get("content") if isinstance(reply, dict) else reply
    handle.set_output(sanitize("" if content is None else str(content)))
    payload = _parsed(content)
    if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
        handle.set_attribute("pko.tool.ok", payload["ok"])
        if not payload["ok"]:
            handle.add_event("tool_rejected", error=payload.get("error", ""))
    handle.finish()
    return reply


def trace_client(client: Any, spec: Any) -> Any:
    """Клиент, каждый `chat` которого — LLM-спан. Без трассировки — тот же клиент."""
    if _tracer is None or isinstance(client, TracedClient):
        return client
    if client is None:
        from pko.llm.client import ChatClient
        client = ChatClient(spec=spec)
    return TracedClient(client)


class TracedClient:
    """Прозрачная обёртка клиента: LLM-спан на каждый вызов `chat`.

    Спан несёт всю очередь сообщений (`llm.input_messages`), схемы
    инструментов, ответ с вызовами инструментов и токены. Шагу агента он
    ставит вход — новые сообщения очереди — и выход — ответ модели.
    """

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        # Очередь отправляется целиком на каждом шаге: сообщение, которое не
        # менялось, повторно не маскируется.
        self._clean: dict[int, tuple[Any, Any, Any, dict[str, Any]]] = {}
        self._schemas: tuple[Any, dict[str, Any]] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        if _tracer is None:
            return self.delegate.chat(messages, **kwargs)

        from opentelemetry import trace as otel_trace

        session = _SESSION.get()
        if session is not None:
            new, first = session.fresh(messages)
        else:
            new, first = list(messages), True
        # Сообщения ассистента в хвосте — выход прошлого шага, а не вход этого.
        incoming = [_brief_message(self._message(m)) for m in new if m.get("role") != "assistant"]
        step = _STEP.get()
        spec = getattr(self.delegate, "spec", None)
        model = str(getattr(spec, "model", "") or "")

        span = _tracer.start_span(f"llm · {model}" if model else "llm")
        handle = _SpanHandle(span)
        attributes: dict[str, Any] = {
            _KIND: "LLM",
            "llm.model_name": model,
            "llm.invocation_parameters": _invocation_parameters(kwargs),
            "pko.queue.messages": len(messages),
            "pko.queue.chars": sum(len(str(m.get("content") or "")) for m in messages),
        }
        journal = current_audit()
        if journal is not None:
            attributes["session.id"] = journal.run_id
        if step is not None and step.step is not None:
            attributes["pko.step"] = step.step
        for index, message in enumerate(messages):
            attributes.update(_message_attributes("llm.input_messages", index, self._message(message)))
        attributes.update(self._tool_attributes(kwargs.get("tools")))
        for key, value in attributes.items():
            handle._raw(key, value)
        handle.set_input(incoming)
        if step is not None and incoming:
            step.set_input(incoming)
        if session is not None and first:
            session.handle.set_input(incoming)

        try:
            with otel_trace.use_span(span, end_on_exit=False, record_exception=False,
                                     set_status_on_exception=False):
                result = self.delegate.chat(messages, **kwargs)
        except BaseException as exc:
            handle.record_error(exc)
            handle.finish()
            # The step may recover through a bounded retry. Its context manager
            # marks it failed only if the exception ultimately leaves the step.
            raise

        reply = sanitize({"role": "assistant", "content": result.text or "",
                          "tool_calls": result.tool_calls or []})
        for key, value in _message_attributes("llm.output_messages", 0, reply).items():
            handle._raw(key, value)
        usage = result.usage or {}
        for key, name in (("prompt_tokens", "prompt"), ("completion_tokens", "completion"),
                          ("total_tokens", "total")):
            if usage.get(key):
                handle._raw(f"llm.token_count.{name}", int(usage[key]))
        if "cached_tokens" in usage:
            handle._raw("llm.token_count.prompt_details.cache_read", int(usage["cached_tokens"]))
        handle._raw("pko.llm.from_cache", bool(result.from_cache))
        output = _reply_output(reply)
        handle.set_output(output)
        handle.finish()
        if step is not None:
            step.set_output(output)
            step.update_name(_step_title(step.step, reply.get("tool_calls") or []))
        return result

    def _message(self, message: dict[str, Any]) -> dict[str, Any]:
        content, calls = message.get("content"), message.get("tool_calls")
        cached = self._clean.get(id(message))
        if cached is not None and cached[0] is message and cached[1] is content and cached[2] is calls:
            return cached[3]
        clean = sanitize(message)
        self._clean[id(message)] = (message, content, calls, clean)
        return clean

    def _tool_attributes(self, tools: Any) -> dict[str, Any]:
        if not tools:
            return {}
        if self._schemas is None or self._schemas[0] is not tools:
            self._schemas = (tools, {
                f"llm.tools.{index}.tool.json_schema": _dumps(schema)
                for index, schema in enumerate(tools)
            })
        return self._schemas[1]


class _AuditedSpan:
    """Mirror bounded span metadata to the CLI journal, even without Phoenix."""

    def __init__(self, delegate: Any, local: Any = None) -> None:
        self._delegate = delegate
        self._local = local

    @property
    def trace_id(self) -> str:
        return self._delegate.trace_id

    def set_attribute(self, key: str, value: Any) -> None:
        self._delegate.set_attribute(key, value)
        if "reasoning" not in key:
            record("span_attribute", key=key, value=value)

    def set_input(self, value: Any) -> None:
        self._delegate.set_input(value)
        record("span_input", value=_bounded(value))

    def set_output(self, value: Any) -> None:
        self._delegate.set_output(value)
        record("span_output", value=_bounded(value))

    def set_status(self, status: str, reason: str = "") -> None:
        """Local terminal status for a failure the span's owner handled."""
        if self._local is not None:
            self._local.set_status(status, reason)

    def add_event(self, name: str, **attributes: Any) -> None:
        self._delegate.add_event(name, **attributes)
        record("gate_result", name=name, attributes=attributes)

    def record_error(self, exc: BaseException) -> None:
        self._delegate.record_error(exc)
        if self._local is not None:
            self._local.fail(exc)


def _bounded(value: Any, limit: int = 4000) -> Any:
    """Span payloads in the local journal are capped; truncation is explicit."""
    text = _dumps(value)
    return value if len(text) <= limit else {"truncated": True, "chars": len(text), "head": text[:limit]}


def _display_name(name: str, step: Any) -> str:
    if name in _SESSION_LABELS:
        return _SESSION_LABELS[name]
    if name in STEP_PASSES and step is not None:
        return f"шаг {step}"
    return name


def _step_title(step: Any, calls: list[dict[str, Any]]) -> str:
    """«шаг 3 · read_file ×2, submit_stage» — что агент сделал на шаге."""
    counts: dict[str, int] = {}
    for call in calls:
        name = str((call.get("function") or {}).get("name") or "?")
        counts[name] = counts.get(name, 0) + 1
    tools = ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in counts.items())
    title = f"шаг {step}" if step is not None else "шаг"
    return f"{title} · {tools}" if tools else f"{title} · без инструмента"


def _message_attributes(prefix: str, index: int, message: dict[str, Any]) -> dict[str, Any]:
    """Сообщение в плоских атрибутах OpenInference: так Phoenix рисует чат."""
    base = f"{prefix}.{index}.message"
    out: dict[str, Any] = {f"{base}.role": str(message.get("role") or "")}
    content = message.get("content")
    if content not in (None, ""):
        out[f"{base}.content"] = content if isinstance(content, str) else _dumps(content)
    if message.get("tool_call_id"):
        out[f"{base}.tool_call_id"] = str(message["tool_call_id"])
    for number, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        key = f"{base}.tool_calls.{number}.tool_call"
        out[f"{key}.id"] = str(call.get("id") or "")
        out[f"{key}.function.name"] = str(function.get("name") or "")
        out[f"{key}.function.arguments"] = str(function.get("arguments") or "")
    return out


def _brief_message(message: dict[str, Any]) -> dict[str, Any]:
    brief = {"role": message.get("role"), "content": message.get("content")}
    if message.get("tool_call_id"):
        brief["tool_call_id"] = message["tool_call_id"]
    return brief


def _reply_output(reply: dict[str, Any]) -> Any:
    calls = reply.get("tool_calls") or []
    if not calls:
        return str(reply.get("content") or "")
    output: dict[str, Any] = {}
    if reply.get("content"):
        output["content"] = reply["content"]
    output["tool_calls"] = [
        {"id": call.get("id"),
         "name": (call.get("function") or {}).get("name"),
         "arguments": _parsed((call.get("function") or {}).get("arguments"))}
        for call in calls
    ]
    return output


def _invocation_parameters(kwargs: dict[str, Any]) -> str:
    parameters = {key: value for key, value in kwargs.items() if key != "tools"}
    if kwargs.get("tools"):
        parameters["tool_choice"] = "auto"
    return _dumps(parameters)


def _arguments(raw: Any) -> Any:
    """Аргументы вызова: объект, если это JSON, иначе строка как есть."""
    value = _parsed(raw)
    return sanitize(value) if isinstance(value, (dict, list)) else sanitize(str(value or ""))


def _parsed(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _attribute_value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return _dumps(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _base_url(endpoint: str) -> str:
    suffix = "/v1/traces"
    return endpoint[: -len(suffix)] if endpoint.endswith(suffix) else endpoint


def _reachable(endpoint: str) -> bool:
    """Отвечает ли сервер Phoenix. Любой HTTP-ответ, даже ошибка, — отвечает."""
    try:
        urllib.request.urlopen(_base_url(endpoint), timeout=_PROBE_TIMEOUT_SECONDS).close()
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True
