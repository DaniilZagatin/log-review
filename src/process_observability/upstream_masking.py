"""Compatibility with the first agent's published line masks, without importing it.

Patterns are adapted from Belka's sources._decode / tools._mask_secrets. This is
an input/publication boundary, not a change to either research methodology.
"""
import hashlib
import re

from .redaction import redact_text

_QUOTED = re.compile(r'''(?i)(["'][\w.-]*(?:key|token|secret|password|passwd|credential|authorization)[\w.-]*["']\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,}\]]+)''')
_URL = re.compile(r'''(\b[a-z][a-z0-9+.-]*://)[^/@\s"']+:[^/@\s"']+@''', re.I)
_BEARER = re.compile(r'(\bBearer\s+)[A-Za-z0-9._~+/=-]+', re.I)
_ASSIGNMENT = re.compile(r'''(?i)(\b\w*(?:key|token|secret|password)\w*\s*[:=]\s*)(["']?)([^\s"']{4,})\2''')


def upstream_text(raw):
    raw = raw.removeprefix('\ufeff')
    def mask(line):
        line = _QUOTED.sub(lambda m: m.group(1) + '"***"', line)
        line = _URL.sub(r'\1***:***@', line)
        line = _BEARER.sub(r'\1***', line)
        return _ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + '***' + m.group(2), line)
    return '\n'.join(mask(line) for line in raw.split('\n'))


def published_text(raw):
    return redact_text(upstream_text(raw))


def upstream_sha(raw):
    return hashlib.sha256(upstream_text(raw).encode()).hexdigest()


def shared_snapshot(snapshot):
    """Reuse admitted bytes; one consistent model/citation policy for the new path."""
    from .source_tools import RepoSnapshot, SourceFile
    files = {path: SourceFile(path, source.sha256, published_text(source._raw_text), source._raw_text)
             for path, source in snapshot.files.items()}
    return RepoSnapshot(files, dict(snapshot.scope), snapshot.logging_calls, snapshot.inventory)
