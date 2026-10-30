"""Мини-разбор YAML без внешних зависимостей.

PKO читает единственный YAML — `business_intent.yaml`, который заполняет человек.
Нужны плоские ключи, списки строк, вложенные словари и многострочные блоки
(`|`, `>`); тянуть PyYAML в зависимости ради этого не стоит. Всё, что сложнее
(якоря, сложные ключи), честно отвергается с подсказкой, а не разбирается
наполовину.

Блочные скаляры поддержаны не для полноты, а по факту: поля вроде
`business_meaning` и `success_criteria` — это два-три предложения, и человек
запишет их через `>-`. Раньше такой файл отвергался целиком, и решение Gate не
выносилось из-за формы записи, а не из-за содержания.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class Entry(NamedTuple):
    """Значимая строка файла, подготовленная к разбору."""

    indent: int
    content: str
    lineno: int
    value: Any          # `_MISSING`, кроме заранее собранного блочного скаляра


# Отличает «значения нет» от честного `None`, который пишется как `ключ:` или `~`.
_MISSING = object()

_TRUE = {"true", "yes", "да", "on"}
_FALSE = {"false", "no", "нет", "off"}

# Индикаторы блочного скаляра: стиль (| литеральный, > свёрнутый) и правило
# обрезки хвостовых переводов строки (-, +, по умолчанию).
_BLOCK_INDICATORS = {"|", "|-", "|+", ">", ">-", ">+"}


class YamlSubsetError(ValueError):
    """Конструкция за пределами поддерживаемого подмножества."""


def loads(text: str, notes: list[str] | None = None) -> Any:
    """Разобрать поддерживаемое подмножество YAML.

    `notes` — куда сложить предупреждения. Сейчас туда попадает единственный
    неочевидный для человека случай: незакавыченное значение обрезано
    комментарием. Правило совпадает с настоящим YAML, но потеря части значения
    не должна оставаться незаметной для того, кто заполняет файл руками.
    """
    lines = _meaningful(text, notes)
    if not lines:
        return {}
    value, pos = _block(lines, 0, lines[0].indent)
    if pos != len(lines):
        lineno, content = lines[pos].lineno, lines[pos].content
        raise YamlSubsetError(f"строка {lineno}: неожиданный сдвиг отступа — «{content}»")
    return value


def _meaningful(text: str, notes: list[str] | None = None) -> list[Entry]:
    """(отступ, содержимое, номер строки, готовое значение) без пустых строк и комментариев.

    Четвёртый элемент обычно `_MISSING`. Он заполняется только для блочного
    скаляра: его текст собирается здесь из сырых строк, потому что внутри блока
    ни комментарии, ни отступы обрабатывать нельзя — это данные, а не разметка.
    """
    raw_lines = text.splitlines()
    out: list[Entry] = []
    index = 0
    while index < len(raw_lines):
        lineno = index + 1
        raw = raw_lines[index]
        stripped_line, spaces_before = _strip_comment(raw)
        line = stripped_line.rstrip()
        # Выровненный комментарий (два и более пробела) — общепринятая запись,
        # предупреждать о нём значит завалить список пробелов ложными строками.
        ambiguous = 0 <= spaces_before < 2
        if ambiguous and notes is not None and line.strip() and not line.strip().startswith("#"):
            notes.append(
                f"строка {lineno}: значение обрезано комментарием «#». "
                "Если решётка — часть значения, возьмите его в кавычки"
            )
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in {"---", "..."}:
            index += 1
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise YamlSubsetError(f"строка {lineno}: отступ табуляцией не поддерживается")

        indent = len(line) - len(line.lstrip())
        indicator = _block_indicator(stripped)
        if indicator:
            key = stripped.split(":", 1)[0].strip()
            value, consumed = _block_scalar(raw_lines, index + 1, indent, indicator)
            out.append(Entry(indent, f"{key}:", lineno, value))
            index += 1 + consumed
            continue

        out.append(Entry(indent, stripped, lineno, _MISSING))
        index += 1
    return out


def _block_indicator(content: str) -> str:
    """Вернуть индикатор блочного скаляра из строки «ключ: >-» или пустую строку."""
    key, sep, value = content.partition(":")
    if not sep or not key.strip():
        return ""
    value = value.strip()
    return value if value in _BLOCK_INDICATORS else ""


def _block_scalar(
    raw_lines: list[str], start: int, key_indent: int, indicator: str
) -> tuple[str, int]:
    """Собрать текст блочного скаляра. Возвращает (значение, сколько строк съедено)."""
    collected: list[str] = []
    index = start
    while index < len(raw_lines):
        raw = raw_lines[index]
        if raw.strip() and (len(raw) - len(raw.lstrip())) <= key_indent:
            break
        collected.append(raw)
        index += 1

    body = [ln for ln in collected]
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return "", index - start

    block_indent = min(
        (len(ln) - len(ln.lstrip()) for ln in body if ln.strip()), default=key_indent + 1
    )
    stripped = [ln[block_indent:] if len(ln) > block_indent else ln.strip() for ln in body]

    if indicator.startswith("|"):
        value = "\n".join(stripped)
    else:
        # Свёрнутый стиль: строки абзаца склеиваются пробелом, пустая строка —
        # это разделитель абзацев и остаётся переводом строки.
        paragraphs: list[list[str]] = [[]]
        for ln in stripped:
            if ln.strip():
                paragraphs[-1].append(ln.strip())
            else:
                paragraphs.append([])
        value = "\n".join(" ".join(p) for p in paragraphs if p)

    if indicator.endswith("+"):
        value += "\n"
    return value, index - start


def _strip_comment(line: str) -> tuple[str, int]:
    """Отрезать комментарий, не трогая содержимое кавычек.

    Возвращает (строка, сколько пробелов было перед `#`; -1 — отреза не было).
    Раньше строка резалась по первому ` #` до разбора кавычек, и значение
    `title: "a #b"` превращалось в незакрытую кавычку. Для незакавыченных значений
    правило прежнее и совпадает с YAML.

    Число пробелов важно: `значение   # пояснение` — обычный выровненный
    комментарий, а `Отчёт #1 по HR` — вероятно, часть значения. Отличать их можно
    только по форме записи, поэтому предупреждается лишь второй случай.
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            before = line[:i]
            spaces = len(before) - len(before.rstrip(" "))
            return before, spaces
    return line, -1


