"""The map-first entrypoint: existing map → logging, coverage and proposals only."""
import hashlib
import json
from pathlib import Path
import sys

from .contracts import strict_json_loads
from .logging_contracts import CONTRACTS_SCHEMA, LoggingReviewError, canonical, validate_logging_contracts
from .logging_review import ReviewOptions, review_logging
from .logging_report import write_report
from .map_input import load_hierarchy, load_map, read_regular

CONTRACTS_FILE = 'logging_contracts.json'


def load_logging_contracts(folder):
    """Optional operator declaration of external logging APIs (schema logging-contracts/1) in the artifacts folder.

    It is auditable input recorded with its SHA-256; nothing is inferred from method names. Returns (contracts, sha|None).
    """
    path = Path(folder) / CONTRACTS_FILE
    if not path.exists(): return [], None
    raw = read_regular(path)
    payload = strict_json_loads(raw)
    if type(payload) is not dict or payload.get('schema_version') != CONTRACTS_SCHEMA:
        raise LoggingReviewError(CONTRACTS_FILE + ' must declare schema_version ' + CONTRACTS_SCHEMA)
    return validate_logging_contracts(payload), hashlib.sha256(raw.encode()).hexdigest()


def _business_config(path):
    from .llm import ModelConfig
    location = Path(path).absolute()
    payload = strict_json_loads(read_regular(location))
    if type(payload) is not dict: raise LoggingReviewError('Model config must be a JSON object')
    profile = dict(payload['business']) if 'business' in payload else payload
    for key in ('ca_bundle', 'client_cert', 'client_key'):
        if profile.get(key) is not None: profile[key] = str((location.parent / profile[key]).absolute())
    return ModelConfig.from_dict(profile)


def run_logging_review(args):
    from .cli import _outside_repository, _whole_snapshot
    from .llm import OpenAICompatibleModel
    output = Path(args.out).absolute()
    _outside_repository(args.repo, output)
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise LoggingReviewError('Choose a new output directory without symbolic links')
    if not args.inventory_only and not args.config:
        raise LoggingReviewError('Provide --config for semantic review, or explicitly choose --inventory-only')
    view = load_map(args.map)
    source_roots = {}
    for pair in args.source_root:
        key, sep, value = pair.partition('=')
        if not sep or not key or key in source_roots: raise LoggingReviewError('Expected unique SOURCE_ID=SUBDIR mappings')
        source_roots[key] = value
    hierarchy_path = args.hierarchy or str(Path(args.map).parent/'map_hierarchy.json')
    hierarchy, warnings = load_hierarchy(hierarchy_path, view)
    options = ReviewOptions(max_requests=args.max_requests, request_chars=args.request_chars,
                            max_navigation_turns=args.max_navigation_turns, max_context_lines=args.max_context_lines,
                            stop_on_failed_repair=args.stop_on_failed_repair)
    snapshot = _whole_snapshot(args)
    artifacts = args.artifacts or Path(args.map).parent
    contracts, contracts_sha = load_logging_contracts(artifacts)
    previous = None
    if args.resume_from:
        source = Path(args.resume_from)
        if source.is_dir():
            source /= 'logging_review.checkpoint.json' if (source/'logging_review.checkpoint.json').exists() else 'logging_review.json'
        previous = strict_json_loads(read_regular(source))
    attempts = []
    def tracked_transport(url, payload, headers, tls):
        from .llm import urllib_transport, LLMError
        if len(attempts) >= args.max_requests:
            raise LLMError('Physical transport attempt budget exhausted', category='request_budget')
        row = {'request_sha256': hashlib.sha256((payload['messages'][0]['content'] + '\n' + payload['messages'][1]['content']).encode()).hexdigest(),
               'attempt': len(attempts) + 1, 'completed': False}
        attempts.append(row)
        try: response = urllib_transport(url, payload, headers, tls)
        except BaseException as exc:
            row['failure_type'] = type(exc).__name__
            raise
        row['completed'] = True
        row['http_status'] = getattr(response, 'status', None)
        return response
    model = None if args.inventory_only or not view.operation_ids else OpenAICompatibleModel(_business_config(args.config), transport=tracked_transport)
    # Own this fresh directory before dispatch; preserve an accepted prefix if the process dies.
    output.mkdir(parents=True, exist_ok=False)
    checkpoint_path = output/'logging_review.checkpoint.json'
    def checkpoint(data):
        temporary = output/'.logging_review.checkpoint.tmp'
        with temporary.open('x', encoding='utf-8') as stream: stream.write(canonical(data) + '\n')
        if checkpoint_path.is_symlink():
            raise LoggingReviewError('Checkpoint path was replaced by a symbolic link')
        temporary.replace(checkpoint_path)
    def progress(state):
        print(f"[logging] {state['completed']}/{state['total']}: {state['operation_id']}", file=sys.stderr, flush=True)
    result = review_logging(view, snapshot, model, options=options, source_roots=source_roots,
                            artifacts=artifacts, resume=previous, on_progress=progress,
                            on_checkpoint=checkpoint, logging_contracts=contracts)
    result['hierarchy'] = hierarchy
    result['presentation_warnings'] = warnings
    result['mode'] = 'inventory_only' if args.inventory_only else 'semantic_review'
    result['transport_attempts'] = attempts
    source_artifacts = {Path(args.map).name: view.file_sha256}
    if hierarchy is not None:
        source_artifacts[Path(hierarchy_path).name] = hashlib.sha256(read_regular(hierarchy_path).encode()).hexdigest()
    if contracts_sha is not None:
        source_artifacts[CONTRACTS_FILE] = contracts_sha
    return write_report(result, output, source_artifacts=source_artifacts, owned_output=True)
