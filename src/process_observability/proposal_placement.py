"""Bounded syntactic insertion paths; never execute source or infer business meaning.

Guards describe historical branch evaluations along the path, not present values.
Unsupported flow is visible, never silently certified. Source quotes are host-owned.
"""
import ast
import bisect
import io
import re
import tokenize
from dataclasses import dataclass, field

from .logging_contracts import LoggingReviewError, fields

VERSION = 'statement-path/1'
LIMIT = 'Проверена только синтаксическая согласованность точки и ветвей; бизнес-смысл, выполнение, исключения и доставка не доказаны'


@dataclass
class _Node:
    start: tuple
    end: tuple
    kind: str = 'statement'
    condition: tuple | None = None
    yes: list = field(default_factory=list)
    no: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    unsupported: bool = False
    needs_braces: bool = False
    affects_flow: bool = True
    infinite: bool = False
    opaque: list = field(default_factory=list)
    resets_sequence: bool = False


class PlacementReadError(LoggingReviewError):
    def __init__(self,path,first,last):
        self.reads=[{'path':path,'start_line':n,'end_line':min(n+199,last)} for n in range(first,last+1,200)]
        super().__init__('placement.enclosing_member: source lines not delivered; прочитай '+path+':'+str(first)+'–'+str(last))


class PlacementBoundaryError(LoggingReviewError):
    """Host-derived alternatives for conditional feedback, never a replacement anchor."""
    def __init__(self, spans):
        self.boundaries = tuple(spans)
        super().__init__('anchor_not_statement_boundary: укажи полный диапазон одного оператора; '
            'для нескольких операторов на строке укажи column начала (с нуля). Границы (line,column): '+str(spans))


def _python(raw, location, syntax=None):
    tree,tokens = syntax or (ast.parse(raw),list(tokenize.generate_tokens(io.StringIO(raw).readline)))
    lines = raw.splitlines()
    starts=[t.start for t in tokens]
    def header_end(n):
        depth=0;start=pos(n)
        for i in range(bisect.bisect_left(starts,start),len(tokens)):
            token=tokens[i]
            if token.string in ('(','[','{'):depth+=1
            elif token.string in (')',']','}'):depth-=1
            elif token.string==':' and depth==0:return token.end
        raise ValueError('if_header_not_established')
    def pos(node, end=False):
        line = node.end_lineno if end else node.lineno
        byte = node.end_col_offset if end else node.col_offset
        return (line, len(lines[line-1].encode('utf8')[:byte].decode('utf8')))
    owners = [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
              and n.lineno <= location['start_line'] <= location['end_line'] <= n.end_lineno]
    owner = min(owners,key=lambda n:(n.end_lineno-n.lineno,-n.col_offset)) if owners else tree
    def make(n):
        result = _Node(pos(n),pos(n,True))
        if isinstance(n,ast.If):
            result.kind='if';result.condition=(pos(n),header_end(n))
            result.yes=[make(x) for x in n.body];result.no=[make(x) for x in n.orelse]
        elif isinstance(n,(ast.For,ast.AsyncFor,ast.While)):
            result.kind='loop';result.blocks=[[make(x) for x in n.body]]
            if isinstance(n,ast.While) and isinstance(n.test,ast.Constant):result.infinite=bool(n.test.value)
            if n.orelse:
                result.unsupported=True;result.blocks.append([make(x) for x in n.orelse])
        elif isinstance(n,(ast.Return,ast.Raise,ast.Break,ast.Continue)):
            result.kind='exit'
        elif isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
            # A nested declaration is not executed as its body. An anchor inside it uses its own owner.
            result.unsupported=True;result.affects_flow=False
        elif isinstance(n,(ast.Try,ast.TryStar,ast.With,ast.AsyncWith,ast.Match)):
            result.unsupported=True
            for key in ('body','orelse','finalbody'):
                if getattr(n,key,None):result.blocks.append([make(x) for x in getattr(n,key)])
            for handler in getattr(n,'handlers',[]):result.blocks.append([make(x) for x in handler.body])
            for case in getattr(n,'cases',[]):result.blocks.append([make(x) for x in case.body])
        return result
    scope = ((owner.lineno,owner.col_offset),(owner.end_lineno,owner.end_col_offset)) if owners else ((1,0),(len(lines),len(lines[-1])))
    return [make(n) for n in owner.body],scope


