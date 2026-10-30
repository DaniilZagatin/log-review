# Transition support (direct-atomic-validator/10, transition-support/1)

Phase 1 of the source-closure diagnosis (`review-inputs/diagnosis.md`). This is a bounded **structural admissibility
boundary** for operation transitions proposed on code-led map pages. It is **not semantic certification**.

Not part of this slice:
- Phase 2 (bounded context closure, second-level control rows);
- Phase 3 (later-page dispositions);
- source-observation helpers;
- atomic-loop scheduling.

## What changed

- **Active vs inactive.** A transition that a `map-atomic` page proposes is *active* only when the host recomputes a
  structural support from that page's own source. Every other proposal is kept as an *inactive* proposal, with a
  generic reason and an addressed host gap. Nothing is deleted or rewritten.
- **No new calls, no repair.** Demotion adds no model call and triggers no repair. It never fails the page.
- **Unchanged:**
  - the raw reply, its hash and checkpoint;
  - candidate and unit accounting;
  - the request/response counts;
  - the page prompt and the page shape.
- **Only active transitions feed:**
  - `OperationTransition` and stage order;
  - entries, terminals and fragments;
  - continuation gaps;
  - scenario `transition_ids`;
  - the composition's `transitions`.
- **Composition sees inactive proposals apart.** They are listed separately as `unsupported_transitions`
  (`from`, `to`, `basis`, `reason`, no condition). The composition prompt no longer calls transitions "orders that
  supplied code showed". It says that structural connection is eligibility, not proof of order or condition, and that
  unsupported proposals are never an order or a scenario path.

## Supports (`transition_support.py`, host data of the proposing page only)

**Endpoint units** come from the validated accounting of the page:
- **new action:** the units that realize it, plus the supporting units that serve them (`serves`);
- **known action:** its earlier realizing units (stable host IDs), the earlier supporting units that served them, and
  this page's units that realize it again.

A unit's **owned source** is the set of ranges the page supplied for it (page unit or shared row), clipped to the
unit's own lines and merged. A finding ref or emit site of other code supplied with a unit is never its source.

| Support | Basis | Active when |
|---|---|---|
| `same_unit` | `source_call`, `source_condition` | One supplied unit realizes or serves both actions, and a cited range lies inside its owned source. |
| `call_chain` | `source_call`, `source_condition` | At most 2 control links between supplied units connect a unit of one action to a unit of the other. Links may be traversed in either direction, so a return into the caller counts. For every link, the source (calling) unit has a cited range inside its owned source that overlaps a known call line, or any cited range there when no line is known. |
| `contract_both_sides` | `source_handoff` | The existing hard check still applies: the literal is in at least two distinct cited ranges. In addition, one contract-bearing cited range lies in the owned source of a sending unit, and another lies in the owned source of a *different*, receiving unit. Same file is allowed; exact ranges decide. |

**Control links:**
- **Host provenance:**
  - `navigation`: resolved same-file calls of the declaration index, catalog callee anchors, host declaration
    callees; call lines come from the index's resolved call records;
  - `host_static`: resolved static calls of the code graph; call line is the call row.
- **Model provenance:** `worker_proposal`, resolved code-model relations of kind `call`, `dispatch`, `callback`,
  `return` or `control_handoff`.
  - These stay labelled separately. A chain relying on one is `provenance: "model_relation"`, never `host`.
  - `return` is included because it is the reverse of a call.
  - `branch` and `data_handoff` are not links.

Shared data, objects, fields and identifiers never link two actions.

**Search and reasons:**
- **Determinism:** the search is deterministic. Among the shortest paths, the first in supplied order wins.
- **Inactive reasons** (generic, structural):
  - `endpoint_source_not_supplied`
  - `no_source_chain`
  - `chain_without_cited_call`: a chain exists, but the calling unit's call is not cited in its own source;
  - `handoff_one_side`

### Open control links

These are recorded at commit for every unit that realizes an action on a page:
- an **unresolved code-model relation** of kind `dispatch`, `callback` or `control_handoff`, with its label and line
  (`unresolved_relation`);
- a **control-linked callee that the page did not supply** (`dependency_not_supplied`): a navigation callee, host
  callee, or resolved control/static relation target beyond the dependency bound, or a second-level dependency.

**When a link closes (after all pages):** a link to a known unit closes when that unit is:
- pending (the existing `pending_local_work` names it);
- decided as no step of its own (`supporting`, `excluded`, `outside_process`);
- realizing the same action;
- realizing an action joined to this one by an *active* transition.

An unresolved target never closes.

## Gaps

There are two new host gap kinds, valid only under this policy:

- **`unsupported_transition`**
  - Raised for each ordered action pair that has inactive proposals and no active proposal.
  - `operation_ids`: `[from, to]` in direction order. `unit_ids`: both actions' units.
  - The question names:
    - the proposal IDs;
    - the reasons;
    - the cited ranges;
    - the units of both actions with path and lines.
