"""Entry point for `python -m animetag`; `uv run animetag` goes through animetag.cli:main."""

from animetag.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
