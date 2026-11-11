"""Where things live: the library root, the Immich settings, and per-library state.

Every entry point resolves the library root here, so `--dir`, `IMMICHTAG_DIR` and the
config file mean the same thing in the tagger, the filter and the web UI.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IMMICH_URL = "http://127.0.0.1:2283"

#: How the library is chosen, for help texts and the error when none is.
ROOT_SOURCES = "--dir, $IMMICHTAG_DIR, or [library] dir in config.toml"

_PERMISSION_WARNED: set = set()


def resolve_root(cli_dir: str | None) -> Path:
    """`--dir`, else `$IMMICHTAG_DIR`, else `[library] dir` in config.toml.

    There is no built-in default: guessing would tag (and back up, and sync) a folder
    the user never chose. With none of the three set, exit with a usage error.
    """
    if cli_dir:
        return Path(cli_dir).expanduser().resolve()
    env = os.environ.get("IMMICHTAG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    configured = (_load_config().get("library") or {}).get("dir")
    if configured:
        return Path(str(configured).strip()).expanduser().resolve()
    print(
        f"error: no library given. Use {ROOT_SOURCES} ({config_path()}).\n"
        "For Immich, that is the folder mounted as /data (UPLOAD_LOCATION in Immich's .env), "
        "or an external library's folder.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def library_id(root: Path) -> str:
    """Short, stable id of a library, so two libraries never share state."""
    return hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]


def _state_base() -> Path:
    override = os.environ.get("IMMICHTAG_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "immichtag"
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "immichtag"


def state_dir(root: Path) -> Path:
    """Lock, pending Immich sync and backups for one library — never inside the library,
    which is Immich's own storage."""
    path = _state_base() / library_id(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    override = os.environ.get("IMMICHTAG_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "immichtag" / "config.toml"


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


def _load_config() -> dict:
    """The whole config.toml, or {} when there is none."""
    path = config_path()
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    loose = path.stat().st_mode & 0o077
    if (data.get("immich") or {}).get("api_key") and loose and path not in _PERMISSION_WARNED:
        _PERMISSION_WARNED.add(path)
        print(
            f"warning: {path} is readable by other users and holds an API key; chmod 600 it",
            file=sys.stderr,
        )
    return data


def load_immich_settings() -> ImmichSettings:
    """`config.toml`'s [immich] table, each value overridable by IMMICHTAG_*."""
    table: dict = _load_config().get("immich", {}) or {}

    def pick(env: str, key: str, default: str | None = None) -> str | None:
        value = os.environ.get(env) or table.get(key) or default
        return str(value).strip() if value else None

    return ImmichSettings(
        url=(pick("IMMICHTAG_URL", "url", DEFAULT_IMMICH_URL) or DEFAULT_IMMICH_URL).rstrip("/"),
        api_key=pick("IMMICHTAG_API_KEY", "api_key"),
        path_map=pick("IMMICHTAG_PATH_MAP", "path_map"),
    )
