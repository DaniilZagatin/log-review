"""Onboarding draft packet from one saved analysis run, without a model call.

The exporter reads the bundle files that ``analyze``/``demo`` already wrote,
validates them with the existing contracts and cross-references, renders
everything in memory and only then writes a fresh directory:

- ``process.md``: a short reader view of the process, stages, operations,
  scenarios, open questions and the actual stage status;
- ``onboarding.json``: the four validated artifacts, manifest, evidence
  references (without source quotes), snapshot identity and partiality;
- ``events.csv``, ``logging-changes.csv``, ``metrics.csv``: spreadsheet views
  with a reversible formula-safe cell encoding (``decode_cell``); the view of
  a stage that did not complete is omitted, never written empty;
- ``SHA256SUMS``: hashes of the packet files.

Nothing here reads the target repository, calls a model, evaluates formula
text or writes into the source run. Proposed events are never invented from
prose: an ``add_event`` card without a structured key keeps an empty key cell.
"""

from collections import Counter
import csv
import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

from . import report as r
from .contracts import (
    ArtifactStatus, ContractError, EvidenceRef, InstrumentationAction, InstrumentationPlan, LogCoverage,
    MetricCatalog, ProcessMap, SourceKind, strict_json_loads, validate_evidence_links,
)
from .manifest import STAGE_NAMES, ManifestError, RunManifest, StageStatus

SCHEMA_VERSION = "onboarding-export/1"
MAX_INPUT_BYTES = 256 * 1024 * 1024
ARTIFACTS = (("map", "process_map", ProcessMap), ("coverage", "log_coverage", LogCoverage),
             ("instrumentation", "instrumentation_plan", InstrumentationPlan),
             ("metrics", "metric_catalog", MetricCatalog))
# The separate files written next to bundle.json must repeat its members exactly.
COPIES = {"manifest.json": "manifest", "process_map.json": "process_map", "log_coverage.json": "log_coverage",
          "instrumentation_plan.json": "instrumentation_plan", "metric_catalog.json": "metric_catalog"}
INPUT_FILES = ("bundle.json", *COPIES, "evidence.jsonl")

EVENT_COLUMNS = ("row_kind", "event_key", "event_key_status", "stage_id", "stage_name", "operation_ids",
                 "business_action", "mapping_operation", "lifecycle_role", "occurrence_rule", "existing_fields", "semantics",
                 "proposed_fields", "transaction_timing", "state", "static_emit_in_cited_range", "fact_ids",
                 "fact_link", "plan_card_id", "location", "source_refs", "evidence_ids")
CHANGE_COLUMNS = ("card_id", "action", "action_label", "fact_id", "fact_meaning", "stage_id", "operation_ids",
                  "candidate_operation_ids", "location", "source_ref", "rationale", "semantics", "fields",
                  "transaction_timing", "dependencies", "metric_ids", "validation_plan", "evidence_ids")
METRIC_COLUMNS = ("metric_id", "purpose", "formula_text", "counting_unit", "cohort", "window", "time_semantics",
                  "open_case_policy", "late_data_policy", "required_fact_ids", "required_facts",
                  "required_event_keys", "related_change_ids")

# Spreadsheet formula starters (ASCII and full-width) and the apostrophe guard itself.
_FORMULA = frozenset("=+-@'\uff1d\uff0b\uff0d\uff20")
_BIDI = frozenset(map(chr, (0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))))
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?", re.ASCII)
_JSON_CELL = "'json:"
_WWW = re.compile(r"(?i)\bwww\.")
_KINDS = {"start": "начало", "operation": "операция", "decision": "решение", "end": "исход"}
_POLICY_LABELS = {"formula_text": "формулу", "counting_unit": "единицу счёта", "cohort": "состав случаев",
                  "window": "период", "time_semantics": "время начала и конца",
                  "open_case_policy": "незавершённые случаи", "late_data_policy": "поздние данные"}
_UNDECLARED = "Undeclared definition parts: "


class ExportError(ValueError):
    """A refused run or destination; raised before any packet file is written."""


# Reversible formula-safe CSV cells


def _hidden(char):
    return (unicodedata.category(char) == "Cc" and char not in "\n\t") or char in _BIDI


def _guard(text):
    first = text[0]
    if _NUMBER.fullmatch(text):
        return text
    if first in _FORMULA or first.isspace() or unicodedata.category(first) in {"Cc", "Cf"}:
        return "'" + text
    return text


def encode_cell(value):
    """Cell text for None, a string or a list of strings; ``decode_cell`` restores the value.

    A leading formula character, apostrophe, whitespace or control/format character gets one
    apostrophe (plain decimal numbers stay as they are). Values with control or bidirectional
    characters, line breaks inside list items or empty strings are written as ``'json:`` + JSON.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        if any(type(item) is not str for item in value):
            raise TypeError("List cells hold strings only")
        if any(not item or "\n" in item or any(map(_hidden, item)) for item in value):
            return _JSON_CELL + json.dumps(value, ensure_ascii=True)
        return _guard("\n".join(value))
    if type(value) is not str:
        raise TypeError("Cells hold text, lists of text or None")
    if not value or any(map(_hidden, value)):
        return _JSON_CELL + json.dumps(value, ensure_ascii=True)
    return _guard(value)


def decode_cell(text, *, multiple=False):
    """Inverse of encode_cell; ``multiple`` selects list cells (items separated by line breaks)."""
    if text.startswith(_JSON_CELL):
        return json.loads(text[len(_JSON_CELL):])
    if not text:
        return [] if multiple else None
    if text.startswith("'"):
        text = text[1:]
    return text.split("\n") if multiple else text


def _csv(columns, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([encode_cell(row[column]) for column in columns])
    # The BOM lets spreadsheet programs detect UTF-8 Cyrillic text.
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


# Input boundaries


def _run_directory(value):
    run = Path(value).expanduser().absolute()
    if run.is_symlink() or not run.is_dir():
        raise ExportError("--run must name an existing analyze or demo output directory, not a symlink")
    return run.resolve()


def _destination(value, run):
    out = Path(value).expanduser().absolute()
    if out.name in {"", ".", ".."}:
        raise ExportError("--out must name a new directory")
    if os.path.lexists(out):
        raise ExportError("Output directory already exists; choose a new --out path")
    if not out.parent.is_dir():
        raise ExportError("The parent directory of --out must already exist")
    target = out.parent.resolve() / out.name
    if target == run or run in target.parents:
        raise ExportError("--out must be outside the source run directory")
    if os.path.lexists(target):
        raise ExportError("Output directory already exists; choose a new --out path")
    return target


def _read(run, name):
    """Bytes of one regular run file; symlinks, special files and oversized inputs are refused."""
    path = run / name
    try:
        before = path.lstat()
    except FileNotFoundError:
        if name == "bundle.json":
            raise ExportError("The run has no bundle.json: plan-only, interrupted or discovery outputs "
                              "have no analysis results to export") from None
        raise ExportError(f"The run is missing {name}; export only complete saved run directories") from None
    if not stat.S_ISREG(before.st_mode):
        raise ExportError(f"{name} must be a regular file, not a symlink or special file")
    if before.st_size > MAX_INPUT_BYTES:
        raise ExportError(f"{name} exceeds {MAX_INPUT_BYTES} bytes")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
            raise ExportError(f"{name} changed while it was opened")
        data = handle.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ExportError(f"{name} exceeds {MAX_INPUT_BYTES} bytes")
    return data


def _json(name, data):
    try:
        return strict_json_loads(data.decode("utf-8"))
    except (UnicodeError, ContractError) as exc:
        raise ExportError(f"{name}: {exc if isinstance(exc, ContractError) else 'not UTF-8'}") from None


def _jsonl(name, data):
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ExportError(f"{name}: not UTF-8") from None
    lines = text.split("\n")
    if lines[-1] != "":
        raise ExportError(f"{name}: the last record must end with a line break")
    try:
        return [strict_json_loads(line) for line in lines[:-1]]
    except ContractError as exc:
        raise ExportError(f"{name}: {exc}") from None


def _snapshot_id(fingerprints):
    """RepoSnapshot.snapshot_id recomputed from the manifest's repo/ input hashes."""
    files = {path[len("repo/"):]: digest for path, digest in sorted(fingerprints.items()) if path.startswith("repo/")}
    if not files:
        raise ExportError("The manifest has no repo/ source fingerprints; source snapshot identity is unknown")
    content = "\n".join(f"{path}:{digest}" for path, digest in files.items())
    return hashlib.sha256(content.encode()).hexdigest(), files


def load_run(run):
    """Validated, cross-checked view of one saved run; refuses inconsistent or partial copies."""
    raw = {name: _read(run, name) for name in INPUT_FILES}
    bundle = _json("bundle.json", raw["bundle.json"])
    required = {"manifest", "scope", "evidence", *(key for _, key, _ in ARTIFACTS)}
    if type(bundle) is not dict or required - set(bundle):
        raise ExportError("bundle.json lacks the saved run structure")
    for name, key in COPIES.items():
        if _json(name, raw[name]) != bundle[key]:
            raise ExportError(f"{name} differs from bundle.json; the run was modified or only partly written")
    if _jsonl("evidence.jsonl", raw["evidence.jsonl"]) != bundle["evidence"]:
        raise ExportError("evidence.jsonl differs from bundle.json; the run was modified or only partly written")
    try:
        manifest = RunManifest.from_dict(bundle["manifest"])
        if type(bundle["evidence"]) is not list:
            raise ContractError("evidence must be a list")
        evidence = [EvidenceRef.from_dict(item) for item in bundle["evidence"]]
        artifacts = {}
        for stage, key, record_type in ARTIFACTS:
            artifacts[key] = record_type.from_dict(bundle[key])
            validate_evidence_links(artifacts[key], evidence)
    except (ManifestError, ContractError) as exc:
        raise ExportError(f"bundle.json is not a valid saved run: {exc}") from None
    scope = bundle["scope"]
    if type(scope) is not dict or type(scope.get("partial_input")) is not bool:
        raise ExportError("bundle.json scope lacks an explicit partial_input flag")
    source_scope, code_review = scope.get("source"), bundle.get("code_review")
    source_partial = type(source_scope) is dict and source_scope.get("partial_input") is True
    review_partial = type(code_review) is dict and any(
        code_review.get(key) is False for key in ("complete", "coverage_complete"))
    if not scope["partial_input"] and (source_partial or review_partial):
        raise ExportError("bundle.json reports complete scope despite an incomplete source snapshot or code review")
    fingerprints = {item.path: item.sha256 for item in manifest.input_fingerprints}
    for stage, key, _ in ARTIFACTS:
        artifact, state = artifacts[key], getattr(manifest.stages, stage)
        if artifact.run_id != manifest.run_id or artifact.input_fingerprints != fingerprints:
            raise ExportError(f"{key} belongs to a different run or inputs than manifest.json")
        if state.status is StageStatus.COMPLETED:
            consistent = artifact.status in {ArtifactStatus.DRAFT, ArtifactStatus.COMPLETE}
        else:
            consistent = (state.status is not StageStatus.NOT_STARTED and list(state.blockers) == artifact.blockers
                          and artifact.status in {ArtifactStatus.INCOMPLETE, ArtifactStatus.BLOCKED})
        if not consistent:
            raise ExportError(f"{key} status disagrees with the manifest stage state")
    snapshot_id, files = _snapshot_id(fingerprints)
    for item in evidence:
        if item.kind is SourceKind.CODE and (item.snapshot_id != snapshot_id or item.source_anchor.path not in files):
            raise ExportError("Code evidence does not belong to the run's source snapshot")
    _check_links(artifacts, {item.id: item for item in evidence})
    actions = _action_accounting(bundle, artifacts["process_map"], snapshot_id)
    atomic = _atomic_accounting(bundle, artifacts["process_map"], snapshot_id)
    if atomic is not None:
        _atomic_source_copies(run, raw, atomic, fingerprints)
    lifecycle = _coverage_lifecycle(bundle, artifacts["log_coverage"])
    return {"raw": raw, "bundle": bundle, "manifest": manifest, "artifacts": artifacts, "evidence": evidence,
            "snapshot_id": snapshot_id, "snapshot_files": files, "actions": actions, "atomic": atomic,
            "lifecycle": lifecycle}


LIFECYCLE_SCHEMA = "coverage-lifecycle/1"


def _coverage_lifecycle(bundle, coverage):
    """Site reconciliation and per-fact lifecycle checks of coverage; None when the run has none (older runs).

    A present record must name exactly the coverage facts with their own check states; otherwise it is refused.
    """
    record = bundle.get("coverage_lifecycle")
    if record is None:
        return None
    if (type(record) is not dict or set(record) != {"schema", "basis", "facts", "sites"}
            or record["schema"] != LIFECYCLE_SCHEMA or type(record["facts"]) is not list
            or type(record["sites"]) is not list):
        raise ExportError("bundle.json coverage_lifecycle is not a complete coverage-lifecycle/1 record")
    checks = {fact.id: (fact.lifecycle_check, fact.instrumentation) for fact in coverage.facts}
    seen = {}
    for item in record["facts"]:
        if (type(item) is not dict or type(item.get("fact_id")) is not str or item["fact_id"] not in checks
                or item["fact_id"] in seen):
            raise ExportError("coverage_lifecycle names an unknown or repeated coverage fact")
        seen[item["fact_id"]] = (item.get("lifecycle_check"), item.get("instrumentation"))
    if seen != checks:
        raise ExportError("coverage_lifecycle disagrees with the lifecycle checks of log_coverage")
    return record


ACTION_SCHEMA = "action-refinement/1"
_ACTION_STATUSES = frozenset({"disabled", "skipped_no_candidates", "not_run", "complete", "partial", "failed"})
_ACTION_KEYS = frozenset({"schema", "status", "status_reasons", "catalog", "settings", "requests", "candidates",
                          "areas", "stages", "operations", "event_mappings", "transitions", "scenario_inheritance",
                          "summary", "basis"})


def _action_strings(value):
    return type(value) is list and all(type(item) is str and item.strip() for item in value)


def _action_heads(record, process_map):
    """Validate persisted candidate states before using their status, counts or operation links."""
    stage_ids = {stage.id for stage in process_map.stages}
    operations = {op.id: op for op in process_map.operations}
    scope_states = {"assigned", "outside_process", "unresolved", "deferred", "request_failed",
                    "refinement_disabled", "map_stage_incomplete", "refinement_failed"}
    stage_states = {"accounted", "deferred", "request_failed", "merge_rejected"}
    incomplete = bool(record["status_reasons"])
    for identifier, head in record["candidates"].items():
        if (type(identifier) is not str or not identifier.strip() or type(head) is not dict
                or type(head.get("scope")) is not dict or type(head.get("stages")) is not list):
            raise ExportError("An action candidate has malformed scope or stage accounting")
        scope = head["scope"]
        if type(scope.get("state")) is not str or scope["state"] not in scope_states:
            raise ExportError("An action candidate has an unknown scope state")
        assigned = scope.get("stage_ids", [])
        if not _action_strings(assigned) or len(assigned) != len(set(assigned)) or set(assigned) - stage_ids:
            raise ExportError("An action candidate references an unknown or repeated stage")
        if scope["state"] == "assigned" and not assigned:
            raise ExportError("An assigned action candidate needs at least one stage")
        incomplete |= scope["state"] not in {"assigned", "outside_process", "unresolved"}
        seen = set()
        for state in head["stages"]:
            if (type(state) is not dict or type(state.get("state")) is not str or state["state"] not in stage_states
                    or type(state.get("stage_id")) is not str or state["stage_id"] not in assigned
                    or state["stage_id"] in seen):
                raise ExportError("An action candidate has malformed or repeated stage decisions")
            seen.add(state["stage_id"])
            links = state.get("operation_ids", [])
            if not _action_strings(links) or len(links) != len(set(links)):
                raise ExportError("An action decision has malformed operation identities")
            if any(key not in operations or operations[key].stage_id != state["stage_id"] for key in links):
                raise ExportError("An action decision references an absent or foreign-stage operation")
            incomplete |= state["state"] != "accounted"
            if state["state"] == "accounted":
                disposition = state.get("disposition")
                if type(disposition) is not str or disposition not in {"realized", "excluded", "unresolved"}:
                    raise ExportError("An action candidate has an unknown disposition")
                if (disposition == "realized") != bool(links):
                    raise ExportError("Only a realized candidate names one or more operations")
            elif links:
                raise ExportError("An unaccepted action decision cannot supply operation identities")
        incomplete |= scope["state"] == "assigned" and set(assigned) != seen
    if record["status"] == "complete" and incomplete:
        raise ExportError("Complete action accounting contains deferred, failed or unplanned decisions")


