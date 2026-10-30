"""Offline typography and Horizon tokens for the vendored map renderer.

``embedded_fonts`` is copied from producer ``backend/pko/render/dpss_theme.py`` and
``horizon_tokens`` from producer ``backend/pko/render/horizon_theme.py``. Only the asset
location differs: everything this package reads lies under its own ``assets`` folder, so a
wheel or a plain copy works offline without the producer.
"""
import base64
from functools import lru_cache
from pathlib import Path

_ASSETS = Path(__file__).with_name("assets")


@lru_cache(maxsize=1)
def embedded_fonts() -> str:
    assets = _ASSETS / "dpss"
    rules = []
    for weight in (400, 600, 700):
        payload = base64.b64encode((assets / f"SBSansDisplay-{weight}.woff2").read_bytes()).decode("ascii")
        rules.append("@font-face{font-family:'SB Sans Display';font-style:normal;"
                     f"font-weight:{weight};font-display:swap;"
                     f"src:url(data:font/woff2;base64,{payload}) format('woff2')}}")
    return '<style data-passport-fonts>' + ''.join(rules) + '</style>'


@lru_cache(maxsize=1)
def horizon_tokens() -> str:
    return (_ASSETS / "horizon" / "tokens.css").read_text(encoding="utf-8")
