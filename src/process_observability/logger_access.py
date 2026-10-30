"""Source-bound proposal logger access. No target code is imported or executed.

An accessible identifier is not necessarily a logger. Purpose must come from a standard API,
an established chain or a binding already validated by the consumer. These checks affect proposals only.
"""
import ast
import bisect
import io
import re
import symtable
import tokenize
from .logging_contracts import LoggingReviewError, fields, text

_IDENT = r'(?:[^\W\d]|[$_])[\w$]*'
_DOTTED = re.compile(_IDENT + r'(?:\.' + _IDENT + r')*\Z')
_PY_METHODS = frozenset({'debug','info','warning','warn','error','exception','critical','fatal','log'})


def logging_purpose(site):
    if site.get('kind') == 'log_call': return 'detected_logging_api'
    if site.get('_validated_logging_binding'): return site['_validated_logging_binding']
    if (site.get('logging_chain') or {}).get('status') == 'confirmed': return 'confirmed_logging_chain'
    return None


def transfer_validated_java_purpose(sites, bindings, context, excluded=()):
    """Reuse a validated adapter interpretation, never an operation/log occurrence.

    Resolution and the entire adapter route must match. Names or a common terminal
    alone do not establish purpose. Availability at the insertion point is separate.
    ``sites`` is a private proposal-only copy; the inventory is never annotated.
    """
    def span(ref):
        if type(ref) is not dict or not all(k in ref for k in ('path','start_line','end_line')): return None
        return tuple(ref[k] for k in ('path','start_line','end_line'))
    def signature(chain):
        if type(chain) is not dict: return None
        contract, terminal = chain.get('source_contract'), chain.get('terminal')
        if type(contract) is not dict or type(terminal) is not dict: return None
        refs = [span(contract.get('declaration')), span(contract.get('implementation')), span(terminal)]
        hops = [span(h) for h in chain.get('hops', [])]
        if not hops or any(r is None for r in refs + hops): return None
        return (tuple(refs), terminal.get('callee'), chain.get('basis',terminal.get('basis')),
                tuple(hops), contract.get('authority'))
    def target(site):
        if not site.get('path','').endswith('.java'): return None
        call, problem = context.navigation.java.site_call(site)
        if problem or call is None: return None
        targets = context.navigation.java.follow(call)
        return targets[0] if len(targets)==1 else None
    proven = []
    for binding in bindings:
        source = sites.get(binding['site_id'])
        if source is None or source['id'] in excluded or not logging_purpose(source): continue
        key = signature(binding.get('chain'))
        resolved = target(source)
        if key is not None and resolved is not None:
            proven.append((resolved,key,source['id'],logging_purpose(source),binding['emission_evidence']))
    if not proven: return
    from copy import deepcopy
    for site in sites.values():
        if site['id'] in excluded or logging_purpose(site): continue
        resolved = target(site)
        if resolved is None: continue
        choices = (site.get('logging_chain') or {}).get('adapter_candidates', [])
        for other,key,source_id,authority,evidence in proven:
            if resolved!=other or sum(signature(c)==key for c in choices)!=1: continue
            site['_validated_logging_binding'] = authority
            site['_purpose_transferred_from'] = source_id
            site['_purpose_evidence'] = deepcopy(evidence)
            break


def _code(context, path):
    cache = getattr(context, '_proposal_logger_code', None)
    if cache is None: cache = context._proposal_logger_code = {}
    if path in cache: return cache[path]
    raw = context.snapshot.files[path]._raw_text
    # Snapshot rows use splitlines; Python tokenize uses physical newlines only.
    from .log_discovery import _ODD_LINES
    if _ODD_LINES.search(raw):
        cache[path] = None; return None
    chars = list(raw); starts = [0]
    for line in raw.splitlines(keepends=True): starts.append(starts[-1] + len(line))
    def erase(first,last):
        for i in range(first,last):
            if chars[i] not in '\r\n': chars[i] = ' '
    if path.endswith('.py'):
        try:
            for tok in tokenize.generate_tokens(io.StringIO(raw).readline):
                if tok.type in {tokenize.STRING, tokenize.COMMENT, getattr(tokenize,'FSTRING_MIDDLE',-1)}:
                    erase(starts[tok.start[0]-1]+tok.start[1], starts[tok.end[0]-1]+tok.end[1])
        except (tokenize.TokenError, IndentationError):
            cache[path] = None; return None
    else:
        from .java_declarations import _TOKEN
        for match in _TOKEN.finditer(raw):
            if match.lastgroup in ('comment','string','char','text_block'): erase(match.start(),match.end())
            elif match.lastgroup.startswith('open_'):
                cache[path] = None; return None
    cache[path] = ''.join(chars).splitlines()
    return cache[path]


