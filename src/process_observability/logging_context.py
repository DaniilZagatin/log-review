"""Bounded neighboring-call navigation over admitted text, not a fact/coverage engine."""

PY_EXACT = frozenset({'local_def','resolved_import','same_class_lexical'})


def neighboring_logs(navigation, ranges, focus, sites, maximum=3):
    """Read up to ``maximum`` log-bearing calls outside each owner's anchor.

    Only exact one-hop targets are selected. Known deeper or ambiguous log-bearing
    targets remain an explicit frontier. A call does not establish business meaning.
    """
    def ref(value):
        return {k:value[k] for k in ('path','start_line','end_line','column') if k in value}
    def calls(owner, java):
        return navigation.java.member_calls(owner['id']) if java else navigation.graph.outgoing(owner['id'])
    def targets(call, java):
        if java:
            return [navigation.java.members[i] for i in navigation.java.follow(call)]
        target=navigation.nodes.get(call.get('target_id'))
        return [target] if target and target['kind'] in navigation.callable_kinds and call.get('status') in PY_EXACT else []
    def logs(target):
        return [s['id'] for s in sites if s['path']==target['path']
                and target['start_line']<=s['start_line']<=s['end_line']<=target['end_line']]
    selected, frontier, seen = [], [], set()
    for span in ranges:
        owner=navigation.owner(span)
        if not owner or owner['id'] in seen: continue
        seen.add(owner['id']);java=owner['path'].endswith('.java')
        anchors=[f for f in focus if f['path']==owner['path']
                 and owner['start_line']<=f['start_line']<=f['end_line']<=owner['end_line']]
        if not anchors: continue
        candidates=[]
        for call in calls(owner,java):
            if any(f['start_line']<=call['start_line']<=f['end_line'] for f in anchors): continue
            if call['start_line']<=min(f['end_line'] for f in anchors):continue
            # These calls already have candidate/adapter-chain handling; don't let
            # repeated logger invocations consume the neighboring business-call slots.
            if any(s['path']==call['path'] and s['start_line']<=call['start_line']<=s['end_line']
                   and (s.get('column') is None or s['column']==call.get('column')) for s in sites):continue
            found=targets(call,java)
            if not found:
                if java:
                    resolution=navigation.java.resolve(call)
                    hints=[navigation.java.members[i] for i in dict.fromkeys(
                        [*resolution.get('targets',[]),*resolution.get('hints',[])])]
                else:
                    hints=[navigation.nodes.get(h.get('target_id')) for h in call.get('not_edges',[])]
                for target in hints:
                    if target and logs(target):
                        frontier.append({'invocation':ref(call),'target':ref(target),
                                         'candidate_site_ids':logs(target),'reason':'ambiguous_call_target'})
                continue
            for target in found:
                identifiers=logs(target)
                entry={'invocation':ref(call),'target':ref(target),'candidate_site_ids':identifiers}
                if identifiers:
                    distance=min(min(abs(call['start_line']-f['start_line']),
                                     abs(call['start_line']-f['end_line'])) for f in anchors)
                    after=any(call['start_line']>f['end_line'] for f in anchors)
                    candidates.append(((distance,not after,call['start_line'],call.get('column',0)),entry))
                else:
                    # A known second-hop record is a search limit, never a negative finding.
                    for inner in calls(target,java):
                        for deeper in targets(inner,java):
                            identifiers=logs(deeper)
                            if identifiers:
                                frontier.append({'invocation':ref(call),'via':ref(inner),'target':ref(deeper),
                                                 'candidate_site_ids':identifiers,'reason':'context_depth_limit'})
        for index,(_,entry) in enumerate(sorted(candidates,key=lambda p:p[0])):
            if index<maximum:selected.append(entry)
            else:frontier.append({**entry,'reason':'context_call_limit'})
    return {'selected':selected,'frontier':frontier,'maximum_per_owner':maximum,
            'authority':'Навигация по исходникам; не привязки логов и не доказательство результата операции'}


def refresh_frontier(packet, context):
    from .source_tools import covering_ranges
    from .logging_contracts import digest
    neighbors=packet.get('downstream_context')
    if not neighbors:return
    ids={s['id'] for s in packet['candidates']}
    pending_entries=[]
    for e in neighbors['frontier']:
        if (e['reason']=='ambiguous_call_target' or not set(e['candidate_site_ids'])<=ids
                or not covering_ranges(context.shown,e['target']['path'],e['target']['start_line'],e['target']['end_line'])):
            pending_entries.append(e)
        else:
            entry={k:v for k,v in e.items() if k not in ('reason','context_call_id')}
            entry['context_call_id']='context-'+digest(entry)[:16]
            if not any(x['context_call_id']==entry['context_call_id'] for x in neighbors.setdefault('delivered',[])):
                neighbors['delivered'].append(entry)
    neighbors['frontier']=pending_entries
    prefix='Не завершена проверка соседнего контекста с кандидатом журнала: '
    pending=packet['search']['pending']
    pending[:]=[p for p in pending if not p.startswith(prefix)]
    pending.extend(prefix+e['target']['path']+':'+str(e['target']['start_line'])+' ('+e['reason']+')'
                   for e in neighbors['frontier'])
