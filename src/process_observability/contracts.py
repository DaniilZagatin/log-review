"""Initial output contracts, strict JSON and a separate model proposal boundary.

Internal decoding validates structure; it does not verify source text, runtime
observations or decisions. Model responses must use parse_model_proposal instead.
No formula evaluator or analyzed-code execution is provided here.
"""

from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from datetime import date
from enum import Enum
import json
import math
from pathlib import Path, PurePosixPath
from types import UnionType
from typing import get_args, get_origin


class ContractError(ValueError):
    """Invalid contract data or an attempted model trust escalation.

    code is a stable host category and field a declared field label; neither carries input content, so
    callers may report them where the message text could include a key taken from the rejected data.
    """

    def __init__(self, message="", *, code="contract", field=None):
        super().__init__(message)
        self.code, self.field = code, field


def _fail(message, code="contract", field=None):
    raise ContractError(message, code=code, field=field)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(text):
    if type(text) is not str:
        _fail("JSON input must be text")
    try:
        return json.loads(text, object_pairs_hook=_object, parse_float=_finite_float,
                          parse_constant=lambda value: _fail(f"Nonfinite JSON number: {value}"))
    except ContractError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractError("Invalid JSON") from exc


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        _fail("Nonfinite JSON number")
    return value


def _value(expected, value, path, decode=False):
    origin, args = get_origin(expected), get_args(expected)
    if origin is UnionType:
        for option in args:
            try:
                return _value(option, value, path, decode)
            except ContractError:
                pass
        _fail(f"{path}: value does not match its declared alternatives", "type")
    if origin is list:
        if type(value) is not list:
            _fail(f"{path}: expected list", "type")
        return [_value(args[0], item, f"{path}[{i}]", decode) for i, item in enumerate(value)]
    if origin is dict:
        if type(value) is not dict:
            _fail(f"{path}: expected object", "type")
        return {_value(args[0], key, f"{path}.key", decode):
                _value(args[1], item, f"{path}.{key}", decode) for key, item in value.items()}
    if isinstance(expected, type) and issubclass(expected, Enum):
        if type(value) is expected:
            return value
        if decode and type(value) is str:
            try:
                return expected(value)
            except ValueError:
                pass
        _fail(f"{path}: invalid {expected.__name__}", "invalid_enum")
    if isinstance(expected, type) and is_dataclass(expected):
        if decode and type(value) is dict:
            return expected.from_dict(value)
        if type(value) is not expected:
            _fail(f"{path}: expected {expected.__name__}", "type")
        value.__post_init__()
        return value
    if expected is float:
        try:
            finite = type(value) in (float, int) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            _fail(f"{path}: expected finite number", "type")
        return value
    if type(value) is not expected:
        _fail(f"{path}: expected {getattr(expected, '__name__', expected)}", "type")
    if expected is str and not value.strip():
        _fail(f"{path}: unknown text must be null, not blank", "blank_text")
    if type(value) is float and not math.isfinite(value):
        _fail(f"{path}: nonfinite number", "type")
    return value


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _nonblank(value, label):
    if type(value) is not str or not value.strip():
        _fail(f"{label}: nonblank text required", "blank_text", label)


def _strings(values, label):
    for value in values:
        _nonblank(value, label)
    if len(values) != len(set(values)):
        _fail(f"{label}: duplicate values", "duplicate_values", label)


class Record:
    def __post_init__(self):
        for item in fields(self):
            _value(item.type, getattr(self, item.name), f"{type(self).__name__}.{item.name}")
            if get_origin(item.type) is list and get_args(item.type) == (str,):
                _strings(getattr(self, item.name), item.name)
        self._check()

    def _check(self):
        pass

    def to_dict(self):
        self.__post_init__()
        return _plain(self)

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, payload):
        if type(payload) is not dict:
            _fail(f"{cls.__name__}: expected object")
        declared = {item.name: item for item in fields(cls)}
        if set(payload) - set(declared):
            _fail(f"{cls.__name__}: unknown fields")
        for name, item in declared.items():
            if name not in payload and item.default is MISSING and item.default_factory is MISSING:
                _fail(f"{cls.__name__}: missing {name}")
        return cls(**{name: _value(declared[name].type, value, name, True)
                      for name, value in payload.items()})

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(strict_json_loads(text))


class SourceKind(str, Enum):
    CODE = "code"
    TELEMETRY = "telemetry"
    DOCUMENT = "document"
    UPSTREAM = "derived_upstream"


class ValidationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class Authority(str, Enum):
    UNKNOWN = "unknown"
    PROPOSAL = "proposal"
    SNAPSHOT = "supplied_snapshot"
    ACCEPTED = "accepted"


