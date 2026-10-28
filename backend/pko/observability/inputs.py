"""Optional report inputs: a domain profile and a runtime log profile.

Both are read before research starts, so a bad file fails fast. Runtime
records are profiled locally; only the aggregate profile reaches later stages,
the model or the report. The profile describes delivered records in the
supplied file, not verified business occurrences.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from pko.code_tools.tools import _is_blocked
from pko.errors import PkoError
from pko.scenario_map.sources import _decode, _is_link

PROFILES = Path(__file__).with_name("profiles")
MAX_RUNTIME_RECORDS = 100_000
MAX_RUNTIME_BYTES = 20_000_000
MAX_EVENT_KEYS = 1000
MAX_EVENT_FIELDS = 64
RUNTIME_FIELDS = ("event_key", "case_id", "occurred_at", "event_id")
_EVENT = re.compile(r"[\w .:/-]{1,200}")
_FIELD_NAME = re.compile(r"[A-Za-z_][\w.-]{0,63}")


def _checked_file(path, label: str, limit: int) -> bytes:
    path = Path(path)
    # Inspect ancestors before opening: is_symlink() on the leaf misses both
    # directory links and Windows junctions. Do not normalise away a link/.. path.
    if ".." in path.parts:
        raise PkoError(f"{label}: путь с переходом в родительский каталог недопустим.")
    path = path.absolute()
    try:
        for part in reversed((path, *path.parents)):
            if _is_link(part) or _is_blocked(part.name):
                raise PkoError(f"{label}: ссылки, junction и пути с ключами недопустимы.")
        if not path.is_file():
            raise PkoError(f"{label}: нужен обычный файл.")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise PkoError(f"{label}: не удалось прочитать файл: {exc}") from exc
    if len(data) > limit:
        raise PkoError(f"{label}: файл больше {limit} байт; выборка без явного решения не делается.")
    return data


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


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _event_key(value):
    # Parse raw JSON locally first. Masking the entire document would mask the
    # value of the canonical event_key field merely because its name ends in key.
    # Only bounded event names enter the aggregate; reject a name containing a
    # recognised secret instead of replacing it with a shared, false event key.
    if type(value) is str and _EVENT.fullmatch(value) and _decode(value.encode("utf-8")) == value:
        return value
    return None


def _nonempty(value):
    return value is not None and not (isinstance(value, str) and not value.strip())


def profile_runtime_log(path, field_map: dict | None = None) -> dict:
    """Keep only event names, field names and aggregate counts, never record values."""
    mapping = parse_field_map(None)
    if field_map is not None:
        if (not isinstance(field_map, dict) or set(field_map) - set(RUNTIME_FIELDS)
                or any(not isinstance(value, str) or not _FIELD_NAME.fullmatch(value) for value in field_map.values())):
            raise PkoError("Соответствие полей логов: ожидаются поддерживаемые имена и имена полей.")
        mapping.update(field_map)
    data = _checked_file(path, "Файл логов", MAX_RUNTIME_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise PkoError("Файл логов: ожидается JSONL в UTF-8.") from None
    events, quality, cases, identities = {}, {}, set(), {}
    count = invalid_times = missing_case = missing_event_id = invalid_keys = conflicts = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        count += 1
        if count > MAX_RUNTIME_RECORDS:
            raise PkoError(f"Файл логов: больше {MAX_RUNTIME_RECORDS} записей; выборка без явного решения не делается.")
        try:
            row = json.loads(line, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            raise PkoError(f"Файл логов: строка {number} не является JSON.") from None
        if not isinstance(row, dict):
            raise PkoError(f"Файл логов: строка {number} не является JSON-объектом.")
        event = _event_key(row.get(mapping["event_key"]))
        if event is not None:
            events[event] = events.get(event, 0) + 1
            if len(events) > MAX_EVENT_KEYS:
                raise PkoError("Файл логов: больше 1000 разных ключей событий; передайте записи выбранного процесса.")
            stats = quality.setdefault(event, {"record_count": 0, "missing_case_count": 0,
                                                "invalid_time_count": 0, "missing_event_id_count": 0,
                                                "conflicting_event_id_count": 0, "field_counts": {}})
            stats["record_count"] += 1
            for name, value in row.items():
                if _FIELD_NAME.fullmatch(name):
                    stats["field_counts"][name] = stats["field_counts"].get(name, 0) + int(_nonempty(value))
        else:
            invalid_keys += 1
            event = None
        case_id = _identity(row.get(mapping["case_id"]))
        if case_id is None:
            missing_case += 1
            if event is not None:
                quality[event]["missing_case_count"] += 1
        else:
            cases.add(case_id)
        event_id = _identity(row.get(mapping["event_id"]))
        if event_id is None:
            missing_event_id += 1
            if event is not None:
                quality[event]["missing_event_id_count"] += 1
        elif event_id in identities and identities[event_id] != (case_id, event):
            conflicts += 1
            # A conflict affects both named event keys, not unrelated records.
            for affected in {event, identities[event_id][1]} - {None}:
                quality[affected]["conflicting_event_id_count"] += 1
        else:
            identities[event_id] = (case_id, event)
        stamp = row.get(mapping["occurred_at"])
        try:
            parsed = datetime.fromisoformat(stamp) if type(stamp) is str else None
            invalid_time = parsed is None or parsed.tzinfo is None
        except ValueError:
            invalid_time = True
        invalid_times += invalid_time
        if event is not None:
            quality[event]["invalid_time_count"] += int(invalid_time)
    limitations = ["Полнота файла за период и бизнес-смысл идентификаторов не установлены.",
                   "Профиль описывает доставленные записи, а не подтверждённые выполнения операций."]
    for amount, label in ((invalid_times, "записей без корректного времени с часовым поясом"),
                          (missing_case, "записей без идентификатора экземпляра"),
                          (missing_event_id, "записей без идентификатора записи"),
                          (invalid_keys, "записей без допустимого ключа события"),
                          (conflicts, "конфликтующих идентификаторов записей")):
        if amount:
            limitations.append(f"{label[0].upper() + label[1:]}: {amount}.")
    deduplicated = not missing_event_id and not conflicts
    truncated = []
    for event, stats in quality.items():
        names = sorted(stats["field_counts"])
        stats["field_names_truncated"] = len(names) > MAX_EVENT_FIELDS
        if stats["field_names_truncated"]:
            truncated.append(event)
        stats["field_counts"] = {name: stats["field_counts"][name] for name in names[:MAX_EVENT_FIELDS]}
    if truncated:
        limitations.append("Каталог полей ограничен 64 именами на событие; отсутствие отброшенного поля не установлено.")
    return {"profile_version": 2, "name": Path(path).name, "sha256": hashlib.sha256(data).hexdigest(), "record_count": count,
            "event_counts": dict(sorted(events.items())),
            "fields_by_event": {event: list(stats["field_counts"]) for event, stats in sorted(quality.items())},
            "quality_by_event": dict(sorted(quality.items())), "field_names_truncated": bool(truncated),
            "identified_case_count": len(cases), "missing_case_count": missing_case,
            "invalid_time_count": invalid_times, "invalid_key_count": invalid_keys,
            "duplicate_record_count": count - len(identities) if deduplicated else None,
            "source_fields": mapping, "limitations": limitations}
