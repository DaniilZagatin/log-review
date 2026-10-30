"""One durable pipeline: Belka -> logging and changes -> metric definitions -> HTML."""
from __future__ import annotations
import hashlib
import html
import json
import math
import os
from pathlib import Path
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from .contracts import strict_json_loads
from .logging_contracts import canonical
from .map_input import MapView, load_map, load_hierarchy, read_regular

SCHEMA = 'scenario-product/1'


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.tmp')
    if path.is_symlink() or temp.exists():
        raise OSError('Unsafe or unfinished publication target')
    with temp.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(canonical(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def _read(path):
    return strict_json_loads(read_regular(path))


def _safe_destination(out, sources=()):
    out = Path(out).absolute()
    if out.exists() or any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (out, *out.parents)):
        raise ValueError('Choose a new output directory without links or junctions')
    for source in sources:
        root = Path(source).resolve()
        if out.resolve().is_relative_to(root) or root.is_relative_to(out.resolve()):
            raise ValueError('Output and source directories must be separate')
    return out


class RunBudget:
    def __init__(self, limit, seconds, manifest, save):
        if type(limit) is not int or limit < 1 or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('Positive finite request and time limits required')
        self.limit, self.deadline = limit, time.monotonic() + seconds
        self.manifest, self.save = manifest, save

    def reserve(self, stage):
        from .llm import LLMError
        if len(self.manifest['requests']) >= self.limit:
            raise LLMError('Product request budget exhausted', category='request_budget')
        if time.monotonic() >= self.deadline:
            raise LLMError('Product time budget exhausted', category='request_budget')
        row = {'attempt': len(self.manifest['requests']) + 1, 'stage': stage, 'status': 'started'}
        self.manifest['requests'].append(row)
        self.save()  # no network call if durable reservation fails
        return row


class ProductTransport:
    def __init__(self, budget, timeout):
        self.budget, self.timeout, self.stage = budget, timeout, 'map'

    def __call__(self, url, payload, headers, tls):
        from .llm import LLMError, TransportResponse, MAX_RESPONSE_BYTES
        import httpx
        row = self.budget.reserve(self.stage)
        remaining = self.budget.deadline - time.monotonic()
        try:
            with httpx.Client(verify=tls if tls is not None else True, follow_redirects=False,
                              timeout=min(self.timeout, max(.01, remaining))) as client:
                with client.stream('POST', url, json=payload, headers=headers) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES or time.monotonic() >= self.budget.deadline:
                            raise LLMError('Response limit exceeded', category='request_budget')
                        chunks.append(chunk)
                    result = TransportResponse(response.status_code, b''.join(chunks))
            row.update(status='received', http_status=result.status)
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__)
            self.budget.save()
            if isinstance(exc, LLMError):
                raise
            raise LLMError('Product transport failed', category='connection') from None
        self.budget.save()
        return result


class BelkaClient:
    """Tool-call bridge through the same verified TLS transport and physical budget."""
    def __init__(self, model):
        self.model = model

    def chat(self, messages, temperature=0, max_tokens=8192, tools=None, enable_thinking=None):
        from pko.llm.client import ChatResult
        from .llm import _successful_body
        payload = {'model': self.model.config.model, 'messages': messages, 'temperature': temperature,
                   'max_tokens': min(max_tokens, self.model.config.max_output_tokens), 'stream': False}
        if tools:
            payload.update(tools=tools, tool_choice='auto')
        thinking = self.model.config.thinking if enable_thinking is None else enable_thinking
        if thinking is not None:
            payload['chat_template_kwargs'] = {'enable_thinking': thinking}
        payload.update(self.model.config.to_dict().get('extra_body') or {})
        body = strict_json_loads(_successful_body(self.model._transport(self.model._url, payload, self.model._headers(), self.model._ssl_context)).decode('utf-8'))
        choices = body.get('choices')
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError('Expected one model choice')
        choice = choices[0]
        message = choice.get('message') or {}
        return ChatResult(text=message.get('content') or '',
                          usage={k: v for k, v in (body.get('usage') or {}).items() if type(v) is int},
                          tool_calls=message.get('tool_calls'), finish_reason=choice.get('finish_reason') or '')


