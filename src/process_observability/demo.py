"""Explicit synthetic replay; no live fallback and no test-oracle access."""

import hashlib
import json
from pathlib import Path

from .contracts import strict_json_loads


def request_fingerprint(system, user):
    return hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()


class RecordedModel:
    def __init__(self, path):
        payload = strict_json_loads(Path(path).read_text(encoding="utf-8"))
        if type(payload) is not dict or payload.get("data_origin") != "synthetic" or type(payload.get("records")) is not dict:
            raise ValueError("Invalid synthetic model replay")
        self.records = payload["records"]

    def complete_json(self, system, user):
        key = request_fingerprint(system, user)
        if key not in self.records:
            raise ValueError("Synthetic replay miss: task, prompt or input fingerprint changed")
        # Return independent mutable data, never the oracle or another task's response.
        return json.loads(json.dumps(self.records[key], ensure_ascii=False))
