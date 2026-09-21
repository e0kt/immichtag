"""Command line entry point for the web UI: `uv run animetag-web`."""

from __future__ import annotations

import argparse

from animetag import config, web


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animetag-web",
        description=(
            "Browser UI for the library: tag new images, pick tags with autocomplete, "
            "see the matches as thumbnails, and export them to a folder."
        ),
    )
    parser.add_argument(
        "--dir",
        default=None,
        help=f"Directory to index (default: $ANIMETAG_DIR, else {config.DEFAULT_ROOT})",
    )
    # None, not the default itself: an explicitly chosen port must never be swapped
    # for a free one, or the server comes up somewhere the user isn't looking.
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to serve on (default: {web.DEFAULT_PORT}, or the next free one)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind (default: 127.0.0.1, i.e. this machine only)",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    root = config.resolve_root(args.dir)
    if not root.is_dir():
        raise SystemExit(f"Directory does not exist: {root}")

    return web.serve(
        root,
        host=args.host,
        port=args.port if args.port is not None else web.DEFAULT_PORT,
        open_browser=not args.no_browser,
        allow_other_port=args.port is None,
    )
