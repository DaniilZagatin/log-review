"""Small evidence and extraction-coverage types shared by source analyzers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    """Прямая ссылка на факт: коммит, путь, строка и краткое основание.

    Соответствует облегчённому режиму доказательств BASIC (§5.2.4): источник
    однозначно идентифицируется, привязан к неизменяемой версии и к конкретному
    утверждению. Текст кода намеренно не хранится.
    """

    commit: str
    path: str
    line: int | None = None
    basis: str = ""

    def ref(self) -> str:
        """Полная ссылка для машин и для сверки: `путь:строка@коммит`."""
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"{loc}@{self.commit[:8]}"

    def where(self) -> str:
        """Место без коммита: он один на всю версию и печатается в шапке.

        `path:line@6a225969` в каждой строке паспорта — это шум: хеш
        повторяется дословно сотни раз и ничего не добавляет читателю.
        """
        return f"{self.path}:{self.line}" if self.line else self.path

    def explain(self) -> str:
        """Чем это доказано, словами. Пусто — значит, объяснения нет."""
        return self.basis.strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Evidence":
        return Evidence(
            commit=d["commit"], path=d["path"], line=d.get("line"), basis=d.get("basis", "")
        )


@dataclass
class Coverage:
    """Насколько репозиторий вообще был проанализирован.

    Без этой цифры пустая модель выглядит «чистой»: непокрытый фронтенд
    неотличим от отсутствующей функциональности.
    """

    files_total: int = 0
    files_analyzed: int = 0
    analyzed_globs: list[str] = field(default_factory=list)
    skipped_globs: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return (self.files_analyzed / self.files_total) if self.files_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ratio"] = round(self.ratio, 4)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Coverage":
        return Coverage(
            files_total=d.get("files_total", 0),
            files_analyzed=d.get("files_analyzed", 0),
            analyzed_globs=list(d.get("analyzed_globs", [])),
            skipped_globs=list(d.get("skipped_globs", [])),
        )
