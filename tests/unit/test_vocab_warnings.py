import pytest

from animetag import vocab

MODEL = {
    "blunt_bangs": ("general", "tag"),
    "swept_bangs": ("general", "tag"),
    "parted_bangs": ("general", "tag"),
    "hair_between_eyes": ("general", "tag"),
    "blue_hair": ("general", "tag"),
    "blue_hairband": ("general", "tag"),
    "long_hair": ("general", "tag"),
    "vocaloid": ("copyright", "parody"),
    "fate∕grand_order": ("copyright", "parody"),
    "hatsune_miku": ("character", "character"),
    "tony_taka": ("style", None),
}


def classify(pattern, count, model=MODEL):
    return vocab.classify_pattern(vocab.normalize(pattern), count, model)


def test_legacy_tag_in_library_but_not_model():
    warning = classify("bangs", 42)
    assert warning["kind"] == "legacy"
    assert warning["count"] == 42
    assert {"blunt_bangs", "swept_bangs"} <= set(warning["similar"])


def test_unknown_tag_with_suggestion():
    warning = classify("blue_hiar", 0)
    assert warning["kind"] == "unknown"
    assert warning["similar"][0] == "blue_hair"
    assert len(warning["similar"]) <= 5


def test_known_tag_without_images_is_empty():
    assert classify("hatsune_miku", 0)["kind"] == "empty"


def test_known_tag_with_images_is_fine():
    assert classify("long_hair", 10) is None
    assert classify("parody/vocaloid", 3) is None
    assert classify("fate/grand_order", 3) is None


def test_prefix_must_fit_category():
    warning = classify("tag/vocaloid", 0)
    assert warning["kind"] == "unknown"
    assert "parody/vocaloid" in warning["similar"]


def test_rating_meta_and_untagged_are_known():
    assert classify("rating/explicit", 5) is None
    assert classify("rating/banana", 0)["kind"] == "unknown"
    assert classify("meta/ai-generated", 2) is None
    assert classify("meta/highres", 0)["kind"] == "unknown"   # not whitelisted, never written
    assert classify("tag/untagged", 1) is None


def test_style_names_are_never_written():
    assert classify("tony_taka", 0)["kind"] == "unknown"


def test_globs_are_never_classified():
    assert classify("blue_*", 0) is None
    assert classify("rating/*", 0) is None


def test_offline_falls_back_to_library_only():
    assert vocab.classify_pattern("bangs", 42, None) is None
    warning = vocab.classify_pattern("blue_hiar", 0, None, library_names=["blue_hair", "red_hair"])
    assert warning["kind"] == "unknown" and warning["similar"] == ["blue_hair"]


def test_suggest():
    names = ["blunt_bangs", "swept_bangs", "parted_bangs", "asymmetrical_bangs", "crossed_bangs",
             "long_hair", "bangs_pinned_back"]
    got = vocab.suggest("bangs", names)
    assert 1 <= len(got) <= 5
    assert "long_hair" not in got
    assert all("bangs" in g for g in got)


def test_load_model_vocabulary_offline(monkeypatch):
    import huggingface_hub

    def boom(*args, **kwargs):
        raise OSError("offline")

    vocab.load_model_vocabulary.cache_clear()
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", boom)
    try:
        assert vocab.load_model_vocabulary() is None
    finally:
        vocab.load_model_vocabulary.cache_clear()


@pytest.mark.integration
def test_load_model_vocabulary_real():
    vocab.load_model_vocabulary.cache_clear()
    model = vocab.load_model_vocabulary()
    assert model["hatsune_miku"] == ("character", "character")
    assert model["fate∕grand_order"] == ("copyright", "parody")
    assert model["long_hair"] == ("general", "tag")
    assert "bangs" not in model
