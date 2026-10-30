"""Assemble unique source routes for already selected bindings, without interpreting prose."""
from copy import deepcopy
from .logging_contracts import LoggingReviewError
from .logging_context import PY_EXACT
from .source_tools import covering_ranges


def _covers(outer, inner):
    return (outer['path'] == inner['path'] and outer['start_line'] <= inner['start_line']
            <= inner['end_line'] <= outer['end_line'])


def _call_key(call):
    return tuple(call.get(k) for k in ('path', 'start_line', 'end_line', 'column'))


def _unique_entry(packet, context, site):
    if any(_covers(r, site) for r in packet['operation_ranges']):
        return None  # Own map code never acquires an invented caller requirement.
    neighbors = packet.get('downstream_context', {})
    options = [e for e in neighbors.get('delivered', []) if site['id'] in e['candidate_site_ids']]
    if len(options) != 1 or any(site['id'] in e['candidate_site_ids'] for e in neighbors.get('frontier', [])):
        return None
    entry = options[0]
    if not all(covering_ranges(context.shown, r['path'], r['start_line'], r['end_line'])
               for r in (site, entry['target'], entry['invocation'])):
        return None
    nav = context.navigation
    target = nav.owner(site)
    if target is None:
        return None
    # Inspect all operation calls, including those excluded from the downstream shortlist.
    languages, possible = set(), set()
    for span in packet['operation_ranges']:
        owner = nav.owner(span)
        if owner is None:
            return None  # Unindexed anchors cannot establish a unique route.
        languages.add(owner['path'].lower().endswith('.java'))
    for java in languages:
        # Full source index includes nested Python functions and Java lambda/anonymous bodies.
        # An enclosing owner's outgoing list alone can miss a second call in the same range.
        calls = nav.java.calls.values() if java else nav.data['calls']
        for call in calls:
            if not any(_covers(r, call) for r in packet['operation_ranges']):
                continue
            if java:
                resolution = nav.java.resolve(call)
                candidates = set(resolution.get('targets', [])) | set(resolution.get('hints', []))
                exact = nav.java.follow(call) == [target['id']]
            else:
                candidates = {call.get('target_id')} | {h.get('target_id') for h in call.get('not_edges', [])}
                exact = call.get('target_id') == target['id'] and call.get('status') in PY_EXACT
            if target['id'] in candidates or exact:
                if not exact:
                    return None
                possible.add(_call_key(call))
    if possible != {_call_key(entry['invocation'])}:
        return None
    invocation = entry['invocation']
    route = nav.invocation_route(invocation, site, [invocation], context.shown)
    if route is not True and (type(route) is not dict or route.get('status') != 'confirmed'):
        return None
    from .logging_source_context import contextual_basis
    if contextual_basis(context, packet, site, invocation, True)['status'] != 'supported':
        return None
    return entry


def assemble_context_routes(reply, packet, context):
    """Return a private copy and audit notes; never alter the stored model answer.

    An explicit conflicting field or contextual judgment is not repaired. Semantic
    fields, contract choices and coverage still pass the ordinary validator.
    """
    result = deepcopy(reply)
    if type(result) is not dict or type(result.get('bindings')) is not list:
        return result, []
    sites = {s['id']: s for s in packet['candidates']}
    reviews = result.get('context_reviews', [])
    discussed = {r.get('context_call_id') for r in reviews if type(r) is dict
                 and type(r.get('context_call_id')) is str} if type(reviews) is list else set()
    notes = []
    for index, binding in enumerate(result['bindings']):
        if (type(binding) is not dict or type(binding.get('site_id')) is not str
                or binding.get('context_call_id') is not None or binding.get('invocation_ref') is not None
                or binding.get('argument_evidence') not in (None, [])):
            continue
        site = sites.get(binding['site_id'])
        if site is None:
            continue
        try:
            entry = _unique_entry(packet, context, site)
        except (LoggingReviewError, ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
            continue  # Missing proof retains the original strict rejection, not a fabricated route.
        if entry is None or entry['context_call_id'] in discussed:
            continue
        binding['context_call_id'] = entry['context_call_id']
        notes.append('Ссылки bindings[' + str(index) + '] собраны хостом по единственному проверенному вызову '
                     + entry['context_call_id'] + '; смысл записи задан моделью')
    return result, notes
