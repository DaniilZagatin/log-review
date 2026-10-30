"""Run manifest: identity, provenance and stage states of one analysis run.

The manifest records what a run was given and how far each of the four stages
got; it carries no stage results. Data origin (synthetic/real) and model mode
(fake/live) are independent: a synthetic fixture may be analysed by a live
model and real inputs by a scripted fake one.
"""

from __future__ import annotations

import json
import math
import platform
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from . import __version__

SCHEMA_VERSION = "0.1"

# Run IDs may later become directory names, so they are kept path-safe.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ManifestError(ValueError):
    """Invalid manifest data; ``path`` names the offending field, outermost first."""

    def __init__(self, message: str, path: tuple[str, ...] = ()) -> None:
        self.message = message
        self.path = path
        super().__init__(f"{'.'.join(path)}: {message}" if path else message)


class DataOrigin(Enum):
    """Where the analysed inputs come from."""

    SYNTHETIC = "synthetic"
    REAL = "real"


class ModelMode(Enum):
    """Whether semantic judgments come from a scripted fake model or a live LLM."""

    FAKE = "fake"
    LIVE = "live"


class StageStatus(Enum):
    """How far a stage got; ``completed`` means it ran, not that its findings are correct."""

    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"


_NEEDS_BLOCKERS = frozenset({StageStatus.INCOMPLETE, StageStatus.BLOCKED})


@dataclass(frozen=True)
class StageState:
    """State of one stage. Incomplete and blocked stages must list their blockers."""

    status: StageStatus = StageStatus.NOT_STARTED
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_instance(self.status, StageStatus, "status")
        _require_instance(self.blockers, tuple, "blockers")
        for index, reason in enumerate(self.blockers):
            _require_text(reason, f"blockers[{index}]")
        if self.status in _NEEDS_BLOCKERS and not self.blockers:
            raise ManifestError(
                f"at least one reason is required when status is {self.status.value}",
                ("blockers",),
            )
        if self.status not in _NEEDS_BLOCKERS and self.blockers:
            raise ManifestError(f"must be empty when status is {self.status.value}", ("blockers",))

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "blockers": list(self.blockers)}

    @classmethod
    def from_dict(cls, data: object) -> StageState:
        data = _object(data, _field_names(cls))
        return cls(
            status=_enum(StageStatus, data["status"], "status"),
            blockers=_array(data["blockers"], "blockers"),
        )


@dataclass(frozen=True)
class Stages:
    """The four process stages; every one is always present, even if never started."""

    map: StageState = field(default_factory=StageState)
    coverage: StageState = field(default_factory=StageState)
    instrumentation: StageState = field(default_factory=StageState)
    metrics: StageState = field(default_factory=StageState)

    def __post_init__(self) -> None:
        for name in STAGE_NAMES:
            _require_instance(getattr(self, name), StageState, name)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).to_dict() for name in STAGE_NAMES}

    @classmethod
    def from_dict(cls, data: object) -> Stages:
        data = _object(data, STAGE_NAMES)
        return cls(**{name: _nested(name, StageState.from_dict, data[name]) for name in STAGE_NAMES})


STAGE_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Stages))


