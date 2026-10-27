"""Запись результатов через временные файлы с откатом при ошибке замены."""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pko.errors import PkoError

@dataclass
class WrittenFile:
    kind: str
    path: Path


def write_outputs(out_dir: Path, files: dict[str, tuple[str, str]]) -> list[WrittenFile]:
    """Транзакционно записать полный комплект отчётов.

    Сначала все файлы создаются и валидируются на том же filesystem. Если хотя
    бы один replace не удался, восстанавливается весь прежний комплект.
    """
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    _validate_names(name for name, _ in files.values())

    with tempfile.TemporaryDirectory(prefix=".pko-stage-", dir=out_dir.parent) as raw:
        stage = Path(raw)
        staged: list[tuple[str, Path, Path]] = []
        for kind, (name, content) in files.items():
            source = stage / name
            # Exact bytes: no platform newline translation, so manifests can fingerprint content.
            source.write_bytes(content.encode("utf-8"))
            staged.append((kind, source, out_dir / name))
        _validate_staged([source for _, source, _ in staged])
        _commit_staged([(source, target) for _, source, target in staged])

    return [WrittenFile(kind=kind, path=target) for kind, _, target in staged]




def _validate_names(names) -> None:
    seen: set[str] = set()
    for name in names:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise PkoError(f"Недопустимое имя выходного файла: {name}")
        if name in seen:
            raise PkoError(f"Дублирующееся имя выходного файла: {name}")
        seen.add(name)


def _validate_staged(paths: list[Path]) -> None:
    """Проверить весь stage до первого изменения целевого набора."""
    for path in paths:
        try:
            if not path.is_file() or path.stat().st_size == 0:
                raise PkoError(f"Сгенерирован пустой артефакт: {path.name}")
            path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise PkoError(f"Артефакт {path.name} не является UTF-8 текстом.") from exc
        except OSError as exc:
            raise PkoError(f"Не удалось проверить артефакт {path.name}: {exc}") from exc


def _replace(source: Path, target: Path) -> None:
    """Atomic replace with a short, bounded retry for Windows access conflicts.

    Access denied can be permanent as well as temporary. Retry only the native
    access/sharing/lock errors; after 0.75 seconds of backoff the original OS
    failure still reaches the caller's transaction rollback. Permissions and
    file guards remain unchanged.
    """
    delays = (0.05, 0.1, 0.2, 0.4)
    for attempt in range(len(delays) + 1):
        try:
            source.replace(target)
            return
        except OSError as exc:
            if (sys.platform != "win32" or getattr(exc, "winerror", None) not in (5, 32, 33)
                    or attempt == len(delays)):
                raise
            time.sleep(delays[attempt])


def _commit_staged(staged: list[tuple[Path, Path]]) -> None:
    """Заменить набор файлов с полным rollback при ошибке любого replace."""
    if not staged:
        return
    parent = staged[0][1].parent
    for _, target in staged:
        if target.parent != parent:
            raise PkoError("Все файлы одной транзакции должны иметь общий каталог назначения.")
    parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".pko-rollback-", dir=parent) as raw:
        rollback = Path(raw)
        previous: dict[Path, Path | None] = {}
        for index, (_, target) in enumerate(staged):
            if target.exists():
                saved = rollback / f"{index}-{target.name}"
                shutil.copy2(target, saved)
                previous[target] = saved
            else:
                previous[target] = None

        try:
            for source, target in staged:
                _replace(source, target)
        except OSError as exc:
            restore_errors: list[str] = []
            for target, saved in previous.items():
                try:
                    if saved is None:
                        target.unlink(missing_ok=True)
                    else:
                        restore = rollback / (saved.name + ".restore")
                        shutil.copy2(saved, restore)
                        _replace(restore, target)
                except OSError as restore_exc:
                    restore_errors.append(f"{target.name}: {restore_exc}")
            detail = f"Не удалось атомарно обновить комплект отчётов: {exc}"
            if restore_errors:
                detail += "; ошибки восстановления: " + "; ".join(restore_errors)
            raise PkoError(detail, "Предыдущий комплект сохранён в backup/rollback насколько возможно.") from exc
