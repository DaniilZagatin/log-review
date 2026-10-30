"""Portable MVP commands. Target repositories are read-only analysis inputs."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from threading import Lock
import uuid

from . import __version__
from .contracts import strict_json_loads
from .redaction import redact_text


ROOT = Path(__file__).resolve().parents[2]


class UserInputError(ValueError):
    pass


def status_report():
    return {"package": "process_observability", "version": __version__, "state": "map_first_logging",
            "analysis_pipeline_implemented": True,
            "implemented": ["published map input", "source-bound logging review", "deterministic operation coverage", "addressed logging changes", "offline HTML report"],
            "default_command": "review-logging", "business_metrics": "frozen",
            "manifest_schema_version": "0.1",
            "stages": {"logging": "implemented", "coverage": "implemented", "instrumentation": "proposals", "metrics": "frozen"},
            "message": "Reviews existing map operations. Semantic findings need independent acceptance; runtime delivery is unverified."}


def build_parser():
    parser = argparse.ArgumentParser(prog="python run.py", description="Process observability pilot MVP (Python 3.11+, stdlib only).")
    parser.add_argument("--version", action="version", version=f"process_observability {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    logging = commands.add_parser("review-logging", help="review existing logs over a published atomic map; never rebuild the map or call business metrics")
    logging.add_argument('--map', required=True, help='published scenario_map.json, read only')
    logging.add_argument('--repo', required=True, help='the same repository already investigated by the first agent')
    logging.add_argument('--out', required=True, help='new output directory, outside the target repository')
    logging.add_argument('--hierarchy', help='optional map_hierarchy.json; by default looked up next to the map')
    logging.add_argument('--artifacts', help='optional producer code_context.json/read_fragments.jsonl directory; defaults to map directory')
    logging.add_argument('--source-root', action='append', default=[], metavar='SOURCE_ID=SUBDIR', help='source prefix mapping within --repo; one source defaults to repository root')
    logging.add_argument('--config', help='explicit model profile; a business profile or existing code/business configuration')
    logging.add_argument('--inventory-only', action='store_true', help='no model or credentials; operation judgments remain unassessed')
    logging.add_argument('--resume-from', help='earlier logging_review.json or its directory; exact decisions are revalidated')
    logging.add_argument('--max-requests', type=int, default=128, help='total semantic requests including repairs (default 128)')
    logging.add_argument('--request-chars', type=int, default=180000, help='whole system+user request limit; refuses oversized packets, never truncates')
    logging.add_argument('--max-navigation-turns', type=int, default=4)
    logging.add_argument('--stop-on-failed-repair', action='store_true',
                         help='save a partial result and stop before another model call if the single repair fails')
    logging.add_argument('--max-context-lines', type=int, default=1600)
    logging.add_argument('--include', action='append')
    logging.add_argument('--log-call', action='append', default=[])
    logging.add_argument('--allow-partial', action='store_true')
    _scope_arguments(logging)
    logging_export = commands.add_parser('export-logging', help='regenerate a logging report/export from saved JSON without a model')
    logging_export.add_argument('--run', required=True)
    logging_export.add_argument('--out', required=True)
    commands.add_parser("status", help="show implementation state")
    info=commands.add_parser("process-info",help="verify and describe a bundled process input, without a model")
    info.add_argument("process",choices=("p3283",))
    check = commands.add_parser("check-model", help="send a minimal JSON health request to the configured corporate model")
    check.add_argument("--config", default=str(ROOT / "config/model.corporate.json"))
    check.add_argument("--role",choices=("code","business","all"),default="all")
    demo = commands.add_parser("demo", help="run the explicit synthetic offline demonstration")
    demo.add_argument("--out", required=True, help="new output directory, never overwritten")
    analyze = commands.add_parser("analyze", help="analyze a read-only repository using the configured live model")
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--out", required=True, help="new output directory, never overwritten")
    analyze.add_argument("--config", default=str(ROOT / "config/model.corporate.json"))
    analyze.add_argument("--business-file", help="optional business description / external agent response as plain source text")
    analyze.add_argument("--process",choices=("p3283",),help="load the bundled process description automatically")
    analyze.add_argument("--method-file", action="append", default=[], help="additional methodology text as an unreviewed source; repeatable")
    analyze.add_argument("--goal", default="", help="business process or question to focus on")
    analyze.add_argument("--logs", help=argparse.SUPPRESS)
    analyze.add_argument("--field-map", help=argparse.SUPPRESS)
    analyze.add_argument("--include", action="append", help="relative glob; repeat to narrow the selected source scope")
    _scope_arguments(analyze)
    analyze.add_argument("--allow-partial", action="store_true", help="explicitly continue when eligible inputs or lines cannot be reviewed; omissions stay visible and the exit code is 1")
    analyze.add_argument("--log-call", action="append", default=[], help="additional logging/wrapper callee, e.g. audit_event; repeatable")
    analyze.add_argument("--review-request-chars", type=int, default=32000, help="exact system+user size of each code review request (default 32000)")
    analyze.add_argument("--review-workers", type=int, default=2, help="parallel code review requests, 1-8 (default 2)")
    analyze.add_argument("--reduction-workers", type=int, choices=range(1, 11), default=1,
                         help="parallel summaries within each reduction level, 1-10 (default 1)")
    analyze.add_argument("--split-invalid-reduction-batches", action="store_true",
                         help="split an invalid summary batch after its one repair; bounded to two smaller-task levels")
    analyze.add_argument("--max-followups", type=int, default=32, help="typed follow-up reads after the base review, one generation (default 32)")
    analyze.add_argument("--business-request-chars", type=int, default=240000, help="exact system+user budget of each business request; not a verified model context window (default 240000)")
    analyze.add_argument('--business-context-tokens', type=int,
                         help='opt-in total input+maximum output token ceiling; needs a compatible local tokenizer')
    analyze.add_argument('--business-tokenizer', help='local tokenizer.json data; no downloads or remote code')
    analyze.add_argument("--digest-chars", type=int,
                         help="technical context budget within the business request; default 75%% of --business-request-chars; fitting results pass unchanged")
    analyze.add_argument("--map-mode", choices=("atomic", "coarse-refine"), default="atomic",
                         help="atomic (default): code-led map: bounded pages of verified command units propose actions "
                              "and source-shown transitions, then one composition request derives stages and facts; "
                              "coarse-refine: the earlier drafted map refined by catalog scope/refine requests")
    analyze.add_argument("--technical-relations", choices=("collect", "off"), default=None,
                         help="code review relation contract: collect (default in atomic mode) asks workers for typed "
                              "calls, dispatch, callbacks, branches, returns and handoffs with exact refs (a new "
                              "technical identity; earlier checkpoints are not reused); off keeps the original "
                              "technical requests and reuses their checkpoints, with relations not collected")
    analyze.add_argument("--approved-obligations", metavar="FILE",
                         help="atomic mode: human-approved obligations, one per line, matched against accepted code "
                              "operations and kept apart from the generated business text; never creates actions")
    analyze.add_argument("--atomic-page-units", type=int, default=10,
                         help="atomic mode: primary command units per page, 1-128 (default 10); called units join their "
                              "caller's page, and input/answer bounds may close a page earlier")
    analyze.add_argument("--atomic-answer-tokens", type=int, default=10000,
                         help="page-sizing answer estimate (default 10000); does not change the provider output cap")
    analyze.add_argument("--atomic-input-tokens", type=int, default=40000,
                         help="target input size per atomic page (default 40000); whole-request context checks still apply")
    analyze.add_argument("--atomic-input-tokens-hard", type=int, default=80000,
                         help="hard atomic input size including repair reserve (default 80000; maximum 1000000)")
    analyze.add_argument("--triage", choices=("auto", "off", "on"), default="auto",
                         help="atomic mode: descriptor-only triage before atomic pages; auto runs it only when the "
                              "planned pages exceed --refine-max-requests (default auto)")
    analyze.add_argument("--compose", choices=("on", "off"), default="off",
                         help="atomic mode: the composition request that derives stages and facts always runs; on also "
                              "asks it for display scenarios through accepted transitions (default off)")
    analyze.add_argument("--map-priors", metavar="RUN_DIR",
                         help="atomic mode: earlier analyze output whose process_map.json event mappings are shown on exact "
                              "emit sites as evidence-free hints; never operations or coverage")
    analyze.add_argument("--action-refinement", choices=("on", "off"), default=None,
                         help="coarse-refine mode only: refine drafted map stages from the action catalog in bounded "
                              "map-stage requests (default on); the catalog itself is always saved")
    analyze.add_argument("--refine-stage", action="append", default=[], metavar="STAGE",
                         help="coarse-refine mode only: refine only drafted stages whose id equals STAGE or whose name "
                              "contains it; repeatable; other stages' candidates are deferred explicitly")
    analyze.add_argument("--refine-max-requests", type=int, default=16,
                         help="planned business requests after the map-stage reply, 1-1000 (default 16; one repair each "
                              "is extra): scope/refine pages in coarse-refine mode, atomic/triage/compose pages in atomic "
                              "mode; work beyond it is deferred with IDs, never dropped")
    analyze.add_argument("--resume-from", help="earlier analyze output directory; validated review checkpoints are reused, results go to the new --out")
    analyze.add_argument("--plan-only", action="store_true", help="save the code graph and review plan without any model client or call")
    discovery = commands.add_parser("discover-logging", help="inventory logging sites without a model call")
    discovery.add_argument("--repo", required=True)
    discovery.add_argument("--out", required=True, help="new output directory")
    discovery.add_argument("--include", action="append")
    _scope_arguments(discovery)
    discovery.add_argument("--allow-partial", action="store_true")
    discovery.add_argument("--log-call", action="append", default=[])
    feedback=commands.add_parser("feedback",help="package local run artifacts and traces for manual feedback")
    feedback.add_argument("--run",required=True)
    feedback.add_argument("--out",required=True,help="new ZIP file")
    export = commands.add_parser("export-onboarding", help="write an onboarding draft packet from a saved analyze/demo run, without a model")
    export.add_argument("--run", required=True, help="earlier analyze or demo output directory; read only")
    export.add_argument("--out", required=True, help="new output directory outside the run, never overwritten")
    evaluate = commands.add_parser("evaluate-map", help="compare a saved run's map with a human-declared oracle offline, "
                                                        "without a model; overlaps are candidates, not semantic proof")
    evaluate.add_argument("--run", required=True, help="earlier analyze or demo output directory; read only")
    evaluate.add_argument("--oracle", required=True, help="operation-oracle/1 JSON file bound to the run's source snapshot")
    evaluate.add_argument("--out", required=True, help="new JSON result file outside the run, never overwritten")
    return parser


def _scope_arguments(parser):
    # The whole eligible scope is always read; these limits only abort before any model call.
    parser.add_argument("--max-files", type=int, help="safety limit: abort when more eligible files are found; never truncates")
    parser.add_argument("--max-chars", type=int, help="safety limit on numbered source characters: abort, never truncate (replaces the old 120000 cutoff)")
    parser.add_argument("--max-repo-bytes", type=int, help="safety limit on eligible source bytes: abort, never truncate")
    parser.add_argument("--max-file-bytes", type=int, default=512000, help="per-file limit; larger files are reported as unsupported inputs (default 512000)")


def _outside_repository(repo, out):
    """Refuse an output path inside the analysed repository before anything is written.

    Its artifacts would otherwise enter later snapshots as application source. Paths are resolved
    (symlinked parents, ``..``) and existing ancestors are also compared by file identity, which
    catches aliases such as case variants on case-insensitive file systems.
    """
    root = Path(repo).expanduser().resolve()
    try:
        identity = root.stat()
    except OSError:
        identity = None  # a missing repository is reported by the snapshot
    target = Path(out).expanduser().resolve()
    for candidate in (target, *target.parents):
        try:
            inside = candidate == root or (identity is not None and os.path.samestat(candidate.stat(), identity))
        except OSError:
            inside = False
        if inside:
            raise UserInputError("--out resolves inside the analyzed --repo, so its artifacts would become source "
                                 "input of later runs; choose an output directory outside the repository")


def _whole_snapshot(args):
    from .source_tools import snapshot_repository
    for name in ("max_files", "max_chars", "max_repo_bytes", "max_file_bytes"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise UserInputError(f"--{name.replace('_', '-')} must be a positive integer")
    snapshot = snapshot_repository(args.repo, includes=args.include, max_files=None, max_chars=None,
                                   allow_partial=args.allow_partial, logging_calls=args.log_call,
                                   max_file_bytes=args.max_file_bytes)
    files = [entry for entry in snapshot.inventory["entries"]
             if entry.get("kind") == "file" and entry.get("status") in ("eligible", "resource_unsupported")]
    measured = {"max_files": (len(files), "eligible source files"),
                "max_chars": (snapshot.scope["selected_chars"], "numbered source characters"),
                "max_repo_bytes": (sum(entry.get("bytes") or 0 for entry in files), "eligible source bytes")}
    for name, (value, label) in measured.items():
        limit = getattr(args, name)
        if limit is not None and value > limit:
            raise UserInputError(f"{value} {label} exceed --{name.replace('_', '-')} {limit}; nothing was truncated. "
                                 "Narrow --include or raise the limit")
    return snapshot


def _model_configs(path):
    from .llm import ModelConfig
    location=Path(path).expanduser().absolute()
    payload=strict_json_loads(location.read_text(encoding="utf-8"))
    if type(payload) is not dict or set(payload)!={"code","business"}:
        raise UserInputError("Model config must contain exactly code and business profiles")
    configs={}
    for role in ("code","business"):
        data=payload[role]
        config=ModelConfig.from_dict(data)
        resolved=config.to_dict()
        for name in ("ca_bundle","client_cert","client_key"):
            if resolved[name] is not None:
                resolved[name]=str((location.parent / resolved[name]).resolve())
        configs[role]=ModelConfig.from_dict(resolved)
    return configs


def _safe(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _safe(member) for key, member in value.items()}
    if isinstance(value, list):
        return [_safe(member) for member in value]
    return value


class _ProgressModel:
    def __init__(self, inner,role="business"):
        self.inner, self.calls, self.role = inner, [], role
        self._calls_lock = Lock()

    def complete_json(self, system, user):
        label = system.splitlines()[0] if system else "semantic task"
        print(f"[model:{self.role}] {label}", file=sys.stderr, flush=True)
        call = {"task": label,"role":self.role, "request_sha256": hashlib.sha256((system + "\n" + user).encode()).hexdigest(), "completed": False}
        with self._calls_lock:
            self.calls.append(call)
        try:
            result = self.inner.complete_json(system, user)
        except ValueError as exc:
            from .llm import LLMError
            with self._calls_lock:
                call["failure"] = {"category": exc.category, "status": exc.status} if isinstance(exc, LLMError) else {"category":"unusable_response"}
            raise
        with self._calls_lock:
            call["completed"] = True
        return result


def _business(path, goal):
    from .source_tools import blocked_path
    text = ""
    if path:
        source = Path(path)
        if source.is_symlink() or blocked_path(source.as_posix()):
            raise UserInputError("Business input cannot be a credential file or symlink")
        with source.open("rb") as handle:
            raw = handle.read(200001)
        if len(raw) > 200000:
            raise UserInputError("Business input exceeds 200000 bytes")
        text = raw.decode("utf-8")
    if goal:
        text = "Requested analysis focus: " + goal + "\n\n" + text
    return redact_text(text)


def _write_bundle(bundle, output):
    from .report import build_html, build_markdown
    names = {"manifest":"manifest.json", "process_map":"process_map.json", "log_coverage":"log_coverage.json",
             "instrumentation_plan":"instrumentation_plan.json", "metric_catalog":"metric_catalog.json"}
    for key, name in names.items():
        (output / name).write_text(json.dumps(bundle[key], ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "evidence.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n" for item in bundle.get("evidence", [])), encoding="utf-8")
    (output / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "diagnostics.json").write_text(json.dumps(bundle["diagnostics"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.html").write_text(build_html(bundle), encoding="utf-8")
    (output / "report.md").write_text(build_markdown(bundle), encoding="utf-8")


def _analyze(args):
    from .tracing import TraceRecorder
    output = Path(args.out).expanduser().absolute()
    if args.command == "analyze":
        _outside_repository(args.repo, output)
    if output.exists():
        raise UserInputError("Output directory already exists; choose a new --out path")
    output.mkdir(parents=True,exist_ok=False)
    trace=TraceRecorder(output)
    trace("run.started",{"command":args.command,"version":__version__})
    print(f"[trace] {trace.path}",file=sys.stderr,flush=True)
    try:
        code=_analyze_into(args,output,trace)
        trace("run.finished",{"exit_code":code})
        return code
    except BaseException as exc:
        trace("run.interrupted" if isinstance(exc,KeyboardInterrupt) else "run.failed",
              {"type":type(exc).__name__,"message":str(exc)})
        raise
    finally:
        trace.close()


def _model_fingerprint(config):
    """Endpoint identity for review checkpoints: no credentials, key variable names or TLS paths."""
    data = config.to_dict()
    identity = {key: data[key] for key in ("base_url", "model", "thinking", "max_output_tokens")}
    if data.get("extra_body"):
        identity["extra_body"] = data["extra_body"]
    return json.dumps(identity, sort_keys=True)


def _resume_dir(value):
    old = Path(value).expanduser().absolute()
    if old.is_symlink() or not old.is_dir():
        raise UserInputError("--resume-from must name an earlier analyze output directory")
    old = old.resolve()
    if not (old / "review" / "run.json").is_file():
        raise UserInputError("--resume-from has no saved code review checkpoints (review/run.json)")
    return old


def _map_priors(value):
    """Retrieval priors from an earlier run's saved process_map.json; read only, never trusted as evidence."""
    from .direct_atomic import priors_from_map
    run = Path(value).expanduser().absolute()
    from .review_reduction import _unlinked
    try:
        path = _unlinked(run / "process_map.json")
    except ValueError:
        raise UserInputError("--map-priors must not traverse symbolic links") from None
    if not path.is_file():
        raise UserInputError("--map-priors must name an earlier analyze output directory with process_map.json")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise UserInputError("--map-priors process_map.json exceeds 64 MiB")
    try:
        data = strict_json_loads(path.read_text(encoding="utf-8"))
        source = data.get("run_id") if type(data) is dict and type(data.get("run_id")) is str else "earlier-run"
        return priors_from_map(data, source=source)
    except ValueError as exc:
        raise UserInputError(f"--map-priors process_map.json is not usable: {exc}") from None