def _action_accounting(bundle, process_map, snapshot_id):
    """The run's action-candidate accounting, cross-checked against its map; None for runs that predate it.

    A present record must be complete and consistent: an altered or foreign record is refused, never read as
    an older run without accounting.
    """
    if "action_refinement" not in bundle:
        return None
    record = bundle["action_refinement"]
    if type(record) is not dict or set(record) != _ACTION_KEYS or record["schema"] != ACTION_SCHEMA:
        raise ExportError("bundle.json action_refinement is not a complete action-refinement/1 record")
    if (type(record["status"]) is not str or record["status"] not in _ACTION_STATUSES
            or type(record["candidates"]) is not dict):
        raise ExportError("bundle.json action_refinement has an unknown status or candidate ledger")
    if (not _action_strings(record["status_reasons"]) or type(record["settings"]) is not dict
            or type(record["summary"]) is not dict or type(record["basis"]) is not str):
        raise ExportError("bundle.json action_refinement has malformed settings, summary or reasons")
    for key in ("requests", "areas", "stages", "operations", "event_mappings", "transitions", "scenario_inheritance"):
        if type(record[key]) is not list or any(type(item) is not dict for item in record[key]):
            raise ExportError(f"bundle.json action_refinement has malformed {key}")
    for key in ("deferred_by_reason", "failed_by_reason", "dispositions"):
        counts = record["summary"].get(key, {})
        if (type(counts) is not dict or any(type(name) is not str or type(count) is not int or count < 0
                                          for name, count in counts.items())):
            raise ExportError("bundle.json action_refinement has malformed summary counts")
    catalog = record["catalog"]
    if type(catalog) is not dict or catalog.get("snapshot_id") != snapshot_id:
        raise ExportError("bundle.json action_refinement belongs to a different source snapshot")
    summary = bundle.get("action_catalog")
    if type(summary) is not dict or summary.get("catalog_sha256") != catalog.get("catalog_sha256"):
        raise ExportError("bundle.json action_refinement and action_catalog identities differ")
    if type(catalog.get("catalog_sha256")) is not str or not re.fullmatch(r"[0-9a-f]{64}", catalog["catalog_sha256"]):
        raise ExportError("bundle.json action_refinement has a malformed catalog hash")
    _action_heads(record, process_map)
    operations = {op.id: op for op in process_map.operations}
    mapped = {(item.operation_id, item.event_key) for item in process_map.event_mappings if item.operation_id}
    for op in record["operations"]:
        if type(op.get("id")) is not str or op["id"] not in operations or operations[op["id"]].stage_id != op.get("stage_id"):
            raise ExportError("An accounted atomic operation is absent from the process map")
    for item in record["event_mappings"]:
        if (type(item.get("operation_id")) is not str or type(item.get("event_key")) is not str
                or (item["operation_id"], item["event_key"]) not in mapped):
            raise ExportError("An accounted event mapping is absent from the process map")
    return record


ATOMIC_SCHEMA = "atomic-map/1"
# atomic-map/2: the code-led record (direct-atomic-validator/9): code_led replaces the skeleton. /1 records of earlier
# runs keep their own validation unchanged.
CODE_LED_SCHEMA = "atomic-map/2"
_ATOMIC_STATUSES = frozenset({"complete", "partial", "failed", "not_run", "skipped_no_candidates"})
_ATOMIC_KEYS = frozenset({"schema", "status", "status_reasons", "catalog", "units", "settings", "skeleton", "priors",
                          "triage", "compose", "requests", "unit_states", "candidates", "operations", "event_mappings",
                          "transitions", "prior_diagnostics", "required_scenarios", "groups", "summary", "basis"})
_CODE_LED_KEYS = (_ATOMIC_KEYS - {"skeleton"}) | {"code_led"}
_LED_KEYS = frozenset({"working_groups", "stages", "facts", "obligations", "hypotheses", "gaps", "frontier",
                       "technical_relations", "basis"})
_GAP_KINDS = frozenset({"open_continuation", "unresolved_handoff", "unknown_continuation", "pending_local_work"})
# direct-atomic-validator/10 (transition-support/1): host gaps of inactive proposals and open control links. A record
# without transition_support (an earlier /9 record) keeps exactly the four kinds above.
_SUPPORT_GAPS = frozenset({"unsupported_transition", "unresolved_control_link"})
_HOST_GAPS = frozenset({"open_continuation", "pending_local_work"}) | _SUPPORT_GAPS
_SUPPORT_POLICY = "transition-support/1"
_SUPPORT_KEYS = frozenset({"policy", "proposals", "open_links", "basis"})
_PROPOSAL_KEYS = frozenset({"id", "request_id", "page", "from", "to", "condition", "basis", "contract", "basis_note",
                            "evidence_ids", "state", "reason", "support", "endpoints"})
_SUPPORT_RECORD_KEYS = frozenset({"kind", "unit_ids", "links", "witness_evidence_ids", "provenance", "unit_spans"})
_SUPPORT_LINK_KEYS = frozenset({"from_unit", "to_unit", "origins", "relation_ids", "call_lines", "witness_evidence_id"})
_OPEN_LINK_KEYS = frozenset({"operation_id", "unit_id", "request_id", "page", "kind", "relation_id", "target_unit_id",
                             "target", "where"})
_SUPPORT_KINDS = frozenset({"same_unit", "call_chain", "contract_both_sides"})
_SUPPORT_REASONS = frozenset({"endpoint_source_not_supplied", "no_source_chain", "chain_without_cited_call",
                              "handoff_one_side"})
_LINK_ORIGINS = frozenset({"host_static", "navigation", "worker_proposal"})
_HOST_LINK_ORIGINS = frozenset({"host_static", "navigation"})
_CONTROL_RELATIONS = frozenset({"call", "dispatch", "callback", "return", "control_handoff"})
_OPEN_RELATIONS = frozenset({"dispatch", "callback", "control_handoff"})
_SUPPORT_SUMMARY = ("transition_proposals", "transitions_inactive", "transitions_inactive_by_reason",
                    "open_control_links")
_STAGE_SOURCES = frozenset({"compose", "provisional_groups", "none"})
_STAGE_ORIGINS = {"compose": {"compose"}, "provisional_groups": {"working_group", "ungrouped"}, "none": set()}
_LED_GROUP_KEYS = frozenset({"id", "name", "contract", "page", "request_id"})
_LED_STAGE_KEYS = frozenset({"id", "name", "contract", "origin", "group_id", "operation_ids"})
_LED_FACT_KEYS = frozenset({"id", "origin", "stage_id", "operation_ids", "obligation_id", "lifecycle_roles",
                            "mapping_indexes"})
_LED_GAP_KEYS = frozenset({"id", "kind", "operation_ids", "unit_ids", "question", "source"})
_LED_FRAGMENT_KEYS = frozenset({"id", "operation_ids", "entry_ids"})
_LED_HYPOTHESIS_KEYS = frozenset({"id", "statement", "status", "operation_ids", "note", "source"})
_LED_OBLIGATIONS_KEYS = frozenset({"schema", "source", "source_sha256", "obligations_sha256", "items"})
_LED_OBLIGATION_KEYS = frozenset({"id", "text", "status", "operation_ids", "question", "fact_ids"})
_LED_EXITS = frozenset({"terminal", "continues", "external_handoff", "unknown"})
_LED_BASES = frozenset({"source_condition", "source_call", "source_handoff"})
_LED_ROLES = frozenset({"request", "start", "result", "refusal", "error", "retry", "duplicate", "no_op", "ui"})
_OBLIGATION_STATES = frozenset({"matched", "partial", "not_found", "not_checked"})
_HYPOTHESIS_STATES = frozenset({"supported", "contradicted", "not_found", "unclear"})
_RELATION_BINDING_KEYS = frozenset({"file", "schema", "registry_sha256", "review_run_fingerprint", "status", "worker",
                                    "host_static"})
_UNIT_STATES = frozenset({"accounted", "deferred", "request_failed", "triaged"})
_UNIT_DISPOSITIONS = {"accounted": {"realized", "supporting", "excluded", "outside_process", "unresolved"},
                      "triaged": {"supporting", "outside_process", "unresolved"}}
_HASH = re.compile(r"[0-9a-f]{64}")
_ATOMIC_ACCEPTED = frozenset({"accepted", "accepted_after_repair", "accepted_from_checkpoint",
                              "accepted_after_local_revalidation"})
# Local revalidation of an atomic page after its one repair (direct_atomic: _AUXILIARY, MAX_AUXILIARY_OMISSIONS).
_LOCAL_SCHEMA = "atomic-local-revalidation/1"
_DERIVATION = "local_auxiliary_omission"
# Composition only: invalid facts[i].guidance_ids values omitted after the one repair (direct_atomic GUIDANCE_*).
_GUIDANCE_SCHEMA = "compose-guidance-omission/1"
_GUIDANCE_DERIVATION = "local_guidance_omission"
_GUIDANCE_CODES = frozenset({"missing_guidance_ids", "null_not_allowed", "unknown_guidance_id", "wrong_type"})
_GUIDANCE_PATH = re.compile(r"facts\[\d+\]\.guidance_ids(?:\[\d+\])?")
_GUIDANCE_KEYS = frozenset({"schema", "after_attempt", "model_calls", "api_request_sha256", "raw_response_sha256",
                            "sanitized_response_sha256", "outcome", "warnings", "final"})
_GUIDANCE_WARNING_KEYS = frozenset({"path", "code", "actual_type", "action", "allowed_values_count", "attribution"})
_LOCAL_OMISSIONS = 12
_AUXILIARY_FIELDS = {"external_without_contract": "external_interactions",
                     "explanation_symbol_not_in_source": "code_explanations"}
_LOCAL_KEYS = frozenset({"schema", "derivation", "after_attempt", "source", "api_request_sha256", "raw_response_sha256",
                         "omissions", "omission_limit", "omission_limit_reached", "outcome",
                         "sanitized_response_sha256", "final_layer", "final_code", "final_path", "model_calls", "basis"})
_OMISSION_KEYS = frozenset({"operation", "field", "original_index", "code", "path", "item_sha256", "question"})
# direct_atomic /6: an omitted event_mappings entry of a supplied site without any operation or fact.
_SITE_OMISSION_KEYS = frozenset({"field", "original_index", "code", "path", "item_sha256", "site_id", "question"})
# Host primary units of unanchored declarations (declaration_index.declaration_coverage, direct_atomic /5).
_DECLARATION_ORIGIN = "declaration_inventory"
_COVERAGE_SCHEMA = "declaration-coverage/1"
_COVERAGE_BINDING_KEYS = frozenset({"file", "schema", "coverage_sha256", "snapshot_id", "index_sha256",
                                    "catalog_sha256", "units_sha256", "primary_units", "open_scope"})
# direct_atomic /7: context-anchored callable bodies are primary work under their own command unit. A binding without
# a policy is the original inventory, valid only for the validators that produced it.
_CONTEXT_ORIGIN = "context_anchor"
_CONTEXT_POLICY = "context-callables/1"
_ORIGINAL_POLICY_VALIDATORS = frozenset({"direct-atomic-validator/5", "direct-atomic-validator/6"})


def _atomic_units(record, process_map):
    """Validate every persisted unit state before using status, counts or operation links."""
    stage_ids = {stage.id for stage in process_map.stages}
    operations = {op.id: op for op in process_map.operations}
    open_work = False
    for identifier, state in record["unit_states"].items():
        if type(identifier) is not str or not identifier.strip() or type(state) is not dict:
            raise ExportError("An atomic unit has a malformed state")
        kind = state.get("state")
        if type(kind) is not str or kind not in _UNIT_STATES:
            raise ExportError("An atomic unit has an unknown state")
        links, stages = state.get("operation_ids"), state.get("stage_ids")
        if (not _action_strings(links) or len(links) != len(set(links)) or not _action_strings(stages)
                or len(stages) != len(set(stages)) or set(stages) - stage_ids):
            raise ExportError("An atomic unit has malformed operation or stage identities")
        if any(key not in operations for key in links):
            raise ExportError("An atomic unit decision references an operation absent from the map")
        if sorted({operations[key].stage_id for key in links}) != sorted(stages):
            raise ExportError("An atomic unit decision disagrees with the stages of its operations")
        if state.get("origin") is None:
            if not _action_strings(state.get("candidate_ids")) or not state["candidate_ids"]:
                raise ExportError("An atomic unit has no candidate identities")
        elif state["origin"] == _CONTEXT_ORIGIN:
            # A context unit is its own verified command unit (ID = declaration ID): no candidate, only the findings
            # that anchored it as context; source copies compare them with command_units.json.
            if (state.get("candidate_ids") != [] or not _action_strings(state.get("finding_ids"))
                    or state.get("declaration_id") != identifier):
                raise ExportError("A context declaration unit carries candidates or is not its own command unit")
        elif (state["origin"] != _DECLARATION_ORIGIN or state.get("candidate_ids") != []
              or state.get("finding_ids") != [] or type(state.get("declaration_id")) is not str
              or not state["declaration_id"].strip()):
            # A host declaration unit owns no catalog candidate and no model finding, only its declaration.
            raise ExportError("A host declaration unit carries candidates, findings or an unknown origin")
        disposition = state.get("disposition")
        if kind in _UNIT_DISPOSITIONS:
            if type(disposition) is not str or disposition not in _UNIT_DISPOSITIONS[kind]:
                raise ExportError("An atomic unit has an unknown disposition")
            if (disposition == "realized") != bool(links):
                raise ExportError("Only a realized unit names one or more operations")
        else:
            open_work = True
            if links or disposition is not None:
                raise ExportError("A deferred or failed atomic unit cannot carry a decision")
            if type(state.get("reason")) is not str or not state["reason"].strip():
                raise ExportError("A deferred or failed atomic unit needs its reason")
    for identifier, item in record["candidates"].items():
        if (type(identifier) is not str or type(item) is not dict or type(item.get("unit_id")) is not str
                or item["unit_id"] not in record["unit_states"]
                or identifier not in record["unit_states"][item["unit_id"]]["candidate_ids"]):
            raise ExportError("An atomic candidate references an unknown unit")
    owned = [identifier for state in record["unit_states"].values() for identifier in state["candidate_ids"]]
    if len(owned) != len(set(owned)) or set(owned) != set(record["candidates"]):
        raise ExportError("Atomic candidates and units do not form one partition")
    return open_work


