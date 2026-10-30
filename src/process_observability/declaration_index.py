"""Business-only static declarations from a frozen snapshot, independent of technical caches.

Only parse original text; never import or execute the target. No source text or AST values are serialized.
A declaration groups navigation fragments, not operation occurrences or established business meaning.
Python files are parsed with ast; Java files are lexed by java_declarations (a conservative navigation index,
not a compiler: no Java calls are recorded). Every other language stays explicitly unsupported_language.
declaration_coverage(catalog, index) lists the declarations that no catalog anchor represents, turns every such
callable with a body into one host primary unit for the atomic pages, lists (policy context-callables/1) every
callable with a body that only context anchors represent as primary work under its existing command unit, and lists
source files whose declaration inventory is unknown as open scope; check_coverage verifies a saved copy against its
catalog and index under the policy the copy records (none: the original inventory).
"""
import ast
from collections import Counter
import copy
import hashlib
import json
import warnings
from pathlib import PurePosixPath

from .java_declarations import EXTRACTOR as JAVA_EXTRACTOR, TYPE_KINDS, UNICODE_GUARD, extract as extract_java
from .redaction import redact_text
from .source_tools import blocked_path
from .tracing import safe_data

SCHEMA = 'declaration-index/1'
UNIT_SCHEMA = 'command-units/1'
COVERAGE_SCHEMA = 'declaration-coverage/1'
ORIGIN = 'declaration_inventory'  # a host unit made from a declaration, never from a model finding
# Policy of a coverage document that also makes callable bodies anchored only by context findings primary work; a
# document without a policy key is the original inventory (direct-atomic-validator/5 and /6) and stays checkable.
CONTEXT_POLICY = 'context-callables/1'
CONTEXT_ORIGIN = 'context_anchor'  # an existing verified context command unit, promoted to primary work as it is
_TYPE_KINDS = frozenset(TYPE_KINDS)  # Python classes are "class"; Java adds interface, enum, record, annotation_type
_CALLABLE_KINDS = frozenset({'function', 'method', 'constructor', 'initializer'})
_JAVA_STATES = {'indexed': ('indexed', 'ok'), 'partial': ('partial', 'partial'), 'parse_failed': ('parse_failed', 'failed')}
_UNIT_POLICY = 'Only explicitly supplied excerpts may be cited; no gap union'
# Languages with callable declarations; any other snapshot file (configuration, markup, SQL) is a resource whose logic
# is covered only by the technical findings.
_SOURCE_LANGUAGES = {'.py': 'python', '.java': 'java', '.kt': 'kotlin', '.scala': 'scala', '.js': 'javascript',
                     '.jsx': 'javascript', '.ts': 'typescript', '.tsx': 'typescript', '.go': 'go', '.cs': 'csharp',
                     '.rs': 'rust', '.php': 'php', '.rb': 'ruby'}


class DeclarationError(ValueError):
    def __init__(self, message, code='invalid_declarations'):
        super().__init__(message)
        self.code = code


DeclarationIndexError = DeclarationError


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _seal(value):
    key = 'index_sha256' if value.get('schema') == SCHEMA else 'units_sha256'
    value = {k: v for k, v in value.items() if k not in {'sha256', key}}
    value[key] = _sha(_canonical(value))
    return value


def _safe_path(path):
    return (type(path) is str and bool(path) and not PurePosixPath(path).is_absolute()
            and '..' not in PurePosixPath(path).parts and not blocked_path(path))


def _check_index(index):
    if type(index) is not dict or index.get('schema') != SCHEMA:
        raise DeclarationError('Unsupported declaration index')
    data = {k: v for k, v in index.items() if k != 'index_sha256'}
    if index.get('index_sha256') != _sha(_canonical(data)):
        raise DeclarationError('Declaration index checksum mismatch')
    if type(index.get('files')) is not dict or type(index.get('declarations')) is not list:
        raise DeclarationError('Malformed declaration index')
    ids = set()
    for declaration in index['declarations']:
        if type(declaration) is not dict or declaration.get('id') in ids:
            raise DeclarationError('Malformed or repeated declaration')
        ids.add(declaration.get('id'))
        first, last = declaration.get('start_line'), declaration.get('end_line')
        if (not _safe_path(declaration.get('path')) or type(first) is not int or type(last) is not int
                or not 1 <= first <= last or declaration['path'] not in index['files']
                or declaration.get('file_sha256') != index['files'][declaration['path']]['sha256']):
            raise DeclarationError('Invalid declaration source binding')
    return index