def _matches(expression, ref, context):
    lines = _code(context,ref['path'])
    if lines is None: return False
    pattern = r'(?<![\w$.])' + r'\s*\.\s*'.join(re.escape(p) for p in expression.split('.')) + r'(?![\w$])'
    return re.search(pattern,'\n'.join(lines[ref['start_line']-1:ref['end_line']])) is not None


def _java_positions(file, owner, location):
    """Token positions, including an insertion gap after a token on a previous line."""
    if location.get('_insertion_point') is not None:
        line,column=location['_insertion_point']
        offsets=list(zip(file.lines,file.columns))
        point=bisect.bisect_left(offsets,(line,column))-0.5
        body=owner.get('body')
        return [point,point] if body and body[0]-1<point<body[1] else []
    def position(line, end=False):
        first=bisect.bisect_left(file.lines,line);last=bisect.bisect_right(file.lines,line)
        return (last-1 if end else first) if first<last else last-0.5
    positions=[position(location['start_line']),position(location['end_line'],True)]
    body=owner.get('body')
    if body:
        # A reference to the header/closing line denotes an insertion just inside the body.
        for i,line in enumerate((location['start_line'],location['end_line'])):
            if line==file.lines[body[0]-1]:positions[i]=max(positions[i],body[0]-0.5)
            if line==file.lines[body[1]]:positions[i]=min(positions[i],body[1]-0.5)
    return positions if body and body[0]-1<positions[0]<=positions[-1]<body[1] else []


def _java_visible(context, site, location):
    index = context.navigation.java
    owner = index.owner_member(location['path'], location['start_line'], location['end_line'])
    if owner is None: return False
    calls = [c for c in index.file_calls(site['path']) if c['callee'] == site['callee']
             and c['start_line'] == site['start_line']]
    if not calls: return False
    f = index.files[location['path']]
    positions = _java_positions(f,owner,location)
    if not positions: return False
    root = site['callee'].split('.')[0]
    if root in ('this','super'):
        if owner['static']: return False
        root = site['callee'].split('.')[1]
    for call in calls:
        if call['member'] != owner['id'] or call.get('nested'): continue
        local = index._local(owner,root,call['index'])
        if local != index._local(owner,root,positions[0]) or local != index._local(owner,root,positions[-1]): continue
        if local is None and not any(p['name']==root for p in owner['params']):
            field = index._field_in_scope(owner['type'],root)
            if field and owner['static'] and not field[0]['static']: continue
        return True
    return False


def _site_at(site, ref, location, context):
    from .proposal_placement import evidence_precedes,evidence_scope_allows
    own = {k:site[k] for k in ('path','start_line','end_line')}
    if (site['path'] != ref['path'] or not ref['start_line'] <= site['start_line'] <= ref['end_line']
            or not _matches(site['callee'],own,context) or not context.available_at(ref,location)
            or not context.available_at(own,location) or not evidence_precedes(own,location,context)):
        return False
    if location['path'].endswith('.py') and not evidence_scope_allows(own,location,context,site['callee']):return False
    return not site['path'].endswith('.java') or _java_visible(context,site,location)