class Applicability(str, Enum):
    UNKNOWN = "unknown"
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable_approved"


class FindingState(str, Enum):
    UNKNOWN = "unknown"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IMPLEMENTED = "implemented"
    OBSERVED = "observed"
    NOT_FOUND = "not_found"


class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    COMPLETE = "complete"


@dataclass(frozen=True, kw_only=True)
class SourceSnapshot(Record):
    id: str
    kind: SourceKind
    role: str
    content_hash: str
    acquisition_method: str
    revision: str | None = None
    config_fingerprint: str | None = None

    def _check(self):
        for name in ("id", "role", "content_hash", "acquisition_method"):
            _nonblank(getattr(self, name), name)


@dataclass(frozen=True, kw_only=True)
class SourceAnchor(Record):
    snapshot_id: str
    path: str
    symbol: str
    start_line: int
    end_line: int

    def _check(self):
        for name in ("snapshot_id", "path", "symbol"):
            _nonblank(getattr(self, name), name)
        if PurePosixPath(self.path).is_absolute() or ".." in PurePosixPath(self.path).parts or "\\" in self.path or ":" in self.path:
            _fail("Source anchor requires a relative snapshot path")
        if self.start_line < 1 or self.end_line < self.start_line:
            _fail("Invalid source line range")


@dataclass(frozen=True, kw_only=True)
class EvidenceRef(Record):
    id: str
    kind: SourceKind
    snapshot_id: str
    locator: str
    safe_quote: str | None = None
    raw_span_hash: str | None = None
    source_anchor: SourceAnchor | None = None
    text_validation: ValidationState = ValidationState.UNVERIFIED
    semantic_validation: ValidationState = ValidationState.UNVERIFIED
    claim_state: FindingState = FindingState.PROPOSED

    def _check(self):
        for name in ("id", "snapshot_id", "locator"):
            _nonblank(getattr(self, name), name)
        if self.kind is SourceKind.CODE and self.source_anchor is None:
            _fail("Code evidence requires a source anchor")
        if self.source_anchor is not None and self.source_anchor.snapshot_id != self.snapshot_id:
            _fail("Evidence and anchor snapshots differ")
        if self.kind is not SourceKind.TELEMETRY and self.claim_state is FindingState.OBSERVED:
            _fail("Non-telemetry evidence cannot establish observed runtime behavior")
        if self.text_validation is ValidationState.VERIFIED and not self.raw_span_hash:
            _fail("Verified source text requires a raw span hash")


@dataclass(frozen=True, kw_only=True)
class Decision(Record):
    id: str
    subject_id: str
    author: str
    role: str
    decided_at: str
    scope: str
    basis: str
    decision: str

    def _check(self):
        for item in fields(self):
            _nonblank(getattr(self, item.name), item.name)
        try:
            parsed = date.fromisoformat(self.decided_at)
        except ValueError as exc:
            raise ContractError("Decision date must be an ISO date") from exc
        if parsed.isoformat() != self.decided_at:
            _fail("Decision date must be YYYY-MM-DD")
        if self.decision not in {"accepted", "not_applicable_approved"}:
            _fail("Unsupported trusted decision")


def load_trusted_decisions(path=None):
    """Explicit project-configuration entry point, never model proposal ingestion."""
    if path is None:
        return []
    payload = strict_json_loads(Path(path).read_text(encoding="utf-8"))
    if type(payload) is not dict or set(payload) != {"schema_version", "decisions"} or payload["schema_version"] != "decisions/1":
        _fail("Invalid trusted decisions envelope")
    if type(payload["decisions"]) is not list:
        _fail("Decisions must be a list")
    decisions = [Decision.from_dict(item) for item in payload["decisions"]]
    _strings([item.id for item in decisions], "decision IDs")
    subjects = [(item.subject_id, item.scope) for item in decisions]
    if len(subjects) != len(set(subjects)):
        _fail("Ambiguous repeated decision for the same subject/scope")
    return decisions


@dataclass(frozen=True, kw_only=True)
class RuleRef(Record):
    id: str
    provenance: str
    source_locator: str
    authority: Authority = Authority.UNKNOWN
    applicability: Applicability = Applicability.UNKNOWN
    decision_id: str | None = None
    data_validation: ValidationState = ValidationState.UNVERIFIED

    def _check(self):
        for name in ("id", "provenance", "source_locator"):
            _nonblank(getattr(self, name), name)
        if (self.authority is Authority.ACCEPTED or self.applicability is Applicability.NOT_APPLICABLE) and not self.decision_id:
            _fail("Accepted authority/applicability needs a trusted decision reference")