def _java(context,path,location):
    index=context.navigation.java
    owner=index.owner_member(path,location['start_line'],location['end_line'])
    f=index.files.get(path)
    if f is None or f.status=='parse_failed' or owner is None or not owner.get('body'):
        raise ValueError('member_or_syntax_not_established')
    values=f.values;match=f.match
    def pos(i,end=False):
        return (f.lines[i],f.columns[i]+(len(values[i] or '') if end else 0))
    def sequence(lo,hi):
        out=[]
        while lo<hi:
            node,nxt=statement(lo,hi)
            if nxt<=lo:raise ValueError('statement_parse_stalled')
            out.append(node);lo=nxt
        return out
    def body(i,hi):
        if values[i]=='{':
            end=match[i];return sequence(i+1,end),end+1
        node,end=statement(i,hi);node.needs_braces=True;return [node],end
    def statement(i,hi):
        start=i;word=values[i]
        if word=='{':
            last=match[i]
            return _Node(pos(i),pos(last,True),kind='block',blocks=[sequence(i+1,last)]),last+1
        if word=='if' and i+1<hi and values[i+1]=='(':
            end=match[i+1];yes,nxt=body(end+1,hi);no=[]
            if nxt<hi and values[nxt]=='else':no,nxt=body(nxt+1,hi)
            opaque=[(pos(k),pos(match[k],True)) for k in range(i+2,end) if values[k]=='{']
            return _Node(pos(i),pos(nxt-1,True),kind='if',condition=(pos(i),pos(end,True)),yes=yes,no=no,opaque=opaque),nxt
        if word in ('for','while') and i+1<hi and values[i+1]=='(':
            end=match[i+1];children,nxt=body(end+1,hi)
            condition=values[i+2:end]
            if word=='for':
                separators=[k for k in range(i+2,end) if values[k]==';']
                condition=values[separators[0]+1:separators[1]] if len(separators)==2 else ['unknown']
            while len(condition)>2 and condition[0]=='(' and condition[-1]==')':condition=condition[1:-1]
            infinite=condition==['true'] or (word=='for' and not condition)
            opaque=[(pos(k),pos(match[k],True)) for k in range(i+2,end) if values[k]=='{']
            return _Node(pos(i),pos(nxt-1,True),kind='loop',blocks=[children],infinite=infinite,opaque=opaque),nxt
        # Blocks of unsupported constructs are parsed for precise anchors, never for path proof.
        control=word in ('switch','try','catch','finally','do','synchronized') or (i+1<hi and values[i+1]==':')
        unsupported=control
        blocks=[];opaque=[];j=i
        while j<hi:
            token=values[j]
            if token in ('(', '['):
                end=match[j]
                for k in range(j+1,end):
                    if values[k]=='{':opaque.append((pos(k),pos(match[k],True)))
                j=end+1;continue
            if token=='{':
                end=match[j]
                if control:blocks.append(sequence(j+1,end))
                else:opaque.append((pos(j),pos(end,True)))
                j=end+1
                if control:
                    if j<hi and ((word=='try' and values[j] in ('catch','finally')) or (word=='do' and values[j]=='while')):continue
                    if j<hi and values[j]==';':j+=1
                    break
                prefix=values[start:next(k for k in range(start,j) if values[k]=='{')]
                if 'new' not in prefix and any(t in prefix for t in ('class','interface','enum','record')):break
                continue
            if token==';':j+=1;break
            j+=1
        if j==i:raise ValueError('statement_boundary_not_established')
        kind='exit' if word in ('return','throw','break','continue') else 'statement'
        if word in ('break','continue') and j-start!=2:unsupported=True  # labelled exit
        return _Node(pos(i),pos(j-1,True),kind=kind,blocks=blocks,unsupported=unsupported,opaque=opaque,
                     resets_sequence=word in ('case','default')),j
    lo,hi=owner['body']
    return sequence(lo,hi),((owner['start_line'],0),(owner['end_line'],0))


