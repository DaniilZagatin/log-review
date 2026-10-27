"""Read registry for report stages: evidence comes only from issued lines.

Stages hand the model numbered fragments of the masked source snapshot. A
citation is accepted only inside a range issued during this run; the quote is
filled from the snapshot by code, never taken from the model.
"""
from __future__ import annotations

MAX_FRAGMENT_LINES = 80


class EvidenceRegistry:
    def __init__(self, tree):
        self.tree = tree
        self.issued: dict[str, list[tuple[int, int]]] = {}
        self._lines: dict[str, list[str] | None] = {}

    def _source(self, path: str) -> list[str] | None:
        if path not in self._lines:
            text = self.tree.read(path)
            self._lines[path] = None if text is None else text.splitlines()
        return self._lines[path]

    def fragment(self, path: str, start: int, end: int, *, focus: int | None = None) -> dict | None:
        """Issue lines ``start..end`` (clamped, at most MAX_FRAGMENT_LINES around ``focus``)."""
        lines = self._source(path)
        if not lines:
            return None
        start, end = max(1, start), min(len(lines), end)
        if end - start + 1 > MAX_FRAGMENT_LINES:
            centre = focus if focus is not None else start
            start = max(1, min(centre - MAX_FRAGMENT_LINES // 2, len(lines) - MAX_FRAGMENT_LINES + 1))
            end = min(len(lines), start + MAX_FRAGMENT_LINES - 1)
        if start > end:
            return None
        self.issued.setdefault(path, []).append((start, end))
        numbered = "\n".join(f"{number}| {lines[number - 1]}" for number in range(start, end + 1))
        return {"path": path, "line_start": start, "line_end": end, "text": numbered}

    def covers(self, path: str, start: int, end: int) -> bool:
        return any(first <= start and end <= last for first, last in self.issued.get(path, []))

    def resolve(self, refs, label: str = "evidence") -> tuple[list[dict], list[str]]:
        """Return citations with exact quotes, or addressed errors for the model."""
        if not isinstance(refs, list):
            return [], [f"{label}: ожидается список ссылок path/line_start/line_end"]
        result, errors = [], []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) - {"path", "line_start", "line_end"}:
                errors.append(f"{label}: ссылка содержит только path, line_start и line_end")
                continue
            path, start, end = ref.get("path"), ref.get("line_start"), ref.get("line_end")
            if not isinstance(path, str) or type(start) is not int or type(end) is not int or not 1 <= start <= end:
                errors.append(f"{label}: неверные path или номера строк")
                continue
            if not self.covers(path, start, end):
                errors.append(f"{label}: {path}:{start}-{end} не входит в выданные фрагменты")
                continue
            lines = self._source(path) or []
            result.append({"path": path, "line_start": start, "line_end": end,
                           "quote": "\n".join(lines[start - 1:end])})
        return result, errors


def overlaps(ref: dict, path: str, start: int, end: int) -> bool:
    return ref["path"] == path and ref["line_start"] <= end and start <= ref["line_end"]