@dataclass(frozen=True)
class InputFingerprint:
    """SHA-256 of one input file, named by its POSIX path relative to the input root."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.path, "path")
        pure = PurePosixPath(self.path)
        if not pure.parts or pure.is_absolute() or ".." in pure.parts or "\\" in self.path:
            raise ManifestError("expected a relative POSIX path inside the input root", ("path",))
        _require_sha256(self.sha256, "sha256")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, data: object) -> InputFingerprint:
        return cls(**_object(data, _field_names(cls)))


@dataclass(frozen=True)
class Provenance:
    """Versions and effective configuration that produced the run."""

    tool_version: str
    python_version: str
    config_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.tool_version, "tool_version")
        _require_text(self.python_version, "python_version")
        _require_sha256(self.config_sha256, "config_sha256")

    @classmethod
    def current(cls, config_sha256: str) -> Provenance:
        """Provenance of this package and interpreter for the given config hash."""
        return cls(__version__, platform.python_version(), config_sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_version": self.tool_version,
            "python_version": self.python_version,
            "config_sha256": self.config_sha256,
        }

    @classmethod
    def from_dict(cls, data: object) -> Provenance:
        return cls(**_object(data, _field_names(cls)))


@dataclass(frozen=True)
class RunManifest:
    """One analysis run: what it analysed, with what, and how far each stage got."""

    run_id: str
    data_origin: DataOrigin
    model_mode: ModelMode
    input_fingerprints: tuple[InputFingerprint, ...]
    provenance: Provenance
    stages: Stages = field(default_factory=Stages)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _check_schema_version(self.schema_version)
        _require_text(self.run_id, "run_id")
        if not _RUN_ID.fullmatch(self.run_id):
            raise ManifestError(
                "expected 1-128 characters from [A-Za-z0-9._-] starting with a letter or digit",
                ("run_id",),
            )
        _require_instance(self.data_origin, DataOrigin, "data_origin")
        _require_instance(self.model_mode, ModelMode, "model_mode")
        _require_instance(self.input_fingerprints, tuple, "input_fingerprints")
        seen: set[str] = set()
        for index, item in enumerate(self.input_fingerprints):
            name = f"input_fingerprints[{index}]"
            _require_instance(item, InputFingerprint, name)
            if item.path in seen:
                raise ManifestError(f"duplicate path {item.path!r}", (name,))
            seen.add(item.path)
        _require_instance(self.provenance, Provenance, "provenance")
        _require_instance(self.stages, Stages, "stages")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "data_origin": self.data_origin.value,
            "model_mode": self.model_mode.value,
            "input_fingerprints": [item.to_dict() for item in self.input_fingerprints],
            "provenance": self.provenance.to_dict(),
            "stages": self.stages.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> RunManifest:
        # Report a foreign schema version before its fields look unknown or missing.
        if isinstance(data, dict) and "schema_version" in data:
            _check_schema_version(data["schema_version"])
        data = _object(data, _field_names(cls))
        items = _array(data["input_fingerprints"], "input_fingerprints")
        return cls(
            schema_version=data["schema_version"],
            run_id=data["run_id"],
            data_origin=_enum(DataOrigin, data["data_origin"], "data_origin"),
            model_mode=_enum(ModelMode, data["model_mode"], "model_mode"),
            input_fingerprints=tuple(
                _nested(f"input_fingerprints[{index}]", InputFingerprint.from_dict, item)
                for index, item in enumerate(items)
            ),
            provenance=_nested("provenance", Provenance.from_dict, data["provenance"]),
            stages=_nested("stages", Stages.from_dict, data["stages"]),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, allow_nan=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RunManifest:
        """Parse manifest JSON; every failure is raised as ManifestError."""
        if not isinstance(text, str):
            raise ManifestError(f"expected JSON text, got {type(text).__name__}")
        try:
            data = json.loads(
                text,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
                object_pairs_hook=_unique_keys,
            )
        except ManifestError:
            raise
        except (ValueError, RecursionError) as exc:
            raise ManifestError(f"invalid JSON: {exc}") from None
        return cls.from_dict(data)


# JSON decoding hooks


def _reject_constant(name: str) -> float:
    raise ManifestError(f"non-finite number {name} is not allowed")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ManifestError(f"non-finite number {text} is not allowed")
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


# Validation helpers


def _field_names(cls: type) -> tuple[str, ...]:
    return tuple(f.name for f in fields(cls))


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _object(data: object, names: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError(f"expected a JSON object, got {_json_type(data)}")
    missing = [name for name in names if name not in data]
    if missing:
        raise ManifestError(f"missing field(s): {', '.join(missing)}")
    unknown = [str(key) for key in data if key not in names]
    if unknown:
        raise ManifestError(f"unknown field(s): {', '.join(unknown)}")
    return data


def _array(value: object, name: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"expected a JSON array, got {_json_type(value)}", (name,))
    return tuple(value)


def _enum(cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, str):
        try:
            return cls(value)
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in cls)
    raise ManifestError(f"expected one of {allowed}, got {value!r}", (name,))


def _nested(name: str, parse: Any, value: object) -> Any:
    try:
        return parse(value)
    except ManifestError as exc:
        raise ManifestError(exc.message, (name, *exc.path)) from None


def _require_instance(value: object, cls: type, name: str) -> None:
    if not isinstance(value, cls):
        raise ManifestError(f"expected {cls.__name__}, got {type(value).__name__}", (name,))


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ManifestError(f"expected a string, got {_json_type(value)}", (name,))
    if not value.strip():
        raise ManifestError("must not be blank", (name,))


def _require_sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ManifestError("expected 64 lowercase hexadecimal characters", (name,))


def _check_schema_version(value: object) -> None:
    if value != SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported schema version {value!r}; expected {SCHEMA_VERSION!r}",
            ("schema_version",),
        )
