# Technical reply diagnostics and conservative recovery

Scope: `src/process_observability/review_execution.py`, i.e. the technical code-review worker replies. One orchestrator and one model repair per task stay as before. Planner, prompts, the model adapter, routing, transport and budgets are unchanged, and no model call is added.

## 1. Precise repair feedback

Every repairable rejection now carries host-built `validation_feedback`:

- `code` — the rejection category (unchanged categories, see `REJECTIONS`);
- `path` — the exact JSON path, e.g.:
  - `findings[2].kind`
  - `findings[0].refs[1].start_line`
  - `relations[0].source`
  - `relations[0].target.ref`
  - `relations[0].refs[1]`
  - `relations[0].target.symbol_id`
  - `requests[3].symbol_id`
  - `interval_status[4]`
  - `unknowns[0]`
  - `$` for the whole reply;
- `expected` — the host expectation;
- bounded host facts where they help, e.g.:
  - `allowed` (enums: finding kinds, request types, interval statuses, relation kinds/resolutions);
  - `required_top_level_arrays`, `missing_keys` (host names), `unexpected_key_count`, `not_array_keys`;
  - `inapplicable_fields` and `required_fields` (request type), `required_fields_by_type`;
  - `owned_interval_ids`, `missing_interval_ids`, `duplicate_interval_ids` (host IDs), `foreign_item_indexes` and `invalid_item_indexes` (indexes only);
  - `supplied_ranges` (same path) or `supplied_paths`;
  - `duplicate_of_index`, `actual_type`, `actual_count`, `actual_length`, `symbol_id_shown_to_task`.

Model text is never copied into feedback: no model keys, values, IDs, labels or descriptions. Lists are capped at 20 items with an `…_omitted` count. The existing `ref_range` feedback dicts are byte-identical.

Repair request: the whole original brief, `previous_reply_rejected`, the unchanged `repair_contract` and the feedback. The source text is never shortened.

Budget, checked over the whole repair (system + user):

1. `detailed` feedback;
2. if that does not fit, `minimal` (`code`, `path`, `expected`);
3. else `location` (`code`, `path`);
4. else the task fails as `repair_exceeds_request_budget`.

Follow-up tasks reserve room only for the rejection category, as before. A very tightly packed follow-up whose earlier category-only repair fitted could now need the `location` level or fail with that honest category. There is deliberately no un-located level.

The chosen level is traced as `model.request.feedback_level` and saved in the designated-repair provenance. Primary requests, their hashes, plan identity and run fingerprint are unchanged.

## 2. Missing content

An adapter `LLMError` with category `missing_content` (HTTP 200 without answer content) now gets the same single repair as `invalid_json`. The feedback is:

```
{"code": "missing_content", "path": "$",
 "expected": "no answer JSON content was returned; return the required object"}
```

The repair is an ordinary `model.complete_json` call, counted like any repair. It is not a transport retry.

If the repair is again `missing_content` or `invalid_json`, the task fails as `invalid_reply` (rejections show both attempts). Every other category is never repaired: HTTP, TLS, credentials, connection, redirect, `invalid_response`, `response_too_large`, config, unknown errors.

## 3. Post-repair recovery (`technical-reply-recovery/1`)

### When it runs

Only when **all** of these hold:

