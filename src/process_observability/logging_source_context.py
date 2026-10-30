"""Host-owned statement witnesses for existing records; no execution or semantic inference."""
from .logging_contracts import LoggingReviewError
from .proposal_placement import _tree, _source_ref, LIMIT

VERSION = 'existing-record-context/1'
_PREVIEW_ERRORS = (LoggingReviewError, ValueError, SyntaxError, TypeError, KeyError,
                   AttributeError, IndexError, RecursionError)


def annotate_navigation(packet, context):
    """Small source-derived previews; no model selection, bindings or coverage claims."""
    for site in packet['candidates']:
        site['own_code'] = any(a['path'] == site['path'] and a['start_line'] <= site['start_line']
                              <= site['end_line'] <= a['end_line'] for a in packet['anchors'])
        if site['own_code']:
            try: path = statement_witness(context, site)
            except _PREVIEW_ERRORS as exc:
                path = {'status': 'not_established', 'reason': type(exc).__name__}
            site['path_preview'] = {k: path[k] for k in ('status', 'path_conditions', 'repetition_lines', 'reason') if k in path}
            site['path_preview'].update(navigation_only=True, semantic_verified=False)
    sites = {s['id']: s for s in packet['candidates']}
    for entry in packet.get('downstream_context', {}).get('delivered', []):
        previews = []
        for key in entry['candidate_site_ids']:
            if key not in sites: continue
            try: basis = contextual_basis(context, packet, sites[key], entry['invocation'], True)
            except _PREVIEW_ERRORS as exc:
                previews.append({'site_id': key, 'navigation_only': True, 'semantic_verified': False,
                    'status_if_explicitly_selected': 'not_established', 'reason': type(exc).__name__})
                continue
            previews.append({'site_id': key, 'navigation_only': True, 'semantic_verified': False,
                'status_if_explicitly_selected': basis['status'], 'exact_single_hop': basis['exact_single_hop'],
                'record_at_entry': basis['emit'].get('entry_statement', False),
                'caller_path_conditions': basis['caller'].get('path_conditions', []),
                'prior_anchors': basis['prior_anchors'], 'reasons': basis['reasons']})
        entry['selection_preview'] = previews


def _entry(context, path, selected, records):
    if selected['scope']:return False
    earlier=[r for r in records if r is not selected and r['node'].end<=selected['node'].start]
    if not earlier:return True
    if path.endswith('.py') and len(earlier)==1 and not earlier[0]['scope']:
        import ast, textwrap
        n=earlier[0]['node']
        raw=context.snapshot.files[path]._raw_text.splitlines()
        try:
            code=textwrap.dedent('\n'.join(raw[n.start[0]-1:n.end[0]]))
            parsed=ast.parse(code).body
            return len(parsed)==1 and isinstance(parsed[0],ast.Expr) and isinstance(parsed[0].value,ast.Constant) and type(parsed[0].value.value) is str
        except (SyntaxError,ValueError):pass
    return False


def _record(context, ref):
    records, scope = _tree(context, ref)
    member = _source_ref(context, ref['path'], scope, 'binding.enclosing_member')
    candidates = [r for r in records if r['node'].start[0] <= ref['start_line']
                  <= ref['end_line'] <= r['node'].end[0]
                  and (ref.get('column') is None or r['node'].start[0] != ref['start_line']
                       or r['node'].start[1] <= ref['column'])
                  and (ref.get('column') is None or r['node'].end[0] != ref['start_line']
                       or ref['column'] < r['node'].end[1])]
    if not candidates: raise ValueError('no_containing_statement')
    candidates.sort(key=lambda r: (r['node'].end[0]-r['node'].start[0],
                                   r['node'].end[1]-r['node'].start[1]))
    selected = candidates[0]
    if len(candidates)>1 and candidates[1]['node'].start == selected['node'].start:
        raise ValueError('ambiguous_statement')
    return selected, records, member


