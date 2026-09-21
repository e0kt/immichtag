"""The tag scheme: which model categories become which prefixes, and how tags are spelled.

This is the one place the naming rules live. The tagger uses them to write tags, the
sidecar merge to tell our tags from the user's, the filter and the web to match
patterns, and the Immich cleanup to decide what it may delete.
"""

from __future__ import annotations

import difflib
import functools
import json
from typing import Dict, Iterable, List, Tuple

#: The model every sidecar is tagged with, pinned to one revision. A sidecar recording
#: anything else is re-tagged. Kept here, not in tagger.py, so that reading sidecars
#: never has to import torch.
MODEL_REPO = "pixai-labs/pixai-tagger-v1.0"
MODEL_REVISION = "f33cfdb53c0c90b049bab9ce066eea1118970ef8"

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


# -- vocabulary: telling legacy and misspelled tags apart ---------------------------

#: Names that are valid under a prefix without being model outputs.
_RATING_NAMES = frozenset(RATING_MAP.values())
_META_NAMES = frozenset(normalize(name) for name in META_WHITELIST)


@functools.lru_cache(maxsize=1)
def load_model_vocabulary() -> Dict[str, Tuple[str, str | None]] | None:
    """normalized name -> (model category, prefix it is written under, or None).

    Read from the pinned model's config.json (700 KB — the weights are not needed).
    None when it cannot be had, e.g. offline with an empty cache.
    """
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(MODEL_REPO, "config.json", revision=MODEL_REVISION)
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception:
        return None
    categories = [category for category, count in config["tags_split"] for _ in range(count)]
    return {
        normalize(name): (category, CATEGORY_PREFIX.get(category))
        for name, category in zip(config["tags"], categories)
    }


def _known(prefix: str | None, name: str, model: Dict[str, Tuple[str, str | None]]) -> bool:
    """Could this tag ever appear in a sidecar written by the current model?"""
    if prefix == "rating":
        return name in _RATING_NAMES
    if prefix == "meta":
        return name in _META_NAMES
    if name == "untagged" and prefix in (None, "tag"):
        return True
    entry = model.get(name)
    if entry is None or entry[1] is None:
        return False
    return prefix is None or entry[1] == prefix


def _writable_names(model: Dict[str, Tuple[str, str | None]]) -> List[str]:
    return [name for name, (category, prefix) in model.items()
            if prefix is not None and (category != "meta" or name in _META_NAMES)]


def suggest(name: str, candidates: Iterable[str], n: int = 5) -> List[str]:
    """Likely replacements for `name`, best first.

    Tags that contain `name` as a whole word come first — `bangs` → `blunt_bangs`,
    `swept_bangs` — in `candidates` order, which for the model vocabulary is most common
    first. With none of those, close spellings: `blue_hiar` → `blue_hair`.
    """
    pool = list(dict.fromkeys(candidates))
    words = [c for c in pool if c != name and name in c.split("_")] if len(name) >= 3 else []
    typos = difflib.get_close_matches(name, pool, n=n, cutoff=0.8)
    close = difflib.get_close_matches(name, pool, n=n, cutoff=0.6)
    ordered = words + typos + close if words else typos + close
    return list(dict.fromkeys(ordered))[:n]


def classify_pattern(pattern: str, library_count: int,
                     model: Dict[str, Tuple[str, str | None]] | None,
                     library_names: Iterable[str] = ()) -> dict | None:
    """A warning for one normalized, non-glob pattern, or None when it is fine.

    - legacy:  images carry it, but the current model never writes it (e.g. `bangs`)
    - unknown: nothing carries it and the model never writes it (a typo, most likely)
    - empty:   the model can write it, no image has it yet
    """
    if is_glob(pattern):
        return None
    prefix, name = split_tag(pattern)
    if model is None:
        # No vocabulary to judge by: only a pattern matching nothing is worth a word.
        if library_count:
            return None
        return {"pattern": pattern, "kind": "unknown", "count": 0,
                "similar": suggest(name, library_names)}

    if _known(prefix, name, model):
        if library_count:
            return None
        return {"pattern": pattern, "kind": "empty", "count": 0, "similar": []}

    if prefix is None:
        similar = suggest(name, _writable_names(model))
    else:
        # Right name under the wrong prefix is the likeliest mistake; offer the right one.
        entry = model.get(name)
        exact = [f"{entry[1]}/{name}"] if entry and entry[1] else []
        same_prefix = [n for n, (category, p) in model.items() if p == prefix]
        similar = (exact + [f"{prefix}/{s}" for s in suggest(name, same_prefix)])[:5]
    kind = "legacy" if library_count else "unknown"
    return {"pattern": pattern, "kind": kind, "count": library_count, "similar": similar}