- **`unresolved_control_link`**
  - Raised for each action with open links that stay open, *whatever exit the model claimed*.
  - `unit_ids`: the realizing units carrying the links.
  - The question names the targets and their addresses. It states that this is neither an external receiver nor an
    end of the process.
  - It deliberately does not reuse `unresolved_handoff`. A local callee that was not shown is not an external
    receiver, and the existing tests keep that distinction.

**Gap IDs and effect:**
- **IDs:** `transition_support.gap_id`. Earlier kinds keep their sorted-operation ID; `unsupported_transition` keeps
  its direction.
- **Effect:** these gaps keep the record `partial` with `open_gaps`, exactly like the earlier gaps.

## Record and export schema

`atomic_map.json` (`atomic-map/2`, validator `/10`) gains `code_led.transition_support`:

```
{"policy": "transition-support/1",
 "proposals": [{"id", "request_id", "page", "from", "to", "condition", "basis", "contract", "basis_note",
                "evidence_ids", "state": "active"|"inactive", "reason": null|REASON,
                "support": null|{"kind", "unit_ids", "links": [{"from_unit", "to_unit", "origins",
                                 "relation_ids", "call_lines": [[path, first, last]], "witness_evidence_id"}],
                                 "witness_evidence_ids", "provenance": "host"|"model_relation",
                                 "unit_spans": {unit: [[path, first, last]]}},
                "endpoints": {"from_unit_ids", "to_unit_ids"}}],
 "open_links": [{"operation_id", "unit_id", "request_id", "page", "kind", "relation_id", "target_unit_id",
                 "target", "where": {"path", "start_line", "end_line"}}],
 "basis": "..."}
```

**Identity and counts:**
- **Proposal IDs** are the earlier `aot-` IDs: `stable_id("aot", page key, position)`.
- **`record.transitions`** is exactly the list of active proposals.
- **Settings:** `settings.code_led.transitions.support` declares the policy.
- **Summary keys:**
  - `transitions_added` counts active transitions;
  - `transition_proposals`, `transitions_inactive`, `transitions_inactive_by_reason` and `open_control_links` count
    the rest.

### Export (`onboarding_export.py`)

**`_code_led` / `_transition_support`** (record and bundle):
- **Presence:** `transition_support` is present exactly when the settings declare the policy.
- **Proposal identity:** every proposal has exact keys, a unique ID, an accepted atomic request of its page, and
  operations of this record.
- **Citations and endpoints:** its citations are in the evidence registry. Its endpoint units realize or serve its
  actions by the unit ledger.
- **Inactive proposals:** a known reason and no support.
- **Active proposals:** a support of their basis, where:
  - every supported unit was supplied on that page (`unit_ids` or `shared_context_unit_ids` of the request);
  - its `unit_spans` lie in that request's excerpts;
  - every witness is the proposal's own citation, inside the exact owned source of the unit it is claimed for: the
    sender and the receiver for a contract, the one unit for same-unit, each link's calling unit for a chain;
  - each witness covers the recorded call lines;
  - provenance is consistent with the link origins.
- **Active subset:** the accepted transitions must equal the active proposals, so no subset can be forged and no
  inactive proposal can reach the map.
- **Open links:** they name a unit that realizes their action and was accounted on their page.
- **Recomputed:**
  - the summary counts;
  - both new gap kinds, from the proposals, the open links and the unit ledger (`still_open`, `unsupported_pairs`).

**`_support_sources`** (saved `command_units.json`, `declaration_coverage.json`, `declaration_index.json`,
`technical_relations.json`):
- **Saved units:** every support and endpoint unit is a saved unit.
- **Unit lines:** every owned range lies on the unit's path inside its own lines.
- **Links:** every chain link is recomputed exactly as the host built it:
  - navigation between exactly its two units;
  - the relation rows whose saved source and target lie in exactly those units;
  - the declaration-index call lines.

  So a real relation ID, origin or call line claimed for other units fails. This was the reproduced hole 1.
- **Open links:** an open relation must be an unresolved dispatch, callback or control relation written in its unit.
  An open dependency must be a control-linked saved unit at its own address.

**Legacy records:**
- A record without `transition_support` (an earlier `/9` record) validates as before.
- It may not carry the new gap kinds or counts.
- It is never read as carrying the new policy.

No source is read, imported or executed during export, and no model is called.

## Identity, replay and checkpoints

- **Validator identity:** `VALIDATOR_VERSION` is `direct-atomic-validator/10`. `VALIDATOR_SHA256` covers
  `direct_atomic.py` and `transition_support.py`. Both enter every checkpoint identity, so a `/9` checkpoint file is
  never a `/10` hit.
- **What changed and what did not:**
  - unchanged: page prompt, page shape and wire decoding; the technical prompts, model identity and technical cache
    recipes are untouched;
  - changed: the business config identity and the composition request;
  - unaffected: technical review reuse.
