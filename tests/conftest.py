"""Shared fixtures. No test may touch the real library, state dir or Immich."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MODEL_WEIGHTS = (
    Path.home()
    / ".cache/huggingface/hub/models--pixai-labs--pixai-tagger-v1.0/snapshots"
    / "f33cfdb53c0c90b049bab9ce066eea1118970ef8/model.safetensors"
)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def tmp_library(tmp_path: Path) -> Path:
    """A tiny library laid out like Immich's upload tree: <user>/<ab>/<cd>/<file>."""
    root = tmp_path / "lib"
    target = root / "259c78e3-0000-0000-0000-000000000000" / "ab" / "cd"
    target.mkdir(parents=True)
    for image in sorted((FIXTURES / "images").iterdir()):
        shutil.copy2(image, target / image.name)
    return root


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMMICHTAG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("IMMICHTAG_CONFIG", str(tmp_path / "config.toml"))
    for name in ("IMMICHTAG_DIR", "IMMICHTAG_URL",
                 "IMMICHTAG_API_KEY", "IMMICHTAG_PATH_MAP"):
        monkeypatch.delenv(name, raising=False)


def _model_available() -> bool:
    if not MODEL_WEIGHTS.exists():
        return False
    try:
        import torch
    except ImportError:
        return False
    return torch.backends.mps.is_available() or torch.cuda.is_available()


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    if _model_available():
        return
    skip = pytest.mark.skip(reason="PixAI model not cached or no GPU")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
