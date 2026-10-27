"""Compare a saved map with a bounded rubric without running analyzed code."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pko.scenario_map.evaluation import evaluate_map


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map", type=Path)
    parser.add_argument("rubric", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate_map(json.loads(args.map.read_text(encoding="utf-8-sig")),
                              json.loads(args.rubric.read_text(encoding="utf-8-sig")))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f"Ошибка контрольной разметки или карты: {exc}\n")
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(content, encoding="utf-8")
        print(f"Findings: {len(result['findings'])}; matched: {result['matched_operations']}/{result['expected_operations']}; {args.out}")
    else:
        print(content, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