def _java(entry, path, source, raw, snapshot_id):
    """Index one Java file from its original text: redacted names, lines, keywords, counts and hashes only.

    A partial file keeps the declarations of its balanced, recognised regions; a failed one keeps none. IDs bind
    the snapshot, path, span, name, kind and parameter types (hashed), so overloads on one line stay distinct.
    """
    found = extract_java(raw)
    status, parse = _JAVA_STATES[found['status']]
    entry.update(status=status, parse=parse, redacted_parse=_JAVA_STATES[extract_java(source.text)['status']][1],
                 error=found['error'], language='java', extractor=JAVA_EXTRACTOR, unicode_escapes=UNICODE_GUARD,
                 calls='not_extracted',
                 diagnostics=found['diagnostics'], diagnostics_omitted=found['diagnostics_omitted'],
                 diagnostic_counts=found['diagnostic_counts'], folded_bodies=found['folded_bodies'])
    if found['error'] is not None:
        entry['reason'] = found['error']['type']
    raw_lines = raw.splitlines(keepends=True)
    local, ids, seen = [], [], set()
    for item in found['declarations']:
        qualname = redact_text(item['qualname'])
        signature = None if item['signature'] is None else _sha(f"{item['qualname']}({item['signature']})")
        first, last = item['start_line'], item['end_line']
        base = f"{snapshot_id}:{path}:{first}:{last}:{qualname}:{item['kind']}:{signature or ''}"
        identifier, ordinal = 'decl-' + _sha(base)[:24], 0
        while identifier in seen:
            ordinal += 1
            identifier = 'decl-' + _sha(f'{base}:{ordinal}')[:24]
        seen.add(identifier)
        ids.append(identifier)
        local.append({'id': identifier, 'path': path, 'file_sha256': source.sha256, 'symbol': qualname,
                      'qualname': qualname, 'kind': item['kind'], 'start_line': first, 'end_line': last,
                      'definition_line': item['definition_line'],
                      'parent_id': None if item['parent'] is None else ids[item['parent']],
                      'span_sha256': _sha(''.join(raw_lines[first - 1:last])), 'language': 'java',
                      'visibility': item['visibility'], 'modifiers': list(item['modifiers']),
                      'annotations': [redact_text(name) for name in item['annotations']], 'body': item['body'],
                      'parameter_count': item['parameter_count'], 'signature_sha256': signature})
    entry['declarations'] = copy.deepcopy(local)
    return local


