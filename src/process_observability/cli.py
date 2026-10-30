"""One product pipeline: Belka map, logging, changes, metric definitions and HTML."""
import argparse
import json
import os
from pathlib import Path
import sys
from . import __version__
from .redaction import redact_text

class UserInputError(ValueError):
    pass

def status_report():
    return {"package":"process_observability", "version":__version__, "state":"integrated_product",
            "default_command":"onboard", "map_producer":"belka",
            "stages":["map", "logging", "changes", "metrics", "report"],
            "metrics":"definitions_only", "semantic_verification":False}

def _logging_options(parser, *, source_scope=True):
    parser.add_argument('--config', help='explicit common model profile (or business profile)')
    parser.add_argument('--inventory-only', action='store_true', help='no model calls; requires a published map')
    parser.add_argument('--max-requests', type=int, default=128)
    parser.add_argument('--request-chars', type=int, default=180000)
    parser.add_argument('--max-navigation-turns', type=int, default=4)
    parser.add_argument('--max-context-lines', type=int, default=1600)
    parser.add_argument('--stop-on-failed-repair', action='store_true')
    parser.add_argument('--log-call', action='append', default=[])
    if source_scope:
        parser.add_argument('--include', action='append')
        parser.add_argument('--allow-partial', action='store_true')
        _scope_arguments(parser)

def build_parser():
    parser=argparse.ArgumentParser(prog='process-observability', description='Сценарий процесса: карта Белки, логи, доработки, метрики и автономный HTML.')
    parser.add_argument('--version', action='version', version=__version__)
    commands=parser.add_subparsers(dest='command', required=True)
    onboard=commands.add_parser('onboard', help='единый полный запуск')
    entry=onboard.add_mutually_exclusive_group(required=True)
    entry.add_argument('--scenario-text')
    entry.add_argument('--process-file')
    entry.add_argument('--map', help='продолжить с опубликованной карты')
    entry.add_argument('--resume-from', help='предыдущая папка полного запуска')
    onboard.add_argument('--source', action='extend', nargs='+', default=[])
    onboard.add_argument('--out', required=True)
    onboard.add_argument('--max-steps', type=int, default=100)
    onboard.add_argument('--max-seconds', type=float, default=7200)
    onboard.add_argument('--model-timeout', type=float, default=900)
    _logging_options(onboard, source_scope=False)
    export=commands.add_parser('export', help='собрать полный HTML из сохранённых результатов без модели')
    export.add_argument('--run', required=True); export.add_argument('--out', required=True)
    logging=commands.add_parser('review-logging', help='отдельный этап анализа готовой карты')
    logging.add_argument('--map', required=True); logging.add_argument('--repo', required=True)
    logging.add_argument('--out', required=True); logging.add_argument('--hierarchy')
    logging.add_argument('--artifacts'); logging.add_argument('--source-root', action='append', default=[])
    logging.add_argument('--resume-from')
    _logging_options(logging)
    logging_export=commands.add_parser('export-logging', help='пересобрать результат этапа логов')
    logging_export.add_argument('--run', required=True); logging_export.add_argument('--out', required=True)
    discovery=commands.add_parser('discover-logging', help='инвентаризация мест логирования без модели')
    discovery.add_argument('--repo', required=True); discovery.add_argument('--out', required=True)
    discovery.add_argument('--include', action='append'); discovery.add_argument('--allow-partial', action='store_true')
    discovery.add_argument('--log-call', action='append', default=[]); _scope_arguments(discovery)
    commands.add_parser('status', help='состав продукта')
    check=commands.add_parser('check-model', help='явная проверка общего модельного профиля')
    check.add_argument('--config', required=True)
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

def _safe(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _safe(member) for key, member in value.items()}
    if isinstance(value, list):
        return [_safe(member) for member in value]
    return value

def _model_fingerprint(config):
    """Endpoint identity for review checkpoints: no credentials, key variable names or TLS paths."""
    data = config.to_dict()
    identity = {key: data[key] for key in ("base_url", "model", "thinking", "max_output_tokens")}
    if data.get("extra_body"):
        identity["extra_body"] = data["extra_body"]
    return json.dumps(identity, sort_keys=True)

def main(argv=None):
    args=build_parser().parse_args(argv)
    try:
        if args.command=='onboard':
            from .pipeline import run_product
            result=run_product(args)
        elif args.command=='export':
            from .pipeline import export_product
            result=export_product(args.run,args.out)
        elif args.command=='review-logging':
            from .logging_cli import run_logging_review
            result=run_logging_review(args)
        elif args.command=='export-logging':
            from .logging_report import export_logging
            result=export_logging(args.run,args.out)
        elif args.command=='status':
            print(json.dumps(status_report(),ensure_ascii=False,indent=2)); return 0
        elif args.command=='check-model':
            from .logging_cli import _business_config
            from .llm import OpenAICompatibleModel
            config=_business_config(args.config)
            reply=OpenAICompatibleModel(config).complete_json('Return only a JSON object: {"ok":true}', 'Minimal connection check; no repository content.')
            if reply.get('ok') is not True: raise UserInputError('Model returned an invalid health response')
            print(json.dumps({'ok':True,'model':config.model})); return 0
        elif args.command=='discover-logging':
            from .log_discovery import discover_logging
            output=Path(args.out).absolute(); _outside_repository(args.repo,output)
            if output.exists(): raise UserInputError('Choose a new output directory')
            snapshot=_whole_snapshot(args)
            inventory=discover_logging(snapshot,call_names=snapshot.logging_calls,max_sites=None,max_per_file=None,max_hints=None)
            inventory['source_scope']=snapshot.scope; inventory['input_fingerprints']=snapshot.fingerprints
            output.mkdir(parents=True,exist_ok=False)
            target=output/'logging_inventory.json'
            target.write_text(json.dumps(_safe(inventory),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'inventory':str(target),**inventory['scope']},ensure_ascii=False))
            return 1 if snapshot.scope['partial_input'] else 0
        print(json.dumps(result,ensure_ascii=False))
        if result.get('interrupted'): return 130
        return 0 if result.get('status')=='completed' else 1
    except (ValueError,OSError,UnicodeError) as exc:
        from .llm import LLMError
        from .contracts import ContractError
        from .source_tools import SourceError
        from .logging_contracts import LoggingReviewError
        known=(LLMError,ContractError,SourceError,UserInputError,LoggingReviewError)
        detail=str(exc) if isinstance(exc,known) else type(exc).__name__
        print('Error: '+redact_text(detail)+'. Check configuration, input scope and --out.',file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Stopped by user.',file=sys.stderr); return 130
