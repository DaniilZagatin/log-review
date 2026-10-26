"""OpenAI-compatible chat/tool client with mTLS and no durable conversation cache."""
from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any

import httpx
import openai

from pko.errors import LlmError
from pko.llm.registry import ModelSpec
from pko.audit import sanitize


@dataclass
class ChatResult:
    """Ответ модели вместе с расходом токенов.

    Агентскому циклу мало текста: в трассу нужно записать, сколько стоил шаг и
    что именно вернул endpoint, иначе разбирать неудачный прогон не по чему.
    `reasoning_content` — внутренняя цепочка рассуждений модели (thinking),
    доступна только в текущем ответе, не подменяет `text`/`tool_calls` и не
    добавляется в очередь сообщений, память исследования, аудит или трассу.
    """

    text: str
    usage: dict[str, Any]
    from_cache: bool = False
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str = ""


# Бюджет ответа по умолчанию. Прежние 2000 не оставляли места тексту, когда
# у роли включён режим рассуждения: рассуждение съедало лимит целиком, и
# `content` приходил пустым.
DEFAULT_MAX_TOKENS = 8192

# Таймаут одного обращения. Повторов в клиенте нет: бюджетом и продолжением
# исследования управляет вызывающий агент.
DEFAULT_TIMEOUT = 180

@dataclass
class ChatClient:
    spec: ModelSpec
    timeout: int = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        # Единственная точка создания openai-клиента — Phoenix auto_instrument
        # перехватывает его вызовы, если init_phoenix_tracing() уже отработал.
        #
        # mTLS (DeepSeek в AI Hub и т.п.): эндпоинт требует клиентского серта.
        # Рабочая комбинация — verify=CERT_NONE + клиентский серт через
        # load_cert_chain (см. DEEPSEEK_MIGRATION.md §4: при verify=путь-к-CA
        # httpx НЕ шлёт client cert → TLSV13_ALERT_CERTIFICATE_REQUIRED).
        # Передаём httpx.Client в openai SDK — одна точка патча, и все вызовы
        # (chat.completions.create, models.list) идут с сертом. Без сертов —
        # клиент строится как раньше, без http_client=, обратная совместимость.
        self._client = openai.OpenAI(
            base_url=self.spec.base_url,
            api_key=self.spec.api_key or "not-needed",
            timeout=self.timeout,
            max_retries=0,
            http_client=self._build_http_client(),
        )

    def _build_http_client(self) -> httpx.Client | None:
        """httpx-клиент с mTLS, если роль сконфигурирована под сертовый эндпоинт.

        `None` — эндпоинт без mTLS: openai SDK создаст свой httpx-клиент по
        умолчанию (старое поведение, обратная совместимость для GLM/sglang).
        """
        if not self.spec.client_cert:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=self.spec.client_cert, keyfile=self.spec.client_key)
        return httpx.Client(verify=ctx, timeout=self.timeout)




    def chat(self, messages: list[dict[str, Any]], temperature: float = 0.0,
             max_tokens: int = DEFAULT_MAX_TOKENS,
             tools: list[dict[str, Any]] | None = None) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.spec.upstream_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        extra_body = self.spec.extra_body

        response = self._create(payload, extra_body=extra_body)
        text, tool_calls, reasoning = self._message_of(response)
        usage = self._usage_of(response)
        return ChatResult(text=text, usage=usage, tool_calls=tool_calls,
                          reasoning_content=reasoning)

    # --- вызов модели -----------------------------------------------------

    def _create(self, payload: dict[str, Any], extra_body: dict[str, Any] | None = None) -> Any:
        options = dict(payload)
        if extra_body:
            options["extra_body"] = extra_body
        try:
            return self._client.chat.completions.create(**options)
        except Exception as exc:
            raise LlmError("Не удалось получить ответ модели.", str(sanitize(str(exc)))[:500]) from exc

    # --- разбор ответа ----------------------------------------------------

    def _require_text(self, text: str, context: dict[str, str]) -> None:
        if text and text.strip():
            return
        finish = context.get("finish_reason", "")
        hint = "Проверьте лимит длины ответа и режим рассуждения для этой роли."
        if finish == "length":
            hint = ("Ответ оборван по длине: увеличьте max_tokens или отключите "
                    "режим рассуждения для этой роли.")
        raise LlmError(
            f"Модель {self.spec.model} (роль {self.spec.role}) вернула пустой ответ.",
            hint,
        )

    def _message_of(self, response: Any) -> tuple[str, list[dict[str, Any]] | None, str]:
        """Текст, tool_calls и reasoning_content ответа для `chat()`.

        Пустой `content` при непустом `tool_calls` — штатная форма ответа при
        вызове инструмента (многие OpenAI-совместимые бэкенды отдают именно
        `content: null`), не отказ. Отказ — только когда пусты оба поля разом.
        `reasoning_content` разбирается отдельно и не сохраняется в истории;
        оно не подменяет `content` (endpoint, исчерпавший бюджет
        на thinking, тем самым не выполнил запрос — `_require_chat_result`
        это поймает по пустым content+tool_calls)."""
        try:
            message = response.choices[0].message
            finish = str(response.choices[0].finish_reason or "")
        except (AttributeError, IndexError) as exc:
            raise LlmError(
                f"Неожиданный ответ модели {self.spec.model}.",
                "Проверьте, что endpoint OpenAI-совместим.",
            ) from exc

        text = str(message.content or "").strip()
        raw_tool_calls = message.tool_calls if hasattr(message, "tool_calls") else None
        tool_calls = self._serialize_tool_calls(raw_tool_calls)
        reasoning = str(getattr(message, "reasoning_content", "") or getattr(message, "reasoning", "") or "")
        self._require_chat_result(text, tool_calls, {"finish_reason": finish})
        return text, tool_calls, reasoning

    def _serialize_tool_calls(self, raw: Any) -> list[dict[str, Any]] | None:
        """Привести tool_calls из openai SDK к dict-формату для истории сессии.

        openai SDK отдаёт Pydantic-модели; агентский цикл работает с dict'ами
        (JSON-сообщения). Без нормализации `message.tool_calls` осел бы в
        history как объект, а не dict."""
        if not raw:
            return None
        out: list[dict[str, Any]] = []
        for tc in raw:
            out.append({
                "id": str(getattr(tc, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(getattr(tc.function, "name", "") or ""),
                    "arguments": str(getattr(tc.function, "arguments", "") or ""),
                },
            })
        return out or None

    def _usage_of(self, response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }

    def _require_chat_result(self, text: str, tool_calls: list[dict[str, Any]] | None,
                              context: dict[str, str]) -> None:
        if tool_calls or (text and text.strip()):
            return
        self._require_text(text, context)
