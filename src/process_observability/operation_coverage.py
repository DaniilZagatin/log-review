"""Host-only accounting. Unknown work never leaves the map-owned denominator."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from fractions import Fraction

from .logging_contracts import LEGACY_POLICY, MOMENTS, POLICIES, POLICY, STATES, LoggingReviewError, task_is_blocking

SENT_MOMENTS = ('after_action', 'after_confirmation')
RECONCILE_POLICY = 'independent-source-uses/1'
COVERAGE_LIMIT_LABELS = {
    'shared_record_non_sufficient':'Общая запись нескольких операций сама по себе не подтверждает полноту каждой',
    'contextual_basis_not_certified':'Связь записи соседнего вызова с этой операцией не полностью подтверждена по коду',
    'choice_branches_not_established':'Не установлено достаточное логирование всех ветвей этой операции',
}


def binding_is_sufficient(operation, binding, model_map, policy=POLICY):
    """Lifecycle compatibility, not a proof of the model's semantic interpretation.

    Policy 2: a record written before the operation's action is intent only and never carries its result, whatever
    the model declares in ``establishes``. A request record establishes at most that a command left the caller, so it
    can satisfy only a command/recommendation operation and only when written after the call. It never proves
    application by a receiver. Saved policy-1 reports keep their original rule.
    """
    return not binding_insufficiency_reasons(operation, binding, model_map, policy)


def binding_insufficiency_reasons(operation, binding, model_map, policy=POLICY):
    """Explain the existing sufficiency rule, without merging or upgrading model claims."""
    reasons = []
    if policy == POLICY:
        if binding.get('shared_with'): reasons.append('shared_non_sufficient')
        if binding.get('context_basis') is not None and not context_certified(binding['context_basis']):
            reasons.append('context_not_certified')
    reviewed_error = any(item.get('node_id') == operation['id'] and item.get('kind') == 'error_handling'
                         for review in model_map.get('execution_reviews', []) if type(review) is dict
                         for item in review.get('operations', []) if type(item) is dict)
    negative = (operation.get('outcome') in ('refusal', 'failure', 'cancelled', 'deferred', 'no_decision') or reviewed_error)
    if policy==POLICY and operation.get('continuation_mode')=='choice' and operation.get('outcome') in (None,'','unknown'):
        declared=set(binding.get('covers_outcomes',[]))
        negative=negative or bool(declared and declared <= {'refusal','failure','cancelled','deferred','no_decision'})
    role = binding.get('role')
    moment = binding.get('moment')
    if policy == LEGACY_POLICY:
        if role in ('error', 'repeat', 'no_op'):
            compatible = negative and operation.get('outcome') != 'success'
        elif role == 'start': compatible = False
        elif role == 'request': compatible = operation.get('effect_kind') in ('command', 'recommendation')
        else: compatible = role in ('result', 'receiver_response')
    else:
        if moment is not None and moment not in MOMENTS: return reasons + ['invalid_moment']
        if moment == 'before_action' and role not in ('request', 'start'):
            return reasons + ['before_action_intent']
        if role in ('error', 'repeat', 'no_op'):
            compatible = negative and operation.get('outcome') != 'success'
        elif role == 'start': compatible = False
        elif role == 'request':
            compatible = operation.get('effect_kind') in ('command', 'recommendation') and moment in SENT_MOMENTS
        else: compatible = role in ('result', 'receiver_response')
    outcome = operation.get('outcome')
    outcome_ok = not outcome or outcome == 'unknown' or outcome in binding.get('covers_outcomes', [])
    if not compatible:
        reasons.append('before_action_intent' if moment == 'before_action' else 'role_incompatible')
    if not outcome_ok: reasons.append('outcome_not_covered')
    for fact in ('occurrence', 'result'):
        if fact not in binding.get('establishes', []): reasons.append('establishes_without_' + fact)
    return reasons


def partial_reason_issue(operation, bindings, model_map):
    return {'code': 'partial_without_gap', 'field': 'bindings / missing',
            'bindings': [{'site_id': b.get('site_id'), 'reasons': binding_insufficiency_reasons(operation, b, model_map)}
                         for b in bindings],
            'instruction': 'Частичная оценка без названного пробела и без границы проверки противоречива. '
                'Укажи, какие факты действительно устанавливает каждая запись. Если исход относится к ЭТОМУ '
                'выполнению, обоснуй occurrence и result; не добавляй их только ради формы. Если факт не установлен, '
                'назови его в missing и предложи нужную доработку. Не объединяй некоррелированные записи '
                'и не выдумывай новый лог. При действительно недоступном коде — inspect или адресный source-вопрос.'}


def partial_explained_by_limit(limits, operation, bindings, model_map):
    if 'choice_branches_not_established' in limits: return True
    for binding in bindings:
        reasons = set(binding_insufficiency_reasons(operation, binding, model_map))
        if reasons and reasons <= {'shared_non_sufficient', 'context_not_certified'}:
            return True
    return False


def validate_partial_reason(state, missing, limits, operation, bindings, model_map):
    if state in ('P', 'U') and not missing and not partial_explained_by_limit(limits, operation, bindings, model_map):
        from .logging_contracts import canonical
        raise LoggingReviewError(canonical(partial_reason_issue(operation, bindings, model_map)))


def context_certified(basis):
    return (type(basis) is dict and basis.get('version') == 'existing-record-context/1'
            and basis.get('status') == 'supported' and basis.get('explicit') is True
            and basis.get('exact_single_hop') is True and basis.get('semantic_verified') is False
            and basis.get('prior_anchors') and not basis.get('reasons')
            and basis.get('caller', {}).get('status') == 'supported'
            and basis.get('emit', {}).get('status') == 'supported'
            and basis.get('emit', {}).get('entry_statement') is True
            and not basis.get('emit', {}).get('path_conditions'))


def coverage_limits(operation, bindings, model_map, policy=POLICY):
    """Coverage bounds are not claims that a record is absent."""
    if policy != POLICY: return []
    limits=[]
    if any(b.get('shared_with') for b in bindings): limits.append('shared_record_non_sufficient')
    if any(b.get('context_basis') is not None and not context_certified(b['context_basis']) for b in bindings):
        limits.append('contextual_basis_not_certified')
    if (operation.get('continuation_mode') == 'choice' and operation.get('outcome') in (None,'','unknown')
            and any(binding_is_sufficient(operation,b,model_map,policy) for b in bindings)):
        sides={}
        for b in bindings:
            if not binding_is_sufficient(operation,b,model_map,policy):continue
            witness=b.get('flow_context') or {}
            if witness.get('status')!='supported':continue
            # The host supplies normalized anchor refs with the path witness.
            conditions=[g for g in witness.get('path_conditions',[]) if any(
                a['path']==g['condition']['path'] and a['start_line']<=g['condition']['start_line']
                <=g['condition']['end_line']<=a['end_line'] for a in b.get('operation_anchors',[]))]
            if len(conditions)!=1:continue
            g=conditions[0];r=g['condition']
            key=(r['path'],r['start_line'],r['end_line'],r.get('column'))
            sides.setdefault(key,set()).add(g['branch'])
        if not any(s=={'true','false'} for s in sides.values()):limits.append('choice_branches_not_established')
    return limits


def sufficient_bindings(operation, bindings, model_map, policy=POLICY):
    if 'choice_branches_not_established' in coverage_limits(operation,bindings,model_map,policy):return []
    return [b for b in bindings if binding_is_sufficient(operation,b,model_map,policy)]


def shared_pair_allowed(group, model_map, policy=POLICY):
    """Narrow mutual opt-in. Shared records never create C by themselves."""
    if policy!=POLICY or model_map is None or len(group)!=2:return False
    (a,ba),(b,bb)=group
    ai,bi=a['operation_id'],b['operation_id']
    if ai==bi or ba.get('shared_with')!=bi or bb.get('shared_with')!=ai:return False
    if not ba.get('shared_meaning') or not bb.get('shared_meaning'):return False
    from .logging_contracts import canonical
    if not ba.get('operation_anchors') or not bb.get('operation_anchors'):return False
    if canonical(ba['operation_anchors'])==canonical(bb['operation_anchors']):return False
    for binding in (ba,bb):
        if binding.get('context_basis') is not None:
            if not context_certified(binding['context_basis']):return False
        elif not any(r['path']==e['path'] and r['start_line']<=e['start_line']<=e['end_line']<=r['end_line']
                     for r in binding['operation_anchors'] for e in binding['evidence']):return False
    return any({e.get('source'),e.get('target')}=={ai,bi} for e in model_map.get('edges',[]))


def physical_binding_key(binding, policy=POLICY):
    """Same source line stays conservatively shared even if refs differ in optional column/quote."""
    from .logging_contracts import canonical
    invocation=binding.get('invocation_ref')
    key=(invocation.get('path'),invocation.get('start_line')) if policy==POLICY and invocation else canonical(invocation)
    return binding.get('site_id'),key


def shared_use_allowed(row, binding, group, model_map, policy=POLICY, *, reconcile_policy=None, sites=None):
    """Source reuse is not an execution count or proof that the meanings agree.

    Each use still needs its own validated witness and per-operation sufficiency. This bounded
    check concerns this use only. Roles are relative to an operation, so different roles do not
    themselves conflict. Free-text meaning remains a semantic review. Explicit shared opt-ins
    retain their per-row non-sufficient limit; legacy exports retain their original group rule.
    """
    if reconcile_policy is None:
        return shared_pair_allowed(group, model_map, policy)
    if reconcile_policy != RECONCILE_POLICY or policy != POLICY or not sites:
        return False
    site = sites.get(binding.get('site_id'))
    if site is None or binding.get('operation_id') != row.get('operation_id'):
        return False
    basis = binding.get('context_basis')
    if basis is not None:
        return bool(context_certified(basis))
    return binding.get('invocation_ref') is None and any(
        _contains(anchor, site) for anchor in binding.get('operation_anchors', []))


def _saved_map_span(ref, data):
    from .logging_contracts import safe_path
    path=safe_path(ref['path']);files=data['source_fingerprints']
    if path not in files:
        prefix,sep,tail=path.partition('/')
        sources=data['map'].get('sources',[])
        roots=dict(data.get('source_roots') or {})
        if len(sources)==1:roots.setdefault(sources[0]['id'],'')
        for source in sources:
            name=source.get('name')
            if source['id'] not in roots and type(name) is str and any(p.startswith(name+'/') for p in files):
                roots[source['id']]=name
        if not sep or prefix not in roots:raise LoggingReviewError('Unknown source mapping in saved anchors')
        folder=roots[prefix]
        if folder:safe_path(folder)
        path=(folder+'/' if folder else '')+tail
        if path not in files:raise LoggingReviewError('Saved map anchor file is absent')
    return {'path':path,'start_line':ref.get('line_start',ref.get('start_line')),
            'end_line':ref.get('line_end',ref.get('end_line'))}


def _contains(outer, inner):
    return outer.get('path')==inner.get('path') and outer.get('start_line',0)<=inner['start_line']<=inner['end_line']<=outer.get('end_line',0)


def validate_saved_rows(view, data):
    try:
        return _validate_saved_rows(view, data)
    except LoggingReviewError:
        raise
    except (KeyError, TypeError, AttributeError, IndexError):
        raise LoggingReviewError('Malformed saved operation evidence') from None


def _validate_saved_rows(view, data):
    """Per-row semantic and witness checks of a saved logging report (any supported policy).

    The host accounting rules are re-applied; a saved status is never trusted because it is present.
    """
    from .logging_contracts import safe_path, safe_public
    from .logging_review import expected_signal
    policy = data.get('policy', LEGACY_POLICY)
    if policy not in POLICIES: raise LoggingReviewError('Unsupported logging policy: ' + str(policy)[:80])
    reconcile_policy = data.get('reconcile_policy')
    if reconcile_policy not in (None, RECONCILE_POLICY):
        raise LoggingReviewError('Unsupported shared-record accounting policy')
    from collections import defaultdict
    from .logging_contracts import canonical
    sites = {site['id']: site for site in data['inventory']['sites']}
    from .logging_correlation import PROFILE, saved_selectors, correlation_issue
    check_correlation = data.get('review_profile', {}).get('correlation_namespace') == PROFILE
    correlation_selectors = saved_selectors(data) if check_correlation else set()
    operations = {op['id']: op for op in view.operations}
    # Cross-row host rules of logging_review.reconcile: a shared physical invocation or a site that another row
    # proved to be a business command cannot leave a non-X state in a saved report.
    shared, negatives = defaultdict(list), defaultdict(set)
    for row in data['operations']:
        for binding in row.get('bindings') or []:
            shared[physical_binding_key(binding,policy)].append((row,binding))
        for item in row.get('candidate_reviews') or []:
            if type(item) is dict and item.get('classification') == 'not_log': negatives[item.get('site_id')].add(row.get('operation_id'))
    for row in data['operations']:
        if row.get('operation_id') not in operations:
            raise LoggingReviewError('Foreign operation in saved report')
        from .context_assessment import validate_saved
        validate_saved(row, data)
        operation = operations[row['operation_id']]
        if row.get('expected_signal') != safe_public(expected_signal(operation), source_data=True):
            raise LoggingReviewError('Expected operation meaning was changed in the saved report')
        if data.get('review_profile', {}).get('coverage_consistency'):
            validate_partial_reason(row.get('state'), row.get('missing'),
                coverage_limits(operation, row.get('bindings', []), data['map'], policy),
                operation, row.get('bindings', []), data['map'])
        if row.get('state') != 'X':
            for binding in row.get('bindings') or []:
                group=shared[physical_binding_key(binding,policy)]
                if (len({r['operation_id'] for r, _ in group})>1 and not shared_use_allowed(
                        row,binding,group,data['map'],policy,reconcile_policy=reconcile_policy,sites=sites)) or negatives.get(binding.get('site_id')):
                    raise LoggingReviewError('A shared or contested physical record cannot keep a non-X state')
        if type(row.get('bindings')) is not list or type(row.get('review_tasks')) is not list:
            raise LoggingReviewError('Invalid bindings/review tasks')
        if check_correlation:
            for index, binding in enumerate(row['bindings']):
                issue = correlation_issue(binding, correlation_selectors, sites.get(binding.get('site_id'), {}), index)
                if issue:
                    raise LoggingReviewError(canonical(issue))
        if type(row.get('missing', [])) is not list: raise LoggingReviewError('Invalid missing list')
        blocking = [t for t in row['review_tasks'] if task_is_blocking(t)]
        if row['state'] == 'C' and (not row['bindings'] or row.get('missing') or blocking
                or not row.get('search', {}).get('checks', {}).get('enclosing_code')
                or not sufficient_bindings(operation,row['bindings'],data['map'],policy)):
            raise LoggingReviewError('A covered operation lacks its necessary source-bound signal')
        if row['state'] in ('P', 'U') and not row.get('search', {}).get('complete'):
            raise LoggingReviewError('Partial/unlogged status requires the completed search checklist')
        if row['state'] in ('P', 'U') and policy != LEGACY_POLICY and blocking:
            raise LoggingReviewError('Partial/unlogged status conflicts with a blocking review task')
        if (policy != LEGACY_POLICY and row['state'] == 'P' and not row.get('missing')
                and row.get('search', {}).get('checks', {}).get('enclosing_code')
                and sufficient_bindings(operation,row['bindings'],data['map'],policy)):
            raise LoggingReviewError('Saved partial status disagrees with the host assessment')
        if row['state'] == 'P' and not row['bindings']: raise LoggingReviewError('Partial status needs an existing binding')
        if row['state'] == 'U' and row['bindings']: raise LoggingReviewError('Unlogged status conflicts with existing bindings')
        if row['state'] == 'X' and not row['review_tasks']: raise LoggingReviewError('Unassessed operation needs an addressed review task')
        if row['state'] == 'X' and policy != LEGACY_POLICY and not blocking:
            raise LoggingReviewError('Unassessed operation needs a blocking review task')
        if row['state'] == 'X' and row.get('missing_status', 'hypothesis') != 'hypothesis':
            raise LoggingReviewError('An unassessed row cannot publish established missing parts')
        if policy==POLICY and row.get('coverage_limits',[]) != coverage_limits(operation,row['bindings'],data['map'],policy):
            raise LoggingReviewError('Saved coverage limits disagree with source-bound accounting')
        for binding in row['bindings']:
            if data.get('review_profile', {}).get('lifecycle'):
                from .logging_contracts import validate_binding_moment
                validate_binding_moment(binding)
            if binding['operation_id'] != row['operation_id'] or binding['site_id'] not in sites:
                raise LoggingReviewError('Foreign binding in saved report')
            site = sites[binding['site_id']]
            evidence = binding.get('evidence')
            if type(evidence) is not list or not evidence: raise LoggingReviewError('Binding has no source evidence')
            if not any(r['path'] == site['path'] and r['start_line'] <= site['start_line'] <= site['end_line'] <= r['end_line'] for r in evidence):
                raise LoggingReviewError('Binding does not cite its physical source site')
            refs = evidence + binding.get('argument_evidence', []) + binding.get('emission_evidence', [])
            if binding.get('invocation_ref') is not None: refs = refs + [binding['invocation_ref']]
            if policy==POLICY:
                anchors=binding.get('operation_anchors',[])
                if type(anchors) is not list or not anchors:raise LoggingReviewError('Binding has no map-owned anchors')
                map_anchors=[_saved_map_span(r,data) for r in operation.get('evidence',[])]
                for a in anchors:
                    if {k:a[k] for k in ('path','start_line','end_line')} not in map_anchors:
                        raise LoggingReviewError('Binding anchor is not from this map operation')
                invocation=binding.get('invocation_ref')
                foreign=False
                if 'record_scope' not in binding:raise LoggingReviewError('Missing host navigation scope of record')
                scope=binding['record_scope'] or site
                if not _contains(scope,site):raise LoggingReviewError('Record scope does not contain the record')
                def intersects(a):return a['path']==scope['path'] and a['start_line']<=scope['end_line'] and scope['start_line']<=a['end_line']
                own_line=any(_contains(a,site) for a in anchors)
                own_member=any(intersects(a) for a in anchors)
                if not own_line or not own_member:
                    for other in operations.values():
                        if other['id']==operation['id']:continue
                        for ref in other.get('evidence',[]):
                            try:
                                other_span=_saved_map_span(ref,data)
                                foreign=foreign or (not own_member and intersects(other_span)) or (not own_line and _contains(other_span,site))
                            except LoggingReviewError:continue
                if invocation is not None and (binding.get('context_call_id') is not None or foreign):
                    if binding.get('context_basis') is None:raise LoggingReviewError('Contextual invocation needs an explicit source basis')
                refs += anchors
                witnesses=[binding.get('flow_context') or {}]
                basis=binding.get('context_basis')
                if basis is not None:
                    if type(basis) is not dict or basis.get('version')!='existing-record-context/1':
                        raise LoggingReviewError('Invalid contextual record basis')
                    witnesses += [basis.get('caller') or {},basis.get('emit') or {}]
                    refs += basis.get('prior_anchors',[])
                    if any(r not in binding.get('operation_anchors',[]) for r in basis.get('prior_anchors',[])):
                        # The predecessor statement or branch header must lie within a map-owned anchor.
                        if any(not any(r['path']==a['path'] and a['start_line']<=r['start_line']<=r['end_line']<=a['end_line']
                                       for a in binding.get('operation_anchors',[])) for r in basis.get('prior_anchors',[])):
                            raise LoggingReviewError('Context prior anchor does not address this operation')
                    if basis.get('status')=='supported':
                        if invocation is None or not _contains(basis.get('caller',{}).get('statement',{}),invocation):
                            raise LoggingReviewError('Context caller statement does not cover the invocation')
                        if not _contains(basis.get('emit',{}).get('statement',{}),site):
                            raise LoggingReviewError('Context emit statement does not cover the record')
                        if binding.get('flow_context')!=basis.get('caller'):
                            raise LoggingReviewError('Context and branch witnesses disagree')
                for witness in witnesses:
                    if witness:
                        if witness.get('version')!='existing-record-context/1' or witness.get('semantic_verified') is not False:
                            raise LoggingReviewError('Invalid existing-record statement witness')
                        refs += [witness[k] for k in ('statement','member') if k in witness]
                        refs += [g['condition'] for g in witness.get('path_conditions',[])]
            for ref in refs:
                safe_path(ref['path'])
                if (ref.get('file_sha256') != data['source_fingerprints'].get(ref['path'])
                        or type(ref.get('quote')) is not str or type(ref.get('start_line')) is not int
                        or type(ref.get('end_line')) is not int or ref['start_line'] < 1 or ref['end_line'] < ref['start_line']):
                    raise LoggingReviewError('Invalid saved source witness')
            route = binding.get('route')
            if route is not None and (type(route) is not dict or route.get('status') != 'confirmed'):
                raise LoggingReviewError('Saved invocation route is not confirmed')
            chain = binding.get('chain')
            if chain is not None:
                if type(chain) is not dict or site.get('kind') == 'log_call':
                    raise LoggingReviewError('Invalid saved logging chain')
                from .source_tools import covering_ranges
                cited = binding.get('emission_evidence', []) + evidence
                spans = chain.get('hops', []) + ([chain['terminal']] if chain.get('terminal') else [])
                contract = chain.get('source_contract')
                if chain.get('basis') == 'source_declared_adapter':
                    if (type(contract) is not dict or contract.get('authority') != 'model_interpretation_of_source'
                            or type(contract.get('meaning')) is not str or not contract['meaning'].strip()
                            or type(contract.get('candidate_id')) is not str):
                        raise LoggingReviewError('Invalid saved source adapter interpretation')
                    spans += [contract['declaration'], contract['implementation']]
                elif contract is not None:
                    raise LoggingReviewError('Source adapter authority does not match the saved chain basis')
                for hop in spans:
                    safe_path(hop['path'])
                    # Same rule as verify_logging_chain at review time: the cited refs together cover the span.
                    if covering_ranges(cited, hop['path'], hop['start_line'], hop['end_line']) is None:
                        raise LoggingReviewError('Saved logging chain hop is not cited by the binding')


def summarize(operation_ids, rows):
    ids = tuple(operation_ids)
    if len(set(ids)) != len(ids):
        raise LoggingReviewError('Duplicate input operation IDs')
    by_id = {}
    for row in rows:
        identifier = row.get('operation_id')
        if identifier not in ids or identifier in by_id or row.get('state') not in STATES:
            raise LoggingReviewError('Foreign, duplicate or invalid operation accounting')
        by_id[identifier] = row['state']
    counts = {key: sum(by_id.get(identifier, 'X') == key for identifier in ids) for key in STATES}
    size = len(ids)
    lower, upper = counts['P'] + counts['U'], counts['P'] + counts['U'] + counts['X']
    def number(n):
        return float(Fraction(100 * n, size)) if size else None
    exact = counts['X'] == 0
    def shown(n, rounding):
        return str((Decimal(100 * n) / Decimal(size)).quantize(Decimal('.1'), rounding=rounding)) if size else None
    return {'denominator': size, 'operation_ids': list(ids), 'counts': counts,
            'scope': 'Покрытие операций входной карты логами по коду; исполнение и доставка не проверены',
            'uncovered': {'numerator_lower': lower, 'numerator_upper': upper, 'denominator': size,
                          'lower': number(lower), 'upper': number(upper), 'exact': exact,
                          'display_lower': shown(lower, ROUND_HALF_UP if exact else ROUND_FLOOR),
                          'display_upper': shown(upper, ROUND_HALF_UP if exact else ROUND_CEILING),
                          'formula': '100 × (P + U) / N … 100 × (P + U + X) / N'},
            'no_record': {'numerator': counts['U'], 'denominator': size, 'percent': number(counts['U'])}}