def _approved_obligations(value):
    """Human-approved obligations, one per line; read only; never generated or inferred."""
    from .direct_atomic import load_obligations
    from .review_reduction import _unlinked
    from .source_tools import blocked_path
    try:
        path = _unlinked(Path(value).expanduser().absolute())
    except ValueError:
        raise UserInputError("--approved-obligations must not traverse symbolic links") from None
    if not path.is_file() or blocked_path(path.as_posix()):
        raise UserInputError("--approved-obligations must name a readable plain text file")
    if path.stat().st_size > 1024 * 1024:
        raise UserInputError("--approved-obligations exceeds 1 MiB")
    try:
        return load_obligations(redact_text(path.read_text(encoding="utf-8")), source=path.name)
    except (ValueError, UnicodeError) as exc:
        raise UserInputError(f"--approved-obligations is not usable: {exc}") from None


def _sources(snapshot, trace):
    print(f"[sources] {snapshot.scope['selected_files']} files selected; partial={snapshot.scope['partial_input']}", file=sys.stderr, flush=True)
    trace("tool.snapshot_repository",{"scope":snapshot.scope,"fingerprints":snapshot.fingerprints})


def _review_plan(args, snapshot, guidance, directory, trace):
    """Graph and deterministic plan, saved before any model client exists."""
    from .code_review import ReviewOptions
    from .workflow import prepare_review, save_json
    # Relations are collected by default only for the code-led atomic map; "off" keeps the original technical
    # request bytes and fingerprints, so their checkpoints stay reusable (relations then not collected).
    relations = args.technical_relations == "collect" or (args.technical_relations is None and args.map_mode == "atomic")
    options = ReviewOptions(request_chars=args.review_request_chars, workers=args.review_workers,
                            max_followups=args.max_followups, relations=relations)
    prepared = prepare_review(snapshot, guidance, options)
    plan, summary = prepared["plan"], prepared["plan"].summary
    save_json(directory / "code_graph.json", prepared["graph"].to_dict())
    save_json(directory / "logging_inventory.json", {**prepared["logging_inventory"], "source_scope": snapshot.scope,
                                                     "snapshot_inventory": snapshot.inventory})
    trace("review.planned", {"plan_id": plan.plan_id, "run_fingerprint": plan.run_fingerprint, "budget": plan.budget,
                             "summary": summary, "guidance_excerpt": prepared["guidance_excerpt"]})
    print(f"[review] {summary['tasks']} base requests for {summary['snapshot_files']} files and {summary['lines_total']} lines; "
          f"{summary['request_chars_total']} request characters; full coverage planned: {summary['planned_coverage_complete']}",
          file=sys.stderr, flush=True)
    return options, prepared


