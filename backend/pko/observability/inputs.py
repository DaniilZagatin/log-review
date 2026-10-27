"""Optional report inputs: a business description and a runtime log profile.

Both are read before research starts, so a bad file fails fast. Business text
is masked like the source snapshot and stays a requirement source, never code
evidence. Runtime records are profiled locally; only the aggregate profile
reaches later stages, the model or the report. The profile describes delivered
records in the supplied file, not verified business occurrences.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from pko.code_tools.tools import _is_blocked
from pko.errors import PkoError
from pko.scenario_map.sources import _decode

PROFILES = Path(__file__).with_name("profiles")
MAX_BUSINESS_BYTES = 200_000
MAX_BUSINESS_CHARS = 40_000
MAX_RUNTIME_RECORDS = 100_000
MAX_RUNTIME_BYTES = 20_000_000
MAX_EVENT_KEYS = 1000
RUNTIME_FIELDS = ("event_key", "case_id", "occurred_at", "event_id")
_EVENT = re.compile(r"[\w .:/-]{1,200}")
_FIELD_NAME = re.compile(r"[A-Za-z_][\w.-]{0,63}")


def _checked_file(path, label: str, limit: int) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or _is_blocked(path.name):
        raise PkoError(f"{label}: нужен обычный файл, не ссылка и не файл с ключами.")
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise PkoError(f"{label}: не удалось прочитать файл: {exc}") from exc
    if len(data) > limit:
        raise PkoError(f"{label}: файл больше {limit} байт; выборка без явного решения не делается.")
    return data


def load_business_text(path) -> dict:
    data = _checked_file(path, "Бизнес-описание", MAX_BUSINESS_BYTES)
    text = _decode(data)
    if text is None:
        raise PkoError("Бизнес-описание: ожидается текст в UTF-8 или UTF-16.")
    truncated = len(text) > MAX_BUSINESS_CHARS
    return {"name": Path(path).name, "sha256": hashlib.sha256(data).hexdigest(),
            "text": text[:MAX_BUSINESS_CHARS], "truncated": truncated}


def profile_names() -> list[str]:
    return sorted(path.stem for path in PROFILES.glob("*.json"))


def load_profile(name: str) -> dict:
    """A trusted product profile; its version and digest are recorded in the report."""
    if name not in profile_names():
        raise PkoError(f"Неизвестный профиль: {name}", hint="доступны: " + ", ".join(profile_names()))
    data = (PROFILES / f"{name}.json").read_bytes()
    return {**json.loads(data), "sha256": hashlib.sha256(data).hexdigest()}


def parse_field_map(value: str | None) -> dict:
    mapping = {name: name for name in RUNTIME_FIELDS}
    for item in filter(None, (part.strip() for part in (value or "").split(","))):
        name, _, field = item.partition("=")
        if name not in RUNTIME_FIELDS or not _FIELD_NAME.fullmatch(field):
            raise PkoError(f"Неверное соответствие полей логов: {item}",
                           hint="формат event_key=event,case_id=request_id,occurred_at=ts,event_id=id")
        mapping[name] = field
    return mapping


def _identity(value):
    if type(value) is str and value.strip():
        return ("string", value)
    if type(value) is int:
        return ("integer", value)
    return None


def profile_runtime_log(path, field_map: dict | None = None) -> dict:
    """Aggregate a JSONL file: keys, field names and identity/time quality counts."""
    mapping = field_map or parse_field_map(None)
    data = _checked_file(path, "Файл логов", MAX_RUNTIME_BYTES)
    text = _decode(data)
    if text is None:
        raise PkoError("Файл логов: ожидается JSONL в UTF-8.")
    events, fields_by_event, cases, identities = {}, {}, set(), {}
    count = invalid_times = missing_case = missing_event_id = invalid_keys = conflicts = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        count += 1
        if count > MAX_RUNTIME_RECORDS:
            raise PkoError(f"Файл логов: больше {MAX_RUNTIME_RECORDS} записей; выборка без явного решения не делается.")
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise PkoError(f"Файл логов: строка {number} не является JSON.") from exc
        if not isinstance(row, dict):
            raise PkoError(f"Файл логов: строка {number} не является JSON-объектом.")
        event = row.get(mapping["event_key"])
        if type(event) is str and _EVENT.fullmatch(event):
            events[event] = events.get(event, 0) + 1
            if len(events) > MAX_EVENT_KEYS:
                raise PkoError("Файл логов: больше 1000 разных ключей событий; передайте записи выбранного процесса.")
            names = fields_by_event.setdefault(event, set())
            names.update(name for name in row if _FIELD_NAME.fullmatch(name))
        else:
            invalid_keys += 1
            event = None
        case_id = _identity(row.get(mapping["case_id"]))
        if case_id is None:
            missing_case += 1
        else:
            cases.add(case_id)
        event_id = _identity(row.get(mapping["event_id"]))
        if event_id is None:
            missing_event_id += 1
        elif event_id in identities and identities[event_id] != (case_id, event):
            conflicts += 1
        else:
            identities[event_id] = (case_id, event)
        stamp = row.get(mapping["occurred_at"])
        try:
            parsed = datetime.fromisoformat(stamp) if type(stamp) is str else None
            invalid_times += parsed is None or parsed.tzinfo is None
        except ValueError:
            invalid_times += 1
    limitations = ["Полнота файла за период и бизнес-смысл идентификаторов не установлены.",
                   "Профиль описывает доставленные записи, а не подтверждённые выполнения операций."]
    for amount, label in ((invalid_times, "записей без корректного времени с часовым поясом"),
                          (missing_case, "записей без идентификатора экземпляра"),
                          (invalid_keys, "записей без допустимого ключа события"),
                          (conflicts, "конфликтующих идентификаторов записей")):
        if amount:
            limitations.append(f"{label[0].upper() + label[1:]}: {amount}.")
    deduplicated = not missing_event_id and not conflicts
    return {"name": Path(path).name, "sha256": hashlib.sha256(data).hexdigest(), "record_count": count,
            "event_counts": dict(sorted(events.items())),
            "fields_by_event": {event: sorted(names)[:64] for event, names in sorted(fields_by_event.items())},
            "identified_case_count": len(cases), "missing_case_count": missing_case,
            "invalid_time_count": invalid_times, "invalid_key_count": invalid_keys,
            "duplicate_record_count": count - len(identities) if deduplicated else None,
            "source_fields": mapping, "limitations": limitations}
