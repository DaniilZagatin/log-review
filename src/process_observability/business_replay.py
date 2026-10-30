"""Bounded, exact reuse of business replies already captured by a prior local run (--resume-from).

One narrow lookup, no model call and no network:

- prior_atomic_reply: for one atomic page request of a prior direct-atomic-validator/7 run (atomic-map/1 record) or
  direct-atomic-validator/9 run (atomic-map/2 record) with the same initial API request hash, model, snapshot,
  catalog, units and coverage bindings, either its final accepted reply or the final repair reply of a request that
  failed only because an inapplicable nullable accounting key was omitted.

The code-led map (direct-atomic-validator/9 and later) sends no skeleton request, so no earlier skeleton reply is read
or rebound. Its page prompt and brief differ from every page request before /9, so a page recorded by such a run has
other request bytes and is always a miss here; only an exactly equal request can hit. /10 changed only the host
transition support and the composition, not the page request, so an exactly recorded /9 page reply is revalidated under
the current policy (its transitions get their current support) and keeps prior_validator /9 in its provenance; a /9
checkpoint file itself is never read as a current checkpoint.

Replies are read from the prior trace.jsonl, paired request -> response in sequence and bound to the prior record by
request and response hashes. Ambiguous, unpaired, corrupt, oversized or symlinked inputs give None (a miss). The
caller must still run the complete current validation on the returned reply and treat any failure as a miss. Local
files are not authenticated: a hit shows only that the same bytes were recorded, never provider authenticity or
semantic truth. Nothing here imports, executes or renders analysed source.
"""

import copy
import hashlib
import json
from pathlib import Path
import re

from .action_refinement import _digest
from .contracts import strict_json_loads
from .review_reduction import _unlinked

PRIOR_VALIDATOR = "direct-atomic-validator/7"
# Record schema -> the one prior validator that wrote it and whose page replies may be revalidated now.
PRIOR_VALIDATORS = {"atomic-map/1": PRIOR_VALIDATOR, "atomic-map/2": "direct-atomic-validator/9"}
MAX_RECORD_BYTES = 256 * 1024 * 1024
MAX_TRACE_BYTES = 1024 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024 * 1024
NULLABLE_KEYS = frozenset({"exclusion", "question"})
_ITEM_PATH = re.compile(r"unit_accounting\[\d+\]")
_HASH = re.compile(r"[0-9a-f]{64}")
# Checkpoint identity fields that must be equal for a prior atomic reply; settings and validator legitimately differ.
_BOUND = ("task", "mode", "api_request_sha256", "shape_sha256", "business_model_sha256", "snapshot_id",
          "catalog_sha256", "units_sha256", "coverage_sha256")


def _file(directory, name, limit):
    """A regular file of the prior run, never through a symbolic link and at most limit bytes, else None."""
    try:
        path = _unlinked(Path(directory) / name)
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path
    except (OSError, ValueError):
        return None


def _record(directory):
    path = _file(directory, "atomic_map.json", MAX_RECORD_BYTES)
    try:
        record = None if path is None else strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if (type(record) is not dict or record.get("schema") not in PRIOR_VALIDATORS
            or type(record.get("settings")) is not dict or type(record.get("requests")) is not list
            or type(record.get("catalog")) is not dict):
        return None
    return record


def _pairs(directory, wanted):
    """(trace sha256, pairs) of business model calls whose request hash is in wanted, or None for an unusable trace.

    Every business model.response must answer the immediately preceding unanswered business model.request with the
    same stage, task, role and attempt; sequence numbers must increase.
    """
    path = _file(directory, "trace.jsonl", MAX_TRACE_BYTES)
    if path is None:
        return None
    digest, pairs, pending, last = hashlib.sha256(), [], None, 0
    try:
        with path.open("rb") as handle:
            for raw in handle:
                digest.update(raw)
                if len(raw) > MAX_EVENT_BYTES:
                    return None
                row = strict_json_loads(raw.decode("utf-8"))
                if type(row) is not dict or type(row.get("seq")) is not int or row["seq"] <= last:
                    return None
                last = row["seq"]
                data = row.get("data")
                if type(data) is not dict or data.get("role") != "business":
                    continue
                event = row.get("event")
                if event == "model.request":
                    if pending is not None:
                        return None  # matching task/attempt keys cannot disambiguate overlapping requests
                    system, user, stated = data.get('system'), data.get('user'), data.get('api_request_sha256')
                    if type(system) is not str or type(stated) is not str or not _HASH.fullmatch(stated):
                        return None
                    if type(user) is dict:
                        user = json.dumps(user, ensure_ascii=False, sort_keys=True)
                    elif type(user) is not str:
                        return None
                    if hashlib.sha256((system + '\n' + user).encode('utf-8')).hexdigest() != stated:
                        return None  # a redacted/unreproducible or changed request is a miss, never a guess
                    pending = (row["seq"], data)
                elif event == "model.failed":
                    if pending is None or any(pending[1].get(key) != data.get(key)
                                              for key in ("stage", "task", "role")):
                        return None
                    # Older/current failure emitters name the sole pending task, but omit the attempt.
                    if "attempt" in data and pending[1].get("attempt") != data["attempt"]:
                        return None
                    pending = None
                elif event == "model.response":
                    if pending is None or any(pending[1].get(key) != data.get(key)
                                              for key in ("stage", "task", "role", "attempt")):
                        return None  # an unpaired or mismatched response makes the whole trace unusable
                    seq, request = pending
                    pending = None
                    if request.get("api_request_sha256") in wanted:
                        pairs.append({"request_seq": seq, "response_seq": row["seq"], "request": request,
                                      "response": data.get("proposal")})
    except (OSError, ValueError, UnicodeError):
        return None
    return None if pending is not None else (digest.hexdigest(), pairs)


