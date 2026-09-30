"""What a selection sent by the page resolves to."""

from __future__ import annotations

import shutil

import pytest

from immichtag import sidecar, web

PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")


@pytest.fixture
def app(tmp_path, fixtures_dir):
    root = tmp_path / "upload"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name, tags in {"1.jpg": ["tag/cat"], "2.jpg": ["tag/cat"], "3.jpg": ["tag/cat"], "4.jpg": ["tag/dog"]}.items():
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
        sidecar.sidecar_path(folder / name).write_bytes(sidecar.build_xmp(tags, PROV))
    state = web.AppState(root)
    state.holder.start()
    state.holder.require()
    return state


def query(**extra):
    return {"filters": [{"include": ["cat"], "exclude": []}], **extra}


def gen(app):
    return app.holder.require().generation


def test_ids_keep_order_and_drop_duplicates(app):
    assert app.resolve_selection({"mode": "ids", "ids": [2, 0, 2], "generation": gen(app)}) == [2, 0]


def test_all_matches_what_search_counts_even_when_capped(app):
    total = app.search(query(maxImages=1))["total"]
    ids = app.resolve_selection({"mode": "all", "query": query(maxImages=1), "generation": gen(app)})
    assert len(ids) == total == 3


def test_all_minus_except(app):
    ids = app.resolve_selection({"mode": "all", "query": query(), "except": [1], "generation": gen(app)})
    assert 1 not in ids and len(ids) == 2


def test_stale_generation_is_409(app):
    with pytest.raises(web.HTTPStatusError) as err:
        app.resolve_selection({"mode": "ids", "ids": [0], "generation": gen(app) - 1})
    assert err.value.status == 409


def test_removed_or_unknown_ids_are_rejected(app):
    app.holder.require().remove({0})
    for bad in (0, 99):
        with pytest.raises(ValueError):
            app.resolve_selection({"mode": "ids", "ids": [bad], "generation": gen(app)})


def test_empty_selection_is_an_error(app):
    with pytest.raises(ValueError):
        app.resolve_selection({"mode": "all", "query": {"filters": [{"include": ["bird"]}]}, "generation": gen(app)})
    with pytest.raises(ValueError):
        app.resolve_selection({"mode": "ids", "ids": [], "generation": gen(app)})


def test_search_and_status_report_revision_and_live_count(app):
    app.holder.require().remove({3})
    assert app.search(query())["revision"] == 1
    status = app.status()
    assert status["images"] == 3 and status["revision"] == 1
