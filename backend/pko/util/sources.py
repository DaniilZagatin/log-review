"""Переносимые идентификаторы внешних входов PKO.

Файл, переданный оператором флагом, лежит вне анализируемого репозитория.
Абсолютный путь к нему не является свойством системы и не должен попадать в
публикуемые evidence, JSON-аудиты или отчёты: на другой машине этот путь
бесполезен, зато раскрывает имя пользователя и внутреннюю структуру каталогов.

Идентификатор сохраняет только basename и короткий SHA-256 содержимого. Этого
достаточно, чтобы различить два одноимённых входа и сверить, какой именно файл
участвовал в прогоне, не публикуя его локальное расположение.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

EXTERNAL_SOURCE_PREFIX = "external"


def portable_source(path: Path | str, content: bytes | str) -> str:
    """Вернуть `external/<basename>-<digest><suffix>` без родительского пути."""
    source = Path(path)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    digest = hashlib.sha256(payload).hexdigest()[:12]
    suffix = "".join(source.suffixes)
    stem = source.name[:-len(suffix)] if suffix else source.name
    # Пустой basename возможен для патологического аргумента вроде `/`.
    # Служебное имя лучше пустой ссылки и всё равно не раскрывает директорию.
    stem = stem or "artifact"
    return f"{EXTERNAL_SOURCE_PREFIX}/{stem}-{digest}{suffix}"


def unavailable_source(path: Path | str, kind: str = "artifact") -> str:
    """Безопасное имя для отсутствующего/нечитаемого входа без выдуманного hash.

    Content digest в этом случае вычислить нельзя. Маркер `unavailable`
    сообщает это явно; хешировать локальный путь означало бы выдавать его
    отпечаток за отпечаток артефакта.
    """
    source = Path(path)
    suffix = "".join(source.suffixes)
    stem = source.name[:-len(suffix)] if suffix else source.name
    stem = stem or kind
    return f"{EXTERNAL_SOURCE_PREFIX}/{stem}-unavailable{suffix}"
