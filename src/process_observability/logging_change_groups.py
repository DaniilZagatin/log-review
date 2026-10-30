"""Report-only index of proposals at one source point; never merges their claims."""
from collections import defaultdict
from .logging_contracts import canonical


def proposal_groups(changes):
    groups=defaultdict(list)
    for change in changes:
        location=change.get('location')
        if type(location) is not dict or change.get('placement') not in ('before','after','at'):continue
        context=change.get('placement_context') or {}
        supported=context.get('status')=='supported'
        guards=context.get('path_conditions',[]) if supported else change.get('guards',[])
        conditions=sorted(canonical({'path':g.get('condition',{}).get('path'),
            'first':g.get('condition',{}).get('start_line'),'last':g.get('condition',{}).get('end_line'),
            'column':g.get('condition',{}).get('column'),'branch':g.get('branch')}) for g in guards)
        point=context.get('point') if supported else {k:location.get(k) for k in ('start_line','end_line','column')}
        key=canonical([change.get('kind'),location.get('path'),change['placement'],point,conditions,supported])
        groups[key].append(change)
    result=[]
    for items in groups.values():
        if len(items)<2:continue
        signatures={canonical({'logger':(c.get('logger') or {}).get('expression'),
            'fields':sorted(canonical({k:f.get(k) for k in ('name','origin','expression')})
                            for f in c.get('fields',[]))}) for c in items}
        result.append({'change_ids':[c['id'] for c in items],
                       'operation_ids':sorted({o for c in items for o in c.get('operation_ids',[])}),
                       'unverified_grouping':(items[0].get('placement_context') or {}).get('status')!='supported',
                       'structured_conflict':len(signatures)>1})
    return result
