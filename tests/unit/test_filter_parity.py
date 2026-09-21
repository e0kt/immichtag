"""The web index and the CLI scan must select exactly the same images (FR-026)."""

import shutil

import pytest

from immichtag import filterer, index, sidecar

SL = "\u2215"
PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")

LIBRARY = {
    "a.jpg": ["character/jeanne_d'arc_alter_(fate)", f"parody/fate{SL}grand_order", "tag/long_hair",
              "meta/official_art", "rating/sensitive"],
    "b.jpg": ["character/hatsune_miku", "parody/vocaloid", "tag/twintails", "tag/blue_hair", "rating/general"],
    "c.jpg": ["tag/bangs", "tag/long_hair", "rating/explicit"],                   # legacy WD14
    "d.jpg": ["tag/blue_hair", "tag/cat_ears", "meta/ai-generated", "rating/questionable", "my_album/trip"],
    "e.jpg": ["tag/\\||/", "tag/cat_ears", "rating/general"],
}

CASES = [
    (["fate/grand_order"], [], False),
    (["parody/fate/grand_order"], [], False),
    (["long_hair"], [], False),
    (["blue_*"], [], False),
    (["rating/*"], ["rating/explicit"], False),
    (["cat_ears", "character/*"], [], False),
    (["cat_ears", "character/*"], [], True),
    (["cat_ears,-rating/questionable"], [], False),
    ([], ["rating/explicit,meta/*"], False),
    (["bangs"], [], False),
    (["my_album/trip"], [], False),
    (["\\||/", "ai_generated"], [], False),
]


@pytest.fixture
def library_root(tmp_path, fixtures_dir):
    root = tmp_path / "lib"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name, tag_list in LIBRARY.items():
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
        prov = None if name == "c.jpg" else PROV
        sidecar.sidecar_path(folder / name).write_bytes(sidecar.build_xmp(tag_list, prov))
    return root


@pytest.mark.parametrize("filters,exclude,match_all", CASES)
def test_index_and_scan_agree(library_root, filters, exclude, match_all):
    parsed = filterer.parse_filters(filters)
    veto = filterer.parse_exclusions(exclude)
    scanned = sorted(m.image.name for m in filterer.find_matches(library_root, parsed, match_all, None, veto))
    built = index.build(library_root)
    selected = sorted(built.images[i].name for i in built.select(parsed, match_all, veto))
    assert scanned == selected


def test_expected_selections(library_root):
    built = index.build(library_root)

    def names(spec):
        return sorted(built.images[i].name for i in built.select(filterer.parse_filters([spec])))

    assert names("fate/grand_order") == ["a.jpg"]
    assert names("blue_*") == ["b.jpg", "d.jpg"]
    assert names("bangs") == ["c.jpg"]
    assert names("my_album/trip") == ["d.jpg"]
    assert names("\\||/") == ["e.jpg"]


def test_index_counts_images_to_retag(library_root):
    assert index.build(library_root).retag_counts() == {"legacy": 1, "outdated": 4}
