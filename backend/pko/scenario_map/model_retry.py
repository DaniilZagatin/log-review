"""Bounded model retries; graph tools are never replayed by this layer."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import random
import ssl
import time

import httpx
import openai

from pko.audit import record, sanitize
from pko.errors import LlmError
from pko.llm.tracing import trace_span

MAX_MODEL_ATTEMPTS = 4


class ModelDeadlineExceeded(TimeoutError):
    """The run budget cannot accommodate another model request."""


def _causes(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def retry_delay(error: Exception, attempt: int) -> float | None:
    """Use typed transport causes, never guess recoverability from error text."""
    causes = list(_causes(error))
    if any(isinstance(exc, (ModelDeadlineExceeded, ssl.SSLCertVerificationError)) for exc in causes):
        return None
    status_error = next((exc for exc in causes if isinstance(exc, (openai.APIStatusError, httpx.HTTPStatusError))), None)
    if status_error is not None:
        response = status_error.response
        status = response.status_code
        if status not in (408, 429) and not 500 <= status <= 599:
            return None
    elif not any(isinstance(exc, (openai.APIConnectionError, httpx.NetworkError,
                                 httpx.TimeoutException, httpx.RemoteProtocolError,
                                 ConnectionError, TimeoutError)) for exc in causes):
        return None
    delay = min(8, 2 ** attempt) * random.uniform(.8, 1.2)
    if status_error is not None:
        raw = response.headers.get('retry-after')
        if raw:
            try:
                seconds = float(raw)
            except ValueError:
                try:
                    date = parsedate_to_datetime(raw)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    seconds = (date - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            if math.isfinite(seconds) and seconds > 0:
                delay = max(delay, seconds)
    return delay


def call_model(client, messages, *, deadline: float, before_retry=None, before_attempt=None, on_event=None, **kwargs):
    """Retry one unchanged conversation turn inside its existing trace and budget."""
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded('Достигнут лимит времени исследования при обращении к модели.')
        # The owner charges each physical dispatch, including transport retries.
        # Admission failures are not transport errors and must not be retried.
        if before_attempt:
            before_attempt()
        try:
            with trace_span('model_attempt', attempt=attempt, max_attempts=MAX_MODEL_ATTEMPTS):
                return client.chat(messages, **kwargs)
        except Exception as exc:
            delay = retry_delay(exc, attempt)
            if delay is None:
                raise
            if attempt >= MAX_MODEL_ATTEMPTS:
                record('model_retries_exhausted', attempts=attempt, error_type=type(exc).__name__)
                hint = str(sanitize(getattr(exc, 'hint', '') or str(exc)))[:500]
                raise LlmError(f'Не удалось получить ответ модели после {attempt} попыток.', hint) from exc
            # Save progress before waiting, including when interrupted during backoff.
            if before_retry:
                before_retry()
            if time.monotonic() + delay >= deadline:
                raise ModelDeadlineExceeded('Недостаточно времени для повторного подключения к модели.') from exc
            data = {'attempt': attempt, 'next_attempt': attempt + 1,
                    'max_attempts': MAX_MODEL_ATTEMPTS, 'delay_seconds': round(delay, 2),
                    'error_type': type(exc).__name__}
            record('model_retry', **data)
            with trace_span('model_retry_wait', **data):
                if on_event:
                    on_event('model_retry', data)
                time.sleep(delay)