@dataclass(frozen=True, kw_only=True)
class Assessment(Record):
    state: FindingState = FindingState.UNKNOWN
    evidence_ids: list[str] = field(default_factory=list)
    reason: str = "Not evaluated"
    decision_id: str | None = None

    def _check(self):
        _nonblank(self.reason, "assessment reason")
        _strings(self.evidence_ids, "assessment evidence")
        if self.state in {FindingState.IMPLEMENTED, FindingState.OBSERVED} and not self.evidence_ids:
            _fail("Implementation/observation requires evidence references")
        if self.state is FindingState.ACCEPTED and not self.decision_id:
            _fail("Accepted requirement needs a trusted decision reference")


@dataclass(frozen=True, kw_only=True)
class Stage(Record):
    id: str
    name: str | None
    required: Assessment = field(default_factory=Assessment)
    implemented: Assessment = field(default_factory=Assessment)
    observed: Assessment = field(default_factory=Assessment)
    roles: list[str] = field(default_factory=list)
    systems: list[str] = field(default_factory=list)
    alternatives_complete: bool = False

    def _check(self):
        _nonblank(self.id, "stage ID")
        allowed = {
            "required": {FindingState.UNKNOWN, FindingState.PROPOSED, FindingState.ACCEPTED},
            "implemented": {FindingState.UNKNOWN, FindingState.PROPOSED, FindingState.IMPLEMENTED, FindingState.NOT_FOUND},
            "observed": {FindingState.UNKNOWN, FindingState.OBSERVED, FindingState.NOT_FOUND},
        }
        for name, states in allowed.items():
            if getattr(self, name).state not in states:
                _fail(f"Invalid state for {name} process view")


@dataclass(frozen=True, kw_only=True)
class Transition(Record):
    from_stage: str
    to_stage: str
    condition: str | None
    evidence_ids: list[str] = field(default_factory=list)
    state: FindingState = FindingState.PROPOSED

    def _check(self):
        _nonblank(self.from_stage, "transition source")
        _nonblank(self.to_stage, "transition target")


# Lifecycle roles of one event record relative to its operation. Only "result" can show a completed action;
# refusal (a business rule declined it), error, retry, duplicate (repeated delivery or already processed) and
# no_op (nothing to do) never establish a new successful occurrence. The first six are the original vocabulary.
LIFECYCLE_ROLES = ("request", "start", "result", "error", "ui", "unknown", "refusal", "retry", "duplicate", "no_op")
# Host assessment of where the mapped emit lies; never a model claim. not_assessed: legacy or free-text mapping.
ROLE_SUPPORT = ("not_assessed", "emit_within_operation_sources", "emit_outside_operation_sources", "no_static_emit")
# Host reconciliation of every binding that names the same static emit site (path, lines, key), a mapping taking the
# worst state of its sites. not_assessed: none of its sites has a host-assessed binding and their bindings agree;
# consistent: an assessed binding exists and all agree; contradicted: two bindings of one site (assessed or drafted)
# give different lifecycle roles or operations; unsupported: assessed bindings of a site exist but none lies inside
# its operation's own sources. Set by review-mode map completion only; legacy-path mappings keep not_assessed.
# Earlier proposals stay in the map either way.
RECONCILIATION = ("not_assessed", "consistent", "contradicted", "unsupported")
# Host outcome of a fact's lifecycle check in coverage. not_assessed: no declared requirement and no assessed site, so
# the original static rule applies; compatible: every key has a clean site whose role the fact declares;
# no_compatible_site: clean sites exist only with other roles (or none); ambiguous: a site is contradicted,
# unsupported or of unknown role; requirement_undeclared: an assessed site exists but the fact declares no role.
LIFECYCLE_CHECKS = ("not_assessed", "compatible", "no_compatible_site", "ambiguous", "requirement_undeclared")


