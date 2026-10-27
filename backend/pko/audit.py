"""A durable record of observable actions and evidence, independent of Phoenix."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pko.util.paths import harden_file

_CURRENT: ContextVar[Any] = ContextVar("pko_audit", default=None)
_SPAN: ContextVar[str | None] = ContextVar("pko_audit_span", default=None)
SCHEMA = "belka-audit/2"
_PRIVATE = re.compile(r"reasoning|chain.of.thought|thinking|api.?key|password|secret|authorization|credential|access.?token", re.I)


def sanitize(value: Any) -> Any:
    """Best-effort credential masking; never export provider private reasoning."""
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() in ("reasoning", "thinking", "redacted_thinking"):
            return {"type": "omitted_private_reasoning"}
        return {str(k): sanitize(v) for k, v in value.items() if not _PRIVATE.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value if value is None or isinstance(value, (bool, int, float)) else str(value)
    # Tool arguments and OpenInference messages can contain nested JSON strings.
    if value.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(sanitize(json.loads(value)), ensure_ascii=False)
        except (ValueError, TypeError, RecursionError):
            pass
    for key, secret in os.environ.items():
        if _PRIVATE.search(key) and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"<(think|thinking|reasoning)>.*?</\1>", "[OMITTED]", value, flags=re.S | re.I)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED]", value, flags=re.S)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    value = re.sub(r"(?i)((?:password|api[_-]?key|secret|access[_-]?token)\s*[=:]\s*)[\"']?[^\s,;\"']+", r"\1[REDACTED]", value)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)


class RunAudit:
    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        self.sequence = 0
        self.spans = 0
        self.failed = False
        self.clients: list[AuditedClient] = []

    def emit(self, event: str, **fields: Any) -> None:
        if self.failed:
            return
        try:
            self.sequence += 1
            # Every event names the span it happened in, so model calls and tool
            # results join the stage tree without Phoenix.
            fields.setdefault("span_id", _SPAN.get())
            entry = sanitize({"event": event, "run_id": self.run_id, **fields})
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                harden_file(self.path)
                stream.write(f"\n=== {self.sequence} | {stamp} | {event} ===\n")
                stream.write(json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:
            self.failed = True
            print(f"Журнал анализа недоступен ({type(exc).__name__}): {self.path}", file=sys.stderr)

    def flush(self) -> None:
        for client in self.clients:
            client.flush()


def record(event: str, **fields: Any) -> None:
    audit = _CURRENT.get()
    if audit is not None:
        audit.emit(event, **fields)


def current_audit() -> RunAudit | None:
    """Current run identity for the optional trace exporter."""
    return _CURRENT.get()


@contextmanager
def audit_run(path: Path, run_id: str):
    audit = RunAudit(path, run_id)
    token = _CURRENT.set(audit)
    audit.emit("run_started", schema=SCHEMA,
               scope="Наблюдаемые действия, материалы, ответы инструментов и объяснения выводов. Внутренние рассуждения не записываются.")
    try:
        yield audit
    except BaseException as exc:
        audit.flush()
        audit.emit("run_finished", result="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                   error_type=type(exc).__name__, error_message=str(exc), hint=getattr(exc, "hint", ""))
        raise
    else:
        audit.flush()
        audit.emit("run_finished", result=getattr(audit, "outcome", "success"))
    finally:
        _CURRENT.reset(token)


def flush() -> None:
    audit = _CURRENT.get()
    if audit is not None:
        audit.flush()


class LocalSpan:
    """Terminal status of a local span; a handled failure is reported by its owner."""

    def __init__(self, span_id: str | None):
        self.span_id = span_id
        self.status: str | None = None
        self.error: dict = {}

    def fail(self, exc: BaseException) -> None:
        self.status, self.error = "error", {"error_type": type(exc).__name__, "error_message": str(exc)}

    def set_status(self, status: str, reason: str = "") -> None:
        self.status = status
        if reason:
            self.error = {"reason": reason}


@contextmanager
def audit_span(name: str, attributes: dict):
    """A local span with its own ID and parent, so the tree survives without Phoenix.

    Every started span ends with a terminal status, including cancellation and
    exceptions; durations come from the monotonic clock. A failure handled
    inside the span is reported through the yielded ``LocalSpan``.
    """
    audit = _CURRENT.get()
    parent = _SPAN.get()
    span_id = None
    if audit is not None:
        audit.spans += 1
        span_id = f"span-{audit.spans}"
    token = _SPAN.set(span_id)
    started = time.monotonic()
    record("span_started", name=name, span_id=span_id, parent_span_id=parent, attributes=attributes)
    status, error, local = "ok", {}, LocalSpan(span_id)
    try:
        yield local
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, (KeyboardInterrupt, GeneratorExit)) else "error"
        error = {"error_type": type(exc).__name__, "error_message": str(exc)}
        record("span_failed", name=name, span_id=span_id, parent_span_id=parent, **error)
        raise
    finally:
        _SPAN.reset(token)
        if local.status and status == "ok":
            status, error = local.status, local.error
        if name == "scenario_research":
            flush()
        record("span_finished", name=name, span_id=span_id, parent_span_id=parent, status=status,
               duration_seconds=round(time.monotonic() - started, 3), **error)


class AuditedClient:
    """Record supplied context and public tool proposals, excluding reasoning."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.messages = None
        self.seen: dict[int, Any] = {}
        audit = _CURRENT.get()
        if audit is not None:
            audit.clients.append(self)

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def flush(self):
        for message in self.messages or []:
            if id(message) not in self.seen:
                self.seen[id(message)] = message
                record("context_message", message=message)

    def chat(self, messages, **kwargs):
        self.flush()
        if self.messages is not messages:
            self.messages, self.seen = messages, {}
            record("tool_schemas", tools=kwargs.get("tools"))
        self.flush()
        record("model_request", model=self.delegate.spec.model,
               parameters={k: v for k, v in kwargs.items() if k != "tools"})
        started = time.monotonic()
        try:
            result = self.delegate.chat(messages, **kwargs)
        except BaseException as exc:
            record("model_error", error_type=type(exc).__name__, error_message=str(exc), hint=getattr(exc, "hint", ""),
                   finish_reason=getattr(exc, "finish_reason", ""), usage=getattr(exc, "usage", {}))
            raise
        record("model_response", text=result.text, tool_calls=result.tool_calls,
               usage=result.usage, from_cache=result.from_cache,
               finish_reason=getattr(result, "finish_reason", ""),
               duration_seconds=round(time.monotonic() - started, 3))
        return result
