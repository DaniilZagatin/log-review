"""Compare saved maps conservatively, without executing sources or calling models."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pko.scenario_map.stability import compare_maps, audit_statistics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path, help="scenario_map.json")
    parser.add_argument("right", type=Path, help="scenario_map.json")
    parser.add_argument("--left-hierarchy", type=Path)
    parser.add_argument("--right-hierarchy", type=Path)
    parser.add_argument("--left-audit", type=Path, nargs="+")
    parser.add_argument("--right-audit", type=Path, nargs="+")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if bool(args.left_hierarchy) != bool(args.right_hierarchy):
        parser.error("Передайте обе группировки или ни одной")
    read = lambda path: json.loads(path.read_text(encoding="utf-8-sig")) if path else None
    try:
        result = compare_maps(read(args.left), read(args.right), read(args.left_hierarchy), read(args.right_hierarchy))
        for side in ("left", "right"):
            paths = getattr(args, side+"_audit")
            if paths:
                files = sorted(set(f for path in paths for f in (path.glob("*.txt") if path.is_dir() else [path])))
                if not files:
                    raise ValueError("В каталоге аудита нет txt-файлов")
                result[side+"_audit"] = audit_statistics(files)
        content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(content, encoding="utf-8")
            print(str(args.out))
        else:
            print(content, end="")
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        parser.exit(2, f"Не удалось сравнить карты: {exc}\n")
    # Differences are observations for review, never a semantic pass/fail gate.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