@dataclass(frozen=True, kw_only=True)
class EventMapping(Record):
    """A technical event key linked to a business action.

    operation is the original free-text label. operation_id, when set, is the concrete identity of an operation of
    the same map and stage (checked by ProcessMap); legacy mappings keep None. role_basis is the model's
    source-bounded explanation of the lifecycle role; role_support is assigned by the host only.
    """
    event_key: str
    business_action: str
    operation: str
    stage_id: str
    lifecycle_role: str
    occurrence_rule: str
    state: FindingState = FindingState.PROPOSED
    evidence_ids: list[str] = field(default_factory=list)
    operation_id: str | None = None
    role_basis: str | None = None
    role_support: str = "not_assessed"
    reconciliation: str = "not_assessed"

    def _check(self):
        for name in ("event_key", "business_action", "operation", "stage_id", "occurrence_rule"):
            _nonblank(getattr(self, name), name)
        if self.lifecycle_role not in LIFECYCLE_ROLES:
            _fail("Unsupported event lifecycle role", "invalid_enum", "lifecycle_role")
        for name in ("operation_id", "role_basis"):
            if getattr(self, name) is not None:
                _nonblank(getattr(self, name), name)
        if self.role_support not in ROLE_SUPPORT:
            _fail("Unsupported event role support state", "invalid_enum", "role_support")
        if self.reconciliation not in RECONCILIATION:
            _fail("Unsupported event binding reconciliation state", "invalid_enum", "reconciliation")


@dataclass(frozen=True, kw_only=True)
class Artifact(Record):
    run_id: str
    input_fingerprints: dict[str, str]
    status: ArtifactStatus
    evidence_ids: list[str]
    schema_version: str = "process-observability/1"
    blockers: list[str] = field(default_factory=list)

    def _check(self):
        _nonblank(self.run_id, "run ID")
        if self.schema_version != "process-observability/1":
            _fail("Unsupported artifact schema version")
        _strings(self.blockers, "artifact blockers")
        _strings(self.evidence_ids, "artifact evidence")
        for key, value in self.input_fingerprints.items():
            _nonblank(key, "fingerprint key")
            _nonblank(value, "fingerprint value")
        if self.status in {ArtifactStatus.BLOCKED, ArtifactStatus.INCOMPLETE} and not self.blockers:
            _fail("Blocked/incomplete artifact requires blockers")
        if self.status is ArtifactStatus.COMPLETE and self.blockers:
            _fail("Complete artifact cannot have blockers")


# Optional structured details of one operation, proposed on its own atomic page and grounded in cited source text.
# A grounded field states what the implementation shows; it is never an observation of runtime behaviour, and a
# verified quote proves only that the text exists. Unknown is the default of every field.
EFFECT_KINDS = ("decision", "recommendation", "command", "result", "unknown")
OUTCOME_KINDS = ("success", "refusal", "failure", "cancelled", "deferred")
CONTINUATION_MODES = ("choice", "all", "unknown")
JOIN_MODES = ("all", "any", "unknown")
EXTERNAL_DIRECTIONS = ("request", "send", "receive")


@dataclass(frozen=True, kw_only=True)
class ExternalInteraction(Record):
    """An explicit call, message or callback in the cited code. A request or send never shows the receiver's outcome."""
    system: str
    direction: str
    purpose: str | None = None
    endpoint: str | None = None
    contract: str | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def _check(self):
        if self.direction not in EXTERNAL_DIRECTIONS:
            _fail("Unsupported external interaction direction", "invalid_enum", "direction")
        if self.endpoint is None and self.contract is None:
            _fail("An external interaction needs an endpoint or a contract", "contract", "contract")
        if not self.evidence_ids:
            _fail("An external interaction needs evidence of the cited call", "contract", "evidence_ids")


@dataclass(frozen=True, kw_only=True)
class CodeExplanation(Record):
    """A model interpretation of cited code; the symbol occurs in the cited text, its meaning stays unverified."""
    symbol: str
    explanation: str
    evidence_ids: list[str] = field(default_factory=list)

    def _check(self):
        if not self.evidence_ids:
            _fail("A code explanation needs evidence of the explained code", "contract", "evidence_ids")


@dataclass(frozen=True, kw_only=True)
class OperationDetails(Record):
    input: str | None = None
    expected_result: str | None = None
    precondition: str | None = None
    completion_condition: str | None = None
    effect_kind: str = "unknown"
    outcomes: list[str] = field(default_factory=list)
    continuation: str = "unknown"
    join: str = "unknown"
    external_interactions: list[ExternalInteraction] = field(default_factory=list)
    code_explanations: list[CodeExplanation] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)

    def claims(self):
        """Whether any grounded field is stated; nested interactions and explanations carry their own evidence."""
        return (any(value is not None for value in (self.input, self.expected_result, self.precondition,
                                                     self.completion_condition))
                or self.effect_kind != "unknown" or bool(self.outcomes) or self.continuation != "unknown"
                or self.join != "unknown")

    def _check(self):
        for name, allowed in (("effect_kind", EFFECT_KINDS), ("continuation", CONTINUATION_MODES),
                              ("join", JOIN_MODES)):
            if getattr(self, name) not in allowed:
                _fail(f"Unsupported operation {name}", "invalid_enum", name)
        if set(self.outcomes) - set(OUTCOME_KINDS):
            _fail("Unsupported operation outcome", "invalid_enum", "outcomes")
        if self.claims() and not self.evidence_ids:
            _fail("Stated operation details need evidence", "contract", "evidence_ids")


