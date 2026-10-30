"""Versioned research state, restored without replaying reads or map mutations.

The outer checkpoint owns integrity, configuration and lifecycle. This module
owns source-bound citation authority, identity high-water marks and public memory.
Saved source/model prose remains data; it never supplies the active instructions.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
import re

from pko.audit import _sanitize, _PRIVATE
from pko.code_tools.tools import _is_blocked, _mask_secrets
from pko.extractors.base import is_vendor
from pko.scenario_map.research_memory import ResearchMemory
from pko.scenario_map.schema import (
    MapState, MapValidationError, SCHEMA_VERSION, SEMANTIC_PROFILE, STEP_KINDS,
    EDGE_KINDS, EVIDENCE_STATUSES, OUTCOME_KINDS, OPERATION_KINDS, EXECUTION_CHECKS,
)
from pko.scenario_map.quality import EFFECT_KINDS, SEMANTIC_CHECKS

MAX_STATE_CHARS = 120 * 1024 * 1024
COLLECTIONS = {'nodes': ('step', 500), 'edges': ('edge', 1500), 'gaps': ('gap', 500),
               'execution_reviews': ('review', 500), 'research_notes': ('note', 200),
               'research_tasks': ('task', 500)}


def _fail(message):
    raise MapValidationError('Checkpoint: ' + message, 'invalid_checkpoint')


def _json_shape(value, depth=0):
    if depth > 40:
        _fail('слишком глубокая структура')
    if isinstance(value, dict):
        if len(value) > 5000 or any(not isinstance(key, str) for key in value):
            _fail('некорректный объект')
        for item in value.values():
            _json_shape(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 50000:
            _fail('слишком длинный список')
        for item in value:
            _json_shape(item, depth + 1)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        _fail('некорректное значение JSON')
    elif isinstance(value, float) and not math.isfinite(value):
        _fail('неконечное число')


def _source_identity(sources):
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        _fail('некорректный список источников')
    if any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or
           not isinstance(row.get('digest'), str) or not row['digest'] or
           type(row.get('file_count')) is not int or not 0 <= row['file_count'] <= 5000 for row in sources):
        _fail('некорректная идентичность источника')
    return [{key: row[key] for key in ('id', 'digest', 'file_count')} for row in sources]


def _sanitizer():
    # One environment snapshot per persistence operation, not per string leaf.
    secrets = tuple(value for key, value in os.environ.items() if _PRIVATE.search(key) and len(value) >= 8)
    strings = {}

    def clean(value):
        if isinstance(value, str):
            if value not in strings:
                strings[value] = _sanitize(value, secrets)
            return strings[value]
        return _sanitize(value, secrets)
    return clean


def _public_model(value, state, clean, quotes):
    # Quoted source lines are sanitized separately, exactly as the restored read
    # ledger. Sanitizing a multiline JSON quote as one string would parse it and
    # silently remove source lines instead of masking their content.
    if isinstance(value, dict):
        if {'path', 'line_start', 'line_end', 'quote'} <= set(value):
            row = {key: deepcopy(part) for key, part in value.items() if key != 'quote'}
            key = (value['path'], value['line_start'], value['line_end'])
            if key not in quotes:
                lines = state._reads[value['path']]
                quotes[key] = '\n'.join(str(clean(lines[n])) for n in range(value['line_start'], value['line_end'] + 1)).strip()
            row['quote'] = quotes[key]
            return row
        return {key: _public_model(part, state, clean, quotes) for key, part in value.items()
                if not _PRIVATE.search(key)}
    if isinstance(value, list):
        return [_public_model(part, state, clean, quotes) for part in value]
    return clean(value)


def export_state(state: MapState) -> dict:
    """Export only durable accepted state; no conversation or pending tool batch."""
    return {
        'model': _public_model(state.model, state, _sanitizer(), {}),
        'counters': dict(state._counters),
        'read_coverage': [
            {'path': path, 'source_id': state._source_for(path), 'line_count': len(lines),
             'ranges': state._ranges(sorted(lines))}
            for path, lines in sorted(state._reads.items()) if lines
        ],
        'memory': state.research_memory.checkpoint(),
    }


def _restore_reads(state, coverage, clean):
    if not isinstance(coverage, list) or len(coverage) > len(state._files):
        _fail('некорректный реестр прочитанных строк')
    for row in coverage:
        if not isinstance(row, dict) or set(row) != {'path', 'source_id', 'line_count', 'ranges'}:
            _fail('некорректная запись прочитанных строк')
        path, ranges = row['path'], row['ranges']
        if (not isinstance(path, str) or path in state._reads or path not in state._files or
                _is_blocked(path) or is_vendor(path) or state._source_for(path) != row['source_id']):
            _fail('недоступный или повторный путь прочитанных строк')
        if not isinstance(ranges, list) or not ranges or len(ranges) > 100000:
            _fail('некорректные диапазоны чтения')
        content = state.workspace.tree.read(path)
        if not isinstance(content, str):
            _fail('прочитанный файл больше недоступен')
        lines, visible, previous = content.splitlines(), {}, 0
        for span in ranges:
            if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not previous < span[0] <= span[1] <= len(lines)):
                _fail('диапазон чтения вне файла, повторён или не упорядочен')
            for number in range(span[0], span[1] + 1):
                visible[number] = str(clean(_mask_secrets(lines[number - 1])))
            previous = span[1]
        if type(row['line_count']) is not int or row['line_count'] != len(visible) or ranges != state._ranges(sorted(visible)):
            _fail('счётчик или диапазоны прочитанных строк повреждены')
        # No observe_read: discovery must not modify the restored task inventory.
        state._reads[path] = visible


def _choice(value, choices, field):
    if value not in choices:
        _fail('некорректное значение ' + field)


def _list(value, field, maximum=1500):
    if not isinstance(value, list) or len(value) > maximum:
        _fail('некорректный список ' + field)


def _strings(value, field, maximum=1500):
    _list(value, field, maximum)
    if any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        _fail('некорректные строки ' + field)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _fail('неподдерживаемая структура сохранённого объекта')


def _check_citations(state, value):
    if isinstance(value, dict):
        if {'path', 'line_start', 'line_end'} <= set(value):
            state._evidence([value])
        else:
            for key, item in value.items():
                # A task implementation range is a navigation hint; its body
                # may still be unread and must not gain citation authority.
                if key != 'implementation':
                    _check_citations(state, item)
    elif isinstance(value, list):
        for item in value:
            _check_citations(state, item)


def _validate_model(state, model, counters):
    _object(model, state.model)
    if model['schema_version'] != SCHEMA_VERSION or model['semantic_profile'] != SEMANTIC_PROFILE:
        _fail('неподдерживаемая схема или методология')
    _object(model['scenario'], state.model['scenario'])
    for key, value in model['scenario'].items():
        if isinstance(state.model['scenario'][key], list):
            _strings(value, 'scenario.' + key)
        elif not isinstance(value, str):
            _fail('некорректное поле сценария')
    _object(counters, state._counters)
    if any(type(n) is not int or not 0 <= n <= 1_000_000_000 for n in counters.values()):
        _fail('некорректные счётчики ID')

    def identifier(value, prefix):
        if (not isinstance(value, str) or not re.fullmatch(prefix + r'-[1-9][0-9]*', value) or
                int(value[len(prefix) + 1:]) > counters[prefix]):
            _fail('ID превышает сохранённый счётчик или повреждён')

    for collection, (prefix, maximum) in COLLECTIONS.items():
        _list(model[collection], collection, maximum)
        ids = set()
        for item in model[collection]:
            if not isinstance(item, dict):
                _fail('ожидался объект реестра')
            identifier(item.get('id'), prefix)
            if item['id'] in ids:
                _fail('повтор ID')
            ids.add(item['id'])

    node_fields = {'id', 'name', 'kind', 'description', 'input', 'output', 'condition', 'basis',
                   'evidence_status', 'evidence', 'outcome', 'effect_kind', 'continuation_mode',
                   'join_mode', 'completion_condition', 'external_interactions', 'code_explanations'}
    edge_fields = {'id', 'source', 'target', 'kind', 'description', 'condition', 'data', 'basis',
                   'evidence_status', 'evidence', 'sender_evidence', 'receiver_evidence', 'correlation',
                   'channel', 'contract', 'code_explanations'}
    for collection, fields in (('nodes', node_fields), ('edges', edge_fields)):
        for row in model[collection]:
            _object(row, fields)
            _choice(row['kind'], STEP_KINDS if collection == 'nodes' else EDGE_KINDS, 'kind')
            _choice(row['evidence_status'], EVIDENCE_STATUSES, 'evidence_status')
            state._support(row)
            if collection == 'nodes':
                for key, values in (('outcome', OUTCOME_KINDS), ('effect_kind', EFFECT_KINDS),
                                    ('continuation_mode', ('sequence', 'choice', 'all')), ('join_mode', ('any', 'all'))):
                    _choice(row[key], values, key)
                state.external_interactions(row['external_interactions'], row['evidence'])
            state.code_explanations(row['code_explanations'], row['evidence'] + row.get('sender_evidence', []) + row.get('receiver_evidence', []))
            for key in fields - {'evidence', 'sender_evidence', 'receiver_evidence', 'external_interactions', 'code_explanations'}:
                if not isinstance(row[key], str):
                    _fail('некорректное текстовое поле графа')

    action_ids = set()
    for review in model['execution_reviews']:
        _object(review, {'id', 'name', 'evidence', 'operations', 'checks', 'node_fingerprints', 'edge_fingerprints'})
        _list(review['operations'], 'operations', 100)
        _object(review['checks'], EXECUTION_CHECKS)
        for key, prefix in (('node_fingerprints', 'step'), ('edge_fingerprints', 'edge')):
            if not isinstance(review[key], dict):
                _fail('некорректные отпечатки обзора')
            for value, fingerprint in review[key].items():
                identifier(value, prefix)
                if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
                    _fail('некорректный отпечаток обзора')
        for action in review['operations']:
            _object(action, {'id', 'node_id', 'mapping_status', 'kind', 'name', 'action', 'effect', 'failure', 'condition', 'evidence'})
            identifier(action['id'], 'op')
            if action['id'] in action_ids:
                _fail('повтор ID найденного действия')
            action_ids.add(action['id'])
            _choice(action['kind'], OPERATION_KINDS, 'operations.kind')
            if action['mapping_status'] != ('mapped' if action['node_id'] else 'unmapped'):
                _fail('противоречивое сопоставление действия')
        for check in review['checks'].values():
            _object(check, {'status', 'explanation', 'evidence', 'node_ids', 'edge_ids', 'gap_ids'})
            _choice(check['status'], ('present', 'not_found', 'unknown'), 'check.status')
            for key in ('node_ids', 'edge_ids', 'gap_ids'):
                _strings(check[key], key)

    for task in model['research_tasks']:
        _object(task, {'id', 'target', 'kind', 'status', 'resolution', 'evidence', 'node_ids', 'gap_ids', 'child_task_ids', 'automatic'}, {'implementation', 'exclusion'})
        _choice(task['status'], ('open', 'read', 'analyzed', 'mapped', 'gap', 'out_of_scope'), 'task.status')
        if type(task['automatic']) is not bool:
            _fail('некорректная отметка автоматической задачи')
        for key in ('node_ids', 'gap_ids', 'child_task_ids'):
            _strings(task[key], key)
    for row in model['gaps']:
        _object(row, {'id', 'description', 'category', 'evidence', 'node_ids', 'edge_ids', 'task_ids'})
        _choice(row['category'], ('missing_source', 'unresolved_behavior', 'unresolved_transition', 'unexplored'), 'gap.category')
        for key in ('node_ids', 'edge_ids', 'task_ids'):
            _strings(row[key], key)
    for row in model['research_notes']:
        _object(row, {'id', 'kind', 'status', 'finding', 'next_action', 'evidence'})
        _choice(row['kind'], ('fact', 'hypothesis', 'question'), 'note.kind')
        _choice(row['status'], ('open', 'resolved'), 'note.status')
    _list(model['source_reviews'], 'source_reviews', 32)
    for row in model['source_reviews']:
        _object(row, {'source_id', 'disposition', 'reason', 'inspected', 'read_files'})
        if row['source_id'] not in state._source_ids:
            _fail('неизвестный источник обзора')
        _choice(row['disposition'], ('reviewed', 'out_of_scope', 'unavailable'), 'source.disposition')
    brief = model['research_brief']
    if brief is not None:
        _object(brief, {'status', 'project_overview', 'scenario_alignment', 'evidence', 'confirmed_facts',
                        'flow_hypothesis', 'open_questions', 'next_actions', 'read_signature', 'revision'})
        _choice(brief['status'], ('working_hypothesis',), 'brief.status')
        for key in ('flow_hypothesis', 'open_questions', 'next_actions'):
            _strings(brief[key], key, 12)
        _list(brief['confirmed_facts'], 'confirmed_facts', 12)
        for fact in brief['confirmed_facts']:
            _object(fact, {'finding', 'evidence'})
        if type(brief['revision']) is not int or brief['revision'] < 1:
            _fail('некорректная ревизия понимания')
    summary = model['context_summary']
    if summary is not None:
        _object(summary, {'revision', 'status', 'through_step', 'based_on_brief_revision', 'input_omitted_exchanges',
                          'input_truncated_fields', 'input_open_notes_complete', 'input_open_tasks_complete', 'summary'},
                         {'input_unmapped_actions_complete'})
        _choice(summary['status'], ('public_summary_not_evidence',), 'summary.status')
        from pko.scenario_map.compaction import validate_summary
        validate_summary(json.dumps(summary['summary'], ensure_ascii=False), state)
    semantics = model['semantic_review']
    if semantics is not None:
        _object(semantics, {'checks', 'route_cases', 'status', 'signature', 'brief_revision'})
        _object(semantics['checks'], SEMANTIC_CHECKS)
        _choice(semantics['status'], ('model_reviewed_not_verified',), 'semantic_review.status')
        for check in semantics['checks'].values():
            _object(check, {'status', 'explanation', 'evidence', 'node_ids', 'edge_ids', 'gap_ids'})
            _choice(check['status'], ('reviewed', 'uncertain'), 'semantic_check.status')
            for key, prefix in (('node_ids', 'step'), ('edge_ids', 'edge'), ('gap_ids', 'gap')):
                _strings(check[key], key)
                for value in check[key]: identifier(value, prefix)
        _list(semantics['route_cases'], 'route_cases', 500)
        for case in semantics['route_cases']:
            _object(case, {'name', 'input', 'path', 'expected_result', 'evidence', 'edge_ids', 'transition_evidence'})
            for key, prefix in (('path', 'step'), ('edge_ids', 'edge')):
                # Repeated actions are intentional in a loop example.
                _list(case[key], key, 1000)
                for value in case[key]: identifier(value, prefix)
    if not isinstance(model['run'], dict) or type(model['run'].get('steps')) is not int or model['run']['steps'] < 0:
        _fail('некорректное состояние запуска')
    # Rejection diagnostics can contain requested/unread ranges. They are not
    # accepted evidence and restoring them must not grant or require authority.
    _check_citations(state, {key: value for key, value in model.items()
                            if key not in {'run', 'sources', 'source_notes', 'source_omissions'}})


def restore_state(workspace, payload: dict) -> MapState:
    """Rehydrate accepted state, validating references and exact masked citations.

    Structural/coverage defects remain inspectable work, not a reason to discard
    an interrupted map. Dangling references and malformed persistence are refused.
    """
    try:
        _json_shape(payload)
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False)) > MAX_STATE_CHARS:
            _fail('состояние превышает лимит размера')
        _object(payload, {'model', 'counters', 'read_coverage', 'memory'})
        model = deepcopy(payload['model'])
        if not isinstance(model, dict) or not isinstance(model.get('scenario'), dict):
            _fail('отсутствует сохранённый сценарий')
        state = MapState(workspace, model['scenario']['description'])
        if _source_identity(model['sources']) != _source_identity(state.model['sources']):
            _fail('состав или содержимое источников изменились')
        if model['source_omissions'] != state.model['source_omissions']:
            _fail('доступность исходных материалов изменилась')
        clean = _sanitizer()
        _restore_reads(state, payload['read_coverage'], clean)
        _validate_model(state, model, payload['counters'])
        # Secrets/private protocol fields are not accepted as recoverable memory.
        model = _public_model(model, state, clean, {})
        state.model = model
        state._counters = dict(payload['counters'])
        state.research_memory = ResearchMemory.from_checkpoint(payload['memory'])
        errors = state._reference_errors()
        if errors:
            _fail('ссылки сохранённого состояния повреждены: ' + '; '.join(errors[:5]))
        # Exercise derived views before the restored instance can reach a model.
        state.snapshot()
        return state
    except MapValidationError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError, OverflowError) as exc:
        raise MapValidationError('Checkpoint: повреждённое состояние исследования', 'invalid_checkpoint') from exc
