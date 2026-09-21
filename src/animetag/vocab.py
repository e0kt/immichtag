"""The tag scheme: which model categories become which prefixes, and how tags are spelled.

This is the one place the naming rules live. The tagger uses them to write tags, the
sidecar merge to tell our tags from the user's, the filter and the web to match
patterns, and the Immich cleanup to decide what it may delete.
"""

from __future__ import annotations

from typing import Tuple

#: Top-level segments this tool owns. Tags under them are rewritten on every re-tag and
#: may be deleted from Immich once empty; anything else belongs to the user.
MANAGED_PREFIXES = ("character", "parody", "tag", "meta", "rating")

#: PixAI category -> sidecar prefix. "style" (really artist names) is not written.
CATEGORY_PREFIX = {
    "general": "tag",
    "character": "character",
    "copyright": "parody",
    "meta": "meta",
    "rating": "rating",
}

#: The model authors' per-category micro-F1 operating points (research R4).
DEFAULT_THRESHOLDS = {
    "general": 0.34,
    "character": 0.37,
    "copyright": 0.46,
    "meta": 0.47,
}

#: Common, low-value general tags dropped unless --keep-common-tags.
JUNK_TAGS = frozenset({
    "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
    "solo", "looking_at_viewer", "simple_background", "white_background",
    "twitter_username", "letterboxed", "virtual_youtuber",
    "closed_mouth", "upper_body", "multiple_girls", "multiple_boys",
    "english_text", "text", "commentary_request", "cropped",
})

#: The only meta-category tags worth browsing by (research R6). Resolution tags and
#: Danbooru bookkeeping (highres, md5_mismatch, check_*) are dropped.
META_WHITELIST = frozenset({
    # AI
    "ai-generated", "ai-assisted",
    # official art
    "official_art", "official_wallpaper", "promotional_art", "key_visual",
    "novel_illustration", "game_asset", "visual_novel_cg", "production_art",
    "concept_art", "artbook",
    # screenshots
    "anime_screenshot", "game_screenshot", "screenshot",
    # photos
    "photo_(medium)", "cosplay_photo",
    # traditional media
    "traditional_media", "watercolor_(medium)", "oil_painting_(medium)",
    "acrylic_paint_(medium)", "graphite_(medium)", "colored_pencil_(medium)",
    "marker_(medium)", "pen_(medium)", "ink_(medium)", "pastel_(medium)",
    "painting_(medium)",
})

#: PixAI's rating tags are `rating:g` etc.; the sidecar keeps the long names users filter on.
RATING_MAP = {"g": "general", "s": "sensitive", "q": "questionable", "e": "explicit"}

#: Stands in for "/" inside a tag's name. Immich splits tag values on "/" into levels,
#: so `parody/fate/grand_order` would become three; U+2215 renders like a slash and
#: survives exiftool unchanged (research R7).
ESCAPE = "∕"

GLOB_CHARS = "*?["


def escape_name(name: str) -> str:
    return name.replace("/", ESCAPE)


def unescape_name(name: str) -> str:
    return name.replace(ESCAPE, "/")


def managed_tag(prefix: str, name: str) -> str:
    return f"{prefix}/{escape_name(name)}"


def is_managed(tag: str) -> bool:
    """True for our prefixes, the bare prefix itself, and the legacy `prefix|name` form."""
    if tag in MANAGED_PREFIXES:
        return True
    head, slash, _ = tag.partition("/")
    if slash and head in MANAGED_PREFIXES:
        return True
    head, pipe, _ = tag.partition("|")
    return bool(pipe) and head in MANAGED_PREFIXES


def split_tag(tag: str) -> Tuple[str | None, str]:
    """(prefix, name) when the first segment is a managed prefix, else (None, whole name).

    Only the first "/" can be a hierarchy separator; every later one is part of the
    name, and comes back escaped so `fate/grand_order` and `fate∕grand_order` agree.
    """
    head, slash, rest = tag.partition("/")
    if slash and head in MANAGED_PREFIXES:
        return head, escape_name(rest)
    return None, escape_name(tag)


def normalize(tag: str) -> str:
    """Fold the spelling differences that are never meaningful: case, spaces, dashes,
    and a literal "/" inside a name."""
    folded = tag.strip().lower().replace(" ", "_").replace("-", "_")
    prefix, name = split_tag(folded)
    return f"{prefix}/{name}" if prefix else name


def leaf_of(normalized: str) -> str:
    """The name a bare pattern is matched against: everything after a managed prefix."""
    return split_tag(normalized)[1]


def is_glob(pattern: str) -> bool:
    return any(char in pattern for char in GLOB_CHARS)