@dataclass(frozen=True, kw_only=True)
class ProcessOperation(Record):
    id: str
    name: str
    stage_id: str
    kind: str = "operation"
    description: str | None = None
    actor: str | None = None
    event_keys: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    # Absent in legacy and coarse maps; an atomic action without proposed details keeps None.
    details: OperationDetails | None = None

    def _check(self):
        for name in ("id", "name", "stage_id"):
            _nonblank(getattr(self, name), name)
        if self.kind not in {"start", "operation", "decision", "end"}:
            _fail("Invalid business operation kind", "invalid_enum", "kind")


@dataclass(frozen=True, kw_only=True)
class OperationTransition(Record):
    id: str
    from_operation: str
    to_operation: str
    condition: str | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def _check(self):
        for name in ("id", "from_operation", "to_operation"):
            _nonblank(getattr(self, name), name)


@dataclass(frozen=True, kw_only=True)
class ProcessScenario(Record):
    id: str
    name: str
    description: str | None = None
    operation_ids: list[str] = field(default_factory=list)
    transition_ids: list[str] = field(default_factory=list)

    def _check(self):
        _nonblank(self.id, "scenario ID")
        _nonblank(self.name, "scenario name")
        if not self.operation_ids:
            _fail("A scenario needs at least one operation", "empty_scenario", "operation_ids")


@dataclass(frozen=True, kw_only=True)
class ProcessMap(Artifact):
    purpose: str | None
    boundaries: str | None
    case_definition: str | None
    stages: list[Stage]
    transitions: list[Transition]
    event_mappings: list[EventMapping]
    unknowns: list[str]
    operations: list[ProcessOperation] = field(default_factory=list)
    operation_transitions: list[OperationTransition] = field(default_factory=list)
    scenarios: list[ProcessScenario] = field(default_factory=list)

    def _check(self):
        super()._check()
        _strings([stage.id for stage in self.stages], "stage IDs")
        ids = {stage.id for stage in self.stages}
        for transition in self.transitions:
            if transition.from_stage not in ids or transition.to_stage not in ids:
                _fail("Transition references an unknown stage", "unknown_reference", "transitions")
        if any(mapping.stage_id not in ids for mapping in self.event_mappings):
            _fail("Event mapping references an unknown stage", "unknown_reference", "event_mappings")
        _strings([item.id for item in self.operations], "operation IDs")
        _strings([item.id for item in self.operation_transitions], "operation transition IDs")
        _strings([item.id for item in self.scenarios], "scenario IDs")
        operations = {item.id: item for item in self.operations}
        edges = {item.id: item for item in self.operation_transitions}
        for mapping in self.event_mappings:
            # A concrete identity must resolve exactly; an unknown or foreign-stage ID never passes as validated.
            target = operations.get(mapping.operation_id) if mapping.operation_id is not None else None
            if mapping.operation_id is not None and (target is None or target.stage_id != mapping.stage_id):
                _fail("Event mapping references an unknown operation", "unknown_reference", "event_mappings")
        for operation in self.operations:
            if operation.stage_id not in ids:
                _fail("Operation references an unknown stage", "unknown_reference", "operations")
            keys = {item.event_key for item in self.event_mappings if item.stage_id == operation.stage_id}
            if set(operation.event_keys) - keys:
                _fail("Operation references an unmapped event key", "unmapped_event_key", "operations")
        for edge in self.operation_transitions:
            if edge.from_operation not in operations or edge.to_operation not in operations:
                _fail("Operation transition references an unknown operation", "unknown_reference",
                      "operation_transitions")
        for scenario in self.scenarios:
            if set(scenario.operation_ids) - set(operations) or set(scenario.transition_ids) - set(edges):
                _fail("Scenario references an unknown operation or transition", "unknown_reference", "scenarios")
            if any({edges[key].from_operation, edges[key].to_operation} - set(scenario.operation_ids)
                   for key in scenario.transition_ids):
                _fail("Scenario transition endpoints must belong to its operations", "scenario_endpoint",
                      "scenarios")


class OpportunityState(str, Enum):
    UNKNOWN = "unknown"
    ZERO = "known_zero"
    KNOWN = "known_n"


