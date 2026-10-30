"""Model connection values and known context window sizes."""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ModelSpec:
    role: str
    base_url: str
    model: str
    api_key: str
    extra_body: dict[str, Any] | None = None
    # Пути к клиентскому сертификату/ключу для mTLS (DeepSeek в AI Hub и т.п.).
    # Пустые строки — эндпоинт без mTLS, клиент строится без http_client= как
    # раньше. Путь — не секрет, но файл ключа в .gitignore (см. .gitignore).
    client_cert: str = ""
    client_key: str = ""
    # Окно контекста в токенах. Нужно агенту: он отправляет всю историю на
    # каждом шаге, и без потолка она перерастает окно — эндпоинт отвечает
    # отказом, а прогон теряется целиком. 0 — неизвестно, тогда действует
    # DEFAULT_CONTEXT_WINDOW.
    context_window: int = 0

    @property
    def upstream_model(self) -> str:
        """DeepSeek-endpoint игнорирует имя модели — если роль сконфигурирована под него."""
        return "" if "deep" in self.model.lower() else self.model

    def masked(self) -> dict[str, str]:
        return {"role": self.role, "base_url": self.base_url, "model": self.model,
                "api_key": "<задан>" if self.api_key else "<пусто>",
                "mtls": "on" if self.client_cert else "off"}


# Окно по умолчанию — DeepSeek V4, на котором Белка работает по умолчанию:
# 524 288 токенов, как в рабочих прогонах deepseek-v4-flash. Модель с меньшим
# окном задаётся в таблице ниже или переменной `PKO_CONTEXT_WINDOW`: завышенное
# окно приводит к отказу эндпоинта, а заниженное лишь раньше включает сжатие.
DEFAULT_CONTEXT_WINDOW = 524_288

# Окна контекста известных моделей, в токенах. Значение из таблицы важнее
# окна по умолчанию; переменная `PKO_CONTEXT_WINDOW` важнее таблицы.
_CONTEXT_WINDOWS = (
    ("kimi", 178_000),
    # GLM-5.2 держит длинный контекст и на нём работает устойчиво: проверено
    # до 300к. Окно важно именно для этого инструмента — агент читает файлы
    # целиком, и на прежних 128к история начинала ужиматься к середине
    # большого репозитория, то есть агент перечитывал уже прочитанное вместо
    # того, чтобы идти дальше.
    ("glm", 300_000),
    ("deepseek", DEFAULT_CONTEXT_WINDOW),
    ("qwen", 128_000),
)


def context_window_for(model: str) -> int:
    """Окно модели в токенах: переменная окружения важнее таблицы."""
    raw = (os.environ.get("PKO_CONTEXT_WINDOW") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    low = (model or "").lower()
    for marker, window in _CONTEXT_WINDOWS:
        if marker in low:
            return window
    return 0