def _atomic_source_copies(run, raw, record, fingerprints):
    """Validate the persisted catalog and complete unit partition without reading target code."""
    from .action_catalog import check_catalog
    from .declaration_index import check_coverage, check_units
    try:
        def read(name):
            raw[name] = _read(run, name)
            return _json(name, raw[name])
        if read("atomic_map.json") != record:
            raise ExportError("atomic_map.json differs from bundle.json")
        catalog = read("action_catalog.json")
        check_catalog(catalog, snapshot_id=record["catalog"]["snapshot_id"])
        if catalog["catalog_sha256"] != record["catalog"]["catalog_sha256"]:
            raise ExportError("Atomic source catalog differs from its recorded identity")
        if record["units"] is None:
            return
        index, units = read("declaration_index.json"), read("command_units.json")
        check_units(units, catalog=catalog, index=index)
        if (index["snapshot_id"] != catalog["snapshot_id"]
                or units["units_sha256"] != record["units"]["units_sha256"]
                or any(fingerprints.get("repo/" + path) != row["sha256"] for path, row in index["files"].items())):
            raise ExportError("Atomic declarations or units refer to foreign source inputs")
        expected = {row["id"]: row["candidate_ids"] for row in units["units"] if row["role"] == "candidate"}
        known = {row["id"]: row for row in units["units"]}  # every saved unit a transition support may name
        binding = record.get("declaration_coverage")
        if binding is not None:
            # The inventory is recomputed from the saved catalog and index: an omitted, moved or foreign host unit
            # cannot pass, even with a resealed document.
            coverage = read("declaration_coverage.json")
            check_coverage(coverage, catalog=catalog, index=index)
            # The whole binding, aggregates included, is derived from the validated document: the same false counts
            # written into both bundle.json and atomic_map.json cannot hide unknown source scope or host units.
            derived = {"file": "declaration_coverage.json", "primary_units": len(coverage["primary_units"]),
                       "open_scope": {"files": len(coverage["open_scope"]["files"]),
                                      "blocking": coverage["open_scope"]["blocking"]},
                       **{key: coverage[key] for key in ("schema", "coverage_sha256", "snapshot_id", "index_sha256",
                                                         "catalog_sha256", "units_sha256")}}
            if "policy" in coverage:  # recomputed above under this policy; the record's settings must declare it
                derived.update(policy=coverage["policy"], context_units=len(coverage["context_units"]))
            if binding != derived:
                raise ExportError("Declaration coverage binding differs from the saved, recomputed coverage")
            for unit in coverage["primary_units"]:
                state = record["unit_states"].get(unit["id"])
                if (unit["id"] in expected or type(state) is not dict or state.get("origin") != _DECLARATION_ORIGIN
                        or state.get("declaration_id") != unit["declaration_id"]):
                    raise ExportError("Atomic ledger omits or changes a host declaration unit")
                expected[unit["id"]] = []
                known[unit["id"]] = unit
            command = {row["id"]: row for row in units["units"]}
            for view in coverage.get("context_units", []):
                state, unit = record["unit_states"].get(view["id"]), command[view["id"]]
                if (view["id"] in expected or type(state) is not dict or state.get("origin") != _CONTEXT_ORIGIN
                        or state.get("declaration_id") != unit["declaration_id"]
                        or state.get("finding_ids") != unit["finding_ids"]):
                    raise ExportError("Atomic ledger omits or changes a context declaration unit")
                expected[view["id"]] = []
        _source_observations(record, catalog["snapshot_id"], known, fingerprints)
        actual = {key: row["candidate_ids"] for key, row in record["unit_states"].items()}
        if actual != expected:
            raise ExportError("Atomic ledger omits or changes units from the saved complete partition")
        relations = (record.get("code_led") or {}).get("technical_relations")
        rows = None
        if relations is not None:
            # The relation registry the pages used: unaltered, bound to this snapshot and technical review.
            from .technical_relations import binding as relation_binding, check_registry
            registry = read(relations["file"])
            check_registry(registry, snapshot_id=record["catalog"]["snapshot_id"],
                           review_run_fingerprint=relations["review_run_fingerprint"])
            if relation_binding(registry) != relations:
                raise ExportError("The technical relation registry differs from its binding in the atomic record")
            rows = {row["id"]: row for row in [*registry["worker"], *registry["host_static"]]}
        support = (record.get("code_led") or {}).get("transition_support")
        if support is not None:
            _support_sources(support, known, rows, _index_calls(index))
    except ExportError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ExportError("Invalid atomic source accounting: " + str(exc)) from None


def _local_revalidation(request, reject, sites_allowed=False):
    """Check one request's local revalidation record against its own attempts and accepted reply.

    Returns "accepted" (the accepted reply was derived locally from the rejected repair reply), "rejected" (it was
    tried and the page stayed rejected) or None (none was made; then no accepted reply may claim a derivation).
    An omitted event_mappings entry is accepted only when the record's settings declare that policy and its site is
    one this request supplied; details omissions are checked exactly as before.
    """
    local, accepted, attempts, outcome = (request.get("local_revalidation"), request.get("accepted"),
                                          request["attempts"], request["outcome"])
    if local is None:
        if outcome == "accepted_after_local_revalidation" or (
                type(accepted) is dict and {"derivation", "raw_response_sha256"} & set(accepted)):
            reject("a locally derived reply has no local revalidation record")
        return None
    if (request.get("kind") != "atomic" or type(local) is not dict or set(local) != _LOCAL_KEYS
            or local["schema"] != _LOCAL_SCHEMA or local["derivation"] != _DERIVATION or local["after_attempt"] != 2
            or local["model_calls"] != 0 or local["omission_limit"] != _LOCAL_OMISSIONS
            or local["outcome"] not in ("accepted", "rejected") or type(local["omission_limit_reached"]) is not bool
            or any(type(local[key]) is not str or not _HASH.fullmatch(local[key])
                   for key in ("api_request_sha256", "raw_response_sha256"))
            or type(local["omissions"]) is not list or not 1 <= len(local["omissions"]) <= _LOCAL_OMISSIONS):
        reject("local revalidation record is malformed")
    seen = set()
    for item in local["omissions"]:
        if type(item) is dict and item.get("field") == "event_mappings":
            supplied = request.get("site_ids") if type(request.get("site_ids")) is list else []
            if (not sites_allowed or set(item) != _SITE_OMISSION_KEYS or item["code"] != "operation_choice"
                    or type(item["original_index"]) is not int or item["original_index"] < 0
                    or item["path"] != f"event_mappings[{item['original_index']}].new_operation"
                    or type(item["site_id"]) is not str or item["site_id"] not in supplied
                    or type(item["item_sha256"]) is not str or not _HASH.fullmatch(item["item_sha256"])
                    or type(item["question"]) is not str or not item["question"].strip()
                    or ("event_mappings", item["original_index"]) in seen):
                reject("local revalidation omission is malformed or not an unassigned supplied site")
            seen.add(("event_mappings", item["original_index"]))
            continue
        if (type(item) is not dict or set(item) != _OMISSION_KEYS or item["code"] not in tuple(_AUXILIARY_FIELDS)
                or item["field"] != _AUXILIARY_FIELDS[item["code"]]
                or any(type(item[key]) is not int or item[key] < 0 for key in ("operation", "original_index"))
                or item["path"] != (f"operations[{item['operation']}].details.{item['field']}[{item['original_index']}]"
                                    + (".symbol" if item["field"] == "code_explanations" else ""))
                or type(item["item_sha256"]) is not str or not _HASH.fullmatch(item["item_sha256"])
                or type(item["question"]) is not str or not item["question"].strip()
                or (item["operation"], item["field"], item["original_index"]) in seen):
            reject("local revalidation omission is malformed or not an auxiliary details item")
        seen.add((item["operation"], item["field"], item["original_index"]))
    derived = local["sanitized_response_sha256"]
    if local["outcome"] == "accepted":
        if (local["omission_limit_reached"] or any(local[key] is not None for key in
                                                   ("final_layer", "final_code", "final_path"))
                or type(derived) is not str or not _HASH.fullmatch(derived) or derived == local["raw_response_sha256"]
                or accepted != {"attempt": 2, "api_request_sha256": local["api_request_sha256"],
                                "response_sha256": derived, "derivation": _DERIVATION,
                                "raw_response_sha256": local["raw_response_sha256"]}):
            reject("locally derived reply is not bound to its raw repair reply")
    elif (derived is not None or type(local["final_code"]) is not str or accepted is not None
          or (local["omission_limit_reached"] and len(local["omissions"]) != _LOCAL_OMISSIONS)):
        reject("rejected local revalidation carries a derived reply")
    if local["source"] == "repair_response":
        allowed = ({"accepted_after_local_revalidation", "commit_failed"} if local["outcome"] == "accepted"
                   else {"rejected_after_repair"})
        first = local["omissions"][0]
        if (outcome not in allowed or len(attempts) != 2
                or any(attempt.get("outcome") != "rejected" for attempt in attempts)
                or attempts[1].get("api_request_sha256") != local["api_request_sha256"]
                or attempts[1].get("response_sha256") != local["raw_response_sha256"]
                or (attempts[1].get("code"), attempts[1].get("path")) != (first["code"], first["path"])):
            reject("local revalidation is not bound to the rejected repair attempt")
    elif local["source"] == "checkpoint":
        if (local["outcome"] != "accepted" or outcome not in {"accepted_from_checkpoint", "commit_failed"}
                or len(attempts) != 1 or attempts[0].get("outcome") != "accepted_from_checkpoint"
                or attempts[0].get("response_sha256") != derived or attempts[0].get("original") != accepted):
            reject("reused local revalidation is not bound to its checkpoint")
    else:
        reject("local revalidation source is unknown")
    return local["outcome"]


def _guidance_revalidation(request, reject):
    """A composition accepted (or still rejected) after omitting only invalid facts[i].guidance_ids values from a
    copy of its rejected repair reply: bound to that attempt, warnings exact, no model call."""
    local, accepted, attempts, outcome = (request["guidance_revalidation"], request.get("accepted"),
                                          request["attempts"], request["outcome"])
    if (request.get("kind") != "compose" or type(local) is not dict or set(local) != _GUIDANCE_KEYS
            or local["schema"] != _GUIDANCE_SCHEMA or local["after_attempt"] != 2 or local["model_calls"] != 0
            or local["outcome"] not in ("accepted", "rejected")
            or any(type(local[key]) is not str or not _HASH.fullmatch(local[key])
                   for key in ("api_request_sha256", "raw_response_sha256"))
            or type(local["warnings"]) is not list or not local["warnings"]
            or any(type(item) is not dict or set(item) != _GUIDANCE_WARNING_KEYS or item["code"] not in _GUIDANCE_CODES
                   or type(item["path"]) is not str or not _GUIDANCE_PATH.fullmatch(item["path"])
                   or item["action"] != "omit_invalid_auxiliary_attribution"
                   or item["attribution"] not in ("partial", "not_established") for item in local["warnings"])
            or len({item["path"] for item in local["warnings"]}) != len(local["warnings"])):
        reject("composition guidance revalidation record is malformed")
    if (len(attempts) != 2 or any(attempt.get("outcome") != "rejected" for attempt in attempts)
            or attempts[1].get("api_request_sha256") != local["api_request_sha256"]
            or attempts[1].get("response_sha256") != local["raw_response_sha256"]
            or (attempts[1].get("code"), attempts[1].get("path")) not in
            {(item["code"], item["path"]) for item in local["warnings"]}):
        reject("composition guidance revalidation is not bound to the rejected repair attempt")
    derived = local["sanitized_response_sha256"]
    if local["outcome"] == "accepted":
        if (local["final"] is not None or type(derived) is not str or not _HASH.fullmatch(derived)
                or derived == local["raw_response_sha256"] or outcome != "accepted_after_local_revalidation"
                or accepted != {"attempt": 2, "api_request_sha256": local["api_request_sha256"],
                                "response_sha256": derived, "derivation": _GUIDANCE_DERIVATION,
                                "raw_response_sha256": local["raw_response_sha256"]}):
            reject("composition derived from its repair is not bound to the raw reply")
    elif (derived is not None or accepted is not None or outcome != "rejected_after_repair"
          or type(local["final"]) is not dict or type(local["final"].get("code")) is not str):
        reject("rejected composition guidance revalidation carries a derived reply")
    return local["outcome"]


def _atomic_details(record, process_map):
    """Check semantic copies and recomputable accounting, not just their identities."""
    def reject(message):
        raise ExportError("Atomic accounting: " + message)
    def counts(values):
        return dict(sorted(Counter(values).items()))
    def hash_or_none(value):
        return value is None or type(value) is str and _HASH.fullmatch(value)
    operations = {op.id: op for op in process_map.operations}
    for item in record["operations"]:
        op = operations[item["id"]]
        for key in ("name", "stage_id", "kind", "description", "actor"):
            actual = getattr(op, key)
            actual = getattr(actual, "value", actual)
            if item.get(key) != actual:
                reject("operation details disagree with process_map")
        # Optional structured details (R08): the record copy must equal the typed map copy, nested evidence included.
        # Records of earlier runs have no key; their map operations have no details either.
        if item.get("details") != (None if op.details is None else op.details.to_dict()):
            reject("operation structured details disagree with process_map")
        refs = item.get("model_evidence_ids"), item.get("unit_evidence_ids")
        if any(not _action_strings(value) for value in refs):
            reject("operation evidence is malformed")
        if list(dict.fromkeys([*refs[0], *refs[1]])) != list(op.evidence_ids):
            reject("operation evidence differs from process_map")
        if not _action_strings(item.get("unit_ids")) or any(
                identifier not in record["unit_states"] or op.id not in record["unit_states"][identifier]["operation_ids"]
                for identifier in item["unit_ids"]):
            reject("operation unit links disagree with the unit ledger")
    keys = ("event_key", "operation_id", "stage_id", "lifecycle_role", "occurrence_rule", "role_basis",
            "role_support", "evidence_ids")
    expected = [{key: item.get(key) for key in keys} for item in record["event_mappings"]]
    actual = [{key: getattr(row, key) for key in keys} for row in process_map.event_mappings if row.operation_id]
    if expected != actual:
        reject("event mappings disagree with process_map")
    if any(item.get("operation_name") != operations[item["operation_id"]].name for item in record["event_mappings"]):
        reject("mapping operation name differs")
    edges = [{"id": edge.id, "from": edge.from_operation, "to": edge.to_operation,
              "condition": edge.condition, "evidence_ids": list(edge.evidence_ids)} for edge in process_map.operation_transitions]
    if [{key: item.get(key) for key in ("id", "from", "to", "condition", "evidence_ids")}
            for item in record["transitions"]] != edges:
        reject("operation transitions disagree with process_map")
    requests = record["requests"]
    identifiers = []
    for request in requests:
        if (type(request.get("id")) is not str or not request["id"].strip()
                or request.get("kind") not in {"atomic", "triage", "compose"}
                or request.get("outcome") not in _ATOMIC_ACCEPTED | {"rejected", "rejected_after_repair",
                                                                      "commit_failed"}
                or type(request.get("attempts")) is not list or not 1 <= len(request["attempts"]) <= 2):
            reject("request or attempts are malformed")
        identifiers.append(request["id"])
        attempts = request["attempts"]
        for index, attempt in enumerate(attempts):
            if (type(attempt) is not dict or type(attempt.get("attempt")) is not int
                    or attempt["attempt"] != (0 if request["outcome"] == "accepted_from_checkpoint" else index + 1)
                    or attempt.get("outcome") not in {"accepted", "rejected", "accepted_from_checkpoint"}
                    or not hash_or_none(attempt.get("api_request_sha256"))
                    or not hash_or_none(attempt.get("response_sha256"))):
                reject("attempt identity or result is malformed")
        accepted = request.get("accepted")
        recovery = record["settings"].get("auxiliary_recovery")
        if "guidance_revalidation" in request:
            if "local_revalidation" in request:
                reject("a request carries two local revalidations")
            local = _guidance_revalidation(request, reject)
        else:
            local = _local_revalidation(request, reject, sites_allowed=type(recovery) is dict
                                        and type(recovery.get("unassigned_sites")) is dict)
        success = request["outcome"].startswith("accepted") or request["outcome"] == "commit_failed"
        if success:
            # A reply derived from the rejected repair is bound to that attempt by _local_revalidation.
            if not (local == "accepted" and attempts[-1]["outcome"] == "rejected") and (
                    type(accepted) is not dict or accepted.get("attempt") not in (1, 2)
                    or not hash_or_none(accepted.get("api_request_sha256"))
                    or type(accepted.get("response_sha256")) is not str
                    or not _HASH.fullmatch(accepted["response_sha256"])
                    or attempts[-1]["outcome"] not in {"accepted", "accepted_from_checkpoint"}
                    or attempts[-1]["response_sha256"] != accepted["response_sha256"]):
                reject("accepted response is inconsistent with attempts")
        elif accepted is not None or attempts[-1]["outcome"] != "rejected":
            reject("failed request carries an accepted response")
        if (not _action_strings(request.get("unit_ids", []))
                or set(request.get("unit_ids", [])) - set(record["unit_states"])):
            reject("request names an unknown primary unit")
    if len(identifiers) != len(set(identifiers)):
        reject("request IDs repeat")
    for state in record["unit_states"].values():
        if state.get("request_id") is not None and state["request_id"] not in identifiers:
            reject("unit refers to an unknown request")
    states = list(record["unit_states"].values())
    expected_summary = {
        "candidates": len(record["candidates"]), "units": len(states),
        "decisions": sum(row["state"] == "accounted" for row in states),
        "triaged": sum(row["state"] == "triaged" for row in states), "requests": len(requests),
        "requests_by_kind": counts(row["kind"] for row in requests),
        "requests_by_outcome": counts(row["outcome"] for row in requests),
        "dispositions": counts(row["disposition"] for row in states if row["state"] in {"accounted", "triaged"}),
        "deferred_by_reason": counts(row["reason"] for row in states if row["state"] == "deferred"),
        "failed_by_reason": counts(row["reason"] for row in states if row["state"] == "request_failed"),
        "operations_added": len(record["operations"]), "mappings_added": len(record["event_mappings"]),
        "mappings_by_role": counts(row["lifecycle_role"] for row in record["event_mappings"]),
        "mappings_by_support": counts(row["role_support"] for row in record["event_mappings"]),
        "transitions_added": len(record["transitions"]),
        "required_scenarios_unresolved": sum(row["status"] == "unresolved" for row in record["required_scenarios"])}
    if any(record["summary"].get(key) != value for key, value in expected_summary.items()):
        reject("summary disagrees with the complete unit/request ledger")
    # Records saved before local revalidation existed have no count and no local records.
    omitted = sum(len(row["local_revalidation"]["omissions"]) for row in requests
                  if (row.get("local_revalidation") or {}).get("outcome") == "accepted")
    if ("auxiliary_omissions" in record["summary"] or omitted) and record["summary"].get("auxiliary_omissions") != omitted:
        reject("summary disagrees with the local revalidation records")
    for item in record["required_scenarios"]:
        if (item.get("source") not in {"business_text", "model_proposal"}
                or not hash_or_none(item.get("source_sha256"))
                or (item["source"] == "business_text" and item.get("source_sha256") is None)):
            reject("scenario source authority is malformed")


