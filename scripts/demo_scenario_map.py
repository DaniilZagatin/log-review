"""Build a clearly labelled offline example using scripted model responses.

This exercises the real loader, tool loop, validators and renderer without a
provider. It is not a real LLM quality evaluation or an actual banking process.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "tests")]

from scenario_script import SCENARIO, SPEC, ScriptedClient, sources
from pko.output.publisher import write_outputs
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.runner import run_scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "scenario-demo")
    args = parser.parse_args()
    model = run_scenario(SCENARIO, sources(), args.out, spec=SPEC, client=ScriptedClient())
    if model.get("schema_version") != "2.0" or any(n.get("kind") != "operation" for n in model["nodes"]):
        raise RuntimeError("Демо должно использовать методологию атомарных операций 2.0")
    model["run"]["demo"] = True
    write_outputs(args.out, {
        "scenario-model": ("scenario_map.json", json.dumps(model, ensure_ascii=False, indent=2)),
        "scenario-map": ("scenario_map.html", render_scenario_map(model)),
    })
    print((args.out / "scenario_map.html").resolve())
    return 0 if model["run"]["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
