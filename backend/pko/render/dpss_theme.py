"""Offline typography from the user-supplied DPSS 09.25 design kit."""
import base64
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def embedded_fonts() -> str:
    assets = Path(__file__).with_name("assets") / "dpss"
    rules = []
    for weight in (400, 600, 700):
        payload = base64.b64encode((assets / f"SBSansDisplay-{weight}.woff2").read_bytes()).decode("ascii")
        rules.append("@font-face{font-family:'SB Sans Display';font-style:normal;"
                     f"font-weight:{weight};font-display:swap;"
                     f"src:url(data:font/woff2;base64,{payload}) format('woff2')}}")
    return '<style data-passport-fonts>' + ''.join(rules) + '</style>'
