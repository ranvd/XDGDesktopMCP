"""CLI entry point for the XDG Desktop MCP server."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .server import mcp


def main(argv: list[str] | None = None) -> int:
    """Run the XDG Desktop MCP server over stdio (or specified transport)."""
    parser = argparse.ArgumentParser(
        prog="xdg-desktop-mcp",
        description="MCP server for Linux desktop GUI observation and control via XDG Desktop Portals.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport mechanism (default: stdio).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="WARNING",
        help="Logging verbosity sent to stderr (default: WARNING).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