def _paths(nodes):
    """Conservative structured flow, with common path facts on branch joins."""
    records=[]
    def block(items,guards=(),reachable=True,uncertain=False,loops=(),lexical=(),scope=()):
        guards=list(guards);direct_exit=False
        for node in items:
            if node.resets_sequence:direct_exit=False;reachable=True;uncertain=True
            rec={'node':node,'before':list(guards),'reachable_before':reachable,
                 'uncertain_before':uncertain or node.unsupported,'loops':loops,'lexical':list(lexical),'scope':scope,
                 'direct_exit_before':direct_exit}
            if node.kind=='if':
                branches=[]
                for children,branch in ((node.yes,'true'),(node.no,'false')):
                    branches.append(block(children,guards+[(node.condition,branch)],reachable,uncertain,loops,
                                          lexical+((node.condition,branch),),scope+((node.start,branch),)))
                live=[b for b in branches if b[1]]
                reachable=bool(live)
                if live:guards=[g for g in live[0][0] if all(g in b[0] for b in live[1:])]
                uncertain=uncertain or any(b[2] for b in branches)
            elif node.kind=='loop':
                for children in node.blocks:
                    block(children,guards,reachable,uncertain or node.unsupported,loops+(node.start,),lexical,
                          scope+((node.start,'loop'),))
                # Loops may run zero times. Never propagate body guards or body exits outside.
                uncertain=uncertain or node.unsupported or node.infinite
            elif node.kind=='block':
                guards,reachable,uncertain=block(node.blocks[0],guards,reachable,uncertain,loops,lexical,
                                                scope+((node.start,'block'),))
            elif node.unsupported:
                for number,children in enumerate(node.blocks):
                    block(children,guards,reachable,True,loops,lexical,scope+((node.start,number),))
                uncertain=uncertain or node.affects_flow
            elif node.kind=='exit':reachable=False
            # Only a directly preceding unconditional exit, never a claim about an opaque branch.
            if node.kind=='exit':direct_exit=True
            elif node.unsupported:direct_exit=False
            rec.update(after=list(guards),reachable_after=reachable,uncertain_after=uncertain or (node.unsupported and node.affects_flow))
            rec['direct_exit_after']=direct_exit
            records.append(rec)
        return guards,reachable,uncertain
    block(nodes)
    return records


def _source_ref(context,path,span,label):
    start,end=span
    return context.verify_ref({'path':path,'start_line':start[0],'end_line':end[0],'column':start[1]},label)


def _anchor(records,location):
    candidates=[r for r in records if r['node'].start[0]==location['start_line']
                and r['node'].end[0]==location['end_line']
                and ('column' not in location or r['node'].start[1]==location['column'])]
    if 'column' not in location and candidates:
        # A complete same-line compound is the anchor; never silently choose a nested statement.
        candidates=[r for r in candidates if not any(q is not r and q['node'].start<=r['node'].start
            and r['node'].end<=q['node'].end for q in candidates)]
    if len(candidates)!=1:
        spans=sorted({(r['node'].start,r['node'].end) for r in records
                      if r['node'].start[0]<=location['start_line']<=r['node'].end[0]})[:5]
        raise PlacementBoundaryError(spans)
    return candidates[0]


