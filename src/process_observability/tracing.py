"""Incremental local diagnostics and explicit feedback packaging, with no network."""

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
import time
import zipfile

from .redaction import redact_text


def safe_data(value):
    if isinstance(value,str):return redact_text(value)
    if isinstance(value,dict):return {redact_text(str(key)):safe_data(member) for key,member in value.items()}
    if isinstance(value,(list,tuple)):return [safe_data(member) for member in value]
    if value is None or type(value) in {int,float,bool}:return value
    return type(value).__name__


class TraceRecorder:
    def __init__(self, directory):
        self.path = Path(directory) / "trace.jsonl"
        self.sequence, self.started = 0, time.monotonic()
        self._secrets=[]
        self._lock, self._closed = RLock(), False
        self.path.touch(exist_ok=False)

    def protect_secret(self,value):
        with self._lock:
            if isinstance(value,str) and value:self._secrets.append(value)

    def close(self):
        """Wait for the current write, then discard late model events."""
        with self._lock:
            self._closed = True

    def __call__(self, event, payload=None):
        with self._lock:
            if self._closed:
                return
            self.sequence += 1
            row = {"seq":self.sequence,"at":datetime.now(timezone.utc).isoformat(),
                   "elapsed_ms":round((time.monotonic()-self.started)*1000,3),
                   "event":event,"data":safe_data(payload)}
            encoded=json.dumps(row,ensure_ascii=False,allow_nan=False)
            for secret in self._secrets:
                encoded=encoded.replace(json.dumps(secret,ensure_ascii=False)[1:-1],"[REDACTED]")
            # Open/close each event: preceding inputs survive an interrupted call.
            with self.path.open("a",encoding="utf-8") as handle:
                handle.write(encoded+"\n")
                handle.flush()


FEEDBACK_FILES = ("trace.jsonl","diagnostics.json","manifest.json","bundle.json","process_map.json",
                  "log_coverage.json","instrumentation_plan.json","metric_catalog.json","evidence.jsonl",
                  "report.html","report.md","logging_inventory.json","code_graph.json","code_review.json",
                  "code_reduction.json")


def package_feedback(directory, output):
    directory,output=Path(directory),Path(output)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Feedback input must be a local run directory")
    selected=[]
    for name in FEEDBACK_FILES:
        path=directory/name
        if path.is_symlink():raise ValueError("Feedback files cannot be symlinks")
        if path.is_file():selected.append(path)
    if not selected:raise ValueError("No run artifacts found")
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,"x",compression=zipfile.ZIP_DEFLATED) as archive:
        for path in selected:archive.write(path,arcname=path.name)
    return len(selected)