def _atomic_fact_links(record, bundle):
    """Exact fact links of accepted bindings: known facts of the binding's own stage, each once."""
    coverage = bundle.get("log_coverage")
    rows = coverage.get("facts") if type(coverage) is dict else None
    stages = {item.get("id"): item.get("stage_id") for item in (rows if type(rows) is list else []) if type(item) is dict}
    for item in record["event_mappings"]:
        links = item.get("fact_ids", [])
        if (not _action_strings(links) or len(links) != len(set(links))
                or any(key not in stages or stages[key] != item.get("stage_id") for key in links)):
            raise ExportError("Atomic accounting: an event mapping links an unknown or foreign-stage fact")


def _declaration_accounting(record):
    """Host declaration units against the record's declaration-coverage binding; legacy records have neither.

    A record whose settings describe declaration accounting must carry the binding whenever it has units, and its
    host unit states, counts and open source scope must agree with it; complete is impossible with blocking scope.
    """
    binding, settings = record.get("declaration_coverage"), record["settings"]
    hosts = [state for state in record["unit_states"].values() if state.get("origin") == _DECLARATION_ORIGIN]
    contexts = [state for state in record["unit_states"].values() if state.get("origin") == _CONTEXT_ORIGIN]
    by_state = dict(sorted(Counter(str(state["state"]) for state in hosts).items()))
    summary = record["summary"]
    fresh = type(binding) is dict and "policy" in binding
    if binding is None:
        if hosts or contexts or ("declaration_completion" in settings and record["units"] is not None):
            raise ExportError("Atomic declaration accounting lacks its declaration coverage binding")
    else:
        scope = binding.get("open_scope") if type(binding) is dict else None
        units = record["units"] if type(record["units"]) is dict else {}
        descriptor = settings.get("declaration_completion")
        policy = descriptor.get("context_units") if type(descriptor) is dict else None
        validator = settings.get("validator")
        original = type(validator) is str and validator in _ORIGINAL_POLICY_VALIDATORS
        # The context policy is declared by binding and settings together, or by neither; the original inventory
        # belongs only to the validators that produced it, so a current run cannot be relabelled as one of them.
        declared = (type(policy) is dict and policy.get("policy") == _CONTEXT_POLICY and not original if fresh
                    else type(descriptor) is dict and "context_units" not in descriptor and original)
        if (not declared or type(binding) is not dict
                or set(binding) != (_COVERAGE_BINDING_KEYS | {"policy", "context_units"} if fresh
                                    else _COVERAGE_BINDING_KEYS)
                or (fresh and (binding["policy"] != _CONTEXT_POLICY or type(binding["context_units"]) is not int
                               or binding["context_units"] != len(contexts)))
                or (not fresh and contexts)
                or binding["file"] != "declaration_coverage.json"
                or binding["schema"] != _COVERAGE_SCHEMA
                or any(type(binding[key]) is not str or not _HASH.fullmatch(binding[key])
                       for key in ("coverage_sha256", "snapshot_id", "index_sha256", "units_sha256"))
                or binding["snapshot_id"] != record["catalog"]["snapshot_id"]
                or binding["catalog_sha256"] != record["catalog"]["catalog_sha256"]
                or binding["units_sha256"] != units.get("units_sha256")
                or binding["index_sha256"] != units.get("index_sha256")
                or type(binding["primary_units"]) is not int or binding["primary_units"] != len(hosts)
                or type(scope) is not dict or set(scope) != {"files", "blocking"}
                or any(type(value) is not int or value < 0 for value in scope.values())
                or scope["blocking"] != scope["files"]):  # every file with unknown declaration inventory blocks
            raise ExportError("Atomic declaration coverage binding is malformed, foreign or miscounted")
        if scope["blocking"] and record["status"] == "complete":
            raise ExportError("A complete atomic map cannot have source files with unknown declaration inventory")
    if (binding is not None or "declaration_units" in summary) and (
            summary.get("declaration_units") != len(hosts) or summary.get("declaration_units_by_state") != by_state):
        raise ExportError("Atomic summary disagrees with its host declaration units")
    counted = {"context_primary_units": len(contexts),
               "context_primary_units_by_state": dict(sorted(Counter(str(state["state"]) for state in contexts).items()))}
    if ({key: summary[key] for key in counted if key in summary} != (counted if fresh else {})):
        raise ExportError("Atomic summary disagrees with its context declaration units")


