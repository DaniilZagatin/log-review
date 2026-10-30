"""Integrity of local resume artifacts; checksums detect corruption, not forgery.

Only declared product files may participate. No discovery of credentials or source
files takes place here. A resumed caller validates inputs before any model call.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import stat

from .map_input import MapView, read_regular

ALLOWED_ARTIFACTS = frozenset({
    'scenario_map.json', 'map_hierarchy.json', 'logging_review.json', 'metric_catalog.json',
    'logging_contracts.json', 'logging_review.checkpoint.json', 'belka/scenario_map.checkpoint.json',
})
FINAL_FILES = ALLOWED_ARTIFACTS | {'product_run.json', 'report.html'}
_DIGEST = re.compile(r'[0-9a-f]{64}')


def _safe_path(directory, name, *, allowed=ALLOWED_ARTIFACTS):
    if type(name) is not str or name not in allowed:
        raise ValueError('Unsupported product artifact path')
    base = Path(directory).absolute()
    path = base / name
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('Product artifacts cannot use links or junctions')
    return path


def _hash(directory, name, *, allowed=ALLOWED_ARTIFACTS):
    path = _safe_path(directory, name, allowed=allowed)
    # read_regular reads raw UTF-8 bytes without platform newline translation and
    # checks a stable regular file before/after opening. Metadata is bounded too.
    text = read_regular(path, maximum=128 * 1024 * 1024)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _hashes(value, *, allowed):
    if type(value) is not dict:
        raise ValueError('Product artifact hashes must be an object')
    if any(type(name) is not str or name not in allowed or type(digest) is not str
           or not _DIGEST.fullmatch(digest) for name, digest in value.items()):
        raise ValueError('Invalid product artifact hash entry')
    return value


def record_artifact(manifest, directory, name):
    """Record one already atomically published artifact; caller saves the manifest."""
    digest = _hash(directory, name)
    hashes = manifest.setdefault('artifact_hashes', {})
    _hashes(hashes, allowed=ALLOWED_ARTIFACTS)
    hashes[name] = digest
    return digest


def verify_resume(directory, manifest):
    """Reject missing, changed or unrecorded reusable files before model dispatch."""
    if type(manifest) is not dict:
        raise ValueError('Invalid product resume manifest')
    checksums_path = _safe_path(directory, 'product_checksums.json', allowed={'product_checksums.json'})
    final = checksums_path.exists()
    hashes = _hashes(manifest.get('artifact_hashes', {}), allowed=ALLOWED_ARTIFACTS)
    if final:
        checksums = json.loads(read_regular(checksums_path))
        _hashes(checksums, allowed=FINAL_FILES)
        if not {'product_run.json', 'report.html'} <= checksums.keys():
            raise ValueError('Incomplete final product checksums')
        for name, digest in checksums.items():
            if _hash(directory, name, allowed=FINAL_FILES) != digest:
                raise ValueError('Product artifact checksum mismatch: ' + name)
        saved = json.loads(read_regular(_safe_path(directory, 'product_run.json', allowed=FINAL_FILES)))
        if saved != manifest:
            raise ValueError('Resume manifest differs from the published manifest')
        for name, digest in checksums.items():
            if name in ALLOWED_ARTIFACTS:
                if name in hashes and hashes[name] != digest:
                    raise ValueError('Conflicting product artifact checksums: ' + name)
                hashes = {**hashes, name: digest}
    elif 'artifact_hashes' not in manifest:
        raise ValueError('Unfinished legacy run has no artifact hashes; start a new run')
    for name, digest in hashes.items():
        if _hash(directory, name) != digest:
            raise ValueError('Product artifact checksum mismatch: ' + name)
    for name in ALLOWED_ARTIFACTS:
        path = _safe_path(directory, name)
        if path.exists() and name not in hashes:
            raise ValueError('Unrecorded product resume artifact: ' + name)
    if 'scenario_map.json' in hashes:
        model = json.loads(read_regular(_safe_path(directory, 'scenario_map.json')))
        revision = MapView.from_dict(model).revision
        expected = manifest.get('map_revision')
        completed = manifest.get('stages', {}).get('map', {}).get('status') == 'completed'
        if expected is not None or completed:
            if revision != expected:
                raise ValueError('Published map revision differs from product manifest')
