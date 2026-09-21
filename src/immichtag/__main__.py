"""Entry point for `python -m immichtag`; `uv run immichtag` goes through immichtag.cli:main."""

from immichtag.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
