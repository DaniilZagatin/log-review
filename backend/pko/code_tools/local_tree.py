"""Minimal immutable-content file tree used by the scenario source loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

MAX_FILE_BYTES = 2_000_000
MAX_WORKSPACE_FILES = 5000
MAX_WORKSPACE_BYTES = 200_000_000


@dataclass
class LocalTree:
    """Admitted snapshot texts; unknown paths never trigger a filesystem read.

    The loader writes the masked snapshot and fills this cache once. ``root``
    identifies its location for diagnostics, not arbitrary read access.
    """

    root: Path
    files: list[str] = field(default_factory=list)
    _text_cache: dict[str, str | None] = field(default_factory=dict, repr=False)
    _files_set: set[str] | None = field(default=None, repr=False)

    @property
    def files_set(self) -> set[str]:
        if self._files_set is None:
            self._files_set = set(self.files)
        return self._files_set

    def read(self, path: str) -> str | None:
        return self._text_cache.get(path) if path in self.files_set else None

    def match(self, suffixes: Iterable[str] = (), names: Iterable[str] = ()) -> list[str]:
        suffixes = tuple(suffixes)
        names = {name.lower() for name in names}
        return [path for path in self.files
                if (suffixes and path.lower().endswith(suffixes)) or
                (names and path.rsplit("/", 1)[-1].lower() in names)]
