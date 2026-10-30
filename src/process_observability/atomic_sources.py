"""Bounded source observations for the existing atomic loop, with no model or file-system access.

Only the redacted text of an already selected RepoSnapshot is exposed. An index hit,
search hit or declaration name is navigation, not a control link or a reviewed action.
Only successful reads add citable ranges; an oversized observation is refused whole.
The orchestrator remains responsible for model/token/answer/transport budgets and the
saved observation/request identity. This helper does not run an investigation itself.
"""
import copy
import hashlib
import json
from pathlib import PurePosixPath

from .source_tools import MAX_CITATION_LINES, blocked_path, covering_ranges


class SourceRequestError(ValueError):
    def __init__(self, code, **details):
        super().__init__(code)
        self.code, self.details = code, details


def _integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise SourceRequestError('invalid_bound', field=field, minimum=low, maximum=high)
    return value


class AtomicSources:
    def __init__(self, snapshot, units, *, max_lines=120, max_chars=100_000):
        self.snapshot = snapshot
        self._files = dict(snapshot.files)
        self._snapshot_id = snapshot.snapshot_id
        self.max_lines = _integer(max_lines, 1, MAX_CITATION_LINES, 'max_lines')
        self.max_chars = _integer(max_chars, 1, 1_000_000, 'max_chars')
        self._supplied = {}
        self._generation = 0
        self._units = {}
        if type(units) is not list:
            raise SourceRequestError('invalid_units')
        for unit in units:
            if (type(unit) is not dict or set(unit) != {'id', 'path', 'symbol', 'start_line', 'end_line'}
                    or type(unit['id']) is not str or not unit['id'] or len(unit['id']) > 128
                    or unit['id'] in self._units or type(unit['symbol']) is not str):
                raise SourceRequestError('invalid_unit')
            self._range(unit['path'], unit['start_line'], unit['end_line'], enforce_read_limit=False)
            self._units[unit['id']] = dict(unit)
        self._ordered = sorted(self._units.values(), key=lambda u: (u['path'], u['start_line'], u['end_line'], u['id']))
        self._by_path = {}
        for unit in self._ordered:
            self._by_path.setdefault(unit['path'], []).append(unit)

    def _unchanged(self):
        if (self.snapshot.files.keys() != self._files.keys()
                or any(self.snapshot.files[path] is not item for path, item in self._files.items())):
            raise SourceRequestError('snapshot_changed')

    def fork(self):
        """Preview one observation without publishing its ranges until the host accepts the proposal."""
        self._unchanged()
        result = copy.copy(self)
        result._supplied = {path: set(lines) for path, lines in self._supplied.items()}
        result._parent, result._parent_generation = self, self._generation
        return result

    def adopt(self, preview):
        """Accept one preview in the sequential host loop; refuse a stale or unrelated preview."""
        self._unchanged()
        if (type(preview) is not AtomicSources or getattr(preview, '_parent', None) is not self
                or getattr(preview, '_parent_generation', None) != self._generation):
            raise SourceRequestError('stale_preview')
        preview._unchanged()
        self._supplied = {path: set(lines) for path, lines in preview._supplied.items()}
        self._generation += 1

    def _allowed_path(self, path):
        return (type(path) is str and bool(path) and '\\' not in path and not PurePosixPath(path).is_absolute()
                and '..' not in PurePosixPath(path).parts and str(PurePosixPath(path)) == path
                and path in self._files and not blocked_path(path))

    def _source(self, path):
        self._unchanged()
        if not self._allowed_path(path):
            raise SourceRequestError('outside_frozen_snapshot')
        return self.snapshot.files[path]

    def _range(self, path, first, last, *, enforce_read_limit=True):
        source = self._source(path)
        lines = source.text.splitlines()  # never source._raw_text and never target application execution
        _integer(first, 1, len(lines), 'start_line')
        _integer(last, first, len(lines), 'end_line')
        if enforce_read_limit and last - first + 1 > self.max_lines:
            raise SourceRequestError('read_exceeds_line_limit', path=path, start_line=first, end_line=last,
                                     maximum=self.max_lines)
        return source, lines

    def _bounded(self, result):
        size = len(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if size > self.max_chars:
            raise SourceRequestError('observation_exceeds_char_limit', required_chars=size, maximum=self.max_chars)
        return result

    @staticmethod
    def _paging(offset, limit):
        _integer(offset, 0, 1_000_000_000, 'offset')
        _integer(limit, 1, 64, 'limit')

    @staticmethod
    def _query(query):
        if type(query) is not str or not query.strip() or len(query) > 256:
            raise SourceRequestError('invalid_literal_query')
        return query.casefold()

    @staticmethod
    def _page(items, offset, limit):
        chosen = items[offset:offset + limit]
        end = min(len(items), offset + len(chosen))
        return chosen, {'total': len(items), 'offset': offset, 'omitted': max(0, len(items) - end),
                        'next_offset': end if end < len(items) else None}

    def index(self, *, offset=0, limit=24, query=None):
        self._unchanged()
        self._paging(offset, limit)
        rows = self._ordered
        if query is not None:
            term = self._query(query)
            rows = [u for u in rows if term in (u['path'] + '\n' + u['symbol']).casefold()]
        selected, page = self._page(rows, offset, limit)
        return self._bounded({'units': [dict(u) for u in selected], **page, 'snapshot_id': self._snapshot_id,
                              'use': 'navigation_only', 'control_flow_proven': False})

    def search(self, query, *, offset=0, limit=24):
        self._paging(offset, limit)
        self._unchanged()
        term, rows, count, unsupported = self._query(query), [], 0, 0
        for path in sorted(self._files):
            if not self._allowed_path(path):
                unsupported += 1
                continue
            source = self._files[path]
            for line, text in enumerate(source.text.splitlines(), 1):
                if term not in text.casefold():
                    continue
                if offset <= count < offset + limit:
                    rows.append({'path': path, 'line': line, 'text': text,
                                 'unit_ids': [u['id'] for u in self._by_path.get(path, ())
                                              if u['start_line'] <= line <= u['end_line']]})
                count += 1
        end = min(count, offset + len(rows))
        page = {'total': count, 'offset': offset, 'omitted': max(0, count - end),
                'next_offset': end if end < count else None}
        return self._bounded({'matches': rows, **page, 'unsupported_paths': unsupported,
                              'snapshot_id': self._snapshot_id,
                              'use': 'navigation_only', 'control_flow_proven': False})

    def read_unit(self, identifier):
        if type(identifier) is not str or identifier not in self._units:
            raise SourceRequestError('unknown_unit_id')
        unit = self._units[identifier]
        return self.read(unit['path'], unit['start_line'], unit['end_line'])

    def read(self, path, start_line, end_line):
        source, lines = self._range(path, start_line, end_line)
        shown = self._supplied.get(path, set())
        requested = set(range(start_line, end_line + 1))
        text = '\n'.join(f'{n}: {lines[n - 1]}' for n in range(start_line, end_line + 1))
        result = self._bounded({'path': path, 'start_line': start_line, 'end_line': end_line, 'text': text,
                                'source_sha256': source.sha256, 'snapshot_id': self._snapshot_id,
                                'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                                'new_lines': len(requested - shown), 'seen_before': requested <= shown,
                                'use': 'source_data_only'})
        self._supplied.setdefault(path, set()).update(requested)  # only after complete bounded delivery
        self._generation += 1
        return result

    def supplied_ranges(self):
        self._unchanged()
        result = []
        for path, shown in sorted(self._supplied.items()):
            for number in sorted(shown):
                if result and result[-1]['path'] == path and result[-1]['end_line'] + 1 == number:
                    result[-1]['end_line'] = number
                else:
                    result.append({'path': path, 'start_line': number, 'end_line': number})
        return result

    def can_cite(self, path, first, last):
        self._unchanged()
        return covering_ranges(self.supplied_ranges(), path, first, last) is not None
