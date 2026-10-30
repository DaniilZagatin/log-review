# Plan: code graph and whole-repository review (R01 patch over 0.1.0)

This plan is read-only. I read only the allowed files. Line numbers come from the current working tree, which includes Codex's unvalidated prototypes. `code_review.py` does not exist, because my interrupted run wrote nothing.

## 0. Current state and what to do with each prototype

| Area | Current code | Keep | Repair or replace |
|---|---|---|---|
| `source_tools.py` | Optional `None` limits, `max_file_bytes`, `include_text` (`:62-72`, `:101-179`) | Optional limits, per-file byte guard, `include_text=False` | `exclusions` are recorded only when both limits are `None` (`:176-178`). Read and decode failures become plain strings (`:167-170`). **Plan:** always build a structured inventory on a new `RepoSnapshot.inventory` attribute. Leave `scope` unchanged so the demo replay fingerprints stay stable. Separate *policy* exclusions (secret, dependency, symlink, extension, include) from *resource* failures (oversized, non-UTF-8, binary, unreadable). Resource failures make the review incomplete. |
| `log_discovery.py` | Global caps: 300 sites, 40 per file (`:188`, `:207-209`) | Extractors and site records | In review mode, coverage `_emits` would see a truncated set. Allow `max_sites=None`, `max_per_file=None` for the bundle; prompts get bounded slices. |
| `review_reduction.py` | Hierarchical summaries, retained tree | Exactly-once child partition, child-ref restriction, cached-reply revalidation | The fixed `.tmp` name blocks resume after a crash (`:30-32`). Batching follows arbitrary input order, not module locality (`:98-112`). The model picks ≤4 refs per summary (`:85-90`), and then `workflow.py:491` restricts business citations to those refs, so rare-branch anchors become uncitable. Any batch error aborts everything. There is no failure trace. **Replace the internals; keep the module.** |
| `workflow.py` review path | Wiring (`:445-503`, `:751-803`) | The pre-send budget check (`:365-366`), the not-cited residual idea, the `evidence_view` filter | It imports a missing module (`:455`). It replaces unknowns over 3000 chars with a pointer, which loses them silently (`:475-476`). It truncates the inventory greedily in file order (`:416-428`). It finds the residual by substring search over map JSON (`:797-798`). It forces every stage to INCOMPLETE when any review gap exists (`:751-753`), mixing stage failure with scope incompleteness. **Repair.** |
| `tracing.py` | Unlocked sequence and append (`:30-41`) | Format | Add a lock and `close()`. |
| `cli.py` | Defaults of 60 files / 120000 chars (`:57-58`, `:190-191`); unlocked `_ProgressModel.calls` (`:103-119`) | | New flags, resume, plan-only (§6). |
| Demo | Replay keyed by `sha256(system+"\n"+user)` (`demo.py:10-26`) | | Re-key for the new prompts (§7). |

## 1. Architecture and interfaces (planned)

```
snapshot_repository(limits=None) ─► RepoSnapshot(+inventory)
      │
      ├─► discover_logging(uncapped) ─► code_graph.build_graph ─► CodeGraph (saved to code_graph.json)
      │
      └─► code_review.plan_review (deterministic, no model)
              ─► base tasks (intervals packed under request_chars)
              ─► daemon worker pool (GLM, "TASK: code", one complete_json per task)
              ─► validate replies; host IDs; atomic checkpoints; ledger
              ─► follow-ups from typed requests (one generation, max_followups)
          review_reduction.reduce (GLM, "TASK: code-reduce", locality batches)
              ─► bounded digest + full leaf catalog
          workflow business stages (DeepSeek) on bounded contexts
              ─► deterministic residual of salient leaves the map does not cite
```

**Interfaces:**
- `code_graph.build_graph(snapshot, inventory) -> CodeGraph`. `CodeGraph` exposes `to_dict()`, `sha256`, `decls_in(path, a, b)`, `outgoing(sym)`, `incoming(sym)` and `literal_sites(text)`.
- `code_review.ReviewOptions`:
  - `request_chars=32000`, `workers=2` (1..8), `max_followups=32`
  - `max_requests_per_task=6`, `max_findings_per_task=40`, `repair_attempts=1`
  - `index_share=0.2`, `parent_share=0.25`
