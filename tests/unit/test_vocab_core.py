from immichtag import vocab


def test_escape_round_trip():
    assert vocab.escape_name("fate/grand_order") == "fate∕grand_order"
    assert vocab.unescape_name("fate∕grand_order") == "fate/grand_order"
    assert vocab.escape_name("|_|") == "|_|"


def test_is_managed():
    for tag in ("tag/x", "parody/x", "character/x", "meta/x", "rating/general",
                "character|x", "tag|/_", "tag"):
        assert vocab.is_managed(tag), tag
    for tag in ("my_album/trip", "untagged_foreign", "vacation", "tagged/x"):
        assert not vocab.is_managed(tag), tag


def test_split_tag():
    assert vocab.split_tag("parody/fate∕grand_order") == ("parody", "fate∕grand_order")
    assert vocab.split_tag("parody/fate/grand_order") == ("parody", "fate∕grand_order")
    assert vocab.split_tag("my_album/trip") == (None, "my_album∕trip")
    assert vocab.split_tag("long_hair") == (None, "long_hair")
    assert vocab.split_tag("rating/*") == ("rating", "*")


def test_normalize_folds_only_inside_name():
    assert vocab.normalize("Parody/Fate/Grand Order") == "parody/fate∕grand_order"
    assert vocab.normalize("  Blue-Hair ") == "blue_hair"
    assert vocab.normalize("fate/grand_order") == "fate∕grand_order"
    assert vocab.normalize("rating/explicit") == "rating/explicit"
    assert vocab.normalize("tag/long hair") == "tag/long_hair"


def test_leaf_of():
    assert vocab.leaf_of("tag/long_hair") == "long_hair"
    assert vocab.leaf_of("parody/fate∕grand_order") == "fate∕grand_order"
    assert vocab.leaf_of("my_album∕trip") == "my_album∕trip"


def test_managed_tag_escapes_name():
    assert vocab.managed_tag("parody", "fate/grand_order") == "parody/fate∕grand_order"


def test_meta_whitelist_is_exactly_research_r6():
    expected = {
        "ai-generated", "ai-assisted",
        "official_art", "official_wallpaper", "promotional_art", "key_visual",
        "novel_illustration", "game_asset", "visual_novel_cg", "production_art",
        "concept_art", "artbook",
        "anime_screenshot", "game_screenshot", "screenshot",
        "photo_(medium)", "cosplay_photo",
        "traditional_media", "watercolor_(medium)", "oil_painting_(medium)",
        "acrylic_paint_(medium)", "graphite_(medium)", "colored_pencil_(medium)",
        "marker_(medium)", "pen_(medium)", "ink_(medium)", "pastel_(medium)",
        "painting_(medium)",
    }
    assert set(vocab.META_WHITELIST) == expected
    assert len(vocab.META_WHITELIST) == 28


def test_rating_map_and_prefixes():
    assert vocab.RATING_MAP == {"g": "general", "s": "sensitive", "q": "questionable", "e": "explicit"}
    assert "style" not in vocab.CATEGORY_PREFIX
    assert vocab.CATEGORY_PREFIX["copyright"] == "parody"
    assert set(vocab.MANAGED_PREFIXES) == {"character", "parody", "tag", "meta", "rating"}
