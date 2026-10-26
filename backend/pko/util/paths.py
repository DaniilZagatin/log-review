"""Права на служебные каталоги PKO.

Правило одно на все кеши: и зеркало репозитория, и кеш ответов модели содержат
сведения о корпоративной системе — имена объектов, пути пакетов, список пробелов.
Раз аргумент конфиденциальности одинаковый, защита тоже должна быть одинаковой,
а не зависеть от umask процесса.
"""

from __future__ import annotations

import os
from pathlib import Path

OWNER_ONLY_DIR = 0o700
OWNER_ONLY_FILE = 0o600


def harden_dir(*paths: Path | str) -> None:
    """Сделать каталоги доступными только владельцу. Ошибки прав не критичны."""
    for path in paths:
        try:
            p = Path(path)
            if p.exists():
                os.chmod(p, OWNER_ONLY_DIR)
        except OSError:
            pass


def harden_file(path: Path | str) -> None:
    """То же для файла с содержимым кеша."""
    try:
        p = Path(path)
        if p.exists():
            os.chmod(p, OWNER_ONLY_FILE)
    except OSError:
        pass
