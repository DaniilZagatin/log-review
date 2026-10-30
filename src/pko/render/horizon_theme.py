"""Offline styles of the Horizon design system (tokens plus component CSS)."""
import re
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).with_name("design") / "horizon"


@lru_cache(maxsize=1)
def horizon_tokens() -> str:
    return (_ROOT / "tokens.css").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def horizon_styles() -> str:
    tokens = (_ROOT / "tokens.css").read_text(encoding="utf-8")
    components = (_ROOT / "components" / "bundle.css").read_text(encoding="utf-8")
    # The bundle imports Google Fonts; the renderer must not make network requests.
    components = re.sub(r"@import\s+url\([^)]*\);?", "", components)
    return f"<style data-horizon>{tokens}{components}</style>"
