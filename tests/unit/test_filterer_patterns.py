import pytest

from animetag import vocab
from animetag.filterer import TagFilter, parse_exclusions, pattern_matches

SL = "\u2215"


def tags(*raw):
    full = [vocab.normalize(t) for t in raw]
    return full, [vocab.leaf_of(t) for t in full]


def matches(spec, *raw):
    full, leaf = tags(*raw)
    return TagFilter.parse(spec).matches(full, leaf)


@pytest.mark.parametrize("spec", ["fate/grand_order", "parody/fate/grand_order",
                                  f"Parody/Fate{SL}Grand Order", "parody/fate∕grand_order"])
def test_slash_names_match_with_or_without_prefix(spec):
    assert matches(spec, f"parody/fate{SL}grand_order")


def test_partial_name_does_not_match():
    assert not matches("grand_order", f"parody/fate{SL}grand_order")


def test_rating_names_unchanged():
    assert matches("rating/explicit", "rating/explicit", "tag/x")
    assert not matches("rating/explicit", "rating/sensitive")
    assert matches("explicit", "rating/explicit")


def test_full_path_globs_and_leaf_globs():
    assert matches("rating/*", "rating/general")
    assert matches("character/*", "character/hatsune_miku")
    assert not matches("character/*", "tag/hatsune_miku")
    assert matches("blue_*", "tag/blue_hair")
    assert not matches("blue_*", "tag/long_hair")
    assert matches("tag/*hair*", "tag/long_hair")


def test_bare_tag_matches_leaf_under_any_prefix():
    assert matches("long_hair", "tag/long_hair")
    assert matches("meta/ai-generated", "meta/ai-generated")
    assert matches("ai_generated", "meta/ai-generated")


def test_foreign_hierarchical_tag_is_one_name():
    assert matches("my_album/trip", "my_album/trip")
    assert matches("my_album/*", "my_album/trip")
    assert not matches("trip", "my_album/trip")


def test_legacy_slash_tag():
    assert matches("\\||/", "tag/\\||/")


def test_exclusions_keep_semantics():
    assert not matches("cat_ears,-rating/explicit", "tag/cat_ears", "rating/explicit")
    assert matches("cat_ears,-rating/explicit", "tag/cat_ears", "rating/general")
    veto = parse_exclusions(["rating/explicit,monochrome"])
    full, leaf = tags("tag/monochrome")
    assert not veto.matches(full, leaf)


def test_pattern_matches_direct():
    assert pattern_matches("parody/vocaloid", "parody/vocaloid", "vocaloid")
    assert pattern_matches("vocaloid", "parody/vocaloid", "vocaloid")
    assert not pattern_matches("tag/vocaloid", "parody/vocaloid", "vocaloid")