- `code_review.plan_review(snapshot, graph, system, options) -> ReviewPlan`.
- `code_review.review_repository(snapshot, graph, model, *, system, options, model_fingerprint, checkpoint_dir, resume_from=None, trace=None) -> {findings, unknowns, ledger, complete}`. This intentionally differs from the interrupted brief: the graph and an options object are passed in.
- `review_reduction.reduce_findings(review, graph, model, *, system, digest_chars, request_chars, checkpoint_dir, trace) -> {digest, tree, accounting}`.
- `review_reduction.residual(process_map, leaves) -> [leaf_id]`.

## 2. Graph (`code_graph.py`, new)

**Python, through `ast` on the redacted frozen text:**
- **Lines and fallback.** Lines are counted with `text.splitlines()`. The same odd-separator rule as `log_discovery.py:69-70` applies: on a mismatch, fall back to `ast_fallback`.
- **Declarations.** Module, class, function, async function and method. The span includes decorators. Each records its immediate parent.
- **Declaration ID.** `sym-` + `H(path, file_sha256, qualname, start, end)`. IDs therefore change whenever the file changes.
- **Call sites.** ID is `call-` + `H(path, file_sha, line, col, callee_text)`. Each call is owned by its **immediately enclosing** scope:
  - a call in a nested function belongs to the inner function, not every ancestor;
  - calls in decorator and default-argument expressions belong to the outer scope;
  - calls in a class body belong to the class.
- **Imports.** Import aliases and relative imports are resolved by path arithmetic. Module names are the dotted suffixes of the path without `.py`.
- **Function references passed as arguments** (`retry(fn=on_fail)`, `submit(job)`) form a separate `reference_arg` relation, never a call edge.

**Resolution statuses (conservative):**

| Status | When |
|---|---|
| `local_def` | Bare name; exactly one top-level or nested def in scope; no other binding of that name |
| `resolved_import` | Alias bound by an import resolving to a unique snapshot file with exactly one top-level def of that name |
| `module_only` | Module resolved, attribute not a unique def (re-export or assignment) |
| `same_class_lexical` | `self.m()` where the lexical class defines `m` once; overrides and subclasses are not checked |
| `ambiguous_binding` | The name is rebound or shadowed |
| `unresolved_receiver` | Receiver type unknown. Carries `name_matches: n` (a count and at most 10 IDs, labelled `not_edges`) |
| `dynamic` | `getattr`, subscripts, calls on call results, lambdas |
| `external_or_builtin` | Not bound in the snapshot |

**Literals.** String literals that are first arguments of publish/send/emit/subscribe/consume-like calls, or decorator arguments, become `literal_sites`. They are **never** joined into edges.

**Wrappers.** A function anywhere whose log call forwards one of its parameters (the rule in `log_discovery.py:99-111`) is a wrapper candidate. Calls that resolve to it with `local_def` or `resolved_import` become cross-file `wrapper_call` candidates. They are not emission evidence, and `_emits` already excludes them.

**Logging sites** attach to the innermost declaration by line containment. This reuses the inventory and creates no second labelling source.

