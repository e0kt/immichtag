"""Entry point for `python -m wd14`; `uv run wd14` goes through wd14.cli:main."""

from wd14.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
