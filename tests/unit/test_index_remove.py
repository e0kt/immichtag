"""Removing deleted images from a live index, and leaving trashed ones out of a build."""

from __future__ import annotations

import shutil

import pytest

from immichtag import filterer, index, sidecar

PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")

TAGS = {
    "1.jpg": ["tag/long_hair", "tag/blue_hair", "rating/general"],
    "2.jpg": ["tag/long_hair", "tag/blue_eyes", "rating/general"],
    "3.jpg": ["tag/short_hair", "rating/general"],
    "4.jpg": ["tag/long_hair", "rating/sensitive"],
}


@pytest.fixture
def root(tmp_path, fixtures_dir):
    root = tmp_path / "upload"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name, tags in TAGS.items():
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
        sidecar.sidecar_path(folder / name).write_bytes(sidecar.build_xmp(tags, PROV))
    return root


def names(idx, ids):
    return {idx.images[i].name for i in ids}


def counts(idx):
    return dict(idx.tag_counts())


def test_remove_drops_an_image_everywhere(root):
    idx = index.build(root)
    two = next(i for i, p in enumerate(idx.images) if p.name == "2.jpg")
    before = counts(idx)
    generation = idx.generation

    idx.remove({two})

    assert "2.jpg" not in names(idx, idx.select([]))
    assert "2.jpg" not in names(idx, idx.expand("long_hair"))
    assert "2.jpg" not in names(idx, idx.select(filterer.parse_filters(["!short_hair"])))
    after = counts(idx)
    assert after["tag/long_hair"] == before["tag/long_hair"] - 1
    assert "tag/blue_eyes" not in after          # its only image is gone
    assert idx.live_count == 3 and idx.image_count == 4
    assert idx.revision == 1 and idx.generation == generation
    assert idx.retag_counts()["outdated"] == 3     # PROV is not the current model revision

    idx.remove({two})                            # removing again changes nothing
    assert idx.revision == 1


def test_remove_clears_cached_glob_expansions(root):
    idx = index.build(root)
    assert names(idx, idx.expand("blue_*")) == {"1.jpg", "2.jpg"}
    idx.remove({i for i, p in enumerate(idx.images) if p.name == "1.jpg"})
    assert names(idx, idx.expand("blue_*")) == {"2.jpg"}


def test_build_leaves_hidden_paths_out(root):
    idx = index.build(root, hidden={"u/aa/2.jpg"})
    assert idx.image_count == 3
    assert "2.jpg" not in {p.name for p in idx.images}
    assert "tag/blue_eyes" not in counts(idx)


def test_holder_asks_for_hidden_paths_on_every_build(root):
    hidden = {"u/aa/3.jpg"}
    holder = index.IndexHolder(root, hidden_provider=lambda: set(hidden))
    holder.start()
    assert holder.require().image_count == 3
    hidden.clear()
    holder.start()
    assert holder.require().image_count == 4