**Other languages.**
- Heuristic declaration-start lines (Java, Kotlin, TS/JS, Go, C#, PHP) are used only as interval boundary hints and labels, marked `extractor: heuristic`.
- Import lines are text with `unresolved_heuristic`.
- No call edges are produced.
- Navigation falls back to `span` and `literal` requests.
- Config and text files use line windows.

**Where the graph is used:**
1. Declaration boundaries define intervals.
2. Each task brief carries index rows for its owned symbols.
3. Typed requests resolve by `symbol_id`.

The graph claims no execution or reachability. The full graph goes to `code_graph.json`, and only `graph_sha256` enters fingerprints.

## 3. Inventory, intervals, budget, ledger (`code_review.py`, new)

**Intervals** give non-overlapping, exhaustive coverage of lines `1..N` for every readable eligible file:
- **Python:** top-level declarations. A class that fits is one interval; otherwise there is a class-header interval plus one interval per method. Module statements between declarations are grouped together.
- **Other files:** heuristic boundaries, or line windows that break at blank lines.
- **Empty file:** an `empty` record with no task.

**Oversized input:**
- A declaration too big for one request splits at child-statement boundaries, recursing into bodies, then at line boundaries. Each segment carries `{parent_symbol_id, index, count}` and a header with the path, the signature line (≤300 chars) and the enclosing class.
- A single line too big for the source budget becomes an `unsupported` interval with `line_exceeds_request_budget`, its length and its hash. Its text never enters a prompt.
- An oversized, binary or non-UTF-8 file is a resource failure. Without `--allow-partial` the run aborts before any model call. With it, the file appears as `unsupported`.
- Non-UTF-8 files (for example cp1251 Java) are therefore reported, not decoded. An encoding option is deferred.

**Budget:**
- Each request is measured exactly as `len(system) + len(user)`, where `user` is the exact `json.dumps(..., ensure_ascii=False, sort_keys=True)` string.
- The system prompt includes the map guidance, which can be up to 14000 chars (`guidance.py:28`).
- Source budget = `request_chars - len(system) - skeleton - index cap - parent cap`. If that is below 4000, it is a configuration error before any call.
- Intervals from the same directory are packed into one task (at most 12 intervals) while they fit.
- Ownership stays per interval.
- Source text uses the redacted numbered `N: line` format. Raw text is never used.

**Ledger** (`code_review.json`, written by the coordinator only):
- `files[]`: path, sha, `eligible|policy_excluded|resource_unsupported`, reason, lines, extractor.
- `intervals[]`: id, path, range, symbol, segment, task, and a state. The state is one of `planned`, `supplied`, `reviewed`, `partial`, `not_understood`, `no_claim`, `failed`, `unsupported` or `empty`.
- `tasks[]`: id, kind, intervals, `request_sha256`, chars, state (`pending`, `completed`, `failed`, `interrupted` or `abandoned`), attempts, failure category, cache hit, parents.
- `requests[]`: id, source task, target, status.
- `summary`: `coverage_complete` plus claim counts.

These are different kinds of statement and are reported separately:
- **Supplied** is a host fact.
- **Reviewed** is the worker's own claim.
- **Verified text** means the cited ranges passed `snapshot.evidence`.
- **Semantic correctness** is never established.

`complete = coverage_complete`. That requires every eligible line to be in a supplied, validated base task, with no unsupported intervals or resource failures and no partial snapshot.

**Scope removal.** No model and no prioritisation can remove scope. Only user includes and the policy exclusions can.

## 4. Workers, requests, cross-file integration

**Brief.** Each task's user payload contains:
- the task kind and the owned intervals with their text;
- index rows, capped at `index_share` with an `index_omitted` count. Per owned symbol these are: outbound calls with status and, when resolved, the target id/path/qualname/signature; up to 5 resolved callers plus a total; file imports (≤20); logging sites; the wrapper flag; and literal sites with `exact_matches_elsewhere: n`.

**Reply** (strict keys; the first system line is `TASK: code`):
```
{"findings":[{"id","kind","description","refs":[REF]}],
 "interval_status":[{"interval_id","status":"reviewed|partial|not_understood","note"}],
 "requests":[{"type":"symbol|span|literal","symbol_id","path","start_line","end_line","literal","reason","finding_ids"}],
 "unknowns":[str]}
```
- `kind` is one of: entry_point, branch, outcome, error, state_change, transaction_boundary, logging, wrapper, identifier, integration, configuration, other.

**Validation is atomic per task:**
- shapes and types;
- descriptions ≤1200 chars, at most 40 findings;
- every ref passes `snapshot.evidence`, is ≤80 lines, and lies inside an owned interval (follow-ups: inside the supplied target or parent ranges);
- any `verified`-style key is rejected.

On failure there is one repair attempt. It adds only `previous_reply_rejected: <category>`, never the reply content. If that fails too, the task is `failed` and other tasks continue.

**Host IDs.**
- Findings: `cf-` + `H(task_id, kind, normalized refs, description)`. Identical duplicates collapse, while distinct findings on distinct anchors are kept.
- Model IDs are only used to link requests within one reply.

**Request rules:**
- `symbol` must name an ID present in that task's brief.
- `span` needs an exact `snapshot.files` key, not `blocked_path`, ≤80 lines. Nothing outside the snapshot is ever read from disk.
- `literal` must be a literal present in the brief. It resolves to the enclosing declarations of the exact occurrences, at most 3. The basis is `model_requested_literal_match`, never "resolved".

**Request statuses:** `dispatched`, `merged`, `out_of_scope`, `excluded`, `unknown_symbol`, `too_large`, `cap_reached`, `depth_limit`, `invalid`.

**Follow-ups:**
- They are planned after all base tasks finish (a barrier), so deduplication is deterministic.
- Key: the target range. Up to 4 requesting parents are merged, sorted by task ID; the rest are kept as IDs only.
- Each follow-up gets: the question, the parent findings (≤`parent_share`), the text of up to 2 parent ref ranges, and the target text.
- Findings are tagged `integration` with `parent_finding_ids`.
- Follow-up requests are recorded as `depth_limit` debt, which gives exactly one generation.
- Order is deterministic, so `max_followups` caps the same requests every time.

**Expected behaviour on the hard cases:**
- **Cross-file wrapper:** the brief shows `audit(...)` resolving through an import alias to a wrapper candidate. The worker requests the symbol, and the follow-up cites both the wrapper body and the call site.
- **Duplicate methods:** they stay `unresolved_receiver` with a count only.
- **Callbacks:** `reference_arg`.
- **Queues:** literal counts only.

## 5. Aggregation and bounded business contexts

- **Leaf catalog.** All validated findings are kept in `code_review.json` and never truncated. Unknowns become catalog items too, which replaces the pointer at `:475-476`.
- **Reducer batches** are cut by locality: package directory, then file, then symbol order. Whole files stay together; big files split in symbol order. Reducer input rows carry `id`, `kind`, a description and `path:symbol`, but no source.
- **Reducer output.** It returns `{description ≤600, source_ids}`. The host attaches the kind histogram and a ref sample that puts **one ref per salient leaf first** (branch, outcome, error, state change, logging, wrapper, integration), with `refs_omitted`.
- **Reducer checks:**
  - exactly-once partition;
  - batches actually reduced;
  - progress on every round;
  - unique temp names for writes;
  - checkpoints per batch.
- **Reducer failure.** A failed batch makes the map stage incomplete with an explicit category. Successful batches stay cached, so a resume reruns only the failed batch.
- **Business citations.** `semantic_ranges` covers **all leaf refs**, not only summary refs. This fixes `workflow.py:491`.
- **Residual.** It is computed deterministically by range overlap between salient leaves and every ref cited by the map. This replaces the substring search. It is shown as "technical findings not reflected in the map". Source-ID accounting proves that every leaf was partitioned; it does not prove meaning survived compression, and the bundle says so.
- **Context priority** for every business request:
  - **Required, or explicit failure:** the business text and the schema.
  - **Then:** the digest, then salient refs.
  - **Then:** an inventory **key catalog** instead of individual sites: `event_key`, `key_status`, kinds, site count, ≤3 paths.
  - **Last:** optional views.

  Every omission is recorded in `context_omissions`.
- **Business budget.** `--business-request-chars` defaults to 240000, sized for the existing 200000-char business-text cap (`cli.py:204-205`) plus the digest (target 24000). Whether DeepSeek can take that much is unverified. If the budget is exceeded, the request is not sent.
- **Stage states.** They reflect only the stages' own outcomes. An incomplete review sets `partial_input`, adds a limitation and gives CLI exit code 1. This replaces the forced INCOMPLETE at `:751-753`.

## 6. Persistence, concurrency, Ctrl+C, traces, CLI

- **Run fingerprint** (`review/run.json`):
  - `snapshot_id`, `graph_sha256`, worker and reducer system SHA, guidance `pack_sha256`;
  - the code model fingerprint: base_url, model, thinking, max_output_tokens (TLS paths excluded);
  - result-affecting options (worker count is excluded);
  - the tool version.
- **IDs.** `task_id = H(run_fp, kind, interval_ids, user_sha256)`. Task files are `review/tasks/<task_id>.json` and contain the request hash and the redacted response. No prompts and no source are stored.
- **Resume** (`--resume-from OLD` with a new `--out`, so nothing is overwritten):
  - Any mismatch in the run fingerprint is refused, naming the category (snapshot, prompt, model, options, guidance).
  - Each cached reply is fully revalidated. A bad task file causes re-execution plus a `checkpoint_invalid` event; it does not cause mixing.
  - Follow-ups are re-derived from the cached parents.
  - Business stages always rerun.
- **Writes.** `mkstemp` in the same directory, then `fsync` and `os.replace`. Directories are created under the run directory only. A symlink at the directory or the file is refused. Stale temp files are ignored and reported.
- **Concurrency.**
  - A small pool of **daemon** threads with job and result queues (`ThreadPoolExecutor` threads are joined at exit).
  - The main thread does all validation, checkpoint, ledger and trace writes. It polls the result queue every 0.25s. That poll keeps Ctrl+C responsive, including on Windows; it is not a deadline.
  - A worker-raised `KeyboardInterrupt` or `BaseException` is re-raised in the main thread.
- **Ctrl+C:**
  - Dispatch stops.
  - In-flight tasks are marked `interrupted` in the ledger, and `review.interrupted` is traced.
  - The CLI writes `run.interrupted`, closes the trace and exits with 130.
  - In-flight HTTP requests are abandoned locally. The server may still process them, and their replies are never recorded.
- **Tracing** (`tracing.py`):
  - A lock around the sequence counter and each write.
  - `close()`; events arriving after close are dropped.
  - The full system prompt is written once (`review.system`); `model.request` then carries `system_sha256`, `task_id` and the user payload.
  - `llm.py`'s `api.response` metadata adds `request_sha256` for correlation.
  - `_ProgressModel.calls` gets a lock, and diagnostics are sorted.
- **CLI `analyze`:**
  - Review mode is the default, with no global truncation.
  - `--max-files` and the new `--max-repo-bytes` only abort.
  - New flags: `--review-request-chars`, `--review-workers`, `--max-followups`, `--business-request-chars`, `--resume-from`, `--plan-only` (writes the plan and ledger with zero model calls).
  - `discover-logging` defaults to no caps.
  - `code_review.json` and `code_graph.json` are added to `FEEDBACK_FILES`.

## 7. Patch delivery

- `scripts/release_patch.py` (stdlib) has two commands:
  - `build --baseline 0.1.0.zip`: verifies SHA `deb153d1…`, diffs against the allowlisted release minus `certs/`, `inputs/` and `config/`, and writes `patch.zip`. The zip contains the changed and new files, `PATCH-MANIFEST.json` (baseline SHA, per-file pre/post SHA, deletions), a `changes.diff`, and the applier itself.
  - `apply --target DIR [--dry-run]`: checks every pre-image, refuses on any mismatch or symlink, writes atomically and touches no unlisted file.
- Portable relative paths. No dependence on Git.
- **Codex verification:**
  1. Clean extraction of the baseline ZIP.
  2. Dry-run, then apply.
  3. Confirm that cert and process-input hashes are unchanged and that a second apply is refused.
  4. Run the full suite, plus demo, `discover-logging` and `--plan-only`.
- **Changed baseline tests** are listed explicitly:
  - `test_cli.py:40-42` (the output file set);
  - `:87` (request count, still 4 for the one-file demo with no reducer round);
  - the demo replay re-keyed with the same responses.

## 8. Offline tests

These use in-memory `RepoSnapshot(SourceFile…)` objects and scripted models that read the brief JSON. They test mechanics, not model quality; real quality needs the corporate pilot, starting with `--plan-only`.

1. **More than 60 files and 120k chars:** union of intervals equals all lines, intervals are disjoint, more than one request is made, every request is ≤`request_chars`, and no prompt contains another file's unique marker.
2. Accounting with an empty file, a comment-only file and YAML line windows.
3. **Cross-file wrapper with an alias:** the brief row is resolved, the follow-up reads the wrapper, citations to a third file are rejected, and parent links are kept.
4. **Ambiguity:**
   - two `save` methods stay `unresolved_receiver` with no edge;
   - `self.save` resolves to the lexical class;
   - a nested call is attributed to its immediate owner;
   - the callback is `reference_arg`;
   - `getattr` is `dynamic`;
   - publish and consume of the same literal produce no edge.
5. **Rare branch:** it appears in the residual when the map does not cite it, and leaves the residual when cited.
6. **Oversized function:** segments, k/n headers, and no citation across segments.
7. **Oversized line:** `unsupported`, its canary is absent from all prompts, and `complete` is false.
8. **Bad refs** (wrong path, out of range, 81 lines, outside ownership, extra `verified` key): repair, then fail, while other tasks keep their findings.
9. One task raises `LLMError` and the rest finish.
10. **Interruption and resume:** an interrupt on call 4 with one worker leaves 3 checkpoints; the resume makes exactly the remaining calls and produces findings identical to an uninterrupted run.
11. **Mismatch on resume:** a changed source file, model fingerprint, system prompt or `request_chars` is refused. A tampered task file is re-executed.
12. **Four workers:** trace sequence numbers 1..n with no gaps, valid JSON on every line, exactly one terminal event per task.
13. **Boundaries:**
    - a symlinked checkpoint directory or file is refused;
    - requests for `../x`, absolute paths, `.env` or excluded paths read nothing (the repo directory is deleted after the snapshot);
    - a `password=` canary is absent from prompts, checkpoints, the ledger and the trace.
14. **Reducer:** a missing or duplicate child, too many summaries and no progress are all rejected, with the leaf catalog retained.
15. **Business budget:** an oversized required context sends no request; the key catalog reports omission counts.
16. **CLI with a mocked transport:** two workers, `--plan-only` makes zero calls, `--resume-from` works, and the demo is reproducible.

**Commands:** `PYTHONPATH=src python3 -B -m unittest discover -s tests -q`, and per file, for example `-p 'test_code_graph.py'`.

## 9. Ordered steps and slices I can own after Codex accepts this plan

| Step | Depends on | Files (owner) | Invariants | Exit criteria |
|---|---|---|---|---|
| S1 Graph | baseline | `code_graph.py`, `tests/test_code_graph.py` (Claude) | No execution; no same-name joins; IDs bound to file hash | Tests 3–4 (graph part) pass; 178 baseline tests untouched and passing |
| S2 Inventory and plan | S1 | `source_tools.py` (inventory repair), `log_discovery.py` (`None` caps), `code_review.py` plan, `tests/test_code_review.py` (Claude) | Exhaustive disjoint intervals; exact budget; legacy `scope` unchanged | Tests 1, 2, 6, 7 (planning); `plan_review` makes zero model calls |
| S3 Execution | S2 | `code_review.py`, tests (Claude) | Coordinator-only writes; atomic per-task validation; one follow-up generation | Tests 3, 8–11, 13 |
| S4 Reduction and residual | S3 | `review_reduction.py`, `tests/test_review_reduction.py` (Claude) | Leaf catalog complete; no voting; accounting is not semantics | Tests 5, 14, 15 (helpers) |
| S5 Integration | S4 | `workflow.py`, `cli.py`, `tracing.py`, `llm.py` (one line), demo replay, changed tests (Claude if delegated, otherwise Codex) | No forced stage failure; never-overwrite; exit 130 | Tests 12, 15, 16; full suite passes |
| S6 Patch and docs | S5 | `scripts/release_patch.py`, README, PILOT, AGENT-START, STATUS, plan (Codex) | Certs and inputs untouched | Clean-extraction dry-run, apply, full suite |

**Most important unresolved choice: scale versus request budget.** The GLM context window and throughput can't be verified here. A large corporate repository could mean thousands of 32k-char calls with no wall-time bound.

My defaults:
- 32000 chars per request and 2 workers;
- mandatory visibility through `--plan-only`, which reports the task count and total characters before anything is spent;
- an optional `--max-review-requests` that aborts instead of truncating.

**Secondary choice:** single-shot follow-ups instead of continuation turns. Follow-ups are better for isolation and resume, but they lose the requesting worker's reasoning. Revisit only if test 3 fails on real models.

**Smallest useful first deliverable: S1 + S2 + S3.**
- The graph drives interval boundaries, brief index rows and `symbol`/`literal` follow-ups.
- Base review covers the whole scope with exact accounting.
- Checkpoints and resume work at module level.

It is fully offline-tested and callable through `review_repository`. S4–S5 then make it visible to users through `analyze`.
