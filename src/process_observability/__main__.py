"""Module entry point for the portable process observability CLI."""

from .cli import build_parser, main, status_report

if __name__ == "__main__":
    raise SystemExit(main())