def _block(lines: list[Entry], pos: int, indent: int) -> tuple[Any, int]:
    if lines[pos].content.startswith("- "):
        return _sequence(lines, pos, indent)
    return _mapping(lines, pos, indent)


def _sequence(lines: list[Entry], pos: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while pos < len(lines):
        cur_indent, content, lineno = lines[pos].indent, lines[pos].content, lines[pos].lineno
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlSubsetError(f"строка {lineno}: неожиданный отступ в списке")
        if not content.startswith("- "):
            break
        items.append(_scalar(content[2:].strip()))
        pos += 1
    return items, pos


def _mapping(lines: list[Entry], pos: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while pos < len(lines):
        entry = lines[pos]
        cur_indent, content, lineno = entry.indent, entry.content, entry.lineno
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlSubsetError(f"строка {lineno}: неожиданный отступ")
        if content.startswith("- "):
            break
        if ":" not in content:
            raise YamlSubsetError(f"строка {lineno}: ожидалось «ключ: значение»")

        key, _, raw_value = content.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        pos += 1

        # Блочный скаляр уже собран в `_meaningful`: его текст — данные,
        # разбирать его как разметку нельзя.
        if entry.value is not _MISSING:
            result[key] = entry.value
            continue

        if raw_value:
            result[key] = _scalar(raw_value)
            continue

        if pos < len(lines) and lines[pos].indent > indent:
            nested, pos = _block(lines, pos, lines[pos].indent)
            result[key] = nested
        elif pos < len(lines) and lines[pos].indent == indent and lines[pos].content.startswith("- "):
            nested, pos = _sequence(lines, pos, indent)
            result[key] = nested
        else:
            result[key] = None
    return result, pos


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    low = value.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if low in {"null", "~", ""}:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    if _looks_float(value):
        return float(value)
    return value


def _looks_float(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return "." in value or "e" in value.lower()