def build_declaration_index(snapshot):
    """Parse frozen original Python and Java before redaction; serialize only redacted structural metadata.

    Changed file fingerprints abort. Unsupported languages and syntax errors are explicit, preserving their
    candidate queue as tentative units. Decorators and annotations are inside a declaration's source span.
    """
    files, declarations, calls = {}, [], []
    snapshot_id = snapshot.snapshot_id
    for path, source in sorted(snapshot.files.items()):
        if not _safe_path(path):
            raise DeclarationError('Unsafe path in frozen snapshot')
        raw = source._raw_text
        if type(raw) is not str or _sha(raw) != source.sha256:
            raise DeclarationError('Frozen source hash mismatch; validate a new snapshot')
        entry = {'sha256': source.sha256, 'lines': len(raw.splitlines()), 'status': 'unsupported_language',
                 'parse': 'not_indexed', 'redacted_parse': 'not_indexed', 'error': None, 'declarations': []}
        files[path] = entry
        if PurePosixPath(path).suffix == '.java':
            declarations.extend(_java(entry, path, source, raw, snapshot_id))
            continue
        if PurePosixPath(path).suffix != '.py':
            continue
        try:
            # Parser warnings may quote original text; keep that text inside the frozen snapshot.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(raw)
        except (SyntaxError, ValueError, RecursionError) as exc:
            entry.update(status='parse_failed', parse='failed', reason=type(exc).__name__,
                         error={'type': type(exc).__name__, 'line': getattr(exc, 'lineno', None)})
            continue
        entry.update(status='indexed', parse='ok', redacted_parse='ok')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                ast.parse(source.text)
        except (SyntaxError, ValueError, RecursionError):
            entry['redacted_parse'] = 'failed'
        local, pending = [], []
        raw_lines = raw.splitlines(keepends=True)

        class Visit(ast.NodeVisitor):
            stack = []

            def declaration(self, node):
                name = '.'.join([*(x['name'] for x in self.stack), node.name])
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                last = node.end_lineno
                identifier = 'decl-' + _sha(f'{snapshot_id}:{path}:{first}:{last}:{name}')[:24]
                parent = self.stack[-1]['id'] if self.stack else None
                item = {'id': identifier, 'path': path, 'file_sha256': source.sha256,
                        'symbol': redact_text(name), 'qualname': redact_text(name),
                        'kind': ('class' if isinstance(node, ast.ClassDef) else
                                 'method' if self.stack and self.stack[-1]['kind'] == 'class' else 'function'),
                        'start_line': first, 'end_line': last, 'definition_line': node.lineno,
                        'parent_id': parent,
                        'span_sha256': _sha(''.join(raw_lines[first - 1:last]))}
                local.append(item)
                self.stack.append({'id': identifier, 'name': node.name, 'kind': item['kind']})
                self.generic_visit(node)
                self.stack.pop()

            visit_ClassDef = declaration
            visit_FunctionDef = declaration
            visit_AsyncFunctionDef = declaration

            def visit_Call(self, node):
                target = None
                if isinstance(node.func, ast.Name):
                    target = node.func.id
                elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                      and node.func.value.id in {'self', 'cls'}):
                    target = '.'.join([*(x['name'] for x in self.stack[:-1]), node.func.attr])
                if target and self.stack:
                    pending.append({'caller_id': self.stack[-1]['id'], 'symbol': redact_text(target),
                                    'path': path, 'start_line': node.lineno, 'end_line': node.end_lineno})
                self.generic_visit(node)

        Visit().visit(tree)
        declarations.extend(local)
        entry['declarations'] = copy.deepcopy(local)
        # Unique same-file lexical declarations only. Imported/dynamic/ambiguous targets remain unresolved.
        for call in pending:
            caller = next(d for d in local if d['id'] == call['caller_id'])
            names = [call['symbol']]
            parent_symbol = caller['symbol'].rsplit('.', 1)[0] if '.' in caller['symbol'] else ''
            if parent_symbol:
                names.insert(0, parent_symbol + '.' + call['symbol'])
            matches = []
            for name in names:
                matches = [d for d in local if d['symbol'] == name and d['kind'] in {'function', 'method'}]
                if matches:
                    break
            call.update(callee_id=matches[0]['id'] if len(matches) == 1 else None,
                        status='resolved' if len(matches) == 1 else 'unresolved')
            calls.append(call)
    return _seal({'schema': SCHEMA, 'snapshot_id': snapshot_id, 'files': files,
                  'declarations': declarations, 'calls': calls,
                  'basis': 'Static original-source declaration containment; not business or execution evidence'})


