"""Автоматическая загрузка переменных из `.env` в корне репозитория.

Используется `python-dotenv`. Если файла нет или пакет не установлен —
молча продолжаем, чтобы проект оставался рабочим и без `.env`.
"""

from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DOTENV = _PROJECT_ROOT / ".env"


def load_env() -> None:
    """Загрузить переменные из `.env`, если файл существует."""
    if load_dotenv is None:
        return
    if not _DOTENV.exists():
        return
    load_dotenv(dotenv_path=_DOTENV, override=True)


__all__ = ["load_env"]
