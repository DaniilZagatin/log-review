"""Source-addressed proposals; a proposal never changes current coverage.

Proposals are optional. After the one clear repair, an invalid proposal is isolated with its diagnostic instead of
discarding an independently sound assessment (``isolate=True``). Mandatory evidence/IDs are validated elsewhere and
still block the assessment.
"""
from copy import deepcopy

from .logging_contracts import (CHANGE_KINDS, EXISTING_RECORD_KINDS, ChangeValidationError, LoggingReviewError,
                                fields, strings, text)

_CHANGE_FIELDS = {'id', 'kind', 'operation_ids', 'problem', 'what', 'why', 'fields', 'location', 'timing',
                  'verification', 'existing_site_ids', 'dependencies', 'guidance_ids', 'logger', 'placement', 'guards'}
_REQUIRED = {'id', 'kind', 'operation_ids', 'problem', 'what', 'why', 'fields', 'location', 'timing', 'verification'}


def _owner(context, location, *, classes=False):
    owners = [d for d in context.declarations(location['path'])
              if (d.get('kind') == 'class') == classes
              and d['start_line'] <= location['start_line'] <= location['end_line'] <= d['end_line']]
    return min(owners, key=lambda d: d['end_line'] - d['start_line']) if owners else None


def _logger_access(item, row, context, sites, operation_id):
    """Explicit, deterministic statement of how the proposed record could be written at its location."""
    location = row['location']
    from .logger_access import inspect_logger, declared_loggers, logging_purpose
    confirmed = logging_purpose
    if item.get('logger') is not None:
        result = inspect_logger(item['logger'], location, context, sites)
        if result.get('status') == 'available': return result
        raise LoggingReviewError('change.logger.expression: ' + result['reason'] + '; укажи существующий доступный '
                                 'receiver/member с подтверждённым назначением, а не новый вызов/литерал/бизнес-команду. '
                                 'Предлагаемый код относится к what; точные варианты перечислены в repair_details.')
    def usable(site, basis):
        try:
            result = inspect_logger({'expression':site['callee'],
                'evidence':[{k:site[k] for k in ('path','start_line','end_line')}]},location,context,sites)
        except LoggingReviewError:return None
        if result.get('status')=='available':
            return {**result,'basis':basis,'site_id':site['id'],'callee':site['callee']}
    known = [sites[identifier] for identifier in row['existing_site_ids'] if confirmed(sites[identifier])]
    for site in known:
        result = usable(site,'existing_record')
        if result:return result
    for classes, basis in ((False, 'existing_record_in_member'), (True, 'existing_record_in_class')):
        owner = _owner(context, location, classes=classes)
        if owner is None: continue
        local = [s for s in sites.values() if confirmed(s) and s['path'] == location['path']
                 and owner['start_line'] <= s['start_line'] <= owner['end_line']]
        for site in sorted(local,key=lambda s:(abs(s['start_line']-location['start_line']),s['id'])):
            result = usable(site,basis)
            if result:return result
    declarations = declared_loggers(context, location)
    for option in declarations:
        if option['reference']:
            return {'status':'available','basis':'module_logger' if option['basis']=='python_module_logger' else option['basis'],
                    'expression':option['expression'],'evidence':[option['reference']], 'purpose_basis':'standard_declaration'}
    if declarations:
        return {'status':'requires_dependency','basis':'source_not_delivered',
                'reads':[ref for d in declarations for ref in d['reads']],
                'note':'Найдено объявление стандартного логгера; сначала прочитать его источник, доступность не подтверждена'}
    candidates = {s['id'] for s in sites.values() if confirmed(s)
                  and (s['path'] == location['path'] or s['id'] in row['existing_site_ids'])}
    return {'status': 'requires_dependency', 'basis': 'not_established', 'site_ids': sorted(candidates),
            'note': 'Доступность логгера в месте вставки не установлена по прочитанным источникам. '
                    'Проверить существующее объявление/область видимости или явно указать зависимость; '
                    'это не доказательство отсутствия логгера'}


def validate_field_evidence(value, location, context):
    from .proposal_placement import evidence_precedes, evidence_scope_allows
    expression = text(value['expression'], 'field.expression')
    if type(value['evidence']) is not list: raise LoggingReviewError('Field evidence must be a list')
    evidence = [context.verify_ref(ref, 'change.field.evidence') for ref in value['evidence']]
    access_location = location
    if not evidence or not all(context.available_at(ref, access_location)
               and evidence_precedes(ref,access_location,context) for ref in evidence):
        raise LoggingReviewError('An existing field needs code evidence at its proposed location')
    if not any(expression in ref['quote'] for ref in evidence):
        raise LoggingReviewError('Existing field expression is absent from its evidence')
    if not any(evidence_scope_allows(ref,access_location,context,expression) for ref in evidence):
        raise LoggingReviewError('An existing field needs code evidence at its proposed location: '
            'existing_field_scope_not_established. Подтверди доступность выражения в этой точке; '
            'если требуется новое поле/зависимость, укажи origin: proposed и объясни её, не выдумывай доступность')


