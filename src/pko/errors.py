"""Ошибки PKO с человекочитаемой подсказкой, что делать."""

from __future__ import annotations


class PkoError(Exception):
    """Базовая ошибка PKO. `hint` показывается пользователю отдельной строкой."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n  → {self.hint}"
        return self.message


class GitError(PkoError):
    """Отказ git-команды."""


class SshAccessError(GitError):
    """Нет доступа к удалённому репозиторию по SSH."""


class UrlError(PkoError):
    """Не удалось разобрать ссылку на репозиторий."""


class ExtractionError(PkoError):
    """Сбой анализа кода."""


class ValidationError(PkoError):
    """Модель или отчёт не прошли детерминированную проверку."""


class LlmError(PkoError):
    """Сбой обращения к языковой модели."""
