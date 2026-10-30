"""Load the product-owned skill, explicitly routed by phase and declared input type.

This is not a plugin loader. Nothing is loaded from the target repository, and
no scripts are executed. A GLM API request gets the selected authored text in its
system message; original documents remain untrusted per-run inputs.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2] / "guidance/process-observability"
ROUTES = {"map":("map.md","coverage.md"), "instrumentation":("coverage.md","instrumentation.md"),
          "metrics":("metrics.md","metric-formulas.md")}
_ID = re.compile(r"^### ([A-Z][A-Z0-9-]+)\s", re.M)


@dataclass(frozen=True)
class Guidance:
    texts: dict[str, str]
    ids: dict[str, list[str]]
    provenance: dict
    fingerprints: dict[str, str]


def load_guidance(*, event_formats=(), deliveries=(), root=ROOT, max_phase_chars=14000):
    if not isinstance(event_formats, (tuple,list)) or not isinstance(deliveries,(tuple,list)):
        raise ValueError("Guidance selection must be lists")
    if set(event_formats) - {"logging","clickstream"} or set(deliveries) - {"smd","kafka"}:
        raise ValueError("Unsupported event format or delivery guidance")
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Guidance pack cannot be a symbolic link")
    formats, delivery = sorted(set(event_formats)), sorted(set(deliveries))
    routes = {phase:["SKILL.md", *("references/"+name for name in names),
                     *("references/"+name+".md" for name in formats),
                     *("references/"+name+".md" for name in delivery if phase == "instrumentation")]
              for phase,names in ROUTES.items()}
    files, texts, ids = {}, {}, {}
    for name in sorted({"sources.json", *(name for names in routes.values() for name in names)}):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Guidance resource must stay inside the product pack")
        raw = path.read_bytes()
        if len(raw)>50000:
            raise ValueError("Guidance resource exceeds its size budget")
        files[name] = {"sha256":hashlib.sha256(raw).hexdigest(), "text":raw.decode("utf-8")}
    core = files["SKILL.md"]["text"]
    if not core.startswith("---\nname: process-observability\n"):
        raise ValueError("Expected the product process-observability skill")
    for phase,names in routes.items():
        text = "\n\n".join(files[name]["text"] for name in names)
        if len(text)>max_phase_chars:
            raise ValueError("Selected phase guidance exceeds its context budget; no silent truncation")
        texts[phase], ids[phase] = text, _ID.findall(text)
        if len(ids[phase]) != len(set(ids[phase])):
            raise ValueError("Duplicate guidance ID")
    fingerprints = {"guidance/process-observability/"+name:item["sha256"] for name,item in files.items()}
    meta = {"skill":"process-observability", "version":"1", "routing":routes,
            "event_formats":formats, "deliveries":delivery,
            "selection_status":"operator_declared" if formats or delivery else "not_declared",
            "files":[{"path":name,"sha256":digest} for name,digest in sorted(fingerprints.items())],
            "active_ids":ids, "source_authority":"Methodological paraphrases; supplied snapshots unconfirmed current; DOCX remains proposal"}
    meta["pack_sha256"] = hashlib.sha256(json.dumps(meta,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return Guidance(texts,ids,meta,fingerprints)
