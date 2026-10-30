#!/usr/bin/env python3
"""Portable entry point: no installation or dependencies are required."""

from pathlib import Path
import sys


if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required.")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from process_observability.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