def _one(directory, request_hash, task, attempt, response_hash=None):
    """The single recorded (request, response) of request_hash, task and attempt, with the trace hash.

    The request text must reproduce the recorded hash exactly; a trace changed by redaction is a miss. The task,
    role and attempt must match, and the response must match the caller's recorded response hash when available.
    """
    found = _pairs(directory, {request_hash})
    if found is None:
        return None
    trace_sha256, pairs = found
    matches = [pair for pair in pairs if pair["request"].get("task") == task and pair["request"].get("attempt") == attempt]
    if len(matches) != 1 or len(pairs) != 1:
        return None
    pair = matches[0]
    if type(pair["response"]) is not dict or (response_hash is not None and _digest(pair["response"]) != response_hash):
        return None
    return trace_sha256, pair


def _final_issue(entry):
    """The final attempt of a request rejected after repair only for omitted inapplicable nullable keys, or None."""
    attempts = entry.get("attempts")
    local = entry.get("local_revalidation")
    if (type(attempts) is not list or len(attempts) != 2 or any(type(item) is not dict for item in attempts)
            or (local is not None and (type(local) is not dict or local.get("outcome") != "rejected"))):
        return None
    final = attempts[1]
    keys = final.get("missing_keys")
    if (final.get("attempt") != 2 or final.get("outcome") != "rejected" or final.get("layer") != "schema"
            or final.get("code") != "missing_keys" or type(final.get("path")) is not str
            or not _ITEM_PATH.fullmatch(final["path"]) or type(keys) is not list or not keys
            or any(type(key) is not str for key in keys)
            or not set(keys) <= NULLABLE_KEYS or len(keys) != len(set(keys))):
        return None
    return final


def prior_atomic_reply(directory, *, api_hash, fields):
    """A checkpoint-like {"response", "response_sha256", "accepted", "recovered"} for one atomic request, or None.

    fields are the current checkpoint identity fields. The prior request (exactly one with this initial API request
    hash) must come from a direct-atomic-validator/7 (atomic-map/1) or /9 (atomic-map/2) record with equal bound
    identity fields, never from a record of the current validator. Its final accepted reply, or the final repair reply
    of a request rejected after repair only for missing exclusion/question keys of one accounting item, is returned
    as recorded. accepted names the original attempt and recovered the prior validator; neither is relabelled.
    """
    if (type(fields) is not dict or fields.get("business_model_sha256") is None
            or fields.get("api_request_sha256") != api_hash):
        return None
    record = _record(directory)
    prior = None if record is None else PRIOR_VALIDATORS[record["schema"]]
    if prior is None or record["settings"].get("validator") != prior or fields.get("validator") == prior:
        return None
    matches = [item for item in record["requests"]
               if type(item) is dict and item.get("kind") == "atomic" and item.get("api_request_sha256") == api_hash]
    if len(matches) != 1:
        return None
    entry = matches[0]
    old = (entry.get("checkpoint") or {}).get("fields") if type(entry.get("checkpoint")) is dict else None
    if (type(old) is not dict or old.get("validator") != prior
            or any(old.get(key) != fields.get(key) for key in _BOUND)):
        return None
    attempts, outcome, issue = entry.get("attempts"), entry.get("outcome"), None
    if outcome in ("accepted", "accepted_after_repair"):
        accepted = entry.get("accepted")
        number = accepted.get("attempt") if type(accepted) is dict else None
        if (number not in (1, 2) or type(attempts) is not list or len(attempts) != number
                or type(attempts[-1]) is not dict or attempts[-1].get("outcome") != "accepted"
                or any(attempts[-1].get(key) != accepted.get(key) for key in ("api_request_sha256", "response_sha256"))
                or set(accepted) != {"attempt", "api_request_sha256", "response_sha256"}):
            return None
        final = attempts[-1]
    elif outcome == "rejected_after_repair":
        final, number = _final_issue(entry), 2
        if final is None:
            return None
        issue = {key: final[key] for key in ("layer", "code", "path", "missing_keys")}
    else:
        return None
    request_hash, response_hash = final.get("api_request_sha256"), final.get("response_sha256")
    if (any(type(value) is not str or not _HASH.fullmatch(value) for value in (request_hash, response_hash))
            or (number == 1 and request_hash != api_hash) or (number == 2 and request_hash == api_hash)):
        return None
    found = _one(directory, request_hash, "map-atomic", number, response_hash)
    if found is None:
        return None
    trace_sha256, pair = found
    return {"response": copy.deepcopy(pair["response"]), "response_sha256": response_hash,
            "accepted": {"attempt": number, "api_request_sha256": request_hash, "response_sha256": response_hash},
            "recovered": {"source": "resume_from", "prior_validator": prior, "prior_request_id": entry.get("id"),
                          "prior_outcome": outcome, "prior_final_issue": issue, "original_attempt": number,
                          "original_api_request_sha256": request_hash, "original_response_sha256": response_hash,
                          "initial_api_request_sha256": api_hash, "trace_sha256": trace_sha256,
                          "trace_request_seq": pair["request_seq"], "trace_response_seq": pair["response_seq"],
                          "model_calls": 0,
                          "basis": "A reply recorded by a prior local run for the same exact page request, accepted "
                                   "now only after the complete current validation. Not provider authentication."}}
