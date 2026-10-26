"""Command line entry point for the scenario researcher."""
from __future__ import annotations

import argparse
import sys

from pko import __version__
from pko.errors import PkoError
from pko.util.env import load_env


def _build_parser() -> argparse.ArgumentParser:
    from pko.scenario_map.runner import add_cli_command
    parser = argparse.ArgumentParser(
        prog="pko", description="Белка: подробная карта выбранного сценария по коду нескольких АС.")
    parser.add_argument("--version", action="version", version=f"pko {__version__}")
    add_cli_command(parser.add_subparsers(dest="command", required=True))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    args = _build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except PkoError as exc:
        print(f"Ошибка: {exc.render()}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return 130


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
