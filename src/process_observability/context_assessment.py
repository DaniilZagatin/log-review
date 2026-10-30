"""Accounting for delivered neighboring calls; never automatic semantic bindings."""
from copy import deepcopy
from .logging_contracts import LoggingReviewError, fields, text

USES = ('prior_fact_not_bound', 'not_relevant', 'unresolved')


def prior_fact_issue(row, profile):
    """Typed coherence only: a generic choice limit cannot hide an acknowledged, unaccounted fact."""
    if (profile.get('context_assessment') != 'delivered-call/2'
            or row.get('state') not in ('P', 'U') or row.get('missing')
            or set(row.get('coverage_limits', [])) != {'choice_branches_not_established'}):
        return None
    keys = [e['context_call_id'] for e in row.get('context_assessment', {}).get('entries', [])
            if e['use'] == 'prior_fact_not_bound']
    if not keys:
        return None
    return {'code': 'prior_fact_unaccounted', 'field': 'context_reviews / bindings / missing',
            'context_call_ids': keys,
            'instruction': 'Ты признал предшествующий факт, но частичное покрытие объяснено только общей '
                'границей ветвления. Если запись подтверждает исход этой операции, оформи binding с '
                'context_call_id. Иначе назови в missing, чего она не устанавливает для этой операции. '
                'Не добавляй привязку или новый лог ради формы; принадлежность другой операции не причина исключения.'}


def _review_value(value, entries, seen):
    """The same raw contract is used by acceptance and multi-error repair feedback."""
    fields(value, {'context_call_id', 'use', 'reason'}, {'context_call_id', 'use', 'reason'}, 'context review')
    key = value['context_call_id']
    if type(key) is not str or key not in entries or key in seen or value['use'] not in USES:
        raise LoggingReviewError('Unknown, duplicate or invalid context review')
    return key, value['use'], text(value['reason'], 'context review reason')


def review_shape_issues(reply, packet):
    """Describe all context review format errors even if a binding failed first; never repair the reply."""
    entries = {e['context_call_id']: e for e in packet.get('downstream_context', {}).get('delivered', [])}
    values = reply.get('context_reviews', [])
    if type(values) is not list or len(values) > 1000:
        return [{'code': 'invalid_context_review', 'field': 'context_reviews',
                 'message': 'Expected a list of at most 1000 context reviews', 'allowed_uses': list(USES)}]
    seen = bound_context_ids(reply.get('bindings'), list(entries.values()))
    for b in reply.get('bindings', []) if type(reply.get('bindings')) is list else []:
        if type(b) is dict:
            seen.update(e['context_call_id'] for e in entries.values()
                        if b.get('context_call_id') == e['context_call_id'] and b.get('site_id') in e['candidate_site_ids'])
    issues = []
    for index, value in enumerate(values):
        try:
            key, _, _ = _review_value(value, entries, seen)
            seen.add(key)
        except LoggingReviewError as exc:
            issues.append({'code': 'invalid_context_review', 'field': 'context_reviews[' + str(index) + ']',
                'message': str(exc), 'required_fields': ['context_call_id', 'use', 'reason'],
                'allowed_uses': list(USES),
                'instruction': 'Это оценка соседнего вызова, а не candidate_reviews. Верни точный выданный '
                    'context_call_id, use и reason. Не используй classification/related_operation_id; '
                    'не дублируй вызов с binding. Значение use выбери по коду, не ради формальной приёмки.'})
    return issues


def bound_context_ids(bindings, entries):
    result = set()
    for binding in bindings if type(bindings) is list else []:
        if type(binding) is not dict: continue
        invocation = binding.get('invocation_ref')
        if type(invocation) is not dict: continue
        matches = []
        for e in entries:
            if binding.get('context_call_id') is not None and binding['context_call_id'] != e['context_call_id']: continue
            target = e['invocation']
            if (binding.get('site_id') in e['candidate_site_ids']
                    and invocation.get('path') == target['path'] and invocation.get('start_line') == target['start_line']
                    and (invocation.get('column') is None or target.get('column') is None
                         or invocation['column'] == target['column'])):
                matches.append(e['context_call_id'])
        if len(matches) == 1: result.add(matches[0])
    return result


def required(reply):
    changes = reply.get('changes') if type(reply.get('changes')) is list else []
    return (bool(reply.get('missing')) or not reply.get('bindings')
            or any(isinstance(c, dict) and c.get('kind') in ('add_event', 'move_emit')
                   for c in changes))


def missing_entries(reply, packet):
    if not required(reply): return []
    entries = packet.get('downstream_context', {}).get('delivered', [])
    answered = bound_context_ids(reply.get('bindings'), entries)
    # Raw selector-only bindings are expanded by the main validator; feedback must not demand a second review.
    for b in reply['bindings'] if type(reply.get('bindings')) is list else []:
        if type(b) is dict:
            answered.update(e['context_call_id'] for e in entries if b.get('context_call_id') == e['context_call_id']
                            and b.get('site_id') in e['candidate_site_ids'])
    entry_by_id = {e['context_call_id']: e for e in entries}
    for value in reply['context_reviews'] if type(reply.get('context_reviews')) is list else []:
        try:
            key, _, _ = _review_value(value, entry_by_id, answered)
            answered.add(key)
        except LoggingReviewError:
            pass  # Malformed reviews get shape feedback; they cannot discharge an obligation.
    return [e for e in entries
            if e['context_call_id'] not in answered]