def build_command_units(catalog, index):
    """Group matching candidate fragments by exact declaration identity, preserving every member ID.

    An absent declaration or cross-child range stays a separate tentative unit. Wrong model symbols are
    flagged; exact host containment alone determines identity, never the model label. A foreign file
    version is an error, never legitimized by tentative status. Context anchors remain separate references.
    """
    _check_index(index)
    if type(catalog) is not dict or catalog.get('snapshot_id') != index['snapshot_id']:
        raise DeclarationError('Catalog and declarations belong to different snapshots')
    candidates = catalog.get('candidates')
    if type(candidates) is not list:
        raise DeclarationError('Catalog candidates must be a list')
    candidates = [*candidates, *catalog.get('context_anchors', [])]
    groups, seen, by_path = {}, set(), {}
    for declaration in index['declarations']:
        by_path.setdefault(declaration['path'], []).append(declaration)
    for candidate in candidates:
        if type(candidate) is not dict or type(candidate.get('id')) is not str or candidate['id'] in seen:
            raise DeclarationError('Every candidate must have a unique ID')
        seen.add(candidate['id'])
        path = candidate.get('path')
        if not _safe_path(path) or path not in index['files']:
            raise DeclarationError('Candidate path outside indexed snapshot')
        file = index['files'][path]
        if candidate.get('file_sha256') != file['sha256']:
            raise DeclarationError('Candidate file hash mismatch; validate a new snapshot')
        first, last = candidate.get('start_line'), candidate.get('end_line')
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= file['lines']:
            raise DeclarationError('Candidate range outside frozen file')
        containing = sorted((d for d in by_path.get(path, []) if d['start_line'] <= first <= last <= d['end_line']),
                            key=lambda d: (d['end_line'] - d['start_line'], -d['start_line']))
        declaration = containing[0] if containing else None
        reason = ('not_indexed' if file['parse'] == 'not_indexed' else
                  'parse_failed' if file['parse'] == 'failed' else
                  'parse_partial' if file['parse'] == 'partial' else 'module_level')
        if declaration:
            descendants = [d for d in by_path[path] if d['parent_id'] == declaration['id']]
            touched = [d for d in descendants if d['start_line'] <= last and first <= d['end_line']]
            if ((declaration['kind'] in _TYPE_KINDS and touched) or
                    (len(touched) > 1 and first > declaration['definition_line'])):
                reason = 'spans_declarations'
            else:
                # The host span establishes identity. A model label is checked and retained as a warning,
                # but cannot move this anchor to a different declaration or merge units by name.
                reason = None
        identifier = declaration['id'] if reason is None else 'tentative-' + _sha(candidate['id'])[:24]
        if identifier not in groups:
            groups[identifier] = {'id': identifier, 'status': 'verified' if reason is None else 'tentative',
                'reason': reason, 'declaration_id': declaration['id'] if reason is None else None,
                'declaration': copy.deepcopy(declaration) if reason is None else None,
                'path': path, 'file_sha256': file['sha256'], 'candidate_ids': [], 'finding_ids': [],
                'member_ids': [], 'event_site_ids': [], 'role': 'context',
                'members': [], 'supplied_range_policy': 'Only explicitly supplied excerpts may be cited; no gap union'}
        unit = groups[identifier]
        unit['member_ids'].append(candidate['id'])
        if candidate.get('role', 'candidate') == 'candidate':
            unit['candidate_ids'].append(candidate['id'])
            unit['role'] = 'candidate'
        for site in candidate.get('event_sites', []):
            if site['id'] not in unit['event_site_ids']:
                unit['event_site_ids'].append(site['id'])
        for finding_id in candidate.get('finding_ids', []):
            if finding_id not in unit['finding_ids']:
                unit['finding_ids'].append(finding_id)
        # Catalog descriptions are already sanitized by the review pipeline; redact again at the boundary.
        member = safe_data(copy.deepcopy(candidate))
        member['symbol_check'] = ('unresolved' if declaration is None or reason is not None else
                                  'consistent' if candidate.get('symbol') in
                                  (declaration['symbol'], declaration['symbol'].rsplit('.', 1)[-1]) else 'stale_symbol')
        unit['members'].append(member)
    units = sorted(groups.values(), key=lambda u: (u['path'], min(m['start_line'] for m in u['members']), u['id']))
    by_member = {identifier: unit for unit in units for identifier in unit['member_ids']}
    by_declaration = {unit['declaration_id']: unit for unit in units if unit['declaration_id'] is not None}
    for unit in units:
        unit['navigation'] = {'callee_unit_ids': [], 'caller_unit_ids': [], 'same_owner_unit_ids': []}
    calls_by_caller = {}
    for call in index['calls']:
        calls_by_caller.setdefault(call['caller_id'], []).append(call)
    for unit in units:
        nav = unit['navigation']
        for member in unit['members']:
            for source_key, target_key in (('callee_anchor_ids', 'callee_unit_ids'),
                                            ('same_owner_anchor_ids', 'same_owner_unit_ids')):
                for anchor_id in member.get('navigation', {}).get(source_key, []):
                    target = by_member.get(anchor_id)
                    if target and target['id'] != unit['id'] and target['id'] not in nav[target_key]:
                        nav[target_key].append(target['id'])
        for call in calls_by_caller.get(unit['declaration_id'], []):
            if call['status'] != 'resolved':
                continue
            target = by_declaration.get(call['callee_id'])
            if target and target['id'] != unit['id'] and target['id'] not in nav['callee_unit_ids']:
                nav['callee_unit_ids'].append(target['id'])
    by_id = {unit['id']: unit for unit in units}
    for unit in units:
        for target in unit['navigation']['callee_unit_ids']:
            by_id[target]['navigation']['caller_unit_ids'].append(unit['id'])
        for key in unit['navigation']:
            unit['navigation'][key].sort()
    from collections import Counter
    return _seal({'schema': UNIT_SCHEMA, 'snapshot_id': index['snapshot_id'],
                  'index_sha256': index['index_sha256'], 'catalog_sha256': catalog.get('catalog_sha256'),
                  'units': units, 'candidate_count': sum(len(u['candidate_ids']) for u in units),
                  'accounting': {'anchors': len(seen), 'candidates': sum(len(u['candidate_ids']) for u in units),
                                 'units': len(units), 'candidate_units': sum(u['role'] == 'candidate' for u in units),
                                 'verified_units': sum(u['status'] == 'verified' for u in units),
                                 'tentative_units': sum(u['status'] == 'tentative' for u in units),
                                 'tentative_by_reason': dict(sorted(Counter(u['reason'] for u in units if u['reason']).items())),
                                 'stale_symbols': sum(m['symbol_check'] == 'stale_symbol' for u in units for m in u['members']),
                                 'fragments_grouped': sum(len(u['member_ids']) for u in units if len(u['member_ids']) > 1)}})