@dataclass(frozen=True, kw_only=True)
class Opportunities(Record):
    state: OpportunityState = OpportunityState.UNKNOWN
    count: int | None = None
    independent_evidence_ids: list[str] = field(default_factory=list)
    scope_complete: bool = False
    window: str | None = None

    def _check(self):
        if self.state is OpportunityState.UNKNOWN:
            if self.count is not None:
                _fail("Unknown opportunities cannot have a numeric count")
        elif not self.independent_evidence_ids or not self.scope_complete or not self.window:
            _fail("Known opportunity count requires independent evidence, scope and window")
        elif self.state is OpportunityState.ZERO and self.count != 0:
            _fail("known_zero requires zero opportunities")
        elif self.state is OpportunityState.KNOWN and (self.count is None or self.count < 1):
            _fail("known_n requires a positive opportunity count")


@dataclass(frozen=True, kw_only=True)
class RequiredFact(Record):
    id: str
    meaning: str
    rationale: str
    scope: str
    condition: str | None
    requirement: Assessment
    applicability: Applicability
    metric_ids: list[str]
    evidence_alternatives: list[str]
    instrumentation: str
    opportunities: Opportunities
    delivery: str
    quality: str
    evidence_ids: list[str] = field(default_factory=list)
    decision_id: str | None = None
    stage_id: str | None = None
    event_keys: list[str] = field(default_factory=list)
    # Proposed lifecycle roles of the event records that directly observe this fact: result for a new completed
    # outcome, duplicate/retry/refusal/no_op/... when the fact is about that record. [] = not declared (legacy).
    lifecycle_roles: list[str] = field(default_factory=list)
    lifecycle_check: str = "not_assessed"  # host-owned, see LIFECYCLE_CHECKS

    def _check(self):
        for name in ("id", "meaning", "rationale", "scope"):
            _nonblank(getattr(self, name), name)
        if set(self.lifecycle_roles) - set(LIFECYCLE_ROLES):
            _fail("Unsupported fact lifecycle role", "invalid_enum", "lifecycle_roles")
        if self.lifecycle_check not in LIFECYCLE_CHECKS:
            _fail("Unsupported fact lifecycle check state", "invalid_enum", "lifecycle_check")
        if self.instrumentation == "found" and self.lifecycle_check not in {"not_assessed", "compatible"}:
            _fail("A fact whose lifecycle check failed cannot be instrumented as found", "contract", "instrumentation")
        if self.requirement.state not in {FindingState.UNKNOWN, FindingState.PROPOSED, FindingState.ACCEPTED}:
            _fail("Invalid requirement state")
        if self.applicability is Applicability.NOT_APPLICABLE and not self.decision_id:
            _fail("Approved non-applicability requires a decision")
        if self.instrumentation not in {"unknown", "found", "missing"}:
            _fail("Invalid instrumentation state")
        if self.delivery not in {"unknown", "present", "missing", "incomplete"}:
            _fail("Invalid delivery state")
        if self.quality not in {"unknown", "usable", "unusable"}:
            _fail("Invalid quality state")
        if self.quality == "usable" and self.delivery != "present":
            _fail("Usable analytical data requires delivered records")


@dataclass(frozen=True, kw_only=True)
class CoverageSummary(Record):
    """Conservative usable-fact count; all dimensions remain available separately."""
    scope_provisional: bool
    numerator: int
    denominator: int
    unknown: int
    candidate_fact_ids: list[str]
    excluded_fact_ids: list[str]

    def _check(self):
        _strings(self.candidate_fact_ids, "candidate fact IDs")
        _strings(self.excluded_fact_ids, "excluded fact IDs")
        if min(self.numerator, self.denominator, self.unknown) < 0 or self.numerator + self.unknown > self.denominator:
            _fail("Invalid explicit coverage counts")