def _publish(out, manifest, logging=None, metrics=None, model_map=None, hierarchy=None):
    from .product_report import build_product_html, build_pending_html
    if logging is not None:
        if metrics is None:
            from .metrics import build_metrics
            metrics = build_metrics(logging)
            metrics['status'] = 'partial'
            metrics['errors'] = ['Этап метрик не завершён; показаны предварительные шаблоны']
            _write(out / 'metric_catalog.json', metrics)
        document = build_product_html(logging, metrics, manifest=manifest)
    else:
        document = build_pending_html(model_map, hierarchy=hierarchy, manifest=manifest)
    path = out / 'report.html'
    temp = out / '.report.html.tmp'
    with temp.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(document)
    temp.replace(path)
    _write(out / 'product_run.json', manifest)
    names = ['product_run.json', 'report.html'] + [n for n in ('scenario_map.json', 'map_hierarchy.json', 'logging_review.json', 'metric_catalog.json') if (out / n).is_file()]
    sums = {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in names}
    _write(out / 'product_checksums.json', sums)
    return {'status': manifest['status'], 'report': str(path), 'manifest': str(out / 'product_run.json'), 'interrupted': manifest.get('interrupted', False)}


def run_product(args, *, belka_runner=None, logging_model=None):
    """Run or resume into a fresh directory. Test injection never affects the public CLI."""
    from pko.scenario_map.sources import build_workspace, read_process_description
    from pko.scenario_map.runner import run_scenario
    from pko.llm.registry import ModelSpec
    from . import __version__
    from pko.build import build_identity
    from .logging_cli import _business_config, load_logging_contracts
    from .llm import OpenAICompatibleModel
    from .logging_review import review_logging, ReviewOptions
    from .metrics import build_metrics
    if type(args.max_steps) is not int or args.max_steps < 1 or not math.isfinite(args.model_timeout) or args.model_timeout <= 0:
        raise ValueError('Positive finite step and timeout limits required')
    if type(args.max_requests) is not int or args.max_requests < 1 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
        raise ValueError('Positive finite request and time limits required')
    sources = list(getattr(args, 'source', None) or [])
    previous, previous_dir = None, None
    if getattr(args, 'resume_from', None):
        previous_dir = Path(args.resume_from)
        if previous_dir.is_file(): previous_dir = previous_dir.parent
        previous = _read(previous_dir / 'product_run.json')
        if previous.get('schema_version') != SCHEMA: raise ValueError('Unsupported product checkpoint')
        sources = sources or previous['source_paths']
    if not sources: raise ValueError('At least one source directory or ZIP is required')
    if getattr(args, 'inventory_only', False) and not (getattr(args, 'map', None) or previous):
        raise ValueError('Inventory mode requires an existing map')
    config = None if args.inventory_only else _business_config(args.config) if args.config else None
    if not args.inventory_only and config is None and logging_model is None:
        raise ValueError('Explicit model configuration required')
    scenario = read_process_description(args.process_file) if getattr(args, 'process_file', None) else getattr(args, 'scenario_text', None)
    scenario = scenario or (previous or {}).get('scenario')
    if not scenario and not getattr(args, 'map', None): raise ValueError('Scenario description required')
    out = _safe_destination(args.out, sources)
    if previous_dir and out.resolve().is_relative_to(previous_dir.resolve()): raise ValueError('Resume output must be separate')
    out.mkdir(parents=True)
    manifest = {'schema_version': SCHEMA, 'product_version': __version__, 'belka_build': build_identity(), 'id': uuid.uuid4().hex, 'parent_id': (previous or {}).get('id'),
                'attempt': (previous or {}).get('attempt', 0) + 1, 'scenario': scenario,
                'source_paths': [str(Path(s).absolute()) for s in sources], 'requests': [], 'status': 'running',
                'limits': {'max_requests': args.max_requests, 'max_seconds': args.max_seconds},
                'stages': {k: {'status': 'pending'} for k in ('map', 'logging', 'changes', 'metrics')},
                'model_config_hash': _digest(config.to_dict()) if config else 'inventory',
                'semantic_verification': False}
    def save(): _write(out / 'product_run.json', manifest)
    budget = RunBudget(args.max_requests, args.max_seconds, manifest, save)
    transport = ProductTransport(budget, args.model_timeout)
    model = logging_model or (OpenAICompatibleModel(config, transport=transport) if config else None)
    save()
    map_data = hierarchy = logging_data = metrics = None
    stage = 'map'
    try:
        workspace = build_workspace(sources, out / '_sources')
        manifest['sources'] = workspace.sources
        from .map_input import snapshot_from_workspace
        snapshot = snapshot_from_workspace(workspace, logging_calls=args.log_call)
        manifest['source_fingerprint'] = _digest(snapshot.fingerprints)
        if previous and previous['source_fingerprint'] != manifest['source_fingerprint']:
            raise ValueError('Source snapshot changed; start a new run instead of resuming')
        manifest['stages']['map']['status'] = 'running'; save()
        saved_map = getattr(args, 'map', None)
        prior_checkpoint = None
        if previous and (previous_dir / 'scenario_map.json').is_file():
            candidate = load_map(previous_dir / 'scenario_map.json')
            if candidate.original['run'].get('stop_kind') == 'finish' or previous['stages']['map'].get('reused'):
                saved_map = str(previous_dir / 'scenario_map.json')
            elif (previous_dir / 'belka' / 'scenario_map.checkpoint.json').is_file():
                from pko.scenario_map.checkpoint import load_checkpoint
                if previous.get('model_config_hash') != manifest['model_config_hash']:
                    raise ValueError('Research configuration changed; start a new map attempt')
                prior_checkpoint = load_checkpoint(previous_dir / 'belka' / 'scenario_map.checkpoint.json')
        if previous and not saved_map and prior_checkpoint is None and (previous_dir / 'belka' / 'scenario_map.checkpoint.json').is_file():
            from pko.scenario_map.checkpoint import load_checkpoint
            if previous.get('model_config_hash') != manifest['model_config_hash']:
                raise ValueError('Research configuration changed; start a new map attempt')
            prior_checkpoint = load_checkpoint(previous_dir / 'belka' / 'scenario_map.checkpoint.json')
        if saved_map:
            view = load_map(saved_map); map_data = view.original
            if {s['id'] for s in map_data.get('sources', [])} != {s['id'] for s in workspace.sources}:
                raise ValueError('Published map and source set differ')
            for source in map_data.get('sources', []):
                actual = next((s for s in workspace.sources if s['id'] == source['id']), None)
                if actual is None or (source.get('digest') and source['digest'] != actual['digest']):
                    raise ValueError('Published map belongs to different sources')
            if map_data.get('semantic_profile') and any(not s.get('digest') for s in map_data['sources']):
                raise ValueError('Current map requires source digests')
            hierarchy, warnings = load_hierarchy(Path(saved_map).parent / 'map_hierarchy.json', view)
            manifest['presentation_warnings'] = warnings
            manifest['stages']['map']['reused'] = True
        else:
            spec = ModelSpec(role='scenario_researcher', base_url=config.base_url, model=config.model,
                             api_key='not-stored', extra_body={**(config.extra_body or {}),
                                 'product_config_sha256': manifest['model_config_hash']})
            map_data = (belka_runner or run_scenario)(scenario, sources, out / 'belka', spec=spec,
                client=BelkaClient(model), prepared_workspace=workspace, max_steps=args.max_steps,
                max_seconds=max(.01, budget.deadline - time.monotonic()), model_timeout=args.model_timeout,
                resume_checkpoint=prior_checkpoint)
            view = MapView.from_dict(map_data)
            hierarchy, warnings = load_hierarchy(out / 'belka' / 'map_hierarchy.json', view)
            manifest['presentation_warnings'] = warnings
        _write(out / 'scenario_map.json', map_data)
        if hierarchy is not None: _write(out / 'map_hierarchy.json', hierarchy)
        manifest['scenario'] = scenario or map_data.get('scenario', {}).get('title', '')
        manifest['map_revision'] = view.revision
        manifest['stages']['map']['status'] = map_data['run']['status']; save()
        if map_data['run'].get('grouping_status') == 'cancelled':
            raise InterruptedError('Map grouping cancelled')
        if map_data['run'].get('stop_kind') in ('cancelled', 'checkpoint_error'):
            raise InterruptedError('Map stopped: ' + map_data['run']['stop_kind'])
        if not view.operation_ids: raise ValueError('Map has no actions to review')
        if time.monotonic() >= budget.deadline: raise TimeoutError('Product time budget exhausted')
        stage = transport.stage = 'logging'
        manifest['stages']['logging']['status'] = 'running'; save()
        resume = None
        if previous:
            prior_log = next((p for p in (previous_dir/'logging_review.json', previous_dir/'logging_review.checkpoint.json') if p.is_file()), None)
            if prior_log: resume = _read(prior_log)
        contracts, _ = load_logging_contracts(Path(saved_map).parent if saved_map else out/'belka')
        options = ReviewOptions(max_requests=max(1, args.max_requests - len(manifest['requests'])),
            request_chars=args.request_chars, max_navigation_turns=args.max_navigation_turns,
            max_context_lines=args.max_context_lines, stop_on_failed_repair=getattr(args, 'stop_on_failed_repair', False))
        logging_data = review_logging(view, snapshot, model, options=options,
            source_roots={s['id']: s['id'] for s in workspace.sources}, artifacts=Path(saved_map).parent if saved_map else out/'belka',
            resume=resume, on_checkpoint=lambda data: _write(out/'logging_review.checkpoint.json', data), logging_contracts=contracts)
        logging_data['hierarchy'] = hierarchy
        logging_data['presentation_warnings'] = manifest.get('presentation_warnings', [])
        _write(out / 'logging_review.json', logging_data)
        manifest['stages']['logging']['status'] = logging_data['status']
        manifest['stages']['changes']['status'] = logging_data['status']; save()
        if logging_data.get('interrupted'): raise InterruptedError('Logging interrupted')
        stage = transport.stage = 'metrics'
        manifest['stages']['metrics']['status'] = 'running'; save()
        metrics = build_metrics(logging_data, model=model,
            max_requests=min(2, max(0, args.max_requests - len(manifest['requests']))), request_chars=args.request_chars)
        _write(out / 'metric_catalog.json', metrics)
        manifest['stages']['metrics']['status'] = metrics['status']
        manifest['status'] = 'partial' if any(s['status'] != 'completed' for s in manifest['stages'].values()) else 'completed'
    except (KeyboardInterrupt, Exception) as exc:
        # Never publish provider bodies/credentials or silently proceed after a failed stage.
        manifest['interrupted'] = isinstance(exc, (KeyboardInterrupt, InterruptedError))
        manifest['status'] = 'partial' if map_data else 'failed'
        manifest['stop_reason'] = type(exc).__name__ + (': ' + str(exc) if isinstance(exc, (InterruptedError, TimeoutError)) else '')
        manifest['stages'][stage]['status'] = 'partial' if (map_data if stage == 'map' else logging_data) else 'failed'
        for state in manifest['stages'].values():
            if state['status'] == 'pending': state['status'] = 'skipped'
        save()
    return _publish(out, manifest, logging_data, metrics, map_data, hierarchy)


def export_product(run, out):
    source = Path(run)
    if source.is_file(): source = source.parent
    checksums = _read(source/'product_checksums.json')
    allowed = {'product_run.json', 'report.html', 'scenario_map.json', 'map_hierarchy.json', 'logging_review.json', 'metric_catalog.json'}
    if not {'product_run.json', 'report.html'} <= set(checksums) or set(checksums) - allowed:
        raise ValueError('Invalid product export manifest')
    for name, expected in checksums.items():
        if hashlib.sha256(read_regular(source/name).encode()).hexdigest() != expected:
            raise ValueError('Product artifact checksum mismatch: ' + name)
    manifest = _read(source/'product_run.json')
    if manifest.get('schema_version') != SCHEMA: raise ValueError('Unsupported product version')
    destination = _safe_destination(out)
    if destination.resolve().is_relative_to(source.resolve()): raise ValueError('Export must be separate')
    destination.mkdir(parents=True)
    data = {}
    for name in checksums:
        if name.endswith('.json') and name != 'product_run.json':
            data[name] = _read(source/name); _write(destination/name, data[name])
    return _publish(destination, manifest, data.get('logging_review.json'), data.get('metric_catalog.json'),
                    data.get('scenario_map.json'), data.get('map_hierarchy.json'))
