"""Console entry point."""

from __future__ import annotations

import argparse
import asyncio
import io
import sys

from . import __version__, logging
from .command import execute_command
from .terminal import Cancelled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osnm-z", description="Mint one OpenSea phase with one wallet"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} (Python)")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("mint", help="Choose one phase and mint with WALLET_KEY.")
    subcommands.add_parser("doctor", help="Check configuration, wallet signing, and RPC readiness.")
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    arguments = build_parser().parse_args()
    try:
        asyncio.run(execute_command(arguments.command))
    except Cancelled:
        logging.info("Mint cancelled.")
        return 0
    except KeyboardInterrupt:
        logging.info("Interrupted. Shutting down.")
        return 130
    except Exception as error:
        message = str(error) or (
            "The operation timed out."
            if isinstance(error, TimeoutError)
            else f"{type(error).__name__}: no error details."
        )
        logging.error(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
