"""Explicit bundled process inputs: bounded text, provenance and integrity, no execution."""

import hashlib
from pathlib import Path

from .contracts import strict_json_loads

ROOT = Path(__file__).resolve().parents[2]
PRESETS = ("p3283",)
_KEYS = {"schema_version", "process_id", "process_name", "source_kind", "authority",
         "source_file", "sha256", "bytes"}


def load_process_preset(name):
    if name not in PRESETS:
        raise ValueError("Unknown process preset")
    folder=ROOT / "inputs/processes" / name
    description,source=folder / "description.md",folder / "source.json"
    for path in (description,source):
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()) or not path.is_file():
            raise ValueError("Bundled process input is missing or unsafe; restore the complete archive")
    with source.open("rb") as handle:
        metadata_bytes=handle.read(10001)
    if len(metadata_bytes)>10000:
        raise ValueError("Process metadata exceeds its size limit")
    metadata=strict_json_loads(metadata_bytes.decode("utf-8"))
    if (type(metadata) is not dict or set(metadata)!=_KEYS
            or metadata["schema_version"]!="process-input/1" or metadata["process_id"]!="П3283"
            or metadata["source_kind"]!="derived_upstream" or metadata["authority"]!="proposal"
            or type(metadata["bytes"]) is not int or not 0<metadata["bytes"]<=200000
            or any(type(metadata[key]) is not str or not metadata[key].strip()
                   for key in ("process_name","source_file","sha256"))):
        raise ValueError("Invalid process metadata or source authority")
    with description.open("rb") as handle:
        raw=handle.read(200001)
    if len(raw)>200000 or len(raw)!=metadata["bytes"] or hashlib.sha256(raw).hexdigest()!=metadata["sha256"]:
        raise ValueError("Bundled process description failed its SHA-256 check; restore it or use --business-file")
    text=raw.decode("utf-8")
    return text,{**metadata,"preset":name,"path":description.relative_to(ROOT).as_posix(),"characters":len(text)}