def assess(reply, packet, bindings, context):
    entries = {e['context_call_id']: e for e in packet.get('downstream_context', {}).get('delivered', [])}
    values = reply.get('context_reviews', [])
    if type(values) is not list or len(values) > 1000:
        raise LoggingReviewError('Invalid context_reviews')
    uses = {}
    for key in bound_context_ids(bindings, list(entries.values())):
        uses[key] = ('bound', 'Смысл записи указан в явной привязке операции')
    for value in values:
        key, use, reason = _review_value(value, entries, uses)
        uses[key] = (use, reason)
    missing = [key for key in entries if key not in uses] if required(reply) else []
    if missing:
        raise LoggingReviewError('Не оценен выданный контекст: ' + ', '.join(missing[:12])
            + '. Выбери binding с context_call_id либо context_reviews: prior_fact_not_bound, not_relevant, unresolved; '
            'объясни связь по коду. Пропуск контекста не доказывает отсутствие лога.')
    result = []
    for key, (use, reason) in uses.items():
        e = entries[key]
        result.append({'context_call_id': key, 'use': use, 'reason': reason,
            'invocation': context.verify_ref(e['invocation']), 'target': context.verify_ref(e['target']),
            'candidate_site_ids': deepcopy(e['candidate_site_ids'])})
    return {'required': required(reply), 'available_context_ids': list(entries), 'entries': result}


def validate_saved(row, data):
    assessment = row.get('context_assessment')
    if assessment is None:
        if data.get('review_profile', {}).get('context_assessment') and 'coverage_limits' in row:
            raise LoggingReviewError('Missing saved context assessment for the current profile')
        return  # Earlier policy/3 exports predate the request-profile change.
    from .logging_contracts import safe_path
    if (type(assessment) is not dict or type(assessment.get('required')) is not bool
            or type(assessment.get('available_context_ids')) is not list or type(assessment.get('entries')) is not list):
        raise LoggingReviewError('Invalid saved context assessment')
    available = assessment['available_context_ids']
    if required({**row,'changes':row.get('changes',[])+row.get('rejected_changes',[])}) and not assessment['required']:
        raise LoggingReviewError('Saved context requirement contradicts the absence/proposal claim')
    if any(type(i) is not str for i in available) or len(set(available)) != len(available):
        raise LoggingReviewError('Invalid saved context IDs')
    sites = {s['id']: s for s in data['inventory']['sites']}
    seen = set()
    for entry in assessment['entries']:
        key = entry.get('context_call_id')
        if key not in available or key in seen or entry.get('use') not in (*USES, 'bound'):
            raise LoggingReviewError('Invalid saved context review')
        seen.add(key)
        text(entry.get('reason'), 'context review reason')
        ids = entry.get('candidate_site_ids')
        if type(ids) is not list or not ids or any(i not in sites for i in ids):
            raise LoggingReviewError('Invalid saved context candidates')
        for ref in (entry['invocation'], entry['target']):
            safe_path(ref['path'])
            if (ref.get('file_sha256') != data['source_fingerprints'].get(ref['path'])
                    or type(ref.get('quote')) is not str or type(ref.get('start_line')) is not int
                    or type(ref.get('end_line')) is not int or not 1 <= ref['start_line'] <= ref['end_line']):
                raise LoggingReviewError('Invalid saved context source')
        target = entry['target']
        if any(sites[i]['path'] != target['path'] or not target['start_line'] <= sites[i]['start_line'] <= sites[i]['end_line'] <= target['end_line'] for i in ids):
            raise LoggingReviewError('Saved context target does not contain its records')
        if entry['use'] == 'bound' and key not in bound_context_ids(row['bindings'], assessment['entries']):
            raise LoggingReviewError('Context assessment cannot fabricate a binding')
        if assessment['required'] and entry['use'] == 'unresolved' and row['state'] in ('P','U'):
            raise LoggingReviewError('Unresolved context cannot publish established absence')
        if entry['use'] == 'prior_fact_not_bound' and row['state'] == 'U':
            raise LoggingReviewError('A recognized prior fact cannot be reported as no record')
    if assessment['required'] and seen != set(available):
        raise LoggingReviewError('Saved absence omitted delivered context')
    issue = prior_fact_issue(row, data.get('review_profile', {}))
    if issue:
        from .logging_contracts import canonical
        raise LoggingReviewError(canonical(issue))


def adjacent_sites(change, location, context, packet):
    """Only the exact current statement, or its immediate predecessor in the same scope."""
    if change.get('kind') not in ('add_event', 'move_emit'): return set()
    from .logging_source_context import _record
    from .proposal_placement import _tree, _anchor
    try:
        records, _ = _tree(context, location)
        anchor = _anchor(records, location)
    except (LoggingReviewError, ValueError, TypeError, KeyError, IndexError, SyntaxError):
        return set()
    result = set()
    for entry in packet.get('downstream_context', {}).get('delivered', []):
        if entry['invocation']['path'] != location['path']: continue
        try: selected, others, _ = _record(context, entry['invocation'])
        except (LoggingReviewError, ValueError, TypeError, KeyError, IndexError, SyntaxError): continue
        match = change.get('placement') == 'before' and anchor['node'].start == selected['node'].start
        if change.get('placement') == 'after' and anchor['scope'] == selected['scope']:
            earlier = [r for r in others if r['scope'] == selected['scope'] and r['node'].end <= selected['node'].start]
            previous = max(earlier, key=lambda r: r['node'].end) if earlier else None
            match = previous is not None and anchor['node'].start == previous['node'].start
        if match: result.update(entry['candidate_site_ids'])
    return result