def _check_instrumentation_accounting(bundle):
    if "instrumentation_accounting" not in bundle:
        return
    from .instrumentation_accounting import account
    try:
        expected = account(bundle["log_coverage"]["facts"], bundle["instrumentation_plan"]["cards"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ExportError("Invalid instrumentation fact accounting") from exc
    if bundle["instrumentation_accounting"] != expected:
        raise ExportError("Instrumentation fact accounting differs from actual facts and proposed cards")


def _atomic_accounting(bundle, process_map, snapshot_id):
    """The run's atomic map record, cross-checked against map, catalog and units; None for other runs.

    A present record must be complete and consistent: an altered or foreign record is refused, never read as an
    older run without accounting.
    """
    _check_instrumentation_accounting(bundle)  # also validate a present record in legacy/coarse bundles
    if "atomic_map" not in bundle:
        return None
    record = bundle["atomic_map"]
    schema = record.get("schema") if type(record) is dict else None
    keys = {ATOMIC_SCHEMA: _ATOMIC_KEYS, CODE_LED_SCHEMA: _CODE_LED_KEYS}.get(schema)
    if keys is None or set(record) not in (keys, keys | {"declaration_coverage"}):
        raise ExportError("bundle.json atomic_map is not a complete atomic-map/1 or atomic-map/2 record")
    if (type(record["status"]) is not str or record["status"] not in _ATOMIC_STATUSES
            or type(record["unit_states"]) is not dict or type(record["candidates"]) is not dict):
        raise ExportError("bundle.json atomic_map has an unknown status or unit ledger")
    if (not _action_strings(record["status_reasons"]) or type(record["settings"]) is not dict
            or type(record["summary"]) is not dict or type(record["basis"]) is not str):
        raise ExportError("bundle.json atomic_map has malformed settings, summary or reasons")
    for key in ("requests", "operations", "event_mappings", "transitions", "prior_diagnostics", "required_scenarios",
                "groups"):
        if type(record[key]) is not list or any(type(item) is not dict for item in record[key]):
            raise ExportError(f"bundle.json atomic_map has malformed {key}")
    for key in ("deferred_by_reason", "failed_by_reason", "dispositions"):
        counts = record["summary"].get(key, {})
        if (type(counts) is not dict or any(type(name) is not str or type(count) is not int or count < 0
                                          for name, count in counts.items())):
            raise ExportError("bundle.json atomic_map has malformed summary counts")
    catalog, summary = record["catalog"], bundle.get("action_catalog")
    if type(catalog) is not dict or catalog.get("snapshot_id") != snapshot_id:
        raise ExportError("bundle.json atomic_map belongs to a different source snapshot")
    if (type(summary) is not dict or type(catalog.get("catalog_sha256")) is not str
            or not _HASH.fullmatch(catalog["catalog_sha256"]) or summary.get("catalog_sha256") != catalog["catalog_sha256"]):
        raise ExportError("bundle.json atomic_map and action_catalog identities differ")
    units, saved = record["units"], bundle.get("command_units")
    if units is not None or record["status"] not in {"failed", "not_run"}:
        if (type(units) is not dict or type(saved) is not dict or type(units.get("units_sha256")) is not str
                or not _HASH.fullmatch(units["units_sha256"]) or saved.get("units_sha256") != units["units_sha256"]
                or units.get("catalog_sha256") != catalog["catalog_sha256"]):
            raise ExportError("bundle.json atomic_map and command_units identities differ")
    if (record.get("settings", {}).get("instrumentation_accounting") == "instrumentation-fact-accounting/1"
            and "instrumentation_accounting" not in bundle):
        raise ExportError("The declared instrumentation fact accounting is absent")
    _source_observations(record, snapshot_id)
    open_work = _atomic_units(record, process_map)
    _declaration_accounting(record)
    operations = {op.id: op for op in process_map.operations}
    for op in record["operations"]:
        if type(op.get("id")) is not str or op["id"] not in operations or operations[op["id"]].stage_id != op.get("stage_id"):
            raise ExportError("An atomic operation is absent from the process map")
    if {op["id"] for op in record["operations"]} != set(operations):
        raise ExportError("The process map holds operations outside the atomic record")
    mapped = {(item.operation_id, item.event_key) for item in process_map.event_mappings if item.operation_id}
    for item in record["event_mappings"]:
        if (type(item.get("operation_id")) is not str or type(item.get("event_key")) is not str
                or (item["operation_id"], item["event_key"]) not in mapped):
            raise ExportError("An atomic event mapping is absent from the process map")
    _atomic_details(record, process_map)
    _atomic_fact_links(record, bundle)
    scenarios = {item.id for item in process_map.scenarios}
    for item in record["required_scenarios"]:
        links = item.get("scenario_ids")
        if (type(item.get("id")) is not str or type(item.get("name")) is not str or not item["name"].strip()
                or item.get("status") not in {"unresolved", "assembled"} or not _action_strings(links)
                or set(links) - scenarios or (item["status"] == "assembled") != bool(links)):
            raise ExportError("An atomic required scenario is malformed or references an absent scenario")
    failed = any(item.get("kind") == "atomic" and item.get("outcome") not in _ATOMIC_ACCEPTED
                 for item in record["requests"])
    if record["status"] == "complete" and (open_work or failed or record["status_reasons"]):
        raise ExportError("Complete atomic accounting contains deferred, failed or unplanned units")
    if schema == CODE_LED_SCHEMA:
        _code_led(record, process_map, bundle)
    return record


def _source_observations(record, snapshot_id, known=None, fingerprints=None):
    """Saved source observations are typed navigation/read metadata, never semantic unit decisions.

    Verify the complete result hash, source identity and exact unit/read arguments without target I/O. Final saved
    units/fingerprints add the actual range and snapshot binding at export. This does not certify a search hit as a
    control link or prove model understanding. The current model request's excerpts authorize citations separately.
    """
    policy = (record.get("settings", {}).get("source_investigation") or {}).get("policy")
    for entry in record["requests"]:
        if "source_observation" not in entry:
            continue
        try:
            observation = entry["source_observation"]
            if (policy != "atomic-source-investigation/1" or entry["kind"] != "atomic"
                    or entry["outcome"] not in _ATOMIC_ACCEPTED or type(observation) is not dict
                    or set(observation) != {"policy", "request", "result", "result_sha256", "unit_id"}
                    or observation["policy"] != policy):
                raise ValueError("unknown policy or request state")
            command, result = observation["request"], observation["result"]
            if (type(command) is not dict or set(command) != {"tool", "unit_id", "path", "start_line", "end_line",
                                                            "query", "section", "offset", "limit"}
                    or type(result) is not dict
                    or observation["result_sha256"] != hashlib.sha256(json.dumps(
                        result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    or type(command["offset"]) is not int or command["offset"] < 0
                    or type(command["limit"]) is not int or not 1 <= command["limit"] <= 64):
                raise ValueError("invalid command or result hash")
            tools = {"index": {"query"}, "search": {"query"}, "read": {"path", "start_line", "end_line"},
                     "read_unit": {"unit_id"}, "inspect_map": {"section"}}
            tool = command["tool"]
            if tool not in tools or any(command[key] is not None for key in
                    {"unit_id", "path", "start_line", "end_line", "query", "section"} - tools[tool]):
                raise ValueError("inapplicable arguments")
            uid = observation["unit_id"]
            if uid is not None and (type(uid) is not str or (known is not None and uid not in known)):
                raise ValueError("foreign unit")
            support = (record.get("code_led") or {}).get("transition_support") or {}
            semantic = [*record["operations"], *record["event_mappings"],
                        *(record.get("code_led") or {}).get("working_groups", []),
                        *support.get("proposals", []), *support.get("reviews", []), *support.get("open_links", []),
                        *record["unit_states"].values()]
            if any(row.get("request_id") == entry["id"] for row in semantic):
                raise ValueError("source observation claims a semantic map decision")
            if tool in ("read", "read_unit"):
                path, first, last = result["path"], result["start_line"], result["end_line"]
                from .source_tools import blocked_path
                if (type(path) is not str or str(PurePosixPath(path)) != path or PurePosixPath(path).is_absolute()
                        or ".." in PurePosixPath(path).parts or "\\" in path or blocked_path(path)
                        or type(first) is not int or type(last) is not int or not 1 <= first <= last
                        or last - first + 1 > 120 or result["snapshot_id"] != snapshot_id
                        or type(result["text"]) is not str or result["use"] != "source_data_only"
                        or result["text_sha256"] != hashlib.sha256(result["text"].encode()).hexdigest()
                        or type(result["new_lines"]) is not int or not 0 <= result["new_lines"] <= last - first + 1
                        or type(result["seen_before"]) is not bool):
                    raise ValueError("invalid source read")
                numbered = result["text"].splitlines()
                if len(numbered) != last - first + 1 or any(
                        not text.startswith(str(number) + ": ") for number, text in enumerate(numbered, first)):
                    raise ValueError("read text does not contain its exact numbered range")
                if tool == "read" and (command["path"], command["start_line"], command["end_line"]) != (path, first, last):
                    raise ValueError("read differs from its command")
                if tool == "read_unit" and command["unit_id"] != uid:
                    raise ValueError("read unit differs from its command")
                if fingerprints is not None and fingerprints.get("repo/" + path) != result["source_sha256"]:
                    raise ValueError("read from a different source")
                if known is not None and uid is not None:
                    unit = known[uid]; start, end = _unit_lines(unit)
                    if path != unit["path"] or not start <= first <= last <= end or (
                            tool == "read_unit" and (first, last) != (start, end)):
                        raise ValueError("read outside its claimed unit")
            else:
                if uid is not None or result["use"] != ("construction_state_only" if tool == "inspect_map"
                                                        else "navigation_only"):
                    raise ValueError("navigation claims source authority")
                if tool != "inspect_map" and result["snapshot_id"] != snapshot_id:
                    raise ValueError("foreign navigation snapshot")
                key = {"index": "units", "search": "matches", "inspect_map": "rows"}[tool]
                if (type(result[key]) is not list or len(result[key]) > command["limit"]
                        or result["offset"] != command["offset"] or type(result["total"]) is not int
                        or type(result["omitted"]) is not int or result["total"] < len(result[key])):
                    raise ValueError("invalid navigation page")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ExportError("Invalid atomic source observation: " + str(exc)) from None


def _transition_support(record, operations, bundle, reject):
    """Strict check of transition_support (transition-support/1); returns the support gaps it requires.

    Every proposal has exact keys, a unique ID, an accepted atomic request of its page, operations of this record,
    registered citations and endpoint units that realize or serve its operations by the unit ledger. Inactive
    proposals carry a known reason and no support; active ones carry a support of their basis whose units are
    endpoint units supplied on that page (page units or shared rows), whose owned source (unit_spans) lies
    in that page's excerpts, and whose witnesses are their own citations inside the exact owned source of the unit
    each one is claimed for (a sender and a receiver, the one unit, or each link's calling unit) and cover the recorded
    call lines. The accepted transitions are exactly the active proposals in order, so no subset can be forged and no
    inactive proposal can reach the map. Open control links name a realizing unit of their operation accounted on
    their page. Unit lines, relation rows and call lines behind the support are checked against the saved units,
    declaration index and registry (_support_sources). This validates structure and provenance only: the source is not
    reread, and a structurally eligible order with a wrong condition still passes.
    """
    from .transition_support import still_open, unsupported_pairs

    def text(value):
        return type(value) is str and bool(value.strip())

    def strings(value):
        return _action_strings(value) and len(value) == len(set(value))

    support = record["code_led"]["transition_support"]
    if (type(support) is not dict or set(support) not in (_SUPPORT_KEYS, _SUPPORT_KEYS | {"reviews"}) or support["policy"] != _SUPPORT_POLICY
            or type(support["proposals"]) is not list or type(support["open_links"]) is not list
            or type(support["basis"]) is not str
            or any(type(item) is not dict for item in [*support["proposals"], *support["open_links"]])):
        reject("transition support is malformed or of another policy")
    units = record["unit_states"]
    evidence = {item.get("id"): item for item in bundle.get("evidence", []) if type(item) is dict}
    requests = {item.get("id"): item for item in record["requests"]
                if item.get("kind") == "atomic" and item.get("outcome") in _ATOMIC_ACCEPTED
                and "source_observation" not in item}

    def anchor(identifier):
        found = (evidence.get(identifier) or {}).get("source_anchor")
        if (type(found) is not dict or type(found.get("path")) is not str or type(found.get("start_line")) is not int
                or type(found.get("end_line")) is not int):
            return None
        return found["path"], found["start_line"], found["end_line"]

    def within(found, spans):
        """Every line of found (path, first, last) lies in spans [(path, first, last)] of the same path."""
        if found is None or type(spans) is not list:
            return False
        path, first, last = found
        lines = {line for item in spans if type(item) in (list, tuple) and len(item) == 3 and item[0] == path
                 and type(item[1]) is int and type(item[2]) is int
                 for line in range(max(item[1], first), min(item[2], last) + 1)}
        return first <= last and len(lines) == last - first + 1

    def excerpts(request):
        return [(item.get("path"), item.get("start_line"), item.get("end_line"))
                for item in request.get("excerpts") or [] if type(item) is dict]

    def owned_source(found, request):
        """unit_spans: each supported unit was supplied on the page and owns exact, supplied ranges."""
        spans = found["unit_spans"]
        page_units = [*(request.get("unit_ids") or []), *(request.get("shared_context_unit_ids") or [])]
        return (type(spans) is dict and set(spans) == set(found["unit_ids"])
                and all(unit in page_units and type(rows) is list and rows
                        and all(type(row) is list and len(row) == 3 and type(row[0]) is str and type(row[1]) is int
                                and type(row[2]) is int and 1 <= row[1] <= row[2]
                                and within(tuple(row), excerpts(request)) for row in rows)
                        for unit, rows in spans.items()))

    def owned(unit, key):
        """The unit realizes operation key or is a supporting unit serving one that does (host accounting)."""
        state, realizing = units.get(unit), operations[key]["unit_ids"]
        return type(state) is dict and (unit in realizing or (
            state.get("disposition") == "supporting" and type(state.get("serves_unit_ids")) is list
            and bool(set(state["serves_unit_ids"]) & set(realizing))))

    ids = set()
    for index, item in enumerate(support["proposals"]):
        where = f"transition proposal {index}"
        request = requests.get(item.get("request_id")) if set(item) == _PROPOSAL_KEYS else None
        if (request is None or type(item["id"]) is not str or item["id"] in ids or item["page"] != request.get("page")
                or item["from"] not in operations or item["to"] not in operations or item["from"] == item["to"]
                or item["basis"] not in _LED_BASES or type(item["basis_note"]) is not str
                or (item["condition"] is not None and type(item["condition"]) is not str)
                or (item["contract"] is not None and type(item["contract"]) is not str)
                or (item["basis"] == "source_handoff" and not text(item["contract"]))
                or not strings(item["evidence_ids"]) or not item["evidence_ids"]
                or any(anchor(key) is None for key in item["evidence_ids"])):
            reject(f"{where} is malformed, foreign or cites evidence absent from the registry")
        ids.add(item["id"])
        ends = item["endpoints"]
        if (type(ends) is not dict or set(ends) != {"from_unit_ids", "to_unit_ids"}
                or not strings(ends["from_unit_ids"]) or not strings(ends["to_unit_ids"])
                or not all(owned(unit, item["from"]) for unit in ends["from_unit_ids"])
                or not all(owned(unit, item["to"]) for unit in ends["to_unit_ids"])):
            reject(f"{where} names endpoint units that neither realize nor serve its operations")
        if item["state"] == "inactive":
            if item["reason"] not in _SUPPORT_REASONS or item["support"] is not None:
                reject(f"{where} is inactive without a known reason or with a support")
            continue
        if item["state"] != "active" or item["reason"] is not None:
            reject(f"{where} has an unknown state")
        found = item["support"]
        if (type(found) is not dict or set(found) != _SUPPORT_RECORD_KEYS or found["kind"] not in _SUPPORT_KINDS
                or not strings(found["unit_ids"]) or not strings(found["witness_evidence_ids"])
                or not found["witness_evidence_ids"] or set(found["witness_evidence_ids"]) - set(item["evidence_ids"])
                or type(found["links"]) is not list or found["provenance"] not in ("host", "model_relation")):
            reject(f"{where} has a malformed support")
        if not owned_source(found, request):
            reject(f"{where}: a supported unit was not supplied on its page or claims source outside that page")
        chain, witnesses, links = found["unit_ids"], found["witness_evidence_ids"], found["links"]
        sending, receiving = set(ends["from_unit_ids"]), set(ends["to_unit_ids"])

        def witnessed(identifier, unit):  # the citation lies in the exact owned source of the unit it is claimed for
            return within(anchor(identifier), [tuple(row) for row in found["unit_spans"][unit]])
        if (found["kind"] == "contract_both_sides") != (item["basis"] == "source_handoff"):
            reject(f"{where}: a handoff needs the contract on both sides, and only a handoff uses it")
        if found["kind"] == "contract_both_sides":
            if (len(chain) != 2 or links or len(witnesses) != 2 or chain[0] not in sending or chain[1] not in receiving
                    or found["provenance"] != "host"
                    or any(item["contract"] not in ((evidence[key].get("safe_quote") or "")) for key in witnesses)
                    or not witnessed(witnesses[0], chain[0]) or not witnessed(witnesses[1], chain[1])):
                reject(f"{where}: the contract is not cited in the sending and in the receiving unit's own source")
        elif found["kind"] == "same_unit":
            if (len(chain) != 1 or links or len(witnesses) != 1 or chain[0] not in sending & receiving
                    or found["provenance"] != "host" or not witnessed(witnesses[0], chain[0])):
                reject(f"{where}: a same-unit support needs one unit of both actions and a citation in its source")
        else:
            if (not 2 <= len(chain) <= 3 or chain[0] not in sending or chain[-1] not in receiving
                    or len(links) != len(chain) - 1):
                reject(f"{where}: a call chain needs at most two links from one action's unit to the other's")
            model = False
            for position, link in enumerate(links):
                calls = link.get("call_lines") if type(link) is dict else None
                if (type(link) is not dict or set(link) != _SUPPORT_LINK_KEYS
                        or {link["from_unit"], link["to_unit"]} != {chain[position], chain[position + 1]}
                        or not strings(link["origins"]) or not link["origins"]
                        or link["origins"] != sorted(link["origins"]) or set(link["origins"]) - _LINK_ORIGINS
                        or not strings(link["relation_ids"]) or link["relation_ids"] != sorted(link["relation_ids"])
                        or bool(link["relation_ids"]) != bool(set(link["origins"]) - {"navigation"})
                        or type(calls) is not list
                        or any(type(row) is not list or len(row) != 3 or type(row[0]) is not str
                               or type(row[1]) is not int or type(row[2]) is not int or not 1 <= row[1] <= row[2]
                               for row in calls)
                        or link["witness_evidence_id"] not in witnesses
                        or not witnessed(link["witness_evidence_id"], link["from_unit"])):
                    reject(f"{where}: link {position} is malformed, does not join its chain units or is not "
                           "witnessed in the source of its calling unit")
                cited = anchor(link["witness_evidence_id"])
                if calls and not any(row[0] == cited[0] and row[1] <= cited[2] and cited[1] <= row[2] for row in calls):
                    reject(f"{where}: link {position} is witnessed by a citation that does not cover its call")
                model = model or not set(link["origins"]) & _HOST_LINK_ORIGINS
            if (found["provenance"] != ("model_relation" if model else "host")
                    or witnesses != list(dict.fromkeys(link["witness_evidence_id"] for link in links))):
                reject(f"{where}: the chain provenance or witnesses differ from its links")
    reviews = support.get("reviews", [])
    if "reviews" in support and (record.get("settings", {}).get("source_investigation") or {}).get(
            "policy") != "atomic-source-investigation/1":
        reject("transition source reviews require the declared source investigation policy")
    if type(reviews) is not list:
        reject("transition source reviews must be a list")
    proposals = {item["id"]: item for item in support["proposals"]}
    withdrawn = set()
    for review in reviews:
        if (type(review) is not dict or set(review) != {"transition_id", "reason", "evidence_ids", "request_id", "page"}
                or review["transition_id"] not in proposals or review["transition_id"] in withdrawn
                or not text(review["reason"]) or not strings(review["evidence_ids"]) or not review["evidence_ids"]
                or review["request_id"] not in requests
                or review["page"] != requests[review["request_id"]].get("page")
                or review["page"] <= proposals[review["transition_id"]]["page"]
                or any(not within(anchor(key), excerpts(requests[review["request_id"]]))
                       for key in review["evidence_ids"])):
            reject("transition source review is foreign, duplicated, not later or not cited on its own page")
        withdrawn.add(review["transition_id"])
    active = [item for item in support["proposals"] if item["state"] == "active" and item["id"] not in withdrawn]
    if record["transitions"] != active:
        reject("the accepted transitions are not exactly the active transition proposals")
    for index, link in enumerate(support["open_links"]):
        place = link.get("where")
        if (set(link) != _OPEN_LINK_KEYS or link["operation_id"] not in operations
                or link["unit_id"] not in operations[link["operation_id"]]["unit_ids"]
                or link["request_id"] not in requests or link["page"] != requests[link["request_id"]].get("page")
                or (units.get(link["unit_id"]) or {}).get("request_id") != link["request_id"]
                or not text(link["target"])
                or not ((link["kind"] == "unresolved_relation" and type(link["relation_id"]) is str
                         and link["target_unit_id"] is None)
                        or (link["kind"] == "dependency_not_supplied" and link["relation_id"] is None
                            and type(link["target_unit_id"]) is str))
                or type(place) is not dict or set(place) != {"path", "start_line", "end_line"}
                or type(place["path"]) is not str or type(place["start_line"]) is not int
                or type(place["end_line"]) is not int or not 1 <= place["start_line"] <= place["end_line"]):
            reject(f"open control link {index} is malformed or not bound to a unit realizing its operation")
    inactive = [item for item in support["proposals"] if item["state"] == "inactive"]
    expected = {"transition_proposals": len(support["proposals"]), "transitions_inactive": len(inactive),
                "transitions_inactive_by_reason": dict(sorted(Counter(item["reason"] for item in inactive).items())),
                "open_control_links": len(support["open_links"])}
    if any(record["summary"].get(key) != value for key, value in expected.items()):
        reject("summary disagrees with the transition proposals")
    required = {("unresolved_control_link", key): list(dict.fromkeys(row["unit_id"] for row in rows))
                for key, rows in still_open(support["open_links"], operations, units, active).items()}
    for (source, target), _ in unsupported_pairs([{**item, "state": "inactive"} if item["id"] in withdrawn
                                                  else item for item in support["proposals"]]):
        required[("unsupported_transition", (source, target))] = list(dict.fromkeys(
            [*operations[source]["unit_ids"], *operations[target]["unit_ids"]]))
    return required


def _unit_lines(unit):
    """Lines of a saved unit as the atomic host reads them: its declaration, else the span of its members."""
    declaration = unit.get("declaration")
    if declaration is not None:
        return declaration["start_line"], declaration["end_line"]
    return min(item["start_line"] for item in unit["members"]), max(item["end_line"] for item in unit["members"])


def _index_calls(index):
    """(caller declaration ID, callee declaration ID) -> [(path, first, last)] of resolved declaration-index calls."""
    calls = {}
    for call in (index or {}).get("calls", []):
        if (type(call) is dict and call.get("status") == "resolved" and type(call.get("start_line")) is int
                and type(call.get("end_line")) is int):
            calls.setdefault((call.get("caller_id"), call.get("callee_id")), []).append(
                (call.get("path"), call["start_line"], call["end_line"]))
    return calls


def _support_sources(support, known, registry, calls=None):
    """Transition support against the saved units, declaration index and relation registry; identity only.

    known: unit ID -> saved unit (command_units.json, then declaration_coverage.json primary units, in that order);
    registry: relation ID -> saved row, or None without a registry; calls: _index_calls of the saved declaration index,
    or None for a narrow helper check, where navigation call lines beyond the relation rows stay unauthenticated.
    Every support and endpoint unit is a saved unit, and each owned range (unit_spans) lies on that unit's path inside
    its own lines. Every chain link is recomputed from saved data exactly as the host built it: navigation between its
    units, the relation rows whose saved source and target lie in exactly those units (host_static static calls, and
    resolved code-model relations of a control kind kept as model provenance) and the call lines of both. A relation
    ID, origin or call line claimed for other units therefore fails. An open relation is an unresolved dispatch,
    callback or control relation written in its unit; an open dependency is a control-linked saved unit. No source is
    read; the semantic meaning of a link is not checked.
    """
    try:
        rows = registry or {}
        hosts = {key for key, unit in known.items() if unit.get("origin") == _DECLARATION_ORIGIN}
        by_path = {}
        for key, unit in known.items():
            by_path.setdefault(unit["path"], []).append(key)

        def unit_at(place):
            """The smallest saved unit whose lines contain place (direct_atomic _Atomic.unit_at), or None."""
            path, first, last = place.get("path"), place.get("start_line"), place.get("end_line")
            if type(path) is not str or type(first) is not int or type(last) is not int:
                return None
            best = None
            for key in by_path.get(path, ()):
                start, end = _unit_lines(known[key])
                if start <= first and last <= end and (best is None or end - start < best[0]):
                    best = (end - start, key)
            return None if best is None else best[1]
        pairs = {}  # (source unit, target unit) -> [(origin, relation ID, source lines)] of control relation rows
        for row in rows.values():
            source = unit_at(row["source"])
            target = unit_at(row["target"]) if row["resolution"] == "resolved" else None
            if source is None or target is None or source == target:
                continue
            if row["origin"] == "host_static" or (row["origin"] == "worker_proposal"
                                                  and row["kind"] in _CONTROL_RELATIONS):
                pairs.setdefault((source, target), []).append(
                    (row["origin"], row["id"], (row["source"]["path"], row["source"]["start_line"],
                                                row["source"]["end_line"])))

        def expected(first, second):
            """(origins, relation IDs, call lines) of the control link first -> second, from saved data only."""
            origins, ids, lines = set(), set(), set()
            source, target = known[first], known[second]
            if (second in source["navigation"]["callee_unit_ids"]
                    or (second in hosts and source.get("origin") != _DECLARATION_ORIGIN
                        and first in target["navigation"]["caller_unit_ids"])):
                origins.add("navigation")
                caller, callee = source.get("declaration_id"), target.get("declaration_id")
                if calls is not None and caller and callee:
                    lines.update(calls.get((caller, callee), []))
            for origin, identifier, line in pairs.get((first, second), []):
                origins.add(origin)
                ids.add(identifier)
                lines.add(line)
            return sorted(origins), sorted(ids), sorted(lines)
        for item in support["proposals"]:
            found = item["support"] or {"unit_ids": [], "links": [], "unit_spans": {}}
            units = [*item["endpoints"]["from_unit_ids"], *item["endpoints"]["to_unit_ids"], *found["unit_ids"]]
            if any(unit not in known for unit in units):
                raise ExportError("Transition support names a unit absent from the saved units")
            for link in found["links"]:
                origins, ids, lines = expected(link["from_unit"], link["to_unit"])
                recorded = sorted(tuple(row) for row in link["call_lines"])
                if (link["origins"] != origins or link["relation_ids"] != ids
                        or (calls is not None and recorded != lines)
                        or (calls is None and (not set(lines) <= set(recorded)
                                               or (set(recorded) - set(lines) and "navigation" not in origins)))):
                    raise ExportError("A transition support link is not the saved navigation, relation rows and call "
                                      "lines between exactly its units")
            spans = found.get("unit_spans")
            if type(spans) is not dict or set(spans) != set(found["unit_ids"]):
                raise ExportError("Transition support lacks the owned source of its units")
            for unit, ranges in spans.items():
                start, end = _unit_lines(known[unit])
                if any(row[0] != known[unit]["path"] or not start <= row[1] <= row[2] <= end for row in ranges):
                    raise ExportError("Transition support claims source outside the lines of its unit")
        for link in support["open_links"]:
            place = link["where"]
            if link["kind"] == "unresolved_relation":
                row = rows.get(link["relation_id"])
                if (row is None or row["origin"] != "worker_proposal" or row["resolution"] != "unresolved"
                        or row["kind"] not in _OPEN_RELATIONS or unit_at(row["source"]) != link["unit_id"]
                        or (place["path"], place["start_line"], place["end_line"])
                        != (row["source"]["path"], row["source"]["start_line"], row["source"]["end_line"])):
                    raise ExportError("An open control link is not an unresolved control relation of its unit")
            else:
                target = link["target_unit_id"]
                if (target not in known or link["unit_id"] not in known or not expected(link["unit_id"], target)[0]
                        or (place["path"], place["start_line"], place["end_line"])
                        != (known[target]["path"], *_unit_lines(known[target]))):
                    raise ExportError("An open control link names no control-linked saved unit at its address")
    except ExportError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, IndexError):
        raise ExportError("Transition support is malformed against the saved units and registry") from None


def _code_led(record, process_map, bundle):
    """Derived stages, stage order, facts, gaps, obligations and hypotheses of an atomic-map/2 record.

    Every nested object has its exact keys and types. Stages must partition the record's operations exactly as the
    map assigns them (working-group stages hold exactly their group's operations); stage transitions are exactly the
    crossings of accepted operation transitions; the frontier (entries, terminals, connected fragments) and the
    exit-derived gaps are recomputed from the record's operations and transitions; each fact row's stage, mapping
    indexes and obligation link are recomputed from the record's mappings and checked against log coverage and the
    mappings' fact links; obligation rows and their facts follow the composition statuses. A pending-local-work gap
    is checked for shape and for naming undecided units only: the call graph behind it is not in the record. Complete
    is impossible with provisional stages or open gaps. These are identity and consistency checks, never semantic
    verification.

    transition_support (direct-atomic-validator/10) is present exactly when the settings declare its policy: the
    accepted transitions must then be exactly its active proposals, and its host gaps are recomputed from its
    proposals and open control links (_transition_support). A record without it is an earlier record and keeps the
    earlier gap kinds; it is never read as carrying the new policy.
    """
    from .code_review import stable_id
    from .transition_support import gap_id

    def reject(message):
        raise ExportError("Code-led map: " + message)

    def text(value, nullable=False):
        return (nullable and value is None) or (type(value) is str and bool(value.strip()))

    def strings(value, known):
        return _action_strings(value) and len(value) == len(set(value)) and set(value) <= known

    led = record["code_led"]
    if (type(led) is not dict or set(led) not in (_LED_KEYS, _LED_KEYS | {"transition_support"})
            or type(led["basis"]) is not str):
        reject("record is malformed")
    declared = ((record["settings"].get("code_led") or {}).get("transitions") or {}).get("support")
    if (type(declared) is dict and declared.get("policy") == _SUPPORT_POLICY) != ("transition_support" in led):
        reject("transition support and the settings that declare its policy must appear together")
    units = record["unit_states"]
    groups = led["working_groups"]
    if (type(groups) is not list
            or any(type(group) is not dict or set(group) != _LED_GROUP_KEYS or type(group["id"]) is not str
                   or not text(group["name"]) or not text(group["contract"]) for group in groups)
            or len({group["id"] for group in groups}) != len(groups)):
        reject("working groups are malformed or repeat an ID")
    group_ids = {group["id"] for group in groups}
    order = [item.get("id") for item in record["operations"]]
    if len(set(order)) != len(order):
        reject("operation IDs repeat")
    operations = {item["id"]: item for item in record["operations"]}
    for key, op in operations.items():
        if (op.get("exit") not in _LED_EXITS or (op.get("group_id") is not None and op["group_id"] not in group_ids)
                or not strings(op.get("unit_ids"), set(units)) or not op["unit_ids"]
                or (op["exit"] in ("external_handoff", "unknown") and not text(op.get("exit_question")))):
            reject(f"operation {key} has a malformed exit, working group or realizing units")
    edges = record["transitions"]
    if len({edge.get("id") for edge in edges}) != len(edges):
        reject("operation transition IDs repeat")
    for edge in edges:
        if (type(edge.get("id")) is not str or edge.get("from") not in operations or edge.get("to") not in operations
                or edge["from"] == edge["to"] or edge.get("basis") not in _LED_BASES
                or (edge["basis"] == "source_handoff" and not text(edge.get("contract")))):
            reject("an operation transition is malformed or names an unknown operation")
    # Every proposal, its host state and the gaps it requires; legacy records carry none of it.
    support_gaps = (_transition_support(record, operations, bundle, reject) if "transition_support" in led
                    else None)
    if support_gaps is None and any(key in record["summary"] for key in _SUPPORT_SUMMARY):
        reject("a record without transition support counts transition proposals")
    # Stages: exact rows, the map's own stages, an exact partition of the operations.
    stage_ids = {stage.id: stage for stage in process_map.stages}
    stages = led["stages"]
    if (type(stages) is not dict or set(stages) != {"source", "items", "stage_transitions"}
            or stages["source"] not in _STAGE_SOURCES or type(stages["items"]) is not list):
        reject("stage derivation is malformed")
    origins, placed, seen = _STAGE_ORIGINS[stages["source"]], {}, set()
    for index, item in enumerate(stages["items"]):
        if (type(item) is not dict or set(item) != _LED_STAGE_KEYS or item["id"] in seen or item["id"] not in stage_ids
                or item["origin"] not in origins or not text(item["name"]) or not text(item["contract"])
                or stage_ids[item["id"]].name != item["name"]
                or (item["group_id"] is not None) != (item["origin"] == "working_group")
                or (item["group_id"] is not None and item["group_id"] not in group_ids)
                or not strings(item["operation_ids"], set(operations)) or not item["operation_ids"]):
            reject(f"derived stage {index} is malformed, absent from the map or empty")
        seen.add(item["id"])
        for key in item["operation_ids"]:
            op = operations[key]
            if (key in placed or op.get("stage_id") != item["id"]
                    or (item["origin"] != "compose" and op.get("group_id") != item["group_id"])):
                reject("derived stages do not partition the accepted operations by stage and working group")
            placed[key] = item["id"]
    if set(placed) != set(operations) or seen != set(stage_ids) - (
            {"unestablished"} if not stages["items"] else set()):
        reject("derived stages differ from the map's stages or operations")
    if operations and (stages["source"] == "compose") != (record["compose"].get("status") == "complete"):
        reject("the stage source disagrees with the composition status")
    if operations and stages["source"] != "compose" and (
            record["status"] == "complete" or "final_grouping_incomplete" not in record["status_reasons"]):
        reject("a map with provisional stages must stay partial with final_grouping_incomplete")
    # Stage order only from accepted operation transitions that cross two stages.
    derived = {}
    for edge in edges:
        if placed[edge["from"]] != placed[edge["to"]]:
            derived.setdefault((placed[edge["from"]], placed[edge["to"]]), []).append(edge["id"])
    rows = stages["stage_transitions"]
    if (type(rows) is not list or len(rows) != len(derived)
            or any(type(row) is not dict or set(row) != {"from_stage", "to_stage", "transition_ids"} for row in rows)
            or {(row["from_stage"], row["to_stage"]): row["transition_ids"] for row in rows} != derived
            or {(item.from_stage, item.to_stage) for item in process_map.transitions} != set(derived)):
        reject("stage transitions are not exactly the crossings of accepted operation transitions")
    for group in record["groups"]:
        if (group.get("stage_id") not in seen or not strings(group.get("operation_ids"), set(operations))
                or any(placed[key] != group["stage_id"] for key in group["operation_ids"])):
            reject("a navigation group leaves its stage or names an unknown operation")
    # Frontier: recomputed from the operations (in record order) and the accepted transitions only.
    incoming, outgoing, neighbours = Counter(), Counter(), {key: set() for key in order}
    for edge in edges:
        outgoing[edge["from"]] += 1
        incoming[edge["to"]] += 1
        neighbours[edge["from"]].add(edge["to"])
        neighbours[edge["to"]].add(edge["from"])
    fragments, visited = [], set()
    for key in order:
        if key in visited:
            continue
        members, stack = set(), [key]
        while stack:
            current = stack.pop()
            if current not in members:
                members.add(current)
                stack.extend(neighbours[current] - members)
        visited |= members
        ordered = [item for item in order if item in members]
        fragments.append({"id": stable_id("afr", ordered), "operation_ids": ordered,
                          "entry_ids": [item for item in ordered if not incoming[item]]})
    frontier = led["frontier"]
    if (type(frontier) is not dict or set(frontier) != {"entries", "terminals", "fragments"}
            or type(frontier["fragments"]) is not list
            or any(type(item) is not dict or set(item) != _LED_FRAGMENT_KEYS for item in frontier["fragments"])):
        reject("frontier is malformed")
    if frontier != {"entries": [key for key in order if not incoming[key]],
                    "terminals": [key for key in order if operations[key]["exit"] == "terminal"],
                    "fragments": fragments}:
        reject("frontier differs from the entries, terminals and fragments of the record's operations and transitions")
    # Gaps: exactly the open exits of the operations, plus pending local work that names undecided units.
    gaps = led["gaps"]
    if type(gaps) is not list:
        reject("gaps are malformed")
    gap_ids, pending, actual, supported = set(), set(), {}, {}
    kinds = _GAP_KINDS | (_SUPPORT_GAPS if support_gaps is not None else frozenset())
    for index, gap in enumerate(gaps):
        pair = type(gap) is dict and gap.get("kind") == "unsupported_transition"
        if (type(gap) is not dict or set(gap) != _LED_GAP_KEYS or gap["kind"] not in kinds
                or gap["source"] != ("host" if gap["kind"] in _HOST_GAPS else "model") or not text(gap["question"])
                or not strings(gap["operation_ids"], set(operations)) or len(gap["operation_ids"]) != (2 if pair else 1)
                or not strings(gap["unit_ids"], set(units)) or not gap["unit_ids"]):
            reject(f"gap {index} is malformed or names an unknown operation or unit")
        if gap["id"] != gap_id(gap["kind"], gap["operation_ids"], gap["unit_ids"]):
            reject(f"gap {index} does not carry the ID of its kind, operation and units")
        if gap["id"] in gap_ids:
            reject(f"gap {index} repeats gap ID {gap['id']}")
        gap_ids.add(gap["id"])
        if gap["kind"] in _SUPPORT_GAPS:
            item = (gap["kind"], tuple(gap["operation_ids"]) if pair else gap["operation_ids"][0])
            if item in supported:
                reject(f"gap {index} repeats a support gap")
            supported[item] = gap
            continue
        [key] = gap["operation_ids"]
        if gap["kind"] == "pending_local_work":
            if key in pending or any(units[unit].get("state") not in ("deferred", "request_failed")
                                     for unit in gap["unit_ids"]):
                reject(f"gap {index}: pending local work repeats or names a decided unit")
            pending.add(key)
        elif (gap["kind"], key) in actual:
            reject(f"gap {index} repeats an open exit")
        else:
            actual[(gap["kind"], key)] = gap
    required = {}
    for key in order:
        op = operations[key]
        if op["exit"] == "continues" and not outgoing[key] and key not in pending:
            required[("open_continuation", key)] = None
        elif op["exit"] == "external_handoff":
            required[("unresolved_handoff", key)] = op["exit_question"]
        elif op["exit"] == "unknown":
            required[("unknown_continuation", key)] = op["exit_question"]
    if set(actual) != set(required) or any(
            actual[item]["unit_ids"] != operations[item[1]]["unit_ids"]
            or (question is not None and actual[item]["question"] != question) for item, question in required.items()):
        reject("gaps differ from the open exits of the accepted operations")
    if support_gaps is not None and (set(supported) != set(support_gaps) or any(
            supported[item]["unit_ids"] != unit_ids for item, unit_ids in support_gaps.items())):
        reject("gaps differ from the inactive transition proposals and open control links")
    if gaps and (record["status"] == "complete" or "open_gaps" not in record["status_reasons"]):
        reject("a map with open gaps must stay partial with open_gaps")
    # Obligations: the supplied list as the settings declare it, one status each.
    obligations = led["obligations"]
    declared = (record["settings"].get("code_led") or {}).get("obligations")
    items = []
    if obligations is None:
        if declared is not None:
            reject("obligations declared in the settings are missing")
    else:
        if (type(obligations) is not dict or set(obligations) != _LED_OBLIGATIONS_KEYS
                or obligations["schema"] != "approved-obligations/1" or type(obligations["items"]) is not list
                or not obligations["items"]
                or declared != {"schema": obligations["schema"], "items": len(obligations["items"]),
                                "source_sha256": obligations["source_sha256"]}):
            reject("obligations are malformed or differ from the record settings")
        items = obligations["items"]
        for index, item in enumerate(items):
            status = item.get("status") if type(item) is dict else None
            if (type(item) is not dict or set(item) != _LED_OBLIGATION_KEYS or item["id"] != f"obl-{index + 1:03d}"
                    or not text(item["text"]) or status not in _OBLIGATION_STATES
                    or not strings(item["operation_ids"], set(operations)) or not text(item["question"], True)
                    or (status == "not_checked") != (stages["source"] != "compose")
                    or (status in ("matched", "partial")) != bool(item["operation_ids"])
                    or (status in ("partial", "not_found") and item["question"] is None)
                    or (status == "not_checked" and item["question"] is not None)):
                reject(f"obligation row {index} is malformed or disagrees with its status")
    obligation_ids = {item["id"] for item in items}
    # Facts: rows recomputed against the mappings, the stages, the obligations and log coverage.
    coverage = bundle.get("log_coverage")
    saved = coverage.get("facts") if type(coverage) is dict else None
    if type(saved) is not list or any(type(item) is not dict for item in saved):
        reject("log coverage facts are missing")
    saved = {item.get("id"): item for item in saved}
    mappings, facts = record["event_mappings"], led["facts"]
    if type(facts) is not list:
        reject("facts are malformed")
    fact_ids, links = [], {index: [] for index in range(len(mappings))}
    for index, row in enumerate(facts):
        if (type(row) is not dict or set(row) != _LED_FACT_KEYS or type(row["id"]) is not str or row["id"] in fact_ids
                or row["origin"] not in ("compose", "host_boundary", "obligation")
                or not strings(row["operation_ids"], set(operations))
                or not strings(row["lifecycle_roles"], _LED_ROLES)
                or (row["obligation_id"] is not None and row["obligation_id"] not in obligation_ids)
                or (row["origin"] == "compose" and stages["source"] != "compose")
                or (row["origin"] == "host_boundary" and stages["source"] == "compose")
                or (row["origin"] == "obligation" and (row["operation_ids"] or row["lifecycle_roles"]
                                                       or row["obligation_id"] is None))
                or (row["origin"] == "host_boundary" and (len(row["operation_ids"]) != 1 or row["obligation_id"]
                                                          or row["lifecycle_roles"] != ["result"]))
                or (not row["operation_ids"] and row["obligation_id"] is None)):
            reject(f"fact row {index} is malformed or names no accepted operation and no supplied obligation")
        fact_ids.append(row["id"])
        stage = {placed[key] for key in row["operation_ids"]}
        if len(stage) > 1 or row["stage_id"] != (next(iter(stage)) if stage else None):
            reject(f"fact row {index} names a stage other than the one of its operations")
        indexes = [position for position, item in enumerate(mappings) if item.get("operation_id") in row["operation_ids"]
                   and (not row["lifecycle_roles"] or item.get("lifecycle_role") in row["lifecycle_roles"])]
        if row["mapping_indexes"] != indexes:
            reject(f"fact row {index} mapping indexes are not exactly its operations' mappings of its roles")
        for position in indexes:
            links[position].append(row["id"])
        fact = saved.get(row["id"])
        requirement = fact.get("requirement") if type(fact) is dict else None
        if (type(requirement) is not dict or fact.get("stage_id") != row["stage_id"]
                or fact.get("lifecycle_roles") != row["lifecycle_roles"]
                or fact.get("event_keys") != list(dict.fromkeys(mappings[position]["event_key"] for position in indexes))
                or (requirement.get("state"), requirement.get("decision_id")) != (
                    ("accepted", row["obligation_id"]) if row["obligation_id"] is not None else ("proposed", None))):
            reject(f"fact row {index} disagrees with its log coverage fact")
    if set(saved) != set(fact_ids):
        reject("log coverage facts are not exactly the derived facts")
    if any(item.get("fact_ids") != links[position] for position, item in enumerate(mappings)):
        reject("an event mapping's fact links differ from the derived facts")
    covered = {row["obligation_id"] for row in facts if row["origin"] == "compose" and row["obligation_id"]}
    for item in items:
        own = [row["id"] for row in facts if row["obligation_id"] == item["id"]]
        alone = [row for row in facts if row["origin"] == "obligation" and row["obligation_id"] == item["id"]]
        if item["fact_ids"] != own or len(alone) != (item["id"] not in covered and item["status"] != "matched"):
            reject(f"obligation {item['id']} and its facts disagree")
    hypotheses = led["hypotheses"]
    if type(hypotheses) is not list or (hypotheses and stages["source"] != "compose"):
        reject("hypotheses are malformed or claimed without a composition")
    if len({item.get("id") for item in hypotheses if type(item) is dict}) != len(hypotheses):
        reject("hypothesis IDs repeat")
    for index, item in enumerate(hypotheses):
        if (type(item) is not dict or set(item) != _LED_HYPOTHESIS_KEYS or type(item["id"]) is not str
                or not text(item["statement"]) or item["status"] not in _HYPOTHESIS_STATES
                or not strings(item["operation_ids"], set(operations)) or not text(item["note"], True)
                or item["source"] != "business_hypothesis"):
            reject(f"hypothesis row {index} is malformed")
    binding = led["technical_relations"]
    if binding is not None:
        review = bundle.get("code_review") if type(bundle.get("code_review")) is dict else {}
        if (type(binding) is not dict or set(binding) != _RELATION_BINDING_KEYS
                or binding["file"] != "technical_relations.json" or binding["status"] not in
                {"collected", "partial", "not_collected"}
                or type(binding["registry_sha256"]) is not str or not _HASH.fullmatch(binding["registry_sha256"])
                or binding["review_run_fingerprint"] != review.get("run_fingerprint")
                or bundle.get("technical_relations") != binding):
            reject("technical relation binding is malformed or foreign")


def _atomic_reasons(atomic):
    if atomic is None or atomic["status"] not in {"partial", "failed", "not_run"}:
        return []
    summary = atomic["summary"]
    detail = [f"{reason}: {count}" for key in ("deferred_by_reason", "failed_by_reason")
              for reason, count in sorted((summary.get(key) or {}).items())]
    if not detail:
        detail = list(atomic["status_reasons"])
    return [f"Atomic map {atomic['status']}: " + (", ".join(detail) or "see atomic_map")]


def _action_reasons(actions):
    if actions is None or actions["status"] not in {"partial", "failed"}:
        return []
    summary = actions["summary"]
    detail = [f"{reason}: {count}" for key in ("deferred_by_reason", "failed_by_reason")
              for reason, count in sorted((summary.get(key) or {}).items())]
    detail += list(actions["status_reasons"])
    return [f"Action refinement {actions['status']}: " + (", ".join(detail) or "see action_refinement")]


def _check_links(artifacts, registry):
    process_map, coverage = artifacts["process_map"], artifacts["log_coverage"]
    plan, catalog = artifacts["instrumentation_plan"], artifacts["metric_catalog"]
    stages = {stage.id for stage in process_map.stages}
    keys = {}
    for mapping in process_map.event_mappings:
        keys.setdefault(mapping.stage_id, set()).add(mapping.event_key)
    facts = {fact.id: fact for fact in coverage.facts}
    for fact in facts.values():
        if fact.stage_id is not None and fact.stage_id not in stages:
            raise ExportError("A coverage fact references a stage absent from the process map")
        if set(fact.event_keys) - keys.get(fact.stage_id, set()):
            raise ExportError("A coverage fact references an event key not mapped to its stage")
    cards = {card.id for card in plan.cards}
    for card in plan.cards:
        if card.fact_id not in facts or set(card.dependencies) - cards - set(facts) or card.id in card.dependencies:
            raise ExportError("A logging change references an unknown fact or dependency")
        if card.source_anchor is not None and not any(
                registry[ref].source_anchor == card.source_anchor for ref in card.evidence_ids):
            raise ExportError("A logging change location is not backed by its evidence reference")
    metrics = {metric.id for metric in catalog.metrics}
    for metric in catalog.metrics:
        if set(metric.dependencies) - set(facts):
            raise ExportError("A metric depends on an unknown fact")
        if metric.value is not None:
            raise ExportError("A metric carries a numeric value; this source-only workflow computes none")
    referenced = {metric for fact in facts.values() for metric in fact.metric_ids}
    referenced |= {metric for card in plan.cards for metric in card.metric_ids}
    if referenced - metrics:
        raise ExportError("A fact or logging change references a metric absent from the catalog")


# Packet content


def _anchor_text(anchor):
    return None if anchor is None else f"{anchor.path}:{anchor.start_line}-{anchor.end_line} {anchor.symbol}"


def _refs(ids, registry):
    return [_anchor_text(registry[ref].source_anchor) or registry[ref].locator for ref in ids]


def _static_emit(mapping, registry, sites):
    """The coverage stage's own static check: a literal emit of this key inside a cited range."""
    from .workflow import _emits
    if sites is None:
        return "not_assessed"
    anchors = [registry[ref].source_anchor for ref in mapping.evidence_ids if registry[ref].source_anchor]
    return "found" if any(_emits(anchor, mapping.event_key, sites) for anchor in anchors) else "not_established"


def _sites(bundle):
    """Well-formed static logging sites of the saved inventory; None when the run has no inventory."""
    inventory = bundle.get("logging_inventory")
    sites = inventory.get("sites") if type(inventory) is dict else None
    if type(sites) is not list:
        return None
    return [site for site in sites if type(site) is dict and type(site.get("path")) is str
            and all(type(site.get(key, site.get("line"))) is int for key in ("start_line", "end_line"))]


def _existing_fields(mapping, registry, sites):
    """Exact field names from matching static sites, without reading the target repository."""
    from .workflow import _emits
    anchors = [registry[ref].source_anchor for ref in mapping.evidence_ids if registry[ref].source_anchor]
    return list(dict.fromkeys(name for site in (sites or [])
                             if any(_emits(anchor, mapping.event_key, [site]) for anchor in anchors)
                             for name in site.get("fields", []) if type(name) is str))


def _event_rows(view):
    artifacts, registry = view["artifacts"], {item.id: item for item in view["evidence"]}
    process_map, plan = artifacts["process_map"], artifacts["instrumentation_plan"]
    facts = {fact.id: fact for fact in artifacts["log_coverage"].facts}
    names = {stage.id: stage.name for stage in process_map.stages}
    sites = _sites(view["bundle"])
    empty = dict.fromkeys(EVENT_COLUMNS)
    # Atomic runs: the facts a binding observes are the exact fact IDs of that accepted site binding, never every
    # fact whose key matches (one key at two emit sites of two operations stays apart). Checked by load_run.
    exact = {}
    for item in (view.get("atomic") or {}).get("event_mappings", []):
        exact.setdefault((item.get("operation_id"), item.get("event_key"), tuple(item.get("evidence_ids") or [])),
                         []).append(item)
    rows = []
    for mapping in process_map.event_mappings:
        queue = exact.get((mapping.operation_id, mapping.event_key, tuple(mapping.evidence_ids)))
        binding = queue.pop(0) if queue else None
        if binding is not None:
            fact_ids, fact_link = list(binding.get("fact_ids") or []), "exact_site_binding"
        else:
            fact_ids = [fact.id for fact in facts.values()
                        if fact.stage_id == mapping.stage_id and mapping.event_key in fact.event_keys]
            fact_link = "stage_and_event_key_candidate"
        rows.append({**empty, "row_kind": "mapped_key", "event_key": mapping.event_key,
                     "event_key_status": "from_process_map", "stage_id": mapping.stage_id,
                     "stage_name": names.get(mapping.stage_id),
                     # A concrete operation identity wins; legacy free-text mappings keep the key-based lookup.
                     "operation_ids": [mapping.operation_id] if mapping.operation_id is not None else
                                      [op.id for op in process_map.operations
                                       if op.stage_id == mapping.stage_id and mapping.event_key in op.event_keys],
                     "business_action": mapping.business_action, "mapping_operation": mapping.operation,
                     "lifecycle_role": mapping.lifecycle_role, "occurrence_rule": mapping.occurrence_rule,
                     "existing_fields": _existing_fields(mapping, registry, sites),
                     "state": mapping.state.value, "static_emit_in_cited_range": _static_emit(mapping, registry, sites),
                     "fact_ids": fact_ids, "fact_link": fact_link,
                     "source_refs": _refs(mapping.evidence_ids, registry), "evidence_ids": list(mapping.evidence_ids)})
    for card in plan.cards if _completed(view["manifest"])["instrumentation"] else []:
        if card.action is not InstrumentationAction.ADD_EVENT:
            continue  # map/join/enrich/field/move/delivery changes are not new events
        stage_id = facts[card.fact_id].stage_id
        rows.append({**empty, "row_kind": "proposed_new_event", "event_key_status": "not_specified_in_plan",
                     "stage_id": stage_id, "stage_name": names.get(stage_id), "semantics": card.semantics,
                     "proposed_fields": list(card.fields), "transaction_timing": card.transaction_timing,
                     "state": "proposed", "fact_ids": [card.fact_id], "fact_link": "plan_card_fact",
                     "plan_card_id": card.id,
                     "location": card.location, "source_refs": [ref for ref in [_anchor_text(card.source_anchor)] if ref],
                     "evidence_ids": list(card.evidence_ids)})
    return rows


def _change_rows(view):
    from .operation_view import card_links
    artifacts = view["artifacts"]
    facts = {fact.id: fact for fact in artifacts["log_coverage"].facts}
    # Exact owners only through the change's fact; overlap or key candidates stay a separate, labelled column.
    links = card_links(view["bundle"])
    return [{"card_id": card.id, "action": card.action.value, "action_label": r._STATES[card.action.value][0],
             "fact_id": card.fact_id, "fact_meaning": facts[card.fact_id].meaning,
             "stage_id": facts[card.fact_id].stage_id,
             "operation_ids": list(links.get(card.id, {}).get("operation_ids", [])),
             "candidate_operation_ids": [item["operation_id"] for item in links.get(card.id, {}).get("candidates", [])],
             "location": card.location,
             "source_ref": _anchor_text(card.source_anchor), "rationale": card.rationale,
             "semantics": card.semantics, "fields": [f"{name}: {meaning}" for name, meaning in card.fields.items()],
             "transaction_timing": card.transaction_timing, "dependencies": list(card.dependencies),
             "metric_ids": list(card.metric_ids), "validation_plan": card.validation_plan,
             "evidence_ids": list(card.evidence_ids)} for card in artifacts["instrumentation_plan"].cards]


def _metric_rows(view):
    artifacts = view["artifacts"]
    facts = {fact.id: fact for fact in artifacts["log_coverage"].facts}
    cards = artifacts["instrumentation_plan"].cards
    rows = []
    for metric in artifacts["metric_catalog"].metrics:
        needed = [facts[key] for key in metric.dependencies]
        rows.append({"metric_id": metric.id, "purpose": metric.purpose, "formula_text": metric.formula_text,
                     **{name: getattr(metric, name) for name in ("counting_unit", "cohort", "window", "time_semantics",
                                                                 "open_case_policy", "late_data_policy")},
                     "required_fact_ids": list(metric.dependencies),
                     "required_facts": [f"{fact.id}: {fact.meaning}" for fact in needed],
                     "required_event_keys": list(dict.fromkeys(key for fact in needed for key in fact.event_keys)),
                     "related_change_ids": [card.id for card in cards if metric.id in card.metric_ids]})
    return rows


def _scope_reasons(bundle):
    """Why the analysed source scope is partial, from the snapshot and review records; [] when it is not."""
    if not bundle["scope"]["partial_input"]:
        return []
    source, review = bundle["scope"].get("source"), bundle.get("code_review")
    limits = source.get("limitations") if type(source) is dict else None
    reasons = [item for item in (limits if type(limits) is list else []) if type(item) is str]
    incomplete = review.get("incomplete_reasons") if type(review) is dict else None
    if type(incomplete) is list and incomplete:
        reasons.append("Code review incomplete: " + ", ".join(map(str, incomplete)))
    return reasons or ["Partial source scope"]


def _completed(manifest):
    return {stage: getattr(manifest.stages, stage).status is StageStatus.COMPLETED for stage in STAGE_NAMES}


def _open_questions(view):
    artifacts = view["artifacts"]
    # A failed map holds only a placeholder and generic limitations; its blocker is in the stage table.
    questions = list(artifacts["process_map"].unknowns) if _completed(view["manifest"])["map"] else []
    for metric in artifacts["metric_catalog"].metrics:
        for blocker in metric.readiness.blockers:
            if blocker.startswith(_UNDECLARED):
                parts = [_POLICY_LABELS.get(part, part) for part in blocker[len(_UNDECLARED):].split(", ")]
                questions.append(f"Метрика «{metric.purpose}» ({metric.id}): уточнить " + ", ".join(parts))
    return questions


def _scope(bundle):
    scope = bundle["scope"]
    source = scope.get("source")
    source = {key: value for key, value in source.items() if key != "exclusions"} if type(source) is dict else None
    return {"partial_input": scope["partial_input"], "business_text_supplied": scope.get("business_text_supplied"),
            "business_input": scope.get("business_input"), "source": source, "telemetry": scope.get("telemetry"),
            "limitations": scope.get("limitations")}


def _technical_review(bundle):
    review, reduction = bundle.get("code_review"), bundle.get("code_reduction")
    if type(review) is not dict:
        return None
    residual = reduction.get("residual") if type(reduction) is dict else None
    return {"code_review_complete": review.get("complete"), "coverage_complete": review.get("coverage_complete"),
            "incomplete_reasons": review.get("incomplete_reasons"),
            "reduction_complete": reduction.get("complete") if type(reduction) is dict else None,
            "residual_count": residual.get("count") if type(residual) is dict else None,
            "full_records": "code_review.json and code_reduction.json in the source run"}


def _quote_free_atomic(record):
    if record is None:
        return None
    exported = copy.deepcopy(record)
    for request in exported.get("requests", []):
        observation = request.get("source_observation")
        if observation is None:
            continue
        result = observation["result"]
        result.pop("text", None)
        for match in result.get("matches", []):
            match.pop("text", None)
    return exported


def _document(view, status, reasons, questions, events):
    bundle, artifacts = view["bundle"], view["artifacts"]
    evidence = [item.to_dict() for item in view["evidence"]]
    for item in evidence:
        item["safe_quote"] = None  # source text stays in the run and the repository
    return {"schema_version": SCHEMA_VERSION, "packet": "onboarding_draft", "status": status,
            "partial_reasons": reasons,
            "source_run": {"run_id": view["manifest"].run_id,
                           "files": {name: hashlib.sha256(data).hexdigest() for name, data in view["raw"].items()}},
            "manifest": view["manifest"].to_dict(),
            "source_snapshot": {"snapshot_id": view["snapshot_id"], "files": view["snapshot_files"]},
            "scope": _scope(bundle),
            **{key: artifacts[key].to_dict() for _, key, _ in ARTIFACTS},
            "event_dictionary": events if _completed(view["manifest"])["map"] else [],
            "evidence": evidence,
            "guidance_links": bundle.get("guidance_links") if type(bundle.get("guidance_links")) is dict else None,
            "review_flags": bundle.get("review_flags") if type(bundle.get("review_flags")) is list else None,
            "technical_review": _technical_review(bundle),
            # Present for runs with an action catalog: every candidate's host state, decision and reason.
            "action_accounting": view.get("actions"),
            # Present for atomic-mode runs: every command unit's state, decision, request and reason.
            "atomic_accounting": _quote_free_atomic(view.get("atomic")),
            **({"instrumentation_accounting": bundle["instrumentation_accounting"]}
               if "instrumentation_accounting" in bundle else {}),
            # Which static emit sites count for which fact, and every same-site binding conflict.
            "coverage_lifecycle": view.get("lifecycle"),
            "open_questions": questions,
            "notes": {"source_quotes": "omitted: evidence keeps locators, anchors and raw_span_hash only",
                      "csv_cells": "decode with onboarding_export.decode_cell; see docs/ONBOARDING-EXPORT.md",
                      "metric_values": "not computed by this source-only workflow",
                      **({"atomic_observations": "read/search source text omitted; result_sha256/text_sha256 bind "
                          "the original full saved result, not this quote-free copy; this is not a resume source"}
                         if any("source_observation" in q for q in (view.get("atomic") or {}).get("requests", []))
                         else {})}}


# process.md


def _md(value, unknown="не указано"):
    text = r._m(value, unknown)
    if r._known(value) is None:
        return text
    # Bare URLs would become links in GitHub-flavoured Markdown; keep them as plain text.
    return _WWW.sub(lambda match: match.group(0)[:-1] + "\\.", text.replace("://", "\\://"))


def _mlist(values, empty="нет"):
    return ", ".join(_md(value) for value in values) if values else empty


def _materials(completed, counts):
    titles = {stage: title for stage, _, title in r._SECTIONS}

    def missing(name, stage):
        return f"- {name} — не сформирован: стадия «{titles[stage]}» не завершена"

    events = ("предложенных новых событий: " + str(counts["proposed"]) if completed["instrumentation"]
              else "предложенные новые события не установлены: стадия доработок не завершена")
    return ["", "## Материалы пакета", "",
            f"- [events.csv](events.csv) — сопоставленных ключей событий: {counts['mapped']}; {events}"
            if completed["map"] else missing("events.csv", "map"),
            f"- [logging-changes.csv](logging-changes.csv) — доработок логирования: {counts['changes']}"
            if completed["instrumentation"] else missing("logging-changes.csv", "instrumentation"),
            f"- [metrics.csv](metrics.csv) — определений метрик: {counts['metrics']}"
            if completed["metrics"] else missing("metrics.csv", "metrics"),
            "- [onboarding.json](onboarding.json) — полные структуры, идентификаторы, основания и технические состояния",
            "- [SHA256SUMS](SHA256SUMS) — контрольные суммы файлов пакета"]


def _markdown(view, status, questions, counts):
    manifest, bundle, artifacts = view["manifest"], view["bundle"], view["artifacts"]
    registry = {item.id: item for item in view["evidence"]}
    process_map = artifacts["process_map"]
    partial, completed = bundle["scope"]["partial_input"], _completed(manifest)
    origin, mode = manifest.data_origin.value, manifest.model_mode.value
    lines = ["# Черновик материалов процесса", "", f"**{_md(process_map.purpose, 'назначение процесса не установлено')}**", "",
             f"Запуск {_md(manifest.run_id)} · {r._STATES[origin][0]} · {r._STATES[mode][0]} · "
             f"пакет: {'все стадии выполнены' if status == 'all_stages_completed' else 'частичный'}", "",
             "Собрано из результатов одного запуска без новых обращений к модели. Этапы, операции, события и метрики — "
             "результаты анализа, а не утверждённые требования; численные показатели процесса не рассчитываются.",
             "", "## Состояние стадий", "", "| Стадия | Состояние | Причина |", "| --- | --- | --- |"]
    for (stage, _, title) in r._SECTIONS:
        state = getattr(manifest.stages, stage)
        label = r._MANIFEST.get(state.status.value) or r._STATES[state.status.value][0]
        lines.append(f"| {title} | {label} | {_mlist(list(state.blockers), '')} |")
    lines += ["", f"- Область исходников: {'неполная' if partial else 'полная'}"]
    lines += [f"  - {_md(reason)}" for reason in _scope_reasons(bundle)]
    lines += ["", "## Процесс", "", f"- Назначение: {_md(process_map.purpose)}",
              f"- Границы: {_md(process_map.boundaries)}", f"- Что считается случаем: {_md(process_map.case_definition)}",
              "", "## Этапы и операции", ""]
    if not completed["map"]:
        lines.append("Карта процесса не получена: " + _mlist(process_map.blockers))
    else:
        lines += _mstages(process_map, registry, partial)
    lines += _mscenarios(process_map, registry)
    lines += _mactions(view.get("actions"))
    lines += _matomic(view.get("atomic"))
    lines += _materials(completed, counts)
    lines += ["", "## Источники", "",
              f"- Снимок исходников: {_md(view['snapshot_id'])}, файлов: {len(view['snapshot_files'])}",
              f"- Бизнес-описание: {'передано' if bundle['scope'].get('business_text_supplied') else 'не передано'}"]
    preset = bundle["scope"].get("business_input")
    if type(preset) is dict:
        lines.append(f"  - {_md(preset.get('process_id'))} — {_md(preset.get('process_name'))}, "
                     f"SHA-256 {_md(preset.get('sha256'))}")
    lines += [f"- Ссылок на код: {len(registry)}; цитаты в пакет не копируются",
              f"- bundle.json исходного запуска: SHA-256 {hashlib.sha256(view['raw']['bundle.json']).hexdigest()}",
              "", "## Открытые вопросы", ""]
    lines += [f"- {_md(question)}" for question in questions] or ["Открытых вопросов в результатах нет."]
    return ("\n".join(lines) + "\n").encode("utf-8")


_ACTION_STATES_RU = {"disabled": "уточнение выключено", "skipped_no_candidates": "кандидатов действий нет",
                     "not_run": "не выполнялось: карта не получена", "complete": "все переданные кандидаты учтены",
                     "partial": "частично", "failed": "не выполнено"}


def _mactions(actions):
    if actions is None:
        return []
    summary = actions["summary"]
    lines = ["", "## Учёт кандидатов действий", "",
             f"- Состояние: {_ACTION_STATES_RU.get(actions['status'], actions['status'])}",
             f"- Кандидатов в каталоге: {summary.get('candidates', len(actions['candidates']))}; решений по этапам: "
             f"{summary.get('stage_decisions', 0)}; добавлено атомарных операций: {summary.get('operations_added', 0)}"]
    for key, title in (("dispositions", "Решения"), ("deferred_by_reason", "Отложено"),
                       ("failed_by_reason", "Без решения из-за ошибки запроса")):
        values = summary.get(key) or {}
        if values:
            lines.append(f"- {title}: " + ", ".join(f"{_md(name)} — {count}" for name, count in sorted(values.items())))
    lines.append("- Полный учёт по каждому кандидату — onboarding.json (action_accounting); полнота учёта не "
                 "означает проверенный смысл решений.")
    return lines + [""]


_ATOMIC_STATES_RU = {"complete": "все переданные единицы команд учтены", "partial": "частично",
                     "failed": "не выполнено", "not_run": "не выполнялось: каркас этапов не получен",
                     "skipped_no_candidates": "кандидатов действий нет"}


def _matomic(atomic):
    if atomic is None:
        return []
    summary = atomic["summary"]
    lines = ["", "## Учёт атомарных действий", "",
             f"- Состояние: {_ATOMIC_STATES_RU.get(atomic['status'], atomic['status'])}",
             f"- Единиц команд: {summary.get('units', len(atomic['unit_states']))}; решений: "
             f"{summary.get('decisions', 0)}; после сортировки: {summary.get('triaged', 0)}; атомарных операций: "
             f"{summary.get('operations_added', 0)}"]
    for key, title in (("dispositions", "Решения"), ("deferred_by_reason", "Отложено"),
                       ("failed_by_reason", "Без решения из-за ошибки запроса")):
        values = summary.get(key) or {}
        if values:
            lines.append(f"- {title}: " + ", ".join(f"{_md(name)} — {count}" for name, count in sorted(values.items())))
    unresolved = [item["name"] for item in atomic["required_scenarios"] if item["status"] == "unresolved"]
    if unresolved:
        lines.append("- Обязательные сценарии без собранной последовательности: " + _mlist(unresolved))
    binding = atomic.get("declaration_coverage")
    if binding is not None:  # absent for runs before declaration accounting
        states = summary.get("declaration_units_by_state") or {}
        lines.append(f"- Объявлений без находок технического разбора найдено: {binding['primary_units']}; состояния: "
                     + (", ".join(f"{name} — {count}" for name, count in sorted(states.items())) or "нет")
                     + f"; файлов исходного кода с неизвестным перечнем объявлений: {binding['open_scope']['blocking']}; "
                       "перечень — declaration_coverage.json исходного запуска.")
        if "context_units" in binding:  # absent for runs before context-anchored bodies became primary work
            states = summary.get("context_primary_units_by_state") or {}
            lines.append("- Объявлений, отмеченных техническим разбором только как контекст: "
                         f"{binding['context_units']}; состояния: "
                         + (", ".join(f"{name} — {count}" for name, count in sorted(states.items())) or "нет") + ".")
    led = atomic.get("code_led") if type(atomic.get("code_led")) is dict else None
    if led is not None:  # atomic-map/2: stages and facts derived from accepted operations
        source = {"compose": "выведены сборкой из принятых действий", "provisional_groups": "рабочие группы страниц "
                  "(сборка не завершена)", "none": "не выведены"}.get(led["stages"]["source"], led["stages"]["source"])
        kinds = Counter(gap["kind"] for gap in led["gaps"])
        lines.append(f"- Этапы: {source}; порядок этапов только из принятых переходов действий; разрывов: "
                     f"{len(led['gaps'])}" + (" (" + ", ".join(f"{name} — {count}" for name, count in sorted(kinds.items()))
                                              + ")" if kinds else "")
                     + f"; несвязанных фрагментов: {len(led['frontier']['fragments'])}.")
        if led["obligations"] is not None:
            states = Counter(item["status"] for item in led["obligations"]["items"])
            lines.append("- Утверждённые требования: " + ", ".join(f"{name} — {count}"
                                                                   for name, count in sorted(states.items())) + ".")
    lines.append("- Полный учёт по каждой единице — onboarding.json (atomic_accounting); полнота учёта не "
                 "означает проверенный смысл решений.")
    return lines + [""]


def _code(ids, registry):
    refs = _refs(ids, registry)
    return "код: " + _mlist(refs) if refs else "код: не указан"


def _mstages(process_map, registry, partial):
    lines = []
    for number, stage in enumerate(process_map.stages, 1):
        implemented = stage.implemented
        if implemented.evidence_ids:
            code = "есть ссылки на код (" + _mlist(_refs(implemented.evidence_ids, registry)) + ")"
        else:
            code = "ссылок на код нет — реализация не установлена" + (" в неполной области исходников" if partial else "")
        lines += [f"### {number}. {_md(stage.name, 'без названия')} ({_md(stage.id)})", "",
                  f"- Требуется: {r._STATES[stage.required.state.value][0]}; {code}",
                  f"- Роли: {_mlist(stage.roles, 'не указаны')}; системы: {_mlist(stage.systems, 'не указаны')}"]
        operations = [op for op in process_map.operations if op.stage_id == stage.id]
        if operations:
            lines.append("- Операции:")
            for op in operations:
                parts = [f"{_md(op.name)} ({_md(op.id)}, {_KINDS.get(op.kind, op.kind)})"]
                if op.description:
                    parts.append(_md(op.description))
                if op.actor:
                    parts.append("исполнитель: " + _md(op.actor))
                parts.append("события: " + _mlist(op.event_keys))
                parts.append(_code(op.evidence_ids, registry))
                lines.append("  - " + "; ".join(parts))
        lines.append("")
    return lines


def _mscenarios(process_map, registry):
    names = {op.id: op.name for op in process_map.operations}
    lines = []
    if process_map.scenarios:
        lines += ["## Сценарии", ""]
        for scenario in process_map.scenarios:
            lines.append(f"- {_md(scenario.name)} ({_md(scenario.id)})" +
                         (": " + _md(scenario.description) if scenario.description else ""))
            lines.append("  - Операции: " + _mlist([f"{names[key]} ({key})" for key in scenario.operation_ids]))
            lines.append("  - Переходы: " + _mlist(scenario.transition_ids))
        lines.append("")
    if process_map.operation_transitions:
        lines += ["## Переходы между операциями", "", "| ID | Из | В | Условие | Код |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {_md(edge.id)} | {_md(names[edge.from_operation])} | {_md(names[edge.to_operation])} | "
                  f"{_md(edge.condition, 'следующий шаг')} | {_mlist(_refs(edge.evidence_ids, registry), 'не указан')} |"
                  for edge in process_map.operation_transitions]
        lines.append("")
    if process_map.transitions:
        stages = {stage.id: stage.name or stage.id for stage in process_map.stages}
        lines += ["## Переходы между этапами", "", "| Из | В | Условие |", "| --- | --- | --- |"]
        lines += [f"| {_md(stages[item.from_stage])} | {_md(stages[item.to_stage])} | {_md(item.condition)} |"
                  for item in process_map.transitions]
    return lines


# Writing


def render(view):
    """Every packet file as bytes, in write order; SHA256SUMS last.

    A CSV whose source stage did not complete is omitted rather than written empty.
    """
    manifest = view["manifest"]
    completed = _completed(manifest)
    reasons = [reason for stage in STAGE_NAMES for reason in getattr(manifest.stages, stage).blockers]
    reasons += _scope_reasons(view["bundle"])
    reasons += _action_reasons(view.get("actions"))
    reasons += _atomic_reasons(view.get("atomic"))
    status = "partial" if reasons else "all_stages_completed"
    questions = _open_questions(view)
    events, changes, metrics = _event_rows(view), _change_rows(view), _metric_rows(view)
    counts = {"mapped": sum(row["row_kind"] == "mapped_key" for row in events),
              "proposed": sum(row["row_kind"] == "proposed_new_event" for row in events),
              "changes": len(changes), "metrics": len(metrics)}
    files = {"process.md": _markdown(view, status, questions, counts),
             "onboarding.json": (json.dumps(_document(view, status, reasons, questions, events), ensure_ascii=False,
                                            indent=2, allow_nan=False) + "\n").encode("utf-8")}
    if completed["map"]:
        files["events.csv"] = _csv(EVENT_COLUMNS, events)
    if completed["instrumentation"]:
        files["logging-changes.csv"] = _csv(CHANGE_COLUMNS, changes)
    if completed["metrics"]:
        files["metrics.csv"] = _csv(METRIC_COLUMNS, metrics)
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                                  for name, data in sorted(files.items())).encode("ascii")
    return status, files


def _create(path):
    """A new regular file; O_EXCL refuses any existing file or symlink at the path."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o666), "wb")


def _write(target, files):
    """Create the fresh directory and its files; on failure remove only what was created here."""
    try:
        os.mkdir(target)
    except FileExistsError:
        raise ExportError("Output directory already exists; choose a new --out path") from None
    created = []
    try:
        for name, data in files.items():
            path = target / name
            handle = _create(path)
            created.append(path)
            with handle:
                handle.write(data)
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            os.rmdir(target)
        except OSError:
            pass  # something else appeared inside; it is left untouched
        raise


def export_onboarding(run, out):
    """Validate the saved run, render the packet in memory, then write it to a new directory."""
    run = _run_directory(run)
    target = _destination(out, run)
    view = load_run(run)
    status, files = render(view)
    _write(target, files)
    return {"export": str(target), "status": status, "run_id": view["manifest"].run_id,
            "files": sorted(files), "sha256sums_sha256": hashlib.sha256(files["SHA256SUMS"]).hexdigest()}
