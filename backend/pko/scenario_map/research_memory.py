"""Bounded public tool observations that survive transcript compaction.

This is a cache of material already returned by tools, not an evidence authority
or a source of instructions. It never derives conclusions, stores model messages
or preserves private reasoning. Callers must still validate citations against the
source-reading ledger and treat recalled source content as untrusted data.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from copy import deepcopy
from typing import Any

from pko.audit import sanitize

MAX_OBSERVATIONS = 512
MAX_CACHE_CHARS = 1_000_000
MAX_PAYLOAD_CHARS = 24_000
MAX_ARGUMENT_CHARS = 12_000
MAX_PREVIEW_CHARS = 800


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_json(value: dict, limit: int) -> tuple[str, bool]:
    """Keep exact JSON when it fits; otherwise return an explicit partial copy."""
    serialized = _serialize(value)
    if len(serialized) <= limit:
        return serialized, False
    # JSON escaping can make a prefix larger than its input. Measure the actual
    # stored representation rather than assuming that N input chars fit N bytes.
    low, high = 0, min(len(serialized), limit)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _serialize({"content_truncated": True, "serialized_prefix": serialized[:middle]})
        if len(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return _serialize({"content_truncated": True, "serialized_prefix": serialized[:low]}), True


class ResearchMemory:
    """A bounded index and independently bounded cache, ordered by last use.

    ``repeat_count`` counts calls after the first observation. Content eviction
    leaves the index and stable ID intact; metadata eviction is reported by
    ``evicted_metadata_count``. Paging and snapshots use newest-first ordering.
    Smaller constructor limits are useful for tests; they cannot raise the caps.
    """

    def __init__(self, *, max_observations: int = MAX_OBSERVATIONS,
                 max_cache_chars: int = MAX_CACHE_CHARS,
                 max_payload_chars: int = MAX_PAYLOAD_CHARS):
        if max_observations < 1 or max_cache_chars < 0 or max_payload_chars < 128:
            raise ValueError("Invalid research-memory limits")
        self._max_observations = min(max_observations, MAX_OBSERVATIONS)
        self._max_cache_chars = min(max_cache_chars, MAX_CACHE_CHARS)
        self._max_payload_chars = min(max_payload_chars, MAX_PAYLOAD_CHARS)
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_chars = 0
        self._evicted_metadata_count = 0
        self._sequence = 0

    @property
    def cache_chars(self) -> int:
        return self._cache_chars

    def checkpoint(self) -> dict:
        """Private persistence format; request digests never enter public recall."""
        return {
            "version": 1,
            "limits": {"max_observations": self._max_observations,
                       "max_cache_chars": self._max_cache_chars,
                       "max_payload_chars": self._max_payload_chars},
            "sequence": self._sequence, "evicted_metadata_count": self._evicted_metadata_count,
            "records": [{"request_digest": key, "record": deepcopy(record)}
                        for key, record in self._records.items()],
            "cache": [{"observation_id": key, "payload": json.loads(value)}
                      for key, value in self._cache.items()],
        }

    @classmethod
    def from_checkpoint(cls, value: dict) -> "ResearchMemory":
        """Restore a bounded public cache without issuing an observation again."""
        if not isinstance(value, dict) or set(value) != {
                "version", "limits", "sequence", "evicted_metadata_count", "records", "cache"} or value['version'] != 1:
            raise ValueError("Unsupported research-memory checkpoint")
        limits = value['limits']
        caps = {"max_observations": MAX_OBSERVATIONS, "max_cache_chars": MAX_CACHE_CHARS,
                "max_payload_chars": MAX_PAYLOAD_CHARS}
        if (not isinstance(limits, dict) or set(limits) != set(caps) or
                any(type(limits[k]) is not int or not 0 <= limits[k] <= cap for k, cap in caps.items())):
            raise ValueError("Invalid research-memory checkpoint limits")
        result = cls(**limits)
        for key in ('sequence', 'evicted_metadata_count'):
            if type(value[key]) is not int or not 0 <= value[key] <= 1_000_000_000:
                raise ValueError("Invalid research-memory counter")
        records, cache = value['records'], value['cache']
        if not isinstance(records, list) or len(records) > result._max_observations or not isinstance(cache, list) or len(cache) > len(records):
            raise ValueError("Research-memory checkpoint exceeds limits")
        identifiers = set()
        fields = {'observation_id', 'name', 'arguments', 'ok', 'preview', 'repeat_count', 'content_truncated'}
        for row in records:
            if not isinstance(row, dict) or set(row) != {'request_digest', 'record'}:
                raise ValueError("Invalid observation record")
            key, record = row['request_digest'], row['record']
            if not isinstance(key, str) or not re.fullmatch(r'[a-f0-9]{64}', key) or key in result._records:
                raise ValueError("Invalid observation identity")
            if not isinstance(record, dict) or not fields <= set(record) or set(record) - fields - {'arguments_truncated'}:
                raise ValueError("Invalid observation metadata")
            identifier = record['observation_id']
            if (not isinstance(identifier, str) or not re.fullmatch(r'obs-[1-9][0-9]*', identifier) or
                    int(identifier[4:]) > value['sequence'] or identifier in identifiers):
                raise ValueError("Invalid observation sequence")
            if (not isinstance(record['name'], str) or len(record['name']) > 120 or
                    not isinstance(record['arguments'], dict) or len(_serialize(record['arguments'])) > MAX_ARGUMENT_CHARS or
                    not isinstance(record['preview'], str) or len(record['preview']) > MAX_PREVIEW_CHARS or
                    type(record['repeat_count']) is not int or record['repeat_count'] < 0 or
                    any(type(record[k]) is not bool for k in ('ok', 'content_truncated'))):
                raise ValueError("Invalid observation metadata values")
            identifiers.add(identifier)
            public = sanitize(record)
            # Preview is compact JSON rendered as a string. Preserve its bytes
            # when a second sanitization only changes JSON whitespace.
            try:
                if json.loads(public['preview']) == json.loads(record['preview']):
                    public['preview'] = record['preview']
            except (ValueError, TypeError):
                pass
            result._records[key] = public
        for row in cache:
            if not isinstance(row, dict) or set(row) != {'observation_id', 'payload'}:
                raise ValueError("Invalid observation cache entry")
            identifier = row['observation_id']
            if identifier not in identifiers or identifier in result._cache or not isinstance(row['payload'], dict):
                raise ValueError("Invalid cached observation reference")
            serialized = _serialize(sanitize(row['payload']))
            if len(serialized) > result._max_payload_chars:
                raise ValueError("Cached observation exceeds payload limit")
            result._cache[identifier] = serialized
            result._cache_chars += len(serialized)
        if result._cache_chars > result._max_cache_chars:
            raise ValueError("Research-memory checkpoint exceeds cache budget")
        result._sequence = value['sequence']
        result._evicted_metadata_count = value['evicted_metadata_count']
        return result

    @property
    def evicted_metadata_count(self) -> int:
        return self._evicted_metadata_count

    def _discard_content(self, observation_id: str) -> None:
        old = self._cache.pop(observation_id, None)
        if old is not None:
            self._cache_chars -= len(old)

    def observe(self, name: str, arguments: dict, payload: dict) -> dict:
        """Remember an already-issued public response, returning a stable receipt."""
        safe_name = str(sanitize(name))[:120]
        safe_arguments = sanitize(arguments)
        safe_payload = sanitize(payload)
        if not isinstance(safe_arguments, dict) or not isinstance(safe_payload, dict):
            raise TypeError("Research observations require dictionary arguments and payload")
        # Identity uses the original request: masking distinct search patterns
        # can produce identical public text. The private digest is never exposed;
        # only sanitized arguments and payloads enter returned metadata/content.
        key = hashlib.sha256(_serialize([name, arguments]).encode("utf-8")).hexdigest()
        previous = self._records.pop(key, None)
        seen_before = previous is not None
        if previous is None:
            self._sequence += 1
            observation_id = f"obs-{self._sequence}"
            repeat_count = 0
        else:
            observation_id = previous["observation_id"]
            repeat_count = previous["repeat_count"] + 1

        serialized, truncated = _bounded_json(safe_payload, self._max_payload_chars)
        argument_json, arguments_truncated = _bounded_json(safe_arguments, MAX_ARGUMENT_CHARS)
        record = {
            "observation_id": observation_id,
            "name": safe_name,
            "arguments": json.loads(argument_json),
            "ok": bool(safe_payload.get("ok", not safe_payload.get("error"))),
            "preview": _serialize(safe_payload)[:MAX_PREVIEW_CHARS],
            "repeat_count": repeat_count,
            "content_truncated": truncated,
        }
        if arguments_truncated:
            record["arguments_truncated"] = True
        self._records[key] = record
        self._discard_content(observation_id)
        # An entry larger than the complete cache budget is explicitly uncached.
        if len(serialized) <= self._max_cache_chars:
            self._cache[observation_id] = serialized
            self._cache_chars += len(serialized)
        while self._cache_chars > self._max_cache_chars:
            _, evicted = self._cache.popitem(last=False)
            self._cache_chars -= len(evicted)
        while len(self._records) > self._max_observations:
            _, evicted = self._records.popitem(last=False)
            self._discard_content(evicted["observation_id"])
            self._evicted_metadata_count += 1
        return {"observation_id": observation_id, "seen_before": seen_before}

    def snapshot(self) -> list[dict]:
        """Return detached metadata without repopulating the model with payloads."""
        result = []
        for record in reversed(self._records.values()):
            item = deepcopy(record)
            item["content_available"] = record["observation_id"] in self._cache
            result.append(item)
        return result

    def recall(self, offset: int = 1, limit: int = 10) -> dict:
        """Page through prior responses; an evicted result is never reconstructed."""
        if isinstance(offset, bool) or isinstance(limit, bool) or not isinstance(offset, int) or not isinstance(limit, int):
            raise ValueError("offset and limit must be integers")
        if offset < 1 or limit < 1:
            raise ValueError("offset and limit must be positive")
        limit = min(limit, 30)
        records = list(reversed(self._records.values()))
        items = []
        for record in records[offset - 1:offset - 1 + limit]:
            item = deepcopy(record)
            cached = self._cache.get(record["observation_id"])
            if cached is None:
                item["content_unavailable"] = True
            else:
                item["payload"] = json.loads(cached)
            items.append(item)
        return {"items": items, "total": len(records),
                "has_more": offset - 1 + len(items) < len(records)}