- **Replay of current checkpoints:** a `/10` checkpoint replays through the complete validation and derives exactly
  the same support.
- **Revalidation of `/9` replies:** `business_replay.prior_atomic_reply` also accepts an exactly recorded `/9` page
  reply. It must be an `atomic-map/2` record with validator `/9`, the same initial API request hash and equal bound
  identity fields, and it is paired from the prior `trace.jsonl` as the `/7` path already did.
  - It is revalidated completely under `/10`.
  - It keeps `recovered.prior_validator: direct-atomic-validator/9` and `revalidated_under: /10` in the request and in
    the checkpoint it joins.
  - A record of the current validator is never a prior.

  No response is faked and no cache is migrated by hand.

## Test migrations (each intentional)

- **Version literals.** `test_code_led_map` (changed-prompt cache miss), `test_atomic_orphan_mapping_recovery`
  (`/5` → no hit, technical reuse) and `test_nullable_accounting` (`/7` → no hit, technical reuse, decoding policy)
  now expect `/10`. Every cache-miss, technical-reuse and config-hash assertion is unchanged.
- **`test_operation_inspector` route test.**
  - Before: the route test accepted `accept → release`, an order between independently invoked commands (webhook
    handler vs a separate task file, no call).
  - Now: the pipeline runs with the grounded route only. `accept` and `refund` are one unit with the cited condition,
    which gives `same_unit`.
  - The display of a second, unconditioned route out of one action is checked on `with_view_route`. This is a copy
    explicitly labelled `synthetic_view_only`: a pure UI-view input, never exported or validated as a run.
  - All branch, condition, basis, "not established" and no-end checks remain.
- **Ordinary-pipeline negative.** `test_an_unsupported_route_between_independent_commands_stays_inactive_and_addressed`
  asserts:
  - the `accept → release` proposal stays inactive (`no_source_chain`);
  - it has an `unsupported_transition` gap addressed to both operations' questions and the process unknowns;
  - there is no stage order and no extra model call.
- **`test_choice_join_and_conditional_completion…`.** Uses the grounded route. Continuation, join and completion
  come from cited details, so every check stays.
- **`test_detail_levels` stage-order test.**
  - The earlier crossing `refund → release` is now an asserted negative: inactive, no stage order.
  - The cross-stage order is derived from a real active route (`accept → refund`, `refund` placed in the other
    stage).
  - The stage/view assertions are unchanged.
- **`test_detail_levels` addressed-question test.**
  - Runs the pipeline with the grounded route; the transition-subject question is checked on the synthetic view.
  - Operation, unit and fact questions, backlinks and lossless unknowns are the pipeline's own.
- **`test_code_led_map` interpage test.** It now also asserts that the known-operation link is an active `call_chain`
  through the shared row of the known action.

## Limitations (exact)

- **Structure, not meaning.** A real call chain with a wrong or arbitrary condition is still active. So are branch
  versus sequence in a common caller, a wrong registry mapping and the meaning of an outcome. These stay model text
  and a semantic question for independent review.
- **Same unit is not order.** Same-unit support does not prove order or predicate.
- **Undirected reachability is not proof.** Two-link reachability is navigation eligibility only, never proof of
  causal execution.
- **Recall cost.** Cross-page links whose earlier caller's code is not supplied on the later page are demoted
  (`endpoint_source_not_supplied`). So are chains through omitted or second-level units. Java and other languages
  without host call resolution need resolved code-model relations. Expect more fragments and gaps until Phase 2/3.
- **Shared-row source.** Shared rows supply finding ranges, not whole declarations (except small host declarations).
  A call line outside them cannot be witnessed.
- **What export does not see.** Export does not see which unit's span supplied a line of the merged page excerpts. It
  checks that each owned range lies in the excerpts and in the unit's saved lines. Pending/omitted closure uses the
  final unit ledger; the producer's page context itself is not in the record.
- **Narrow helper mode.** `_support_sources` without index calls (`calls=None`) authenticates navigation call lines
  only through relation rows. The full export always passes the saved declaration index.
- **Merge rejection.** A merge-rejected record keeps no proposals, the same as its operations.
- **No live evidence.** No live or actual-target run is implied. Codex measures demotions on its own recorded runs.

## Files

- `src/process_observability/transition_support.py` (new)
- `src/process_observability/direct_atomic.py`
- `src/process_observability/onboarding_export.py`
- `src/process_observability/business_replay.py`
- `tests/test_transition_support.py` (new)
- tests migrated:
  - `tests/test_code_led_map.py`
  - `tests/test_business_replay.py`
  - `tests/test_atomic_orphan_mapping_recovery.py`
  - `tests/test_nullable_accounting.py`
  - `tests/test_operation_inspector.py`
  - `tests/test_detail_levels.py`
