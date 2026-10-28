"""Identity of the running Belka build, so runs on different machines can be compared.

The code digest covers the package files actually loaded (code, prompts,
methodology, profiles and offline assets). The git revision is added when the
package runs from a checkout; a dirty tree is reported, since HEAD alone does
not identify a modified working copy.
"""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path

from pko import __version__

_PACKAGE = Path(__file__).resolve().parent
_SUFFIXES = {".py", ".md", ".json", ".css", ".js", ".woff2", ".txt"}


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=_PACKAGE, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


@lru_cache(maxsize=1)
def build_identity() -> dict:
    digest = hashlib.sha256()
    for path in sorted(p for p in _PACKAGE.rglob("*") if p.is_file() and p.suffix in _SUFFIXES
                       and "__pycache__" not in p.parts):
        digest.update(path.relative_to(_PACKAGE).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", ".") if head else None
    return {"version": __version__, "code_sha256": digest.hexdigest(),
            "git_head": head, "git_dirty": bool(status) if status is not None else None}
