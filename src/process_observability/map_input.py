"""Read-only map input and source access; no map generation and no target import."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .contracts import strict_json_loads
from .logging_contracts import LoggingReviewError, canonical, digest, safe_path
from .redaction import redact_text
from .upstream_masking import published_text, upstream_text, upstream_sha


class MapInputError(LoggingReviewError):
    pass


def read_regular(path, maximum=32 * 1024 * 1024):
    from .source_tools import _read_source, blocked_path
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or blocked_path(path.name):
        raise MapInputError('Input must be a regular non-secret file without symbolic links')
    try:
        _, value = _read_source(path, maximum)
    except (OSError, ValueError, UnicodeError) as exc:
        raise MapInputError('Cannot read the declared input file: ' + type(exc).__name__) from None
    return value


@dataclass(frozen=True)
class MapView:
    _json: str
    file_sha256: str | None = None

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or value.get('schema_version') != '2.0':
            raise MapInputError('Expected a published atomic scenario map, schema_version 2.0')
        nodes, edges = value.get('nodes'), value.get('edges')
        if type(nodes) is not list or type(edges) is not list:
            raise MapInputError('Map nodes and edges must be lists')
        if type(value.get('run')) is not dict or value['run'].get('status') not in ('completed', 'partial'):
            raise MapInputError('Expected a published completed/partial map, not running/failed state')
        sources = value.get('sources')
        if (type(sources) is not list or any(type(s) is not dict or type(s.get('id')) is not str
                or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', s['id']) is None for s in sources)
                or len({s['id'] for s in sources}) != len(sources)):
            raise MapInputError('Invalid map source IDs')
        ids, operations = set(), set()
        for node in nodes:
            if (type(node) is not dict or type(node.get('id')) is not str or not node['id'].strip()
                    or node['id'] in ids or node.get('kind') not in ('operation', 'group')
                    or type(node.get('name')) is not str or not node['name'].strip()):
                raise MapInputError('Invalid or duplicate map node; non-atomic maps are unsupported')
            ids.add(node['id'])
            if node['kind'] == 'operation': operations.add(node['id'])
        edge_ids = set()
        for edge in edges:
            if (type(edge) is not dict or type(edge.get('id')) is not str or not edge['id']
                    or edge['id'] in edge_ids or edge.get('source') not in operations or edge.get('target') not in operations):
                raise MapInputError('Invalid, duplicate or dangling map transition')
            edge_ids.add(edge['id'])
        return cls(canonical(value))

    @property
    def original(self):
        return strict_json_loads(self._json)

    @property
    def operations(self):
        return tuple(n for n in self.original['nodes'] if n['kind'] == 'operation')

    @property
    def operation_ids(self):
        return tuple(n['id'] for n in self.operations)

    @property
    def sha256(self):
        return hashlib.sha256(self._json.encode()).hexdigest()

    @property
    def revision(self):
        model = self.original
        return 'sha256:' + digest({k: model.get(k) for k in ('schema_version', 'scenario', 'nodes', 'edges')})


def load_map(path):
    if 'checkpoint' in Path(path).name.lower():
        raise MapInputError('A checkpoint is not a published map')
    raw = read_regular(path)
    view = MapView.from_dict(strict_json_loads(raw))
    return MapView(view._json, hashlib.sha256(raw.encode('utf-8')).hexdigest())


def load_hierarchy(path, view):
    """An incompatible optional hierarchy disables grouping, never drops operations."""
    if path is None or not Path(path).exists(): return None, []
    try:
        data = strict_json_loads(read_regular(path))
        if type(data) is not dict or data.get('map_revision') != view.revision:
            raise MapInputError('Hierarchy belongs to a different map revision')
        assigned, group_ids = set(), set()
        membership = data.get('membership', {})
        if type(membership) is not dict or set(membership) - set(view.operation_ids):
            raise MapInputError('Hierarchy has foreign operation membership')
        for group in data.get('stages', []):
            if type(group) is not dict or type(group.get('id')) is not str or group['id'] in group_ids:
                raise MapInputError('Invalid hierarchy group')
            group_ids.add(group['id'])
            members = group.get('operation_ids', [k for k, v in membership.items() if v == group['id']])
            if type(members) is not list: raise MapInputError('Invalid grouped operations')
            for identifier in members:
                if identifier not in view.operation_ids or identifier in assigned:
                    raise MapInputError('Foreign or repeated grouped operation')
                assigned.add(identifier)
            group['operation_ids'] = members
        if set(membership.values()) - group_ids:
            raise MapInputError('Hierarchy names an unknown stage')
        return data, []
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return None, ['Группировка отключена: ' + redact_text(str(exc))]


class RepoContext:
    """Addressed access to the same admitted frozen texts used by the logging detector."""
    def __init__(self, snapshot, view, source_roots=None):
        self.snapshot, self.view = snapshot, view
        self.source_roots = dict(source_roots or {})
        self.shown = []
        self.reused_fragments = 0
        self._fragment_lines = {}
        self._fragment_tags = {}
        self.used_fragments = set()
        self.cache_line_lookups = 0
        self.artifact_notes = []
        self.facts = []
        self._declarations = {}
        self._navigation = None
        # Review-path state: operator-declared logging contracts, per-operation wrapper-chain plans and the
        # detector's direct log sites (possible chain terminals). Navigation hints only, never evidence by themselves.
        self.logging_contracts = []
        self.logging_chains = {}
        self.log_call_sites = []
        sources = view.original.get('sources', [])
        if len(sources) == 1:
            self.source_roots.setdefault(sources[0]['id'], '')
        for source in sources:
            name = source.get('name')
            if source['id'] not in self.source_roots and type(name) is str:
                try: safe_path(name)
                except LoggingReviewError: continue
                if any(p.startswith(name + '/') for p in snapshot.files): self.source_roots[source['id']] = name
        for prefix, folder in self.source_roots.items():
            if type(prefix) is not str or prefix not in {s['id'] for s in sources}:
                raise MapInputError('Invalid source ID mapping')
            if folder: safe_path(folder)

    def resolve(self, path):
        try: safe_path(path)
        except LoggingReviewError as exc: raise MapInputError(str(exc)) from None
        if path in self.snapshot.files: return path
        prefix, sep, tail = path.partition('/')
        if sep and prefix in self.source_roots:
            folder = self.source_roots[prefix]
            resolved = (folder + '/' if folder else '') + tail
            if resolved in self.snapshot.files: return resolved
        raise MapInputError('Source path is not in the admitted repository: ' + path)

    def line_count(self, path):
        return len(self.snapshot.files[self.resolve(path)].text.splitlines())

    @staticmethod
    def _range_text(first, last):
        shown = [str(v) if type(v) is int else repr(v)[:40] for v in (first, last)]
        return shown[0] + '–' + shown[1]

    def _span(self, path, first, last):
        path = self.resolve(path)
        lines = self.snapshot.files[path].text.splitlines()
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(lines):
            # Exact feedback: the requested lines and the real file length. Evidence is never clamped into validity.
            raise MapInputError('Invalid source range for ' + path + ': requested ' + self._range_text(first, last)
                                + '; the file has ' + str(len(lines)) + ' lines (valid 1–' + str(len(lines)) + ')')
        return {'path': path, 'start_line': first, 'end_line': last, 'quote': '\n'.join(lines[first - 1:last]),
                'file_sha256': self.snapshot.files[path].sha256}

    def read(self, path, first, last, *, max_lines=240, clamp=False):
        """Deliver source lines. ``clamp`` is only for navigation reads: an end past the file returns the real lines
        with explicit requested/delivered bounds. Citations still go through strict verify_ref."""
        requested = None
        if clamp:
            if type(first) is not int or type(last) is not int or first < 1 or last < first:
                raise MapInputError('Invalid navigation range ' + self._range_text(first, last) + ': expected 1 <= start_line <= end_line')
            total = self.line_count(path)
            if first > total:
                raise MapInputError('Invalid source range for ' + self.resolve(path) + ': requested ' + self._range_text(first, last)
                                    + '; the file has ' + str(total) + ' lines (valid 1–' + str(total) + ')')
            if last > total:
                requested, last = {'start_line': first, 'end_line': last}, total
        if type(first) is not int or type(last) is not int or last - first + 1 > max_lines:
            raise MapInputError('Requested source range ' + self._range_text(first, last) + ' exceeds the declared read limit of '
                                + str(max_lines) + ' lines; request smaller ranges')
        span = self._span(path, first, last)
        if clamp:
            span['line_count'] = self.line_count(path)
            if requested is not None:
                span['requested'] = requested
                span['clamped'] = True
        lines = span['quote'].split('\n')
        for offset, number in enumerate(range(first, last + 1)):
            key = (span['path'], number)
            if key in self._fragment_lines:
                lines[offset] = self._fragment_lines[key]
                self.used_fragments.add(self._fragment_tags[key])
                self.cache_line_lookups += 1
        span['quote'] = '\n'.join(lines)
        self.shown.append({k: span[k] for k in ('path', 'start_line', 'end_line')})
        return span

    def verify_map_ref(self, ref):
        if type(ref) is not dict: raise MapInputError('Map evidence must be an object')
        span = self._span(ref.get('path'), ref.get('line_start', ref.get('start_line')),
                          ref.get('line_end', ref.get('end_line')))
        quote = ref.get('quote', ref.get('safe_quote'))
        source = self.snapshot.files[span['path']]
        original_span = '\n'.join(upstream_text(source._raw_text).splitlines()[span['start_line'] - 1:span['end_line']])
        if type(quote) is not str or not (quote.strip() == original_span.strip() or published_text(quote).strip() == span['quote'].strip()):
            raise MapInputError('Map quote does not match the current admitted text: ' + span['path'])
        return span

    REF_FIELDS = ('path', 'start_line', 'end_line', 'quote', 'symbol', 'column')
    REF_ALIASES = {'line_start': 'start_line', 'line_end': 'end_line'}

    @classmethod
    def normalize_ref(cls, ref, label='source reference'):
        """Exact known aliases only (the map itself uses line_start/line_end); no guessed path, line or ID repair."""
        if type(ref) is not dict:
            raise MapInputError(label + ' must be an object {"path","start_line","end_line"} (optional quote, symbol, column); got '
                                + type(ref).__name__)
        value = dict(ref)
        for alias, name in cls.REF_ALIASES.items():
            if alias in value:
                if name in value and value[name] != value[alias]:
                    raise MapInputError(label + ': conflicting ' + alias + ' and ' + name)
                value[name] = value.pop(alias)
        extra = sorted(str(k)[:60] for k in set(value) - set(cls.REF_FIELDS))
        missing = [k for k in ('path', 'start_line', 'end_line') if k not in value]
        if extra or missing:
            raise MapInputError('Unexpected citation fields in ' + label + ': unexpected ' + (', '.join(extra) or 'none')
                                + '; missing ' + (', '.join(missing) or 'none') + '; allowed ' + ', '.join(cls.REF_FIELDS))
        return value

    def verify_ref(self, ref, label='source reference'):
        from .source_tools import covering_ranges
        ref = self.normalize_ref(ref, label)
        span = self._span(ref.get('path'), ref.get('start_line'), ref.get('end_line'))
        if covering_ranges(self.shown, span['path'], span['start_line'], span['end_line']) is None:
            raise MapInputError('The citation includes source lines not delivered to this operation review')
        # The authoritative quote is always extracted above, never copied from the model.
        # Legacy replies sometimes add a quote with the outer margin removed. Match the
        # map-input convention without normalizing internal text, indentation or lines.
        if 'quote' in ref and (type(ref['quote']) is not str or ref['quote'].strip() != span['quote'].strip()):
            raise MapInputError('Model quote differs from delivered text: ' + label + ' at '
                                + span['path'] + ':' + str(span['start_line']) + '–' + str(span['end_line'])
                                + '. Return path/start_line/end_line only; the host extracts quote.')
        if 'column' in ref:
            if type(ref['column']) is not int or ref['column'] < 0: raise MapInputError('Invalid source column')
            span['column'] = ref['column']
        return span

    def search(self, term, *, maximum=30):
        if type(term) is not str or not term or len(term) > 200:
            raise MapInputError('Search needs a literal term of 1–200 characters')
        hits = [{'path': path, 'line': number} for path, source in sorted(self.snapshot.files.items())
                for number, line in enumerate(source.text.splitlines(), 1) if term in line]
        return {'term': term, 'hits': hits[:maximum], 'total': len(hits), 'omitted': max(0, len(hits) - maximum)}

    def who_calls(self, symbol, *, maximum=30):
        if type(symbol) is not str or re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*', symbol) is None:
            raise MapInputError('who_calls needs an exact identifier, not a regular expression')
        pattern = re.compile(r'(?<![\w$])' + re.escape(symbol) + r'\s*\(')
        hits = [{'path': path, 'line': n} for path, source in sorted(self.snapshot.files.items())
                for n, line in enumerate(source.text.splitlines(), 1)
                if pattern.search(line) and not re.search(r'\b(?:def|class|function)\s+' + re.escape(symbol) + r'\s*\(', line)]
        return {'symbol': symbol, 'hits': hits[:maximum], 'total': len(hits), 'omitted': max(0, len(hits) - maximum),
                'limitation': 'Textual call candidates only; target resolution and execution are unverified'}

    def declarations(self, path):
        path = self.resolve(path)
        if path not in self._declarations:
            from .declaration_index import build_declaration_index
            from .source_tools import RepoSnapshot
            sub = RepoSnapshot({path: self.snapshot.files[path]}, dict(self.snapshot.scope))
            self._declarations[path] = build_declaration_index(sub)['declarations']
        return deepcopy(self._declarations[path])

    @property
    def navigation(self):
        if self._navigation is None:
            from .logging_navigation import LoggingNavigation
            self._navigation = LoggingNavigation(self.snapshot)
        return self._navigation

    def available_at(self, evidence, location):
        if evidence['path'] != location['path']: return False
        owners = [d for d in self.declarations(location['path']) if d.get('kind') != 'class'
                  and d['start_line'] <= location['start_line'] <= location['end_line'] <= d['end_line']]
        if not owners:
            return location['start_line'] <= evidence['start_line'] <= evidence['end_line'] <= location['end_line']
        owner = min(owners, key=lambda d: d['end_line'] - d['start_line'])
        evidence_owners = [d for d in self.declarations(evidence['path']) if d.get('kind') != 'class'
                           and d['start_line'] <= evidence['start_line'] <= evidence['end_line'] <= d['end_line']]
        evidence_owner = min(evidence_owners, key=lambda d: d['end_line'] - d['start_line']) if evidence_owners else None
        if evidence_owner is not None and evidence_owner['id'] != owner['id']: return False
        return owner['start_line'] <= evidence['start_line'] <= evidence['end_line'] <= location['end_line']

    def import_artifacts(self, folder):
        """Sidecars are optional reusable hints, never new evidence or a new source snapshot."""
        folder = Path(folder)
        context_path, reads_path = folder / 'code_context.json', folder / 'read_fragments.jsonl'
        if context_path.exists():
            try:
                data = strict_json_loads(read_regular(context_path))
                if data.get('schema_version') != 'code-context/1': raise MapInputError('Unsupported code context')
                if not self.view.original.get('run', {}).get('id') or data.get('map_run_id') != self.view.original['run']['id']:
                    raise MapInputError('Code context belongs to another map run')
                matching = set()
                for item in data.get('files', []):
                    try:
                        path = self.resolve(item.get('path'))
                        sha = upstream_sha(self.snapshot.files[path]._raw_text)
                        if item.get('sha256') == sha: matching.add(item['path'])
                        else: self.artifact_notes.append('Не переиспользованы находки изменившегося файла: ' + path)
                    except (MapInputError, AttributeError): continue
                for fact in data.get('facts', []):
                    if type(fact) is dict and fact.get('path') in matching:
                        self.facts.append(deepcopy(fact))
                self.artifact_notes.extend(data.get('limitations', []))
            except (ValueError, TypeError, AttributeError):
                self.artifact_notes.append('code_context.json не переиспользован: несовместимый формат или пути')
                self.facts.clear()
        if reads_path.exists():
            try:
                header = False
                for row in read_regular(reads_path).splitlines():
                    if not row.strip(): continue
                    item = strict_json_loads(row)
                    if item.get('schema_version') != 'read-fragment/1': raise MapInputError('Unsupported read fragment')
                    if item.get('record') == 'header':
                        if not self.view.original.get('run', {}).get('id') or item.get('map_run_id') != self.view.original['run']['id']:
                            raise MapInputError('Read fragments belong to another map run')
                        header = True
                        continue
                    if not header: raise MapInputError('Read fragments have no map-bound header')
                    if item.get('record') == 'withheld': continue
                    span = self._span(item.get('path'), item.get('line_start'), item.get('line_end'))
                    values = item.get('lines')
                    if type(values) is list and all(type(v) is str for v in values): fragment = '\n'.join(values)
                    else: fragment = item.get('text', item.get('quote'))
                    expected = upstream_sha(self.snapshot.files[span['path']]._raw_text)
                    upstream = '\n'.join(upstream_text(self.snapshot.files[span['path']]._raw_text).splitlines()[span['start_line'] - 1:span['end_line']])
                    if item.get('file_sha256') != expected or upstream != fragment:
                        raise MapInputError('Saved read fragment differs from admitted text')
                    self.reused_fragments += 1
                    # Reuse only the shared, fully redacted values. Loading alone is not a new model delivery.
                    for offset, value in enumerate(span['quote'].split('\n')):
                        key = (span['path'], span['start_line'] + offset)
                        self._fragment_lines[key] = value
                        self._fragment_tags[key] = self.reused_fragments
            except (ValueError, TypeError, AttributeError):
                self.artifact_notes.append('read_fragments.jsonl не переиспользован: несовместимый текст или формат')
                self.reused_fragments = 0
                self._fragment_lines.clear(); self._fragment_tags.clear()