@dataclass(frozen=True, kw_only=True)
class LogCoverage(Artifact):
    facts: list[RequiredFact]
    summary: CoverageSummary

    def _check(self):
        super()._check()
        _strings([fact.id for fact in self.facts], "fact IDs")
        accepted = [fact for fact in self.facts if fact.requirement.state is FindingState.ACCEPTED]
        included = [fact for fact in accepted if fact.applicability is not Applicability.NOT_APPLICABLE]
        candidates = {fact.id for fact in self.facts if fact.requirement.state is not FindingState.ACCEPTED}
        excluded = {fact.id for fact in accepted if fact.applicability is Applicability.NOT_APPLICABLE}
        if self.summary.denominator != len(included):
            _fail("Denominator must retain all accepted requirements of applicable/unknown scope")
        if set(self.summary.candidate_fact_ids) != candidates or set(self.summary.excluded_fact_ids) != excluded:
            _fail("Candidate/excluded lists must match the requirement ledger")
        if any(fact.applicability is Applicability.UNKNOWN for fact in included) and not self.summary.scope_provisional:
            _fail("Unknown applicability requires provisional scope")
        if not accepted and not self.summary.scope_provisional:
            _fail("Unestablished obligations require provisional scope")
        if self.summary.unknown < sum(fact.applicability is Applicability.UNKNOWN for fact in included):
            _fail("Unknown applicability must remain visible in summary unknowns")
        def unknown(fact):
            return (fact.applicability is Applicability.UNKNOWN or fact.instrumentation == "unknown"
                    or fact.delivery == "unknown" or fact.quality == "unknown"
                    or fact.opportunities.state is OpportunityState.UNKNOWN)
        supported = sum(not unknown(fact) and fact.delivery == "present" and fact.quality == "usable"
                        for fact in included)
        if self.summary.numerator > supported:
            _fail("Coverage numerator exceeds fully assessed usable facts")
        if self.summary.unknown < sum(unknown(fact) for fact in included):
            _fail("Unknown fact dimensions must remain visible in summary unknowns")


class InstrumentationAction(str, Enum):
    MAP = "map_existing"
    JOIN = "join"
    ENRICH = "enrich"
    ADD_FIELD = "add_field"
    ADD_EVENT = "add_event"
    MOVE = "move_emit"
    DELIVERY = "fix_delivery"


@dataclass(frozen=True, kw_only=True)
class InstrumentationCard(Record):
    id: str
    fact_id: str
    action: InstrumentationAction
    rationale: str
    semantics: str
    fields: dict[str, str]
    transaction_timing: str | None
    dependencies: list[str]
    metric_ids: list[str]
    validation_plan: str
    location: str = "location_unresolved"
    source_anchor: SourceAnchor | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def _check(self):
        for name in ("id", "fact_id", "rationale", "semantics", "validation_plan"):
            _nonblank(getattr(self, name), name)
        if self.location not in {"anchored", "location_unresolved"}:
            _fail("Invalid instrumentation location state")
        if (self.location == "anchored") != (self.source_anchor is not None):
            _fail("Instrumentation location and source anchor disagree")


def _acyclic(cards):
    """Card-to-card dependencies must form a DAG; iterative, so a long plan cannot exhaust recursion.

    Fact IDs and other non-card IDs are not edges here; callers check that they resolve.
    """
    ids = {card.id for card in cards}
    waiting = {card.id: len(set(card.dependencies) & ids) for card in cards}
    dependents = {}
    for card in cards:
        for dependency in set(card.dependencies) & ids:
            dependents.setdefault(dependency, []).append(card.id)
    ready = [card_id for card_id, count in waiting.items() if count == 0]
    resolved = 0
    while ready:
        resolved += 1
        for dependent in dependents.get(ready.pop(), ()):
            waiting[dependent] -= 1
            if not waiting[dependent]:
                ready.append(dependent)
    if resolved != len(cards):
        _fail("Instrumentation card dependencies form a cycle", "dependency_cycle", "cards")


@dataclass(frozen=True, kw_only=True)
class InstrumentationPlan(Artifact):
    cards: list[InstrumentationCard]

    def _check(self):
        super()._check()
        _strings([card.id for card in self.cards], "instrumentation card IDs")
        _acyclic(self.cards)


@dataclass(frozen=True, kw_only=True)
class Readiness(Record):
    primary: str
    blockers: list[str]
    decision_id: str | None = None

    def _check(self):
        allowed = {"unknown", "incomplete", "design_only", "method_missing", "telemetry_missing", "partial_input", "ready", "not_applicable_approved"}
        if self.primary not in allowed:
            _fail("Invalid metric readiness")
        _strings(self.blockers, "metric blockers")
        if self.primary in {"ready", "not_applicable_approved"}:
            if self.blockers:
                _fail("Ready/non-applicable metric cannot have blockers")
        elif not self.blockers:
            _fail("Unready metric requires all known blockers")
        if self.primary == "not_applicable_approved" and not self.decision_id:
            _fail("Approved metric non-applicability needs a trusted decision")