def _analyze_into(args,output,trace):
    from .demo import RecordedModel
    from .llm import OpenAICompatibleModel
    from .source_tools import snapshot_repository
    from .telemetry import profile_telemetry
    from .workflow import ReviewSettings, plan_record, run_analysis, save_json
    from .guidance import load_guidance
    business_input=None
    review=None
    if args.command == "demo":
        # The strict four-call synthetic replay keeps the legacy single code request.
        example = ROOT / "examples/synthetic"
        snapshot = snapshot_repository(example, includes=["app.py"])
        _sources(snapshot, trace)
        business = _business(example / "business.md", "")
        telemetry = None
        guidance = load_guidance()
        inner = RecordedModel(example / "model-responses.json")
        code_inner=inner
        run_id, origin, mode, model_meta = "synthetic-demo", "synthetic", "fake", {"adapter":"fingerprint-bound synthetic replay"}
        model_config = model_meta
    else:
        if args.plan_only and args.resume_from:
            raise UserInputError("--plan-only makes no model call; do not combine it with --resume-from")
        if args.business_request_chars <= 1000:
            raise UserInputError("--business-request-chars must be greater than 1000 to leave room for the technical digest")
        if (args.business_context_tokens is None) != (args.business_tokenizer is None):
            raise UserInputError('--business-context-tokens and --business-tokenizer must be provided together')
        digest_chars = args.digest_chars if args.digest_chars is not None else max(1000, args.business_request_chars * 3 // 4)
        if not 1000 <= digest_chars < args.business_request_chars:
            raise UserInputError("--digest-chars must be at least 1000 and smaller than --business-request-chars")
        from .workflow import MAX_REFINE_REQUESTS
        if not 1 <= args.refine_max_requests <= MAX_REFINE_REQUESTS:
            raise UserInputError(f"--refine-max-requests must be between 1 and {MAX_REFINE_REQUESTS}")
        if any(not item.strip() for item in args.refine_stage):
            raise UserInputError("--refine-stage needs a nonblank stage id or name fragment")
        from .workflow import MAX_ATOMIC_PAGE_UNITS
        if not 1 <= args.atomic_page_units <= MAX_ATOMIC_PAGE_UNITS:
            raise UserInputError(f"--atomic-page-units must be between 1 and {MAX_ATOMIC_PAGE_UNITS}")
        if not 1 <= args.atomic_input_tokens <= args.atomic_input_tokens_hard <= 1000000:
            raise UserInputError("atomic input bounds need 1 <= --atomic-input-tokens <= "
                                 "--atomic-input-tokens-hard <= 1000000")
        if not 1 <= args.atomic_answer_tokens <= args.atomic_input_tokens_hard:
            raise UserInputError("--atomic-answer-tokens must be between 1 and --atomic-input-tokens-hard")
        atomic = args.map_mode == "atomic"
        if atomic and (args.action_refinement is not None or args.refine_stage):
            raise UserInputError("--action-refinement and --refine-stage apply only to --map-mode coarse-refine")
        if not atomic and args.map_priors:
            raise UserInputError("--map-priors applies only to --map-mode atomic")
        if not atomic and args.approved_obligations:
            raise UserInputError("--approved-obligations applies only to --map-mode atomic")
        priors = _map_priors(args.map_priors) if args.map_priors else None
        obligations = _approved_obligations(args.approved_obligations) if args.approved_obligations else None
        resume = _resume_dir(args.resume_from) if args.resume_from else None
        snapshot = _whole_snapshot(args)
        _sources(snapshot, trace)
        guidance = load_guidance()
        directory = output.resolve()  # checkpoint writers refuse symlinked ancestors such as macOS /var
        options, prepared = _review_plan(args, snapshot, guidance, directory, trace)
        summary = prepared["plan"].summary
        if args.plan_only:
            save_json(directory / "code_review.json", {"mode": "plan_only", **plan_record(prepared["plan"], full=True),
                                                       "guidance_excerpt": prepared["guidance_excerpt"]})
            trace("review.plan_only", {"model_calls": 0})
            print(json.dumps({"plan": str(output / "code_review.json"), "tasks": summary["tasks"],
                              "request_chars_total": summary["request_chars_total"],
                              "planned_coverage_complete": summary["planned_coverage_complete"],
                              "incomplete_reasons": summary["incomplete_reasons"]}, ensure_ascii=False))
            return 0 if summary["planned_coverage_complete"] else 1
        unsupported = summary["lines_by_state"].get("unsupported", 0)
        if unsupported and not args.allow_partial:
            raise UserInputError(f"{unsupported} source lines exceed the review request budget and cannot be supplied; "
                                 "raise --review-request-chars or use --allow-partial (inspect with --plan-only)")
        if args.process:
            if args.business_file:
                raise UserInputError("Use --process or --business-file, not both")
            from .process_inputs import load_process_preset
            text,business_input=load_process_preset(args.process)
            goal=args.goal or business_input['process_id']+' — '+business_input['process_name']
            business=redact_text('Business source metadata: '+json.dumps(business_input,ensure_ascii=False,sort_keys=True)
                                 +'\n\nRequested analysis focus: '+goal+'\n\n'+text)
        else:
            business = _business(args.business_file, args.goal)
        for path in args.method_file:
            business += "\n\nAdditional unreviewed methodology source:\n" + _business(path, "")
        if len(business) > 200000:
            raise UserInputError("Combined business/methodology text exceeds 200000 characters")
        if business_input is not None:
            business_input={**business_input,"forwarded_text_sha256":hashlib.sha256(business.encode('utf-8')).hexdigest()}
            trace('input.business_loaded',business_input)
            print(f"[process] {business_input['process_id']}: {business_input['path']} ({business_input['bytes']} bytes)",file=sys.stderr,flush=True)
        field_map = strict_json_loads(Path(args.field_map).read_text(encoding="utf-8")) if args.field_map else None
        telemetry = profile_telemetry(args.logs, field_map) if args.logs else None
        configs = _model_configs(args.config)
        # A page-sizing estimate cannot promise more answer space than this model can emit.
        atomic_answer_tokens = min(args.atomic_answer_tokens, configs['business'].max_output_tokens)
        if atomic:
            trace('review.atomic_answer_budget', {'requested': args.atomic_answer_tokens,
                  'effective': atomic_answer_tokens, 'provider_max_output_tokens': configs['business'].max_output_tokens,
                  'basis': 'page-sizing estimate only; reasoning also consumes the provider output budget'})
        token_budget = None
        if args.business_context_tokens is not None:
            from .token_budget import TokenBudget
            token_budget = TokenBudget.from_file(args.business_tokenizer, context_tokens=args.business_context_tokens,
                                                 output_tokens=configs['business'].max_output_tokens)
            trace('review.business_token_budget', token_budget.identity())
        clients={}
        for role,config in configs.items():
            if config.api_key_env:trace.protect_secret(os.environ.get(config.api_key_env))
            def observe(event,data,role=role,config=config):
                trace(event,{**data,"role":role,"model":config.model})
            clients[role]=OpenAICompatibleModel(config,observer=observe)
        inner,code_inner=clients['business'],clients['code']
        run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
        origin, mode = "real", "live"
        model_meta = {role:{"model":config.model,"base_url":config.base_url,"thinking":config.thinking,
                            "max_output_tokens":config.max_output_tokens} for role,config in configs.items()}
        model_config = {role:config.to_dict() for role,config in configs.items()}
        review = ReviewSettings(graph=prepared["graph"], plan=prepared["plan"], system=prepared["system"],
                                options=options, directory=directory,
                                model_fingerprint=_model_fingerprint(configs["code"]), resume_from=resume,
                                business_model_fingerprint=_model_fingerprint(configs["business"]),
                                business_request_chars=args.business_request_chars,
                                digest_chars=digest_chars,
                                reduction_workers=args.reduction_workers,
                                split_invalid_batches=args.split_invalid_reduction_batches,
                                action_refinement=not atomic and args.action_refinement != "off",
                                refine_stages=tuple(args.refine_stage),
                                refine_max_requests=args.refine_max_requests,
                                business_token_budget=token_budget,
                                map_mode="atomic" if atomic else "coarse_refine",
                                atomic_page_units=args.atomic_page_units, triage=args.triage,
                                atomic_input_tokens=args.atomic_input_tokens,
                                atomic_input_tokens_hard=args.atomic_input_tokens_hard,
                                atomic_answer_tokens=atomic_answer_tokens,
                                compose=args.compose == "on", map_priors=priors,
                                approved_obligations=obligations)
        if resume is not None:
            trace("review.resume", {"from": str(resume), "note": "validated checkpoints only; business stages rerun, "
                                    "except atomic pages whose exact request identity has a saved accepted reply"})
    model = _ProgressModel(inner)
    code_model = _ProgressModel(code_inner,"code")
    bundle = run_analysis(snapshot, model, run_id=run_id, business_text=business, telemetry=telemetry,
                          data_origin=origin, model_mode=mode, guidance=guidance, trace=trace,code_model=code_model,
                          review=review)
    provenance = bundle["manifest"]["provenance"]
    provenance["config_sha256"] = hashlib.sha256(json.dumps({
        "workflow_config_sha256":provenance["config_sha256"], "model_config":model_config,
        "source_scope":snapshot.scope, "telemetry_fields":telemetry.get("source_fields") if telemetry else None
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    bundle.setdefault("scope", {})["telemetry_profile"] = telemetry
    if business_input is not None:bundle["scope"]["business_input"]=business_input
    bundle["diagnostics"] = {"tool_version":__version__, "python_version":platform.python_version(),
                             "model":model_meta, "model_calls":sorted([*code_model.calls,*model.calls],key=lambda call:(call['role']!='code',call['request_sha256'])), "scope":snapshot.scope,
                             "stage_states":bundle["manifest"]["stages"],
                             # Per-stage attempts with stable codes, field paths and request hashes, repaired ones included.
                             "validation":bundle["validation"]}
    if business_input is not None:bundle["diagnostics"]["business_input"]=business_input
    if review is not None:
        bundle["diagnostics"]["review"] = {
            "code_review": {key: bundle["code_review"][key] for key in ("complete", "incomplete_reasons", "summary")},
            "code_reduction": {"complete": bundle["code_reduction"]["complete"], "errors": bundle["code_reduction"]["errors"],
                               "residual_count": (bundle["code_reduction"]["residual"] or {}).get("count")},
            "resumed_from": None if review.resume_from is None else str(review.resume_from),
            "checkpoint_compatibility": bundle["code_review"]["summary"].get("checkpoint_compatibility"),
            "action_catalog": bundle["action_catalog"]["accounting"]}
        for key in ("action_refinement", "atomic_map"):
            if type(bundle.get(key)) is dict:
                bundle["diagnostics"]["review"][key] = {name: bundle[key][name]
                                                        for name in ("status", "status_reasons", "summary")}
        if type(bundle.get("command_units")) is dict:
            bundle["diagnostics"]["review"]["command_units"] = bundle["command_units"]
    from .report_sources import collect_report_sources
    bundle["report_sources"] = collect_report_sources(snapshot, bundle)
    bundle = _safe(bundle)
    _write_bundle(bundle, output)
    trace("artifacts.saved",{"files":sorted(p.name for p in output.iterdir())})
    partial = bundle["scope"].get("partial_input") is True
    refinement = (bundle.get("action_refinement") or {}).get("status")
    atomic = (bundle.get("atomic_map") or {}).get("status")
    summary = {"run_id":run_id, "report":str(output / "report.html"),
               "diagnostics":str(output / "diagnostics.json"), "partial_input":partial}
    if refinement is not None:
        summary["action_refinement"] = refinement
    if atomic is not None:
        summary["atomic_map"] = atomic
    print(json.dumps(summary, ensure_ascii=False))
    incomplete = any(stage["status"] in {"blocked", "incomplete"} for stage in bundle["manifest"]["stages"].values())
    # Partial scope, partial/failed action refinement or atomic work is reported with exit code 1 even when every
    # stage completed: drafts of later stages do not make missing atomic decisions complete.
    return 1 if (incomplete or partial or refinement in {"partial", "failed"}
                 or atomic in {"partial", "failed", "not_run"}) else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'review-logging':
            from .logging_cli import run_logging_review
            result = run_logging_review(args)
            print(json.dumps(result, ensure_ascii=False))
            if result.get('interrupted'): return 130
            return 0 if result['status'] == 'completed' else 1
        if args.command == 'export-logging':
            from .logging_report import export_logging
            result = export_logging(args.run, args.out)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['status'] == 'completed' else 1
        if args.command == "status":
            print(json.dumps(status_report(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "process-info":
            from .process_inputs import load_process_preset
            _,metadata=load_process_preset(args.process)
            print(json.dumps(metadata,ensure_ascii=False,indent=2))
            return 0
        if args.command == "feedback":
            from .tracing import package_feedback
            print("Feedback ZIP includes model messages with selected source code and business text; nothing is uploaded.", file=sys.stderr)
            count=package_feedback(args.run,args.out)
            print(json.dumps({"feedback":str(Path(args.out).absolute()),"files":count},ensure_ascii=False))
            return 0
        if args.command == "export-onboarding":
            from .onboarding_export import export_onboarding
            result = export_onboarding(args.run, args.out)
            print(json.dumps(result, ensure_ascii=False))
            # Like analyze, a partial result is written but reported with exit code 1.
            return 0 if result["status"] == "all_stages_completed" else 1
        if args.command == "evaluate-map":
            from .map_evaluation import evaluate_run
            result = evaluate_run(args.run, args.oracle, args.out)
            print(json.dumps({"evaluation": str(Path(args.out).expanduser().absolute()), "status": result["status"],
                              "counts": result["counts"], "semantic_verification": False}, ensure_ascii=False))
            # A foreign snapshot grades nothing and is reported with exit code 1, like other partial results.
            return 0 if result["source_snapshot_verified"] else 1
        if args.command == "check-model":
            from .llm import OpenAICompatibleModel
            configs = _model_configs(args.config)
            checked=[]
            for role,config in configs.items():
                if args.role not in (role,'all'):continue
                print(f"[check-model:{role}] {config.model}",file=sys.stderr,flush=True)
                result=OpenAICompatibleModel(config).complete_json('Return only a JSON object: {"ok":true}', "Minimal connection check; no repository content.")
                if result.get("ok") is not True:
                    raise UserInputError(f"Model role {role} did not return the expected health-check JSON")
                checked.append({"role":role,"model":config.model,"base_url":config.base_url})
            print(json.dumps({"ok":True,"models":checked}))
            return 0
        if args.command == "discover-logging":
            from .log_discovery import discover_logging
            output = Path(args.out).expanduser().absolute()
            _outside_repository(args.repo, output)
            if output.exists():
                raise UserInputError("Output directory already exists; choose a new --out path")
            snapshot = _whole_snapshot(args)
            inventory = discover_logging(snapshot, call_names=snapshot.logging_calls, max_sites=None,
                                         max_per_file=None, max_hints=None)
            inventory["source_scope"] = snapshot.scope
            inventory["input_fingerprints"] = snapshot.fingerprints
            output.mkdir(parents=True, exist_ok=False)
            target = output / "logging_inventory.json"
            target.write_text(json.dumps(_safe(inventory), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"inventory":str(target), **inventory["scope"]}, ensure_ascii=False))
            # Like analyze, a partial source scope is written but reported with exit code 1.
            return 1 if snapshot.scope["partial_input"] else 0
        return _analyze(args)
    except (ValueError, OSError, UnicodeError) as exc:
        # Expected errors never dump raw model content, credentials or prompts.
        from .llm import LLMError
        from .contracts import ContractError
        from .code_review import ReviewPlanError
        from .onboarding_export import ExportError
        from .review_execution import ReviewExecutionError
        from .review_reduction import ReductionError
        from .source_tools import SourceError
        from .telemetry import TelemetryError
        from .workflow import WorkflowError
        from .logging_contracts import LoggingReviewError
        known = (LLMError, ContractError, SourceError, TelemetryError, UserInputError, ReviewPlanError,
                 ReviewExecutionError, ReductionError, WorkflowError, ExportError, LoggingReviewError)
        detail = str(exc) if isinstance(exc, known) else type(exc).__name__
        print(f"Error: {redact_text(detail)}. Check configuration, input scope and --out; see docs/PILOT.md.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped by user; no automatic retry or fake fallback.", file=sys.stderr)
        return 130