def _tree(context,location):
    raw=context.snapshot.files[location['path']]._raw_text
    from .log_discovery import _ODD_LINES
    if _ODD_LINES.search(raw):raise ValueError('unsupported_line_separator')
    cache=getattr(context,'_placement_trees',None)
    if cache is None:cache=context._placement_trees={}
    syntax=None
    if location['path'].endswith('.py'):
        from .logger_access import _python_symbols
        parsed=_python_symbols(context,location['path'])
        if parsed is None:raise ValueError('python_syntax_not_established')
        owners=[n for n in ast.walk(parsed[0]) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                and n.lineno<=location['start_line']<=location['end_line']<=n.end_lineno]
        owner=min(owners,key=lambda n:(n.end_lineno-n.lineno,-n.col_offset)) if owners else parsed[0]
        key=(location['path'],getattr(owner,'lineno',0),getattr(owner,'end_lineno',0))
        if key in cache:return cache[key]
        syntax=(parsed[0],list(tokenize.generate_tokens(io.StringIO(raw).readline)))
        nodes,scope=_python(raw,location,syntax)
    elif location['path'].endswith('.java'):
        owner=context.navigation.java.owner_member(location['path'],location['start_line'],location['end_line'])
        key=(location['path'],owner['id'] if owner else None)
        if key in cache:return cache[key]
        nodes,scope=_java(context,location['path'],location)
    else:raise ValueError('unsupported_language')
    cache[key]=(_paths(nodes),scope)
    return cache[key]


def _fallback(position,guards,location,context,reason):
    raw=context.snapshot.files[location['path']]._raw_text
    point=(location['end_line'],len(raw.splitlines()[location['end_line']-1])) if position=='after' else (location['start_line'],0)
    return {'placement':position,'guards':guards,'placement_context':{'version':VERSION,
        'status':'not_established','semantic_verified':False,'point':{'line':point[0],'column':point[1]},
        'point_basis':'conservative_line_boundary','unverified_guards':guards,
        'limitations':[LIMIT,'Синтаксис/границы не поддержаны: '+reason]}},point