def _python_symbols(context,path):
    """Cache immutable syntax only, never delivery-dependent reference availability."""
    cache=getattr(context,'_proposal_python_symbols',None)
    if cache is None:cache=context._proposal_python_symbols={}
    if path in cache:return cache[path]
    try:
        source=context.snapshot.files[path]._raw_text
        tree=ast.parse(source);symbols=symtable.symtable(source,path,'exec')
        counts={};unknown=False
        def bind(name):counts[name]=counts.get(name,0)+1
        class Bindings(ast.NodeVisitor):
            def visit_Name(self,node):
                if isinstance(node.ctx,(ast.Store,ast.Del)):bind(node.id)
            def visit_Import(self,node):
                for n in node.names:bind(n.asname or n.name.split('.')[0])
            def visit_ImportFrom(self,node):
                nonlocal unknown
                for n in node.names:
                    if n.name=='*':unknown=True
                    else:bind(n.asname or n.name)
            def visit_FunctionDef(self,node):
                bind(node.name)
                for value in (*node.decorator_list,*node.args.defaults,*node.args.kw_defaults):
                    if value is not None:self.visit(value)
            visit_AsyncFunctionDef=visit_FunctionDef
            def visit_ClassDef(self,node):
                bind(node.name)
                for value in (*node.decorator_list,*node.bases,*(k.value for k in node.keywords)):self.visit(value)
            def visit_ExceptHandler(self,node):
                if node.name:bind(node.name)
                self.generic_visit(node)
            def visit_MatchAs(self,node):
                if node.name:bind(node.name)
                self.generic_visit(node)
            def visit_MatchStar(self,node):
                if node.name:bind(node.name)
            def visit_MatchMapping(self,node):
                if node.rest:bind(node.rest)
                self.generic_visit(node)
        for statement in tree.body:Bindings().visit(statement)
        pending=list(symbols.get_children());tables=[]
        while pending:
            table=pending.pop();tables.append(table);pending.extend(table.get_children())
            for symbol in table.get_symbols():
                if symbol.is_declared_global() and (symbol.is_assigned() or symbol.is_imported()):bind(symbol.get_name())
        cache[path]=(tree,tables,counts,unknown)
    except (SyntaxError,ValueError,RecursionError):cache[path]=None
    return cache[path]


def declared_loggers(context, location):
    """Standard enclosing declarations only. Unread declaration names remain hints, never available evidence."""
    path = location['path']; result = []
    def add(name, first, last, methods, basis):
        raw = {'path':path,'start_line':first,'end_line':last}
        try: ref = context.verify_ref(raw,'logger declaration')
        except LoggingReviewError: ref = None
        result.append({'expression':name,'reference':ref,'reads':[] if ref else [raw], 'methods':methods,'basis':basis})
    if path.endswith('.py'):
        if _code(context,path) is None:return result
        parsed=_python_symbols(context,path)
        if parsed is None:return result
        tree,tables,counts,unknown=parsed
        if unknown:return result
        aliases = {}
        for node in tree.body:
            if isinstance(node,ast.Import):
                aliases.update({n.asname or n.name:n.name for n in node.names})
            elif isinstance(node,ast.ImportFrom) and node.module == 'logging':
                aliases.update({n.asname or n.name:'logging.'+n.name for n in node.names})
        aliases={key:value for key,value in aliases.items() if counts.get(key)==1}
        def name(node):
            if isinstance(node,ast.Name): return aliases.get(node.id,'')
            if isinstance(node,ast.Attribute):
                parent=name(node.value)
                return parent+'.'+node.attr if parent else ''
            return ''
        owners = [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                  and n.lineno <= location['start_line'] <= location['end_line'] <= n.end_lineno]
        shadowed = set()
        owner_keys={(n.name,n.lineno) for n in owners}
        for table in tables:
            if (table.get_name(),table.get_lineno()) not in owner_keys:continue
            for symbol in table.get_symbols():
                if (symbol.is_local() or symbol.is_free() or symbol.is_nonlocal()
                        or symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter()):
                    shadowed.add(symbol.get_name())
        assignments = {}
        for node in tree.body:
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                targets = node.targets if isinstance(node,ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target,ast.Name): assignments.setdefault(target.id,[]).append(node)
        for key,nodes in assignments.items():
            if len(nodes)!=1 or counts.get(key)!=1 or key in shadowed: continue
            node=nodes[0]
            if isinstance(node.value,ast.Call) and name(node.value.func)=='logging.getLogger':
                if not owners and node.end_lineno>location['start_line']:continue
                if not owners and location.get('_insertion_point') is not None:
                    from .proposal_placement import evidence_precedes
                    if not evidence_precedes({'path':path,'start_line':node.lineno,'end_line':node.end_lineno},location,context):continue
                add(key,node.lineno,node.end_lineno,_PY_METHODS,'python_module_logger')
    elif path.endswith('.java'):
        from .java_logging_graph import STANDARD_LOGGERS
        index=context.navigation.java;owner=index.owner_member(path,location['start_line'],location['end_line'])
        if owner is None:return result
        info=index.types[owner['type']];f=index.files[path];lines=_code(context,path)
        if lines is None:return result
        positions=_java_positions(f,owner,location)
        if not positions:return result
        for key,field in info['fields'].items():
            if (owner['static'] and not field['static']) or index._local(owner,key,positions[0]) is not None or any(p['name']==key for p in owner['params']):continue
            typ=index._typeref_value(field['type'],info['id'])
            if typ[0]!='ext' or typ[1] not in STANDARD_LOGGERS:continue
            # Match a declaration, never a use or a same-named field of another/nested class.
            pattern=r'(?<![\w$])'+re.escape(field['type'][0])+r'\s+'+re.escape(key)+r'(?![\w$])'
            body='\n'.join(lines)
            for match in re.finditer(pattern,body):
                first=body.count('\n',0,match.start())+1;last=body.count('\n',0,match.end())+1
                classes=[t for t in index.types.values() if t['path']==path and t['start_line']<=first<=last<=t['end_line']]
                nearest=min(classes,key=lambda t:t['end_line']-t['start_line']) if classes else None
                if nearest and nearest['id']==info['id'] and index.owner_member(path,first,last) is None:
                    add(key,first,last,STANDARD_LOGGERS[typ[1]],'java_standard_field');break
    return result


