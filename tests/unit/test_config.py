import os
from pathlib import Path

import pytest

from immichtag import config


def test_root_resolution_order(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    cli_dir = tmp_path / "cli"
    file_dir = tmp_path / "file"
    cfg = tmp_path / "config.toml"
    _write_config(cfg, f'[library]\ndir = "{file_dir}"\n')
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(cfg))
    monkeypatch.setenv("IMMICHTAG_DIR", str(env_dir))
    assert config.resolve_root(str(cli_dir)) == cli_dir.resolve()
    assert config.resolve_root(None) == env_dir.resolve()
    monkeypatch.delenv("IMMICHTAG_DIR")
    assert config.resolve_root(None) == file_dir.resolve()


def test_no_library_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(SystemExit) as exit_info:
        config.resolve_root(None)
    assert exit_info.value.code == 2
    assert "no library given" in capsys.readouterr().err


def _write_config(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def test_immich_settings_file_then_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, '[immich]\nurl = "http://file:1"\napi_key = "file-key"\npath_map = "/data=/x"\n')
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(cfg))
    settings = config.load_immich_settings()
    assert (settings.url, settings.api_key, settings.path_map) == ("http://file:1", "file-key", "/data=/x")

    monkeypatch.setenv("IMMICHTAG_URL", "http://env:2")
    monkeypatch.setenv("IMMICHTAG_API_KEY", "env-key")
    monkeypatch.setenv("IMMICHTAG_PATH_MAP", "/data=/y")
    settings = config.load_immich_settings()
    assert (settings.url, settings.api_key, settings.path_map) == ("http://env:2", "env-key", "/data=/y")


def test_immich_settings_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(tmp_path / "missing.toml"))
    settings = config.load_immich_settings()
    assert settings.url == "http://127.0.0.1:2283"
    assert settings.api_key is None
    assert not settings.configured


def test_loose_config_permissions_warn_once(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, '[immich]\napi_key = "k"\n', mode=0o644)
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(cfg))
    config.load_immich_settings()
    config.load_immich_settings()
    assert capsys.readouterr().err.count("chmod 600") == 1


def test_loose_config_without_key_is_fine(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, '[library]\ndir = "/x"\n', mode=0o644)
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(cfg))
    config.load_immich_settings()
    assert capsys.readouterr().err == ""


def test_api_key_never_in_repr():
    settings = config.ImmichSettings(url="http://x", api_key="super-secret-key", path_map=None)
    assert "super-secret-key" not in repr(settings)
    assert "super-secret-key" not in str(settings)


def test_state_dir_and_library_id(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMICHTAG_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "lib"
    root.mkdir()
    first = config.state_dir(root)
    assert first.is_dir()
    assert first.parent == tmp_path / "state"
    assert first.name == config.library_id(root)
    assert config.library_id(root) == config.library_id(tmp_path / "lib" / ".." / "lib")
    assert len(config.library_id(root)) == 12
    assert config.library_id(root) != config.library_id(tmp_path)