def validate_placement(item,location,context,sites):
    """Return public checked placement and an internal exact point for access checks."""
    required=item['kind'] in ('add_event','move_emit')
    position=item.get('placement')
    if position is None and not required:
        return None,None
    if position not in ('before','after','at') or (required and position=='at'):
        raise LoggingReviewError('change.placement: add_event/move_emit требуют before|after; '
                                 'at допустим только для изменения указанной существующей записи')
    if position=='at' and not any(s['path']==location['path'] and s['start_line']==location['start_line']
        and s['end_line']==location['end_line'] for key,s in sites.items() if key in item.get('existing_site_ids',[])):
        raise LoggingReviewError('change.placement at: location должна совпадать с указанной existing_site_ids записью')
    declared=item.get('guards')
    if type(declared) is not list or len(declared)>32:
        raise LoggingReviewError('change.guards: нужен список условий ветви (пустой только для пути без условий)')
    guards=[]
    for guard in declared:
        fields(guard,{'condition','branch'},{'condition','branch'},'change.guard')
        if guard['branch'] not in ('true','false'):raise LoggingReviewError('change.guard.branch: true|false как строки')
        guards.append({'condition':context.verify_ref(guard['condition'],'change.guard.condition'),'branch':guard['branch']})
    try:
        records,scope=_tree(context,location)
    except (SyntaxError,ValueError,RecursionError,TypeError,IndexError,KeyError,tokenize.TokenError) as exc:
        return _fallback(position,guards,location,context,type(exc).__name__)
    # Earlier branch exits affect fallthrough. A line-valid anchor alone is insufficient.
    try:_source_ref(context,location['path'],scope,'placement.enclosing_member')
    except LoggingReviewError:raise PlacementReadError(location['path'],scope[0][0],scope[1][0]) from None
    opaque_record=None
    for record in records:
        for first,last in record['node'].opaque:
            if first[0]<=location['start_line']<=location['end_line']<=last[0]:
                # Whole outer call remains a supported anchor; its inner callback does not.
                if (record['node'].start[0],record['node'].end[0])==(location['start_line'],location['end_line']) and 'column' not in location:continue
                if location['start_line']==first[0] and location.get('column',-1)<=first[1]:continue
                if location['start_line']==last[0] and ('column' not in location or location['column']>=last[1]):continue
                opaque_record=record;break
        if opaque_record is not None:break
    selected=opaque_record or _anchor(records,location)
    side='after' if position=='after' else 'before'
    node=selected['node'];point=node.end if side=='after' else node.start
    if opaque_record is not None:_,point=_fallback(position,guards,location,context,'opaque_callback_or_initializer')
    uncertain=opaque_record is not None or selected['uncertain_'+side]
    certain=selected['lexical'] if uncertain else selected[side]
    actual=[{'condition':_source_ref(context,location['path'],span,'placement.path_condition'),'branch':branch,
             'basis':'lexical' if (span,branch) in selected['lexical'] else 'structured_flow'} for span,branch in certain]
    info={'version':VERSION,'status':'not_established' if uncertain else 'supported','semantic_verified':False,
          'point':{'line':point[0],'column':point[1]},'path_conditions':actual,
          'repetition_lines':[p[0] for p in selected['loops']], 'limitations':[LIMIT]}
    if opaque_record is None and node.needs_braces and position!='at':
        info['required_edits']=['Обернуть одиночное тело ветви/цикла в { } вместе с новой записью; иначе изменится управление']
    raw=context.snapshot.files[location['path']]._raw_text
    line=raw.splitlines()[node.start[0]-1]
    if location['path'].endswith('.py') and node.start[1]>len(line)-len(line.lstrip()) and position!='at':
        info['required_edits']=['Развернуть однострочное тело в отдельные строки с сохранением отступов и ветви; не вставлять запись снаружи тела']
    if opaque_record is None and selected['direct_exit_'+side]:
        raise LoggingReviewError('unreachable_insertion: точка после безусловного выхода в той же последовательности')
    if uncertain:info['limitations'].append('Control flow содержит неподдержанный переход/конструкцию; путь не сертифицирован')
    else:
        info['unreachable_detected']=not selected['reachable_'+side]
        if info['unreachable_detected']:raise LoggingReviewError('unreachable_insertion: точка после безусловного выхода в той же последовательности; '
                                                         'для записи об отказе выбери before, успех перенеси только по смыслу исходника')
    matched=set();unverified=[]
    for guard in guards:
        ref=guard['condition']
        choices=[(i,g) for i,g in enumerate(actual) if all(ref[k]==g['condition'][k] for k in ('path','start_line','end_line'))
                 and ('column' not in ref or ref['column']==g['condition']['column'])]
        if not choices and uncertain:
            unverified.append(guard);continue
        if len(choices)!=1:raise LoggingReviewError('guard_not_on_path: условие не задаёт этот путь или неоднозначно; '
                                                    'укажи полный header if и column для одной строки. Известный путь: '+str(actual))
        i,found=choices[0]
        if found['branch']!=guard['branch']:raise LoggingReviewError('guard_contradiction: выбранная точка в противоположной ветке. '
            'Перенеси точку по смыслу операции либо исправь неверный what/timing/исход; не копируй полярность ради формы. Известный путь: '+str(actual))
        if i in matched:raise LoggingReviewError('duplicate_guard: одно условие пути указано повторно')
        matched.add(i)
    if len(matched)!=len(actual):raise LoggingReviewError('missing_guard: перечисли все известные условия пути (значение в момент проверки, '
                                                          'не текущее значение). Известный путь: '+str(actual))
    if unverified:info['unverified_guards']=unverified
    return {'placement':position,'guards':guards,'placement_context':info},point


def evidence_precedes(ref,location,context):
    """Conservative for same-line references: no availability from code after the insertion point."""
    point=location.get('_insertion_point')
    if point is None:return True
    if ref['path']!=location['path'] or ref['end_line']>point[0]:return False
    if ref['end_line']<point[0]:return True
    line=context.snapshot.files[ref['path']]._raw_text.splitlines()[ref['end_line']-1]
    return len(line.rstrip())<=point[1]


