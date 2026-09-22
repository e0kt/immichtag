import numpy as np
import pytest

from immichtag import vocab
from immichtag.tagger import profile_name, tags_from_probs

NAMES = [
    "long_hair", "1girl", "blue_eyes",                 # general
    "hatsune_miku", "kagamine_rin",                    # character
    "vocaloid", "fate/grand_order",                    # copyright
    "tony_taka",                                       # style
    "official_art", "highres",                         # meta
    "rating:g", "rating:s", "rating:q", "rating:e",    # rating
]
CATS = (["general"] * 3 + ["character"] * 2 + ["copyright"] * 2 + ["style"]
        + ["meta"] * 2 + ["rating"] * 4)


def probs(**overrides):
    values = dict.fromkeys(NAMES, 0.0)
    values.update({k.replace("__", ":"): v for k, v in overrides.items()})
    return np.array([values[n] for n in NAMES], dtype=np.float32)


def test_thresholds_categories_and_order():
    p = probs(long_hair=0.9, blue_eyes=0.33, hatsune_miku=0.95, kagamine_rin=0.36,
              vocaloid=0.8, **{"fate/grand_order": 0.5}, tony_taka=0.99,
              official_art=0.6, highres=0.99, rating__g=0.1, rating__s=0.7,
              rating__q=0.15, rating__e=0.05)
    tags = tags_from_probs(p, NAMES, CATS)
    assert tags == [
        "character/hatsune_miku",
        "parody/vocaloid",
        "parody/fate∕grand_order",
        "tag/long_hair",
        "meta/official_art",
        "rating/sensitive",
    ]


def test_general_threshold_boundary_uses_defaults():
    t = vocab.DEFAULT_THRESHOLDS["general"]
    assert "tag/blue_eyes" in tags_from_probs(probs(blue_eyes=t), NAMES, CATS)
    assert "tag/blue_eyes" not in tags_from_probs(probs(blue_eyes=t - 0.001), NAMES, CATS)


def test_higher_score_first_within_category():
    tags = tags_from_probs(probs(long_hair=0.5, blue_eyes=0.9), NAMES, CATS)
    assert tags.index("tag/blue_eyes") < tags.index("tag/long_hair")


def test_style_never_emitted_and_meta_only_whitelisted():
    tags = tags_from_probs(probs(tony_taka=1.0, highres=1.0, official_art=1.0), NAMES, CATS)
    assert not any("tony_taka" in t for t in tags)
    assert "meta/highres" not in tags
    assert "meta/official_art" in tags


def test_junk_tags_dropped_unless_kept():
    p = probs(**{"1girl": 0.99}, long_hair=0.9)
    assert "tag/1girl" not in tags_from_probs(p, NAMES, CATS)
    assert "tag/1girl" in tags_from_probs(p, NAMES, CATS, keep_common=True)


def test_exactly_one_rating_by_argmax():
    tags = tags_from_probs(probs(long_hair=0.9, rating__q=0.3, rating__e=0.31), NAMES, CATS)
    assert [t for t in tags if t.startswith("rating/")] == ["rating/explicit"]
    no_rating = tags_from_probs(probs(long_hair=0.9, rating__e=0.9), NAMES, CATS, include_rating=False)
    assert not any(t.startswith("rating/") for t in no_rating)


def test_untagged_when_nothing_passes():
    assert tags_from_probs(probs(rating__g=0.9), NAMES, CATS) == ["tag/untagged", "rating/general"]
    assert tags_from_probs(probs(), NAMES, CATS, include_rating=False) == ["tag/untagged"]


def test_custom_thresholds():
    p = probs(long_hair=0.2, hatsune_miku=0.2)
    tags = tags_from_probs(p, NAMES, CATS, thresholds={"general": 0.1, "character": 0.1})
    assert "tag/long_hair" in tags and "character/hatsune_miku" in tags


def test_profile_name():
    assert profile_name(dict(vocab.DEFAULT_THRESHOLDS)) == "v1"
    assert profile_name({**vocab.DEFAULT_THRESHOLDS, "general": 0.3}) == "custom:g0.30,c0.37,p0.46,m0.47"


def test_mismatched_lengths_rejected():
    with pytest.raises(ValueError):
        tags_from_probs(np.zeros(3, dtype=np.float32), NAMES, CATS)


def test_huge_images_are_not_refused():
    from PIL import Image

    assert Image.MAX_IMAGE_PIXELS is None