def inspect_logger(value, location, context, sites):
    """One predicate for validation and repair. Invalid proposals are never silently rewritten."""
    fields(value,{'expression','evidence'},{'expression','evidence'},'change.logger')
    expression=text(value['expression'],'logger.expression',maximum=200)
    if not _DOTTED.fullmatch(expression):return {'reason':'call_syntax','expression':expression}
    if type(value['evidence']) is not list or not value['evidence']:
        raise LoggingReviewError('change.logger.evidence: нужны прочитанные ссылки на существующий логгер')
    refs=[context.verify_ref(ref,'change.logger.evidence') for ref in value['evidence']]
    matching=[ref for ref in refs if _matches(expression,ref,context)]
    scope=False;scope_sites=[]
    for ref in matching:
        for site in sites.values():
            callee=site.get('callee','');receiver=callee.rpartition('.')[0]
            if expression not in (callee,receiver) or not _site_at(site,ref,location,context):continue
            scope=True;scope_sites.append(site['id'])
            purpose=logging_purpose(site)
            if purpose:
                result = {'status':'available','basis':'declared_expression','expression':expression,
                          'evidence':refs,'purpose_basis':purpose}
                if site.get('_purpose_transferred_from'):
                    result['purpose_transferred_from'] = site['_purpose_transferred_from']
                    result['purpose_evidence'] = site['_purpose_evidence']
                return result
    for option in declared_loggers(context,location):
        base=option['expression'];member=expression[len(base)+1:] if expression.startswith(base+'.') else None
        if expression!=base and member not in option['methods']:continue
        proof=option['reference']
        if proof and any(ref['path']==proof['path'] and ref['start_line']<=proof['start_line']<=proof['end_line']<=ref['end_line']
                         and _matches(base,ref,context) for ref in refs):
            return {'status':'available','basis':option['basis'],'expression':expression,'evidence':refs,'purpose_basis':'standard_declaration'}
    if not matching:return {'reason':'not_in_code','expression':expression}
    return {'reason':'logging_purpose_unresolved' if scope else 'outside_member_scope','expression':expression,
            'in_scope_site_ids':sorted(set(scope_sites))}


def logger_feedback(value, location, context, sites):
    try: result=inspect_logger(value,location,context,sites)
    except LoggingReviewError as exc:
        result={'reason':'invalid_expression_or_reference','message':str(exc)}
    if result.get('status')=='available':return None
    choices=set()
    refs=[]
    if type(value) is dict and type(value.get('evidence')) is list:
        for raw in value['evidence']:
            try: refs.append(context.verify_ref(raw,'logger feedback'))
            except LoggingReviewError:pass
    for site in sites.values():
        if not logging_purpose(site) or len(site.get('callee',''))>200:continue
        if any(_site_at(site,ref,location,context) for ref in refs):choices.add(site['callee'])
    for option in declared_loggers(context,location):
        if option['reference'] and any(r['path']==option['reference']['path'] and r['start_line']<=option['reference']['start_line']<=option['reference']['end_line']<=r['end_line'] for r in refs):choices.add(option['expression'])
    return {**result,'expressions_seen_in_evidence':sorted(choices)[:20],'total_expressions':len(choices)}