def seal(document):
    return _seal(copy.deepcopy(document))


def build_index(snapshot):
    return build_declaration_index(snapshot)


def check_index(index, snapshot):
    if type(index) is not dict or index.get('snapshot_id') != snapshot.snapshot_id:
        raise DeclarationError('Index belongs to another snapshot', 'snapshot_mismatch')
    if (type(index.get('files')) is not dict or
            {path: file.get('sha256') for path, file in index['files'].items() if type(file) is dict}
            != snapshot.fingerprints):
        raise DeclarationError('File revision mismatch', 'file_revision_mismatch')
    _check_index(index)
    expected = build_index(snapshot)
    if index != expected:
        raise DeclarationError('Declaration metadata does not match frozen source', 'declaration_span_mismatch')
    return index


def build_units(catalog, index, snapshot):
    check_index(index, snapshot)
    return build_command_units(catalog, index)


def check_units(units, *, catalog, index):
    expected = build_command_units(catalog, index)
    if units != expected:
        raise DeclarationError('Command unit partition/content mismatch', 'units_mismatch')
    return units


def declaration_coverage(catalog, index, policy=CONTEXT_POLICY):
    """Deterministic diagnostic inventory of the indexed declarations that no catalog anchor represents.

    coverage of a declaration: "candidate" or "context" when a command unit of that role is bound to exactly this
    declaration (the containment rules of build_command_units); "overlap_only" when anchors merely overlap it (the
    enclosing type of bound members, an anchor across declarations); "none" otherwise. unrepresented lists, in
    index order, every "none" declaration and every "overlap_only" callable. Files without a complete declaration
    inventory (unsupported language, failed or partial parse) are listed with their anchor counts.

    Nothing here is a candidate or operation and neither input is changed: an unrepresented declaration is a place
    the technical review did not anchor, not an established business action, and a complete declaration inventory
    is not complete business-candidate coverage. primary_units turns each unrepresented callable with a body into
    one host unit (origin declaration_inventory, no candidates or findings) that the atomic pages must decide; see
    _primary_units and _open_scope.

    policy CONTEXT_POLICY (fresh runs) adds context_units: an exactly bound context anchor is a technical label, not a
    decision, so its callable body is primary work too, under its existing command unit ID; see _context_units.
    policy None recomputes the original inventory of a saved earlier document; any other policy is refused.
    """
    if policy not in (None, CONTEXT_POLICY):
        raise DeclarationError('Unknown declaration coverage policy', 'coverage_mismatch')
    units = build_command_units(catalog, index)
    roles = {}
    for unit in units['units']:
        if unit['declaration_id'] is not None:
            roles.setdefault(unit['declaration_id'], set()).add(unit['role'])
    by_path = {}
    for anchor in [*catalog['candidates'], *catalog.get('context_anchors', [])]:
        by_path.setdefault(anchor['path'], []).append(anchor)
    by_coverage, callables, unrepresented = Counter(), 0, []
    for declaration in index['declarations']:
        bound = roles.get(declaration['id'], set())
        overlapping = sum(anchor['start_line'] <= declaration['end_line'] and declaration['start_line'] <= anchor['end_line']
                          for anchor in by_path.get(declaration['path'], ()))
        coverage = ('candidate' if 'candidate' in bound else 'context' if bound else
                    'overlap_only' if overlapping else 'none')
        is_callable = declaration['kind'] in _CALLABLE_KINDS
        by_coverage[coverage] += 1
        callables += is_callable
        if coverage == 'none' or (coverage == 'overlap_only' and is_callable):
            unrepresented.append({
                'declaration_id': declaration['id'], 'path': declaration['path'], 'qualname': declaration['qualname'],
                'kind': declaration['kind'], 'start_line': declaration['start_line'],
                'end_line': declaration['end_line'], 'parent_id': declaration['parent_id'],
                'visibility': declaration.get('visibility'), 'has_body': declaration.get('body', 'block') != 'none',
                'callable': is_callable, 'coverage': coverage, 'overlapping_anchors': overlapping})
    unindexed = [{'path': path, 'status': file['status'], 'parse': file['parse'], 'anchors': len(by_path.get(path, ())),
                  'candidate_anchors': sum(anchor.get('role', 'candidate') == 'candidate' for anchor in by_path.get(path, ()))}
                 for path, file in sorted(index['files'].items()) if file['status'] != 'indexed']
    primary = _primary_units(index, units, unrepresented)
    scope = _open_scope(index, by_path)
    blocking = sum(row['blocking'] for row in scope)
    document = {
        'schema': COVERAGE_SCHEMA, 'snapshot_id': index['snapshot_id'], 'index_sha256': index['index_sha256'],
        'catalog_sha256': catalog.get('catalog_sha256'), 'units_sha256': units['units_sha256'],
        'unrepresented': unrepresented, 'unindexed_files': unindexed, 'primary_units': primary,
        'open_scope': {'files': scope, 'blocking': blocking,
                       'basis': ('Source files whose declaration inventory is unknown: their unanchored code is not '
                                 'checked, and each of them keeps the atomic map from being complete. A rejected '
                                 'parse shows only that this host could not read the file; it says nothing about '
                                 'the runtime of the analysed system.')},
        'summary': {'declarations': len(index['declarations']), 'callables': callables,
                    'by_coverage': dict(sorted(by_coverage.items())), 'unrepresented': len(unrepresented),
                    'unrepresented_callables_with_body': sum(row['callable'] and row['has_body'] for row in unrepresented),
                    'files_by_status': dict(sorted(Counter(file['status'] for file in index['files'].values()).items())),
                    'anchors_in_unindexed_files': sum(row['anchors'] for row in unindexed),
                    'primary_units': len(primary), 'open_scope_files': len(scope), 'open_scope_blocking': blocking},
        'basis': ('Diagnostic inventory of static declarations without an exactly bound catalog anchor. A declaration '
                  'is a navigation entry, not a business operation, candidate or unit; a complete declaration '
                  'inventory is not complete business-candidate coverage. Each primary unit is host work that the '
                  'atomic pages must decide from its supplied source; the list of unchecked declarations is not a '
                  'semantic completeness score.')}
    if policy is not None:
        context = _context_units(index, units)
        document.update(policy=policy, context_units=context)
        document['summary']['context_units'] = len(context)
        document['basis'] += (' Each context unit is a callable that technical findings anchored only as context: it '
                              'keeps its command unit, ID and findings and is host work that the atomic pages must '
                              'decide from its supplied source; the context label decides nothing.')
    document['coverage_sha256'] = _sha(_canonical(document))
    return document