def _validate_one(index, item, operation_id, context, sites, bound_sites):
    fields(item, _CHANGE_FIELDS, _REQUIRED, 'changes[' + str(index) + ']')
    identifier = text(item['id'], 'change.id', maximum=160)
    if item['kind'] not in CHANGE_KINDS: raise LoggingReviewError('Unsupported change kind; runtime delivery is outside this review')
    if item['operation_ids'] != [operation_id]: raise LoggingReviewError('A review can propose changes only for its input operation')
    existing = strings(item.get('existing_site_ids', []), 'existing_site_ids')
    if any(value not in sites for value in existing): raise LoggingReviewError('Change refers to an unknown existing site')
    if item['kind'] in EXISTING_RECORD_KINDS and not existing:
        raise LoggingReviewError('Changing an existing record needs its exact site ID')
    row = {'id': operation_id + ':' + identifier, 'local_id': identifier,
           'kind': item['kind'], 'operation_ids': [operation_id], 'proposed': True,
           **{key: text(item[key], 'change.' + key) for key in ('problem', 'what', 'why', 'timing', 'verification')},
           'existing_site_ids': existing, 'fields': [], 'dependencies': strings(item.get('dependencies', []), 'dependencies')}
    if item['location'] is None:
        # Not a change yet: an addressed task to find the place. No placement-specific rule applies.
        return identifier, row, {'operation_id': operation_id, 'kind': 'proposal_location', 'blocking': False, 'scope': 'source',
                                 'description': 'Найти место для доработки: ' + row['what']}
    if item['kind'] == 'add_event' and bound_sites and not set(existing) & bound_sites:
        raise LoggingReviewError('A new record next to an already bound record must name it in existing_site_ids; '
                                 'prefer map_existing/enrich/add_field before add_event')
    row['location'] = context.verify_ref(item['location'], 'change.location')
    from .proposal_placement import validate_placement, evidence_precedes, evidence_scope_allows
    placement, point = validate_placement(item,row['location'],context,sites)
    access_location = dict(row['location'])
    if placement is not None: row.update(placement)
    if point is not None:
        access_location.update(start_line=point[0],end_line=point[0],_insertion_point=point,_placement_anchor=row['location'])
    values = item['fields']
    if type(values) is not list or len(values) > 40: raise LoggingReviewError('Invalid proposed fields')
    names = set()
    for value in values:
        fields(value, {'name', 'origin', 'expression', 'evidence'}, {'name', 'origin', 'expression', 'evidence'}, 'change.field')
        name = text(value['name'], 'field.name', maximum=160)
        if name in names or value['origin'] not in ('existing', 'proposed'):
            raise LoggingReviewError('Invalid or duplicate proposed field')
        names.add(name)
        expression = text(value['expression'], 'field.expression')
        if type(value['evidence']) is not list: raise LoggingReviewError('Field evidence must be a list')
        evidence = [context.verify_ref(ref, 'change.field.evidence') for ref in value['evidence']]
        if value['origin'] == 'existing': validate_field_evidence(value,access_location,context)
        row['fields'].append({'name': name, 'origin': value['origin'], 'expression': expression, 'evidence': evidence})
    row['logger_access'] = _logger_access(item, {**row,'location':access_location}, context, sites, operation_id)
    row['requirements'] = ([row['logger_access']['note']] if row['logger_access']['status'] == 'requires_dependency' else [])
    if placement is not None and placement['placement_context']['status']=='not_established':
        row['requirements'].append('Точка доработки требует проверки control flow: синтаксическая согласованность не установлена')
    if placement is not None: row['requirements'].extend(placement['placement_context'].get('required_edits',[]))
    return identifier, row, None


def validate_changes(items, operation_id, context, sites, *, isolate=False, bound_sites=(), forced_rejections=None):
    """Return (accepted changes, location tasks, diagnostics). Without ``isolate`` the first error raises
    ChangeValidationError so the existing single repair can ask for a correction.

    ``forced_rejections`` ({index: error}) replays rejections recorded with an isolated checkpoint: the saved reply is
    public-filtered, so a proposal rejected for private text must not become valid on reuse."""
    from .logging_contracts import safe_public
    diagnostics = []
    def failed(message, index=None, local_id=None):
        if not isolate: raise ChangeValidationError(message, index, local_id)
        diagnostics.append({'index': index, 'local_id': None if local_id is None else safe_public(str(local_id)[:160]),
                            'error': safe_public(str(message)[:2000])})
    if type(items) is not list or len(items) > 40:
        failed('changes: expected at most 40 proposals')
        return [], [], diagnostics
    bound_sites = set(bound_sites)
    forced = dict(forced_rejections or {})
    accepted, tasks, identifiers = {}, [], set()
    for index, item in enumerate(items):
        local_id = item.get('id') if type(item) is dict and type(item.get('id')) is str else None
        if index in forced:
            failed(forced[index], index, local_id)
            continue
        try:
            identifier, row, location_task = _validate_one(index, item, operation_id, context, sites, bound_sites)
            if identifier in identifiers: raise LoggingReviewError('Duplicate change ID')
            identifiers.add(identifier)
        except ChangeValidationError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            failed(message, index, local_id)
            continue
        if location_task is not None: tasks.append(location_task)
        else: accepted[identifier] = (index, row)
    # Dependencies may only point at accepted local proposals; a dependent of a rejected proposal is rejected too.
    state = {}
    def ok(identifier, trail=()):
        if identifier in state: return state[identifier]
        if identifier in trail: return False
        if identifier not in accepted: return False
        state[identifier] = all(ok(d, trail + (identifier,)) for d in accepted[identifier][1]['dependencies'])
        return state[identifier]
    result = []
    for identifier, (index, row) in accepted.items():
        if ok(identifier):
            result.append(row)
            continue
        unresolved = [d for d in row['dependencies'] if d not in accepted]
        failed('Unresolved change dependency: ' + ', '.join(unresolved) if unresolved else 'Cyclic or rejected change dependencies',
               index, identifier)
    return result, tasks, diagnostics


def combine_changes(rows):
    """Keep independently proposed actions distinct; no merge by a shared anchor/key."""
    return [deepcopy(change) for row in rows for change in row.get('changes', [])]
