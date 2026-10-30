"""Lossless one-level business-block projection of accepted atomic operations.

Groups come from the saved compose response, never name similarity or stage
membership alone. A group is a navigation aggregate, not another occurrence.
Ambiguous legacy groups are quarantined together; their actions remain visible.
This presentation check does not replace strict artifact or semantic acceptance.
"""
import copy
import json
import re
from collections import Counter, defaultdict


ACCEPTED = frozenset({'accepted', 'accepted_after_repair', 'accepted_from_checkpoint'})


def _accepted_compose(request):
    """Presentation binding only; strict export separately checks the full attempts and omission warnings."""
    if request.get('outcome') in ACCEPTED:
        return True
    if request.get('outcome') != 'accepted_after_local_revalidation':
        return False
    local, accepted = request.get('guidance_revalidation'), request.get('accepted')
    if not isinstance(local, dict) or not isinstance(accepted, dict):
        return False
    hashes = [local.get(key) for key in ('api_request_sha256', 'raw_response_sha256',
                                       'sanitized_response_sha256')]
    return (local.get('schema') == 'compose-guidance-omission/1' and local.get('outcome') == 'accepted'
            and type(local.get('after_attempt')) is int and local['after_attempt'] == 2
            and type(local.get('model_calls')) is int and local['model_calls'] == 0
            and local.get('final') is None
            and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in hashes)
            and hashes[1] != hashes[2]
            and accepted == {'attempt': 2, 'api_request_sha256': hashes[0], 'raw_response_sha256': hashes[1],
                             'response_sha256': hashes[2], 'derivation': 'local_guidance_omission'})


def hierarchy_view(bundle, expanded=()):
    process = bundle.get('process_map') or {}
    if not isinstance(process, dict):
        process = {}
    operations = [x for x in (process.get('operations') or []) if isinstance(x, dict)]
    by_id = {x.get('id'): x for x in operations if isinstance(x.get('id'), str)}
    atomic = bundle.get('atomic_map') or {}
    if not isinstance(atomic, dict):
        atomic = {}
    compose = atomic.get('compose') or {}
    if not isinstance(compose, dict):
        compose = {}
    stages = {x['id'] for x in (process.get('stages') or []) if isinstance(x, dict) and isinstance(x.get('id'), str)}
    accepted_requests = {x.get('id') for x in (atomic.get('requests') or []) if isinstance(x, dict)
                         and isinstance(x.get('id'), str) and x.get('kind') == 'compose' and _accepted_compose(x)}
    proposed = atomic.get('groups') or []
    issues, valid = [], []
    group_ids = Counter(x.get('id') for x in proposed if isinstance(x, dict) and isinstance(x.get('id'), str))
    names = Counter((x.get('stage_id'), x.get('name', '').strip()) for x in proposed
                    if isinstance(x, dict) and isinstance(x.get('stage_id'), str) and isinstance(x.get('name'), str))

    def issue(identifier, code):
        issues.append({'group_id': identifier, 'code': code})

    for group in proposed:
        if not isinstance(group, dict):
            issue(None, 'invalid_group')
            continue
        identifier, members = group.get('id'), group.get('operation_ids')
        if (not isinstance(identifier, str) or not identifier.strip() or group_ids[identifier] != 1
                or not isinstance(group.get('name'), str) or not group['name'].strip()
                or not isinstance(members, list) or not members
                or any(not isinstance(x, str) for x in members) or len(set(members)) != len(members)):
            issue(identifier, 'invalid_group')
        elif (not isinstance(group.get('request_id'), str) or compose.get('status') != 'complete'
              or group.get('request_id') != compose.get('request_id')
              or group.get('request_id') not in accepted_requests):
            issue(identifier, 'unbound_group')
        elif not isinstance(group.get('stage_id'), str) or group.get('stage_id') not in stages:
            issue(identifier, 'unknown_stage')
        elif names[(group['stage_id'], group['name'].strip())] != 1:
            issue(identifier, 'duplicate_group_name')
        elif any(x not in by_id for x in members):
            issue(identifier, 'unknown_operation')
        elif any(by_id[x].get('stage_id') != group.get('stage_id') for x in members):
            issue(identifier, 'group_stage_mismatch')
        elif len(members) == 1:
            issue(identifier, 'single_member_not_a_block')
        else:
            valid.append(copy.deepcopy(group))

    owners = defaultdict(list)
    for group in valid:
        for identifier in group['operation_ids']:
            owners[identifier].append(group['id'])
    conflicts = {group for ids in owners.values() if len(ids) > 1 for group in ids}
    for identifier in sorted(conflicts):
        issue(identifier, 'overlapping_groups')
    groups = [group for group in valid if group['id'] not in conflicts]
    owner = {identifier: group for group in groups for identifier in group['operation_ids']}
    expanded = set(expanded)
    nodes, seen, visible = [], set(), {}
    for operation in operations:
        identifier = operation.get('id')
        if not isinstance(identifier, str) or identifier not in by_id:
            continue
        group = owner.get(identifier)
        collapsed = group is not None and group['id'] not in expanded
        key = 'group:' + group['id'] if collapsed else 'op:' + identifier
        visible[identifier] = key
        if key in seen:
            continue
        seen.add(key)
        if collapsed:
            members = group['operation_ids']
            nodes.append({'id': key, 'kind': 'group', 'group_id': group['id'], 'stage_id': group['stage_id'],
                          'name': group['name'], 'operation_ids': list(members),
                          'evidence_ids': list(dict.fromkeys(e for x in members
                                                            for e in by_id[x].get('evidence_ids', [])))})
        else:
            nodes.append({'id': key, 'kind': operation.get('kind'), 'group_id': group['id'] if group else None,
                          'stage_id': operation.get('stage_id'), 'name': operation.get('name'),
                          'operation_ids': [identifier], 'evidence_ids': list(operation.get('evidence_ids', []))})

    duplicates = {}
    for number, edge in enumerate((x for x in (process.get('operation_transitions') or []) if isinstance(x, dict)), 1):
        if edge.get('from_operation') not in visible or edge.get('to_operation') not in visible:
            issues.append({'transition_id': edge.get('id'), 'code': 'unresolved_endpoint'})
            continue
        key = json.dumps({k: v for k, v in edge.items() if k != 'id'}, sort_keys=True, ensure_ascii=False)
        if key not in duplicates:
            duplicates[key] = {**copy.deepcopy(edge), 'anchor': f'transition-{number}', 'anchors': [], 'transition_ids': [],
                               'from': visible[edge['from_operation']], 'to': visible[edge['to_operation']]}
        duplicates[key]['anchors'].append(f'transition-{number}')
        duplicates[key]['transition_ids'].append(edge.get('id'))

    hidden = defaultdict(list)
    edges = []
    for edge in duplicates.values():
        if edge['from'] == edge['to'] and edge['from'].startswith('group:'):
            hidden[edge['from'][6:]].extend(edge['transition_ids'])
        else:
            edges.append(edge)
    for group in groups:
        group['expanded'] = group['id'] in expanded
        group['internal_transition_ids'] = hidden[group['id']]
        group['visible_member_ids'] = (['op:' + x for x in group['operation_ids']] if group['expanded']
                                       else ['group:' + group['id']])
    return {'nodes': nodes, 'edges': edges, 'groups': groups, 'issues': issues,
            'operation_count': len(operations), 'raw_transition_count': len(process.get('operation_transitions') or []),
            'basis': 'Display projection of saved groups and transitions; not execution order or semantic proof'}