@dataclass(frozen=True, kw_only=True)
class MetricSpec(Record):
    id: str
    purpose: str
    formula_id: str | None
    formula_text: str | None
    counting_unit: str | None
    cohort: str | None
    window: str | None
    time_semantics: str | None
    open_case_policy: str | None
    late_data_policy: str | None
    dependencies: list[str]
    readiness: Readiness
    value: float | None = None
    value_reason: str | None = None
    computed_from: list[str] = field(default_factory=list)

    def _check(self):
        _nonblank(self.id, "metric ID")
        _nonblank(self.purpose, "metric purpose")
        if self.value is None:
            _nonblank(self.value_reason, "reason for absent metric value")
        else:
            if self.readiness.primary != "ready" or not self.computed_from or not self.formula_id:
                _fail("Metric value requires ready state, method ID and computed-from evidence")
            if any(not value for value in (self.counting_unit, self.cohort, self.window, self.time_semantics,
                                           self.open_case_policy, self.late_data_policy)):
                _fail("Computed metric requires population and time policies")


@dataclass(frozen=True, kw_only=True)
class MetricCatalog(Artifact):
    metrics: list[MetricSpec]

    def _check(self):
        super()._check()
        _strings([metric.id for metric in self.metrics], "metric IDs")


def _guard_model(value):
    if isinstance(value, list):
        for item in value:
            _guard_model(item)
    elif isinstance(value, dict):
        forbidden_keys = {"authority", "verified", "accepted", "not_applicable_approved", "text_verified", "semantic_verified"}
        privileged = {"verified", "accepted", "observed", "not_applicable_approved", "implemented", "ready", "usable", "complete", "known_zero", "known_n", "found", "present"}
        for key, item in value.items():
            if type(key) is not str:
                _fail("Model proposal keys must be strings")
            if key in forbidden_keys:
                _fail(f"Model cannot supply trusted field: {key}")
            if key == "decision_id" and item is not None:
                _fail("Model cannot invent a trusted decision reference")
            if key in {"scope_complete", "alternatives_complete"} and item is not False:
                _fail("Model cannot approve scope or branch completeness")
            if key in {"instrumentation", "delivery", "quality"} and item != "unknown":
                _fail("Model cannot assert deterministic coverage findings")
            if key == "observed" and (type(item) is not dict or item.get("state") != "unknown"):
                _fail("Model cannot assert observed runtime state")
            if (key in {"state", "status", "primary", "quality", "decision", "applicability"} or key.endswith("_state") or key.endswith("_validation")) and type(item) is str and item in privileged:
                _fail(f"Model cannot promote trusted state: {item}")
            _guard_model(item)


def parse_model_proposal(record_type, payload, *, expected_run_id=None, expected_fingerprints=None):
    """Only ingestion entry point for untrusted model-generated contract proposals."""
    if type(payload) is str:
        payload = strict_json_loads(payload)
    if type(payload) is not dict:
        _fail("Model proposal must be an object")
    if record_type not in (ProcessMap, LogCoverage, InstrumentationPlan, MetricCatalog):
        _fail("Unsupported proposal contract")
    try:
        _guard_model(payload)
    except RecursionError as exc:
        raise ContractError("Model proposal nesting is too deep") from exc
    if expected_run_id is not None and payload.get("run_id") != expected_run_id:
        _fail("Model proposal run identity differs from orchestrator input")
    if expected_fingerprints is not None and payload.get("input_fingerprints") != expected_fingerprints:
        _fail("Model proposal fingerprints differ from orchestrator input")
    return record_type.from_dict(payload)


def validate_evidence_links(artifact, evidence):
    """Check reference integrity/kinds; does not perform source verification.

    Callers must supply the validator-owned registry, never model trust flags.
    """
    if not isinstance(artifact, Artifact) or type(evidence) is not list or any(type(item) is not EvidenceRef for item in evidence):
        _fail("Expected artifact and evidence registry")
    _strings([item.id for item in evidence], "evidence registry IDs")
    registry = {item.id: item for item in evidence}
    artifact.__post_init__()

    def observed(ids):
        if not any(registry[item].kind is SourceKind.TELEMETRY
                   and registry[item].text_validation is ValidationState.VERIFIED
                   and registry[item].semantic_validation is ValidationState.VERIFIED for item in ids):
            _fail("Observed behavior needs verified telemetry evidence, not code alone")

    def walk(value):
        if is_dataclass(value):
            for item in fields(value):
                member = getattr(value, item.name)
                if item.name in {"evidence_ids", "computed_from", "independent_evidence_ids"}:
                    if any(ref not in registry for ref in member):
                        _fail("Unresolved evidence reference")
                walk(member)
            if isinstance(value, Stage) and value.observed.state is FindingState.OBSERVED:
                observed(value.observed.evidence_ids)
            if isinstance(value, MetricSpec) and value.value is not None:
                observed(value.computed_from)
        elif isinstance(value, list):
            for member in value:
                walk(member)
    walk(artifact)
