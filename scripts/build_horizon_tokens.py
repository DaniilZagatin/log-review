"""Generate tokens.css for the Horizon design system from its tokens.json."""
import json
import re
import sys
from pathlib import Path

HORIZON = Path(__file__).resolve().parents[1] / "backend" / "pko" / "render" / "design" / "horizon"
_REFERENCE = re.compile(r"^\{([a-z0-9-]+)\}$")


def _value(raw: str) -> str:
    match = _REFERENCE.match(raw)
    return f"var(--{match.group(1)})" if match else raw


def _themed(tokens: list, theme: str) -> list[str]:
    lines = []
    for token in tokens:
        value = token["value"]
        lines.append(f"--{token['name']}:{_value(value[theme] if isinstance(value, dict) else str(value))};")
    return lines


def build(tokens: dict) -> str:
    base = [f"--font-{name}:{family};" for name, family in tokens["type"]["families"].items()]
    for group in tokens["type"]["groups"]:
        for style in group["styles"]:
            name = style["name"]
            base += [f"--{name}-size:{style['fontSize']};", f"--{name}-line:{style['lineHeight']};",
                     f"--{name}-weight:{style['fontWeight']};"]
            if style.get("letterSpacing"):
                base.append(f"--{name}-tracking:{style['letterSpacing']};")
    for section in ("spacing", "radius", "control", "duration", "easing", "blur", "zIndex", "opacity", "stroke",
                    "container", "breakpoint"):
        base += _themed(tokens[section]["tokens"], "light")
    light = _themed(tokens["color"]["tokens"], "light") + _themed(tokens["shadow"]["tokens"], "light")
    dark = _themed(tokens["color"]["tokens"], "dark") + _themed(tokens["shadow"]["tokens"], "dark")
    return ("/* Horizon — generated from tokens.json by scripts/build_horizon_tokens.py */\n"
            f":root{{{''.join(base + light)}}}\n"
            f':root[data-theme="dark"]{{{"".join(dark)}}}\n')


def main() -> int:
    tokens = json.loads((HORIZON / "tokens.json").read_text(encoding="utf-8"))
    (HORIZON / "tokens.css").write_text(build(tokens), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
