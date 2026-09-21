"""Where things live: the library root, the Immich settings, and per-library state.

Every entry point resolves the library root here, so `--dir`, `ANIMETAG_DIR` and the
default mean the same thing in the tagger, the filter and the web UI.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: Scanned when neither --dir nor ANIMETAG_DIR is given: Immich's upload tree.
DEFAULT_ROOT = Path("/Users/paul/docker/immich/library/upload")
DEFAULT_IMMICH_URL = "http://127.0.0.1:2283"

_WD14_NOTICE_SHOWN = False


def resolve_root(cli_dir: str | None) -> Path:
    """`--dir`, else `$ANIMETAG_DIR`, else the Immich upload tree."""
    if cli_dir:
        return Path(cli_dir).expanduser().resolve()
    env = os.environ.get("ANIMETAG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if os.environ.get("WD14_DIR"):
        _wd14_notice()
    return DEFAULT_ROOT.resolve()


def _wd14_notice() -> None:
    # The old variable silently pointing nowhere would tag the default library instead
    # of the one the user meant, so say so — once, not on every call.
    global _WD14_NOTICE_SHOWN
    if not _WD14_NOTICE_SHOWN:
        _WD14_NOTICE_SHOWN = True
        print("note: WD14_DIR is no longer read; set ANIMETAG_DIR instead", file=sys.stderr)


def library_id(root: Path) -> str:
    """Short, stable id of a library, so two libraries never share state."""
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]


def _state_base() -> Path:
    override = os.environ.get("ANIMETAG_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "animetag"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "animetag"


def state_dir(root: Path) -> Path:
    """Lock, pending Immich sync and backups for one library — never inside the library,
    which is Immich's own storage."""
    path = _state_base() / library_id(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    override = os.environ.get("ANIMETAG_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "animetag" / "config.toml"


@dataclass(frozen=True)
class ImmichSettings:
    url: str
    api_key: str | None
    path_map: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def __repr__(self) -> str:
        # The key grants tag deletion on the whole library; it must never reach a log.
        key = "<set>" if self.api_key else None
        return f"ImmichSettings(url={self.url!r}, api_key={key}, path_map={self.path_map!r})"

    __str__ = __repr__


def load_immich_settings() -> ImmichSettings:
    """`config.toml`'s [immich] table, each value overridable by ANIMETAG_IMMICH_*."""
    path = config_path()
    table: dict = {}
    if path.is_file():
        if path.stat().st_mode & 0o077:
            print(
                f"warning: {path} is readable by other users and holds an API key; chmod 600 it",
                file=sys.stderr,
            )
        with path.open("rb") as handle:
            table = tomllib.load(handle).get("immich", {}) or {}

    def pick(env: str, key: str, default: str | None = None) -> str | None:
        value = os.environ.get(env) or table.get(key) or default
        return str(value).strip() if value else None

    return ImmichSettings(
        url=(pick("ANIMETAG_IMMICH_URL", "url", DEFAULT_IMMICH_URL) or DEFAULT_IMMICH_URL).rstrip("/"),
        api_key=pick("ANIMETAG_IMMICH_API_KEY", "api_key"),
        path_map=pick("ANIMETAG_IMMICH_PATH_MAP", "path_map"),
    )