- `repair_attempts >= 1` and the designated repair was actually dispatched (`task.repair` set; the received reply's request hash equals the last dispatched request hash);
- the repair's reply is a JSON object and failed strict validation.

### When it never runs

- before the repair (the first reply);
- with `repair_attempts=0`;
- when the repair could not be dispatched (budget);
- after `missing_content`, `invalid_json` or a non-object reply.

It makes no model call.

### How it works

A deep copy of the repair reply goes through a loop. Each step fixes exactly the one strict failure at its host locator, then the complete strict `_validate` runs again. The first failure that is not in the table below ends the recovery rejected, and the task fails as before. The loop is bounded by the reply size.

| Strict failure | Recovery |
|---|---|
| `request_fields` at `requests[i].<field>` for non-null fields of another request type | The fields are removed (warning lists their names). The host then resolves the request as usual. |
| any other `request_fields` / `request_finding_ids` on `requests[i]`: invalid type, reason, own fields, unknown linked finding | The request is quarantined: removed from the derived reply and kept whole **as received** in the provenance. A ledger request row is added with `status: invalid`, `status_reason: quarantined_after_repair`, plus `raw_request_sha256`. It counts in `request_debt` and gets an unknown. Nothing is guessed. |
| `finding_kind`: a nonblank string outside `finding_kinds` | The kind is recorded as `other`. The finding gets `recovered_kind: {original_kinds, accepted_kind: "other", merged_with_unrecovered, basis}` and an unknown. ID, description and refs stay strict and unchanged. |
| `relation_target` on a `resolved` target without a host basis (symbol not shown to the task, or neither symbol nor ref) | Only if the item has its own valid nonblank `label`: `resolution: unresolved`, target `{symbol_id: null, ref: null, label}`. The relation gets `recovery: {original_resolutions, removed_target_fields, actions, merged_with_unrecovered, basis}` and an unknown. Never upgraded. If the item also had a valid supplied `target.ref`, that ref is dropped too (conservative; it stays in the raw reply). |
| `relation_target` on `unresolved`/`external` with a non-null `symbol_id`/`ref` and a valid label | Those fields are cleared. A given `target.ref` has already passed the strict supplied-text check. |
| `interval_status`: an **owned** interval with no item or with an invalid item | Recorded as host-derived `not_understood` (`origin: host_derived`). The interval row gets `model_status: null` and `status_origin: host_derived_after_repair`. Adds the unknown `interval_status_missing` and the reason `intervals_without_valid_worker_status` (so `coverage_complete` is false). |

### What stays strict (terminal after the repair)

- shape and missing top-level arrays;
- more requests than `max_requests_per_task` (so quarantines, debt rows and recovery unknowns stay bounded by that cap and never grow with the model's choice);
- finding fields, finding IDs (also duplicates) and descriptions;
- every ref error (findings, relation `source`, `refs`, `target.ref`): fields, range, outside supplied text, evidence;
- unknowns;
- relation fields, IDs, kind and resolution value, target schema, label type;
- a `resolved` target without a valid label;
- interval items naming a foreign interval, duplicate interval items, items without a string `interval_id`.

An auxiliary defect beside a core defect still fails: the core defect ends the loop. The handled auxiliary actions are recorded as `recovery: {outcome: "rejected", actions, final}` in the ledger row and as the trace event `review.reply_recovery_rejected`.

### What an accepted recovery records

- task ledger row: `recovery: {outcome: "accepted", policy, source, actions, raw_response_sha256, derived_response_sha256, quarantined_requests}`;
- trace event `review.reply_recovered`;
- unknowns with origin `reply_recovery` (classification lost, receiver unknown, unread request);
- the run reason `replies_recovered_after_repair`, so `complete` is false. A derived reply never claims a complete review.

`ledger.summary.reply_recovery` appears only when a recovery or a host-derived status exists, so runs without one keep their exact earlier output.

## 4. Checkpoints and provenance

### Ordinary success

`status: "succeeded"` with `response` and exactly the previous keys. It is unchanged and reused exactly as before.

### Derived success

`status: "recovered"`, keys `RECOVERED_KEYS`:

- `raw_response` — the repair reply **as received**, never the derived JSON;
- `recovery` (`code-review-reply-recovery/1`):
  - `policy`, `task_id`, `request_sha256`;
  - `designated_repair` — category, the exact feedback sent and its level;
  - `attempts` — per received attempt: API request hash, reply hash or null, rejection;
  - `raw_response_sha256`, `derived_response_sha256`;
  - `actions`;
  - `quarantined_requests` — each raw request as received;
  - `basis`;
  - `recovery_sha256` — self hash.

Older readers do not recognise this status and rerun the task; nothing is misread as a raw success.

### Reuse of a derived checkpoint

All of these must hold:

- exact keys, schema, the current policy and the exact basis text;
- the self hash;
- the task ID, request hash and task kind;
- integer attempt numbers and an attempt count of 2;
- rejection categories from the known set;
- saved feedback whose `code` equals the saved category and whose keys match its level;
- reply hashes that are 64-hex strings, null only for a first reply without content or JSON;
- a recomputed repair that fits the request budget;
- attempt 1's request hash equals the task's own primary request;
- attempt 2's request hash equals the repair request recomputed from the task's request and the saved category and feedback (the deterministic `_repair_user`);
- the raw reply hash equals both saved hashes;
- the raw reply still fails strict validation with the saved category (a valid raw reply is never reused as derived);
- the current policy re-derives exactly the saved derived hash, actions and quarantine.

Any mismatch is reported in `checkpoints_not_reused` as `recovery_schema`, `recovery_policy`, `recovery_integrity`, `binding_mismatch`, `recovery_binding`, `recovery_raw` or `recovery_derivation`, and the task is asked again.

Limit: the self hash and the bindings detect accidental change, cross-task copies and inconsistent edits. A local file rewritten fully and consistently cannot be told apart from a real one. That is integrity, not authentication.

## 5. Not done, by design

- No third model call, no transport retry loop, no change to the model adapter or its trace.
- No fabricated model status, finding, ref, label, symbol, receiver or read target.
- No semantic acceptance: a recovered reply is a validated proposal with recorded uncertainty, like any accepted reply.