def _binding_allows(context,location,point,expression,records,target):
    """Resolve root bindings, not the block of an arbitrary usage. No definite-assignment claim."""
    path=location['path'];raw=context.snapshot.files[path]._raw_text
    if path.endswith('.java'):
        from .java_declarations import _TOKEN
        from .logger_access import _java_positions
        tokens=[m.group() for m in _TOKEN.finditer(expression) if m.lastgroup not in ('comment','string','char','text_block','space')]
        roots={t for i,t in enumerate(tokens) if re.fullmatch(r'[^\W\d][\w$]*|[$_][\w$]*',t)
               and (i==0 or tokens[i-1] not in ('.','::')) and t not in ('true','false','null','new')}
        index=context.navigation.java;owner=index.owner_member(path,location['start_line'],location['end_line'])
        if owner is None:return False
        f=index.files[path];positions=_java_positions(f,owner,location)
        if not positions:return False
        occurrence=bisect.bisect_left(list(zip(f.lines,f.columns)),point)
        for root in roots:
            if root in ('this','super'):
                if owner['static']:return False
                continue
            before=index._local(owner,root,occurrence+0.1);after=index._local(owner,root,positions[0])
            if before is not None or after is not None:
                if before is None or before!=after:return False
                # Pattern-variable flow scoping is not modelled by the declaration index.
                k=before[2]-1
                while k>=owner['body'][0] and f.values[k] not in (';','{','}'):
                    if f.values[k]=='instanceof':return False
                    k-=1
            elif not any(p['name']==root for p in owner['params']):
                field=index._field_in_scope(owner['type'],root)
                if field is not None and owner['static'] and not field[0]['static']:return False
                if field is None and any(f.values[k]==root and f.values[k-1]==',' and f.values[k+1] in ('=',',',';','[')
                                         for k in range(owner['body'][0],owner['body'][1]-1)):
                    return False  # secondary declarator lifetime is not indexed; never claim nonlocal availability
        return True
    if not path.endswith('.py'):return None
    from .logger_access import _python_symbols
    parsed=_python_symbols(context,path)
    if parsed is None:return False
    try:roots={n.id for n in ast.walk(ast.parse(expression,mode='eval')) if isinstance(n,ast.Name)}
    except (SyntaxError,ValueError):return None
    tree,tables,_,_=parsed
    owners=[n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
            and n.lineno<=location['start_line']<=location['end_line']<=n.end_lineno]
    owner=min(owners,key=lambda n:(n.end_lineno-n.lineno,-n.col_offset)) if owners else tree
    table=next((t for t in tables if owners and t.get_name()==owner.name and t.get_lineno()==owner.lineno),None)
    lines=raw.splitlines()
    def at(n,end=False):
        line=n.end_lineno if end else n.lineno;column=n.end_col_offset if end else n.col_offset
        return line,len(lines[line-1].encode()[:column].decode())
    def inside(n):return at(n)<=point<at(n,True)
    # Comprehension/lambda names and deleted exception aliases cannot be borrowed from their own scope.
    for n in ast.walk(owner):
        if isinstance(n,(ast.Lambda,ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp)) and inside(n):
            bound={x.id for x in ast.walk(n) if isinstance(x,ast.Name) and isinstance(x.ctx,ast.Store)}
            if isinstance(n,ast.Lambda):bound|={a.arg for a in (*n.args.posonlyargs,*n.args.args,*n.args.kwonlyargs)}
            if roots & bound:return False
        if isinstance(n,ast.ExceptHandler) and n.name in roots and inside(n):
            insertion=location['_insertion_point']
            if not at(n)<=insertion<at(n,True):return False
    transparent=set()
    for n in ast.walk(owner):
        if isinstance(n,(ast.With,ast.AsyncWith)):
            transparent.add((at(n),0))
        if (isinstance(n,(ast.Try,ast.TryStar)) and not any(part[0]==at(n) for part in target['scope'])
                and all(h.body and isinstance(h.body[-1],(ast.Return,ast.Raise,ast.Break,ast.Continue)) for h in n.handlers)):
            transparent.add((at(n),0))
    def scope_allows(binding):
        candidates=[r for r in records if r['node'].start<=binding<r['node'].end]
        if not candidates:return False
        deepest=max(len(r['scope']) for r in candidates)
        def visible(scope):return tuple(part for part in scope if part not in transparent)
        target_scope=visible(target['scope'])
        return all(target_scope[:len(visible(r['scope']))]==visible(r['scope']) for r in candidates if len(r['scope'])==deepest)
    bindings={root:[] for root in roots}
    class Stores(ast.NodeVisitor):
        def visit_Name(self,n):
            if n.id in bindings and isinstance(n.ctx,ast.Store):bindings[n.id].append(at(n))
        def visit_FunctionDef(self,n):
            if n.name in bindings:bindings[n.name].append(at(n))
        visit_AsyncFunctionDef=visit_FunctionDef
        visit_ClassDef=visit_FunctionDef
        def visit_Lambda(self,n):pass
        visit_ListComp=visit_Lambda
        visit_SetComp=visit_Lambda
        visit_DictComp=visit_Lambda
        visit_GeneratorExp=visit_Lambda
        def visit_Import(self,n):
            for alias in n.names:
                name=alias.asname or alias.name.split('.')[0]
                if name in bindings:bindings[name].append(at(n))
        def visit_ImportFrom(self,n):
            for alias in n.names:
                name=alias.asname or alias.name
                if name in bindings:bindings[name].append(at(n))
        def visit_ExceptHandler(self,n):
            if n.name in bindings:bindings[n.name].append(at(n))
            self.generic_visit(n)
    for n in owner.body:Stores().visit(n)
    for root in roots:
        if table is not None:
            try:symbol=table.lookup(root)
            except KeyError:return False
            if symbol.is_parameter() or symbol.is_global() or symbol.is_free() or symbol.is_nonlocal():continue
        if not any(b<=point and b<location['_insertion_point'] and scope_allows(b) for b in bindings[root]):return False
    return True