def _context_units(index, units):
    """The verified context command units of callables with a body, in index order: source-only work views.

    A view names its command unit (same ID, the declaration ID) and that unit's finding IDs; it adds no candidate,
    finding or member, and command_units.json stays as built. Types, fields, abstract or bodiless callables, tentative
    fragments and files without a usable inventory are not listed; a candidate unit is never listed.
    """
    bound = {unit['declaration_id']: unit for unit in units['units']
             if unit['status'] == 'verified' and unit['role'] == 'context'}
    rows = []
    for declaration in index['declarations']:
        unit = bound.get(declaration['id'])
        if (unit is None or declaration['kind'] not in _CALLABLE_KINDS or declaration.get('body', 'block') == 'none'
                or index['files'][declaration['path']]['status'] not in ('indexed', 'partial')):
            continue
        rows.append({'id': unit['id'], 'origin': CONTEXT_ORIGIN, 'declaration_id': declaration['id'],
                     'path': declaration['path'], 'qualname': declaration['qualname'], 'kind': declaration['kind'],
                     'start_line': declaration['start_line'], 'end_line': declaration['end_line'],
                     'finding_ids': list(unit['finding_ids'])})
    return rows


def _primary_units(index, units, unrepresented):
    """One host unit per unrepresented callable with a body, in index order, in the shape of a command unit.

    The ID derives from the declaration ID only, so overloads and nested declarations stay separate. Navigation
    follows only resolved unique same-file calls of the index (Python today; Java records none); owner names the
    enclosing declaration for navigation, never as a call. A unit has no candidates, findings or members.
    """
    declarations = {item['id']: item for item in index['declarations']}
    chosen = [row for row in unrepresented if row['callable'] and row['has_body']
              and index['files'][row['path']]['status'] in ('indexed', 'partial')]
    unit_of = {unit['declaration_id']: unit['id'] for unit in units['units'] if unit['declaration_id'] is not None}
    host_ids = {row['declaration_id']: 'hdecl-' + _sha(row['declaration_id'])[:24] for row in chosen}
    unit_of.update(host_ids)
    callees, callers = {}, {}
    for call in index['calls']:
        source, target = unit_of.get(call.get('caller_id')), unit_of.get(call.get('callee_id'))
        if call.get('status') != 'resolved' or source is None or target is None or source == target:
            continue
        callees.setdefault(call['caller_id'], set()).add(target)
        callers.setdefault(call['callee_id'], set()).add(source)
    primary = []
    for row in chosen:
        declaration = declarations[row['declaration_id']]
        parent = declarations.get(declaration['parent_id'])
        primary.append({
            'id': host_ids[declaration['id']], 'origin': ORIGIN, 'status': 'verified', 'reason': None,
            'coverage': row['coverage'], 'declaration_id': declaration['id'], 'declaration': copy.deepcopy(declaration),
            'path': declaration['path'], 'file_sha256': declaration['file_sha256'], 'candidate_ids': [],
            'finding_ids': [], 'member_ids': [], 'event_site_ids': [], 'role': 'declaration', 'members': [],
            'owner': None if parent is None else {key: parent[key] for key in ('id', 'qualname', 'kind')},
            'navigation': {'callee_unit_ids': sorted(callees.get(declaration['id'], ())),
                           'caller_unit_ids': sorted(callers.get(declaration['id'], ())), 'same_owner_unit_ids': []},
            'supplied_range_policy': _UNIT_POLICY})
    return primary