def statement_witness(context, ref):
    """Path at an existing statement. Conditions are historical evaluations, not current values."""
    info = {'version': VERSION, 'status': 'not_established', 'semantic_verified': False,
            'limitations': [LIMIT]}
    try:
        selected, records, member = _record(context, ref)
        node = selected['node']
        opaque = any(first[0] <= ref['start_line'] <= ref['end_line'] <= last[0]
                     for r in records for first, last in r['node'].opaque)
        supported = (not opaque and not selected['uncertain_before'] and selected['reachable_before'])
        guards = selected['before'] if supported else selected['lexical']
        info.update(status='supported' if supported else 'not_established',
                    statement=_source_ref(context, ref['path'], (node.start,node.end), 'binding.statement'),
                    member=member, point={'line':node.start[0],'column':node.start[1]},
                    path_conditions=[{'condition':_source_ref(context,ref['path'],span,'binding.path_condition'),
                                      'branch':side} for span,side in guards],
                    repetition_lines=[p[0] for p in selected['loops']],
                    entry_statement=_entry(context,ref['path'],selected,records))
    except (LoggingReviewError, ValueError, SyntaxError, TypeError, KeyError, IndexError, RecursionError) as exc:
        info['reason'] = type(exc).__name__
    return info


def contextual_basis(context, packet, site, invocation, explicit):
    """Conservative one-hop, immediate-next-statement, entry-record certificate.

    The caller must explicitly select the contextual use; adjacency avoids silently
    claiming value identity across intervening assignments. Argument meaning remains
    model interpretation even when all syntactic checks pass.
    """
    caller = statement_witness(context, invocation)
    emit = statement_witness(context, site)
    reasons=[]
    if not explicit: reasons.append('contextual_use_not_selected')
    owner=context.navigation.owner(site)
    caller_owner=context.navigation.owner(invocation)
    exact=False
    if owner and caller_owner:
        java=invocation['path'].endswith('.java')
        calls=(context.navigation.java.member_calls(caller_owner['id']) if java
               else context.navigation.graph.outgoing(caller_owner['id']))
        matching=[c for c in calls if c['path']==invocation['path']
                  and invocation['start_line']<=c['start_line']<=c['end_line']<=invocation['end_line']
                  and ('column' not in invocation or c.get('column')==invocation['column'])]
        if len(matching)==1:
            c=matching[0]
            if java: exact=context.navigation.java.follow(c)==[owner['id']]
            else:
                from .logging_context import PY_EXACT
                exact=c.get('target_id')==owner['id'] and c.get('status') in PY_EXACT
    if not exact: reasons.append('not_exact_single_hop')
    if caller['status']!='supported': reasons.append('caller_path_not_established')
    if emit['status']!='supported' or not emit.get('entry_statement') or emit.get('path_conditions'):
        reasons.append('record_not_at_unconditional_entry')
    preceding=[]
    try:
        selected, records, _ = _record(context,invocation)
        node=selected['node']
        earlier=[r for r in records if r['node'].end <= node.start and r['scope']==selected['scope']]
        previous=max(earlier,key=lambda r:r['node'].end) if earlier else None
        parent=None
        if previous is None and selected['scope']:
            start,branch=selected['scope'][-1]
            if branch in ('true','false'):
                parent=next((r for r in records if r['node'].start==start and r['node'].kind=='if'),None)
        for anchor in packet['anchors']:
            if anchor['path']!=invocation['path']:continue
            if previous and anchor['start_line'] <= previous['node'].start[0] <= previous['node'].end[0] <= anchor['end_line']:
                preceding.append(_source_ref(context,anchor['path'],(previous['node'].start,previous['node'].end),'binding.prior_anchor'))
            elif parent and anchor['start_line']<=parent['node'].condition[0][0]<=parent['node'].condition[1][0]<=anchor['end_line']:
                preceding.append(_source_ref(context,anchor['path'],parent['node'].condition,'binding.prior_branch_header'))
        if not preceding:reasons.append('not_immediately_after_operation_anchor')
    except (LoggingReviewError,ValueError,KeyError,TypeError,IndexError):
        reasons.append('anchor_order_not_established')
    return {'version':VERSION,'status':'supported' if not reasons else 'not_established',
            'semantic_verified':False,'explicit':bool(explicit),'exact_single_hop':exact,
            'caller':caller,'emit':emit,'prior_anchors':preceding,'reasons':reasons,
            'meaning_authority':'model_interpretation_of_source'}