def evidence_scope_allows(ref,location,context,expression=None):
    """No proof borrowed from a closed sibling block. This is not full dataflow/definite assignment."""
    if not location.get('_placement_anchor'):return True  # legacy non-insertion proposal
    try:
        records,_=_tree(context,location['_placement_anchor'])
        target=_anchor(records,location['_placement_anchor'])
    except (LoggingReviewError,SyntaxError,ValueError,RecursionError,TypeError,IndexError,KeyError,tokenize.TokenError):
        return False
    if ref['path']!=location['path']:return False
    points=[]
    if expression is not None:
        from .logger_access import _code
        lines=_code(context,ref['path']);raw=context.snapshot.files[ref['path']]._raw_text.splitlines()
        if lines is None:return False
        pattern=r'(?<![\w$])'+re.escape(expression)+r'(?![\w$])'
        for number in range(ref['start_line'],ref['end_line']+1):
            points.extend((number,m.start()) for m in re.finditer(pattern,raw[number-1])
                          if m.start()<len(lines[number-1]) and not lines[number-1][m.start()].isspace())
        if not points:return False
    else:points=[(ref['start_line'],ref.get('column',0))]
    for point in points:
        if expression is not None:
            visible=_binding_allows(context,location,point,expression,records,target)
            if visible is True:return True
            if visible is False:continue
        candidates=[r for r in records if r['node'].start<=point<r['node'].end]
        if not candidates:
            # A containing signature can establish parameters; a foreign member was excluded by available_at.
            if records and point<min(r['node'].start for r in records):return True
            continue
        deepest=max(len(r['scope']) for r in candidates)
        if all(target['scope'][:len(r['scope'])]==r['scope'] for r in candidates if len(r['scope'])==deepest):return True
    return False