def _open_scope(index, by_path):
    """Source-language files without a complete declaration inventory, in path order; every one is blocking.

    No parse failure is excused: the host parser (for Python the host CPython) does not establish which dialect or
    runtime the analysed system uses, so its inventory stays unknown.
    """
    rows = []
    for path, file in sorted(index['files'].items()):
        language = _SOURCE_LANGUAGES.get(PurePosixPath(path).suffix.lower())
        if language is None or file['status'] == 'indexed':
            continue
        reason = ('no_declaration_index_for_language' if file['status'] == 'unsupported_language'
                  else f"declaration_index_{file['status']}")
        rows.append({'path': path, 'language': language, 'status': file['status'], 'parse': file['parse'],
                     'reason': reason, 'blocking': True, 'anchors': len(by_path.get(path, ()))})
    return rows


def check_coverage(coverage, *, catalog, index):
    """The saved declaration coverage must equal the one recomputed from its saved catalog and index.

    It is recomputed under the policy it records; a document without a policy key is an earlier run's original
    inventory and is checked as such (callers bind the policy to the run's settings).
    """
    if type(coverage) is not dict or coverage != declaration_coverage(catalog, index, coverage.get('policy')):
        raise DeclarationError('Declaration coverage does not match the saved catalog and index', 'coverage_mismatch')
    return coverage
