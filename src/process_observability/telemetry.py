"""Allowlisted JSONL profiling, without inferring business cases or durations."""

from datetime import datetime
import hashlib
from pathlib import Path
import re

from .contracts import ContractError, strict_json_loads
from .redaction import redact_text
from .source_tools import blocked_path


class TelemetryError(ValueError):
    pass


_DEFAULT = {name: name for name in ("event_key", "case_id", "occurred_at", "event_id", "delivery_id")}
_EVENT = re.compile(r"[\w .:/-]{1,200}")
_FIELD_NAME = re.compile(r"[A-Za-z_][\w.-]{0,63}")


def _identity(value):
    if type(value) is str and value.strip():
        return ("string", value)
    if type(value) is int:
        return ("integer", value)
    return None


def profile_telemetry(path, field_map=None, max_records=100000, max_bytes=20000000):
    path = Path(path)
    if blocked_path(path.as_posix()) or path.is_symlink():
        raise TelemetryError("Credential or symbolic-link telemetry input is not allowed")
    mapping = dict(_DEFAULT)
    if field_map is not None:
        if type(field_map) is not dict or set(field_map) - set(_DEFAULT) or any(type(v) is not str or not v.strip() for v in field_map.values()):
            raise TelemetryError("Field map must map supported canonical names to source field names")
        mapping.update(field_map)
    if type(max_records) is not int or type(max_bytes) is not int or min(max_records, max_bytes) < 1:
        raise TelemetryError("Telemetry limits must be positive integers")
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise TelemetryError("Telemetry exceeds byte limit; no silent sampling")
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TelemetryError("Cannot read UTF-8 JSONL telemetry") from exc
    events, cases, identities, fields_by_event = {}, set(), {}, {}
    fields_truncated = False
    count = invalid_times = missing_case = missing_event_id = invalid_keys = conflicts = 0
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        count += 1
        if count > max_records:
            raise TelemetryError("Telemetry exceeds record limit; no silent sampling")
        try:
            record = strict_json_loads(line)
        except ContractError as exc:
            raise TelemetryError(f"Invalid JSONL record at line {number}") from exc
        if type(record) is not dict:
            raise TelemetryError(f"Non-object JSONL record at line {number}")
        event = record.get(mapping["event_key"])
        if type(event) is str and _EVENT.fullmatch(event):
            event = redact_text(event)
            events[event] = events.get(event, 0) + 1
            if len(events) > 1000:
                raise TelemetryError("Telemetry exceeds 1000 distinct event keys; choose a process-specific input")
            fields = fields_by_event.setdefault(event, set())
            names = {name for name in record if _FIELD_NAME.fullmatch(name)}
            combined = fields | names
            fields_truncated = fields_truncated or len(combined) > 64
            fields_by_event[event] = set(sorted(combined)[:64])
        else:
            invalid_keys += 1
            event = None
        case_id = _identity(record.get(mapping["case_id"]))
        if case_id is None:
            missing_case += 1
        else:
            cases.add(case_id)
        event_id = _identity(record.get(mapping["event_id"]))
        if event_id is None:
            missing_event_id += 1
        elif event_id in identities and identities[event_id] != (case_id, event):
            conflicts += 1
        else:
            identities[event_id] = (case_id, event)
        timestamp = record.get(mapping["occurred_at"])
        try:
            parsed = datetime.fromisoformat(timestamp) if type(timestamp) is str else None
            if parsed is None or parsed.tzinfo is None:
                invalid_times += 1
        except ValueError:
            invalid_times += 1
    limitations = ["File completeness and business identity semantics are not established",
                   "Profile contains delivered records, not verified business operation occurrences"]
    if invalid_times:
        limitations.append(f"Missing/invalid/timezone-free event times: {invalid_times}")
    if missing_case:
        limitations.append(f"Records without a usable supplied case identifier: {missing_case}")
    if invalid_keys:
        limitations.append(f"Records without an allowlisted event key: {invalid_keys}")
    if conflicts:
        limitations.append(f"Conflicting supplied event identities: {conflicts}")
    if fields_truncated:
        limitations.append("Delivered field-name profile truncated to 64 names per event; field absence is not established")
    return {"record_count": count, "event_keys": sorted(events), "event_counts": events,
            "case_count": len(cases) if not missing_case else None, "identified_case_count": len(cases),
            "logical_event_count": len(identities) if not missing_event_id and not conflicts else None,
            "duplicate_record_count": count - len(identities) if not missing_event_id and not conflicts else None,
            "invalid_time_count": invalid_times, "missing_case_count": missing_case,
            "fields_by_event": {event:sorted(names) for event,names in sorted(fields_by_event.items())},
            "field_names_truncated":fields_truncated,
            "source_fields": mapping, "limitations": limitations, "input_sha256": hashlib.sha256(raw).hexdigest()}
