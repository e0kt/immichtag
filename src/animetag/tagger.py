"""Core tagging logic: model, inference, tag filtering, XMP sidecar output.

No CLI concerns here — argument parsing and the run loop live in animetag.cli.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import numpy as np
import onnxruntime as ort
import pandas as pd
from huggingface_hub import hf_hub_download
from lxml import etree
from PIL import Image

MODEL_REPO = "SmilingWolf/wd-v1-4-convnextv2-tagger-v2"
MODEL_FILENAME = "model.onnx"
CSV_FILENAME = "selected_tags.csv"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"}
SKIP_DIRS = {"thumbs", "encoded-video", "profile", "backups", ".git", "__pycache__"}

# Keep the previous behavior: remove common / low-value / noisy tags from general tags.
# Use --keep-common-tags if you want more raw Danbooru detail such as 1girl, solo, etc.
JUNK_TAGS = {
    "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
    "solo", "looking_at_viewer", "simple_background", "white_background",
    "twitter_username", "letterboxed", "virtual_youtuber",
    "closed_mouth", "upper_body", "multiple_girls", "multiple_boys",
    "english_text", "text", "commentary_request", "cropped",
}

# WD14 / Danbooru selected_tags.csv category ids.
# 0 = general, 3 = copyright/source, 4 = character, 9 = rating
GENERAL_CATEGORY = 0
PARODY_CATEGORY = 3
CHARACTER_CATEGORY = 4
RATING_CATEGORY = 9

# Confidence cut-offs. The CLI's flags default to these and the web UI tags with them,
# so a run started from either place writes the same sidecars.
DEFAULT_GENERAL_THRESHOLD = 0.40
DEFAULT_CHARACTER_THRESHOLD = 0.75
DEFAULT_PARODY_THRESHOLD = 0.60
DEFAULT_RATING_THRESHOLD = 0.50


def load_model() -> Tuple[ort.InferenceSession, pd.DataFrame]:
    model_path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILENAME)
    csv_path = hf_hub_download(repo_id=MODEL_REPO, filename=CSV_FILENAME)
    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    tags_df = pd.read_csv(csv_path)
    return session, tags_df


def preprocess_image(image_path: Path, target_size: int) -> np.ndarray:
    """Resize while preserving aspect ratio, pad to square, and convert RGB -> BGR for WD14."""
    img = Image.open(image_path).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    img = Image.alpha_composite(bg, img).convert("RGB")

    width, height = img.size
    scale = target_size / max(width, height)
    new_width = max(1, int(width * scale))
    new_height = max(1, int(height * scale))
    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    padded = Image.new("RGB", (target_size, target_size), (255, 255, 255))
    left = (target_size - new_width) // 2
    top = (target_size - new_height) // 2
    padded.paste(img, (left, top))

    arr = np.asarray(padded).astype(np.float32)
    arr = arr[:, :, ::-1]  # RGB -> BGR
    return np.expand_dims(arr, 0)


def infer_tags(
    session: ort.InferenceSession,
    tags_df: pd.DataFrame,
    image_path: Path,
    general_threshold: float,
    character_threshold: float,
    parody_threshold: float,
    rating_threshold: float,
) -> Tuple[List[str], List[str], List[str], List[str]]:
    input_name = session.get_inputs()[0].name
    input_shape = session.get_inputs()[0].shape
    target_size = int(input_shape[1])

    image = preprocess_image(image_path, target_size)
    probs = session.run(None, {input_name: image})[0][0]

    names = tags_df["name"].tolist()
    categories = tags_df["category"].tolist()

    rating_tags: List[str] = []
    parody_tags: List[str] = []
    character_tags: List[str] = []
    general_tags: List[str] = []

    for name, category, prob in zip(names, categories, probs):
        if category == RATING_CATEGORY and prob >= rating_threshold:
            rating_tags.append(name)
        elif category == PARODY_CATEGORY and prob >= parody_threshold:
            parody_tags.append(name)
        elif category == CHARACTER_CATEGORY and prob >= character_threshold:
            character_tags.append(name)
        elif category == GENERAL_CATEGORY and prob >= general_threshold:
            general_tags.append(name)

    return rating_tags, parody_tags, character_tags, general_tags


def dedupe_keep_order(tags: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def filter_general_tags(tags: Sequence[str], remove_common_tags: bool = True) -> List[str]:
    filtered: List[str] = []
    for tag in tags:
        if tag.startswith("score_"):
            continue
        if remove_common_tags and tag in JUNK_TAGS:
            continue
        filtered.append(tag)
    return dedupe_keep_order(filtered)


def build_xmp(final_tags: Sequence[str]) -> bytes:
    nsmap = {
        "x": "adobe:ns:meta/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "lr": "http://ns.adobe.com/lightroom/1.0/",
        "digiKam": "http://www.digikam.org/ns/1.0/",
        "dc": "http://purl.org/dc/elements/1.1/",
    }

    xmpmeta = etree.Element("{adobe:ns:meta/}xmpmeta", nsmap=nsmap)
    rdf = etree.SubElement(xmpmeta, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF")
    desc = etree.SubElement(rdf, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")

    bags = []

    # Lightroom / Immich-style hierarchical tags.
    hierarchical = etree.SubElement(desc, "{http://ns.adobe.com/lightroom/1.0/}HierarchicalSubject")
    bags.append(etree.SubElement(hierarchical, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Bag"))

    # digiKam tag list, also useful for Immich sidecar import.
    digikam = etree.SubElement(desc, "{http://www.digikam.org/ns/1.0/}TagsList")
    bags.append(etree.SubElement(digikam, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Bag"))

    # Standard dc:subject for extra compatibility with XMP readers.
    subject = etree.SubElement(desc, "{http://purl.org/dc/elements/1.1/}subject")
    bags.append(etree.SubElement(subject, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Bag"))

    for tag in dedupe_keep_order(final_tags):
        for bag in bags:
            li = etree.SubElement(bag, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li")
            li.text = tag

    return etree.tostring(
        xmpmeta,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    )


def image_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in IMAGE_EXTS:
                yield path


def make_sidecar_path(image_path: Path) -> Path:
    # Immich-style sidecar: image.jpg -> image.jpg.xmp
    return image_path.with_suffix(image_path.suffix + ".xmp")


def is_untagged(image_path: Path) -> bool:
    """A "new" image is one with no sidecar next to it yet."""
    return not make_sidecar_path(image_path).exists()


def process_one(
    image_path: Path,
    session: ort.InferenceSession,
    tags_df: pd.DataFrame,
    general_threshold: float,
    character_threshold: float,
    parody_threshold: float,
    rating_threshold: float,
    overwrite: bool,
    keep_common_tags: bool,
    include_rating: bool,
) -> str:
    xmp_path = make_sidecar_path(image_path)
    if xmp_path.exists() and not overwrite:
        return "skipped"

    rating_tags, parody_tags, character_tags, general_tags = infer_tags(
        session=session,
        tags_df=tags_df,
        image_path=image_path,
        general_threshold=general_threshold,
        character_threshold=character_threshold,
        parody_threshold=parody_threshold,
        rating_threshold=rating_threshold,
    )

    general_tags = filter_general_tags(
        general_tags,
        remove_common_tags=not keep_common_tags,
    )

    final_tags: List[str] = []

    # User-requested additions: character names and parody/source names.
    for tag in dedupe_keep_order(character_tags):
        final_tags.append(f"character/{tag}")

    for tag in dedupe_keep_order(parody_tags):
        final_tags.append(f"parody/{tag}")

    for tag in general_tags:
        final_tags.append(f"tag/{tag}")

    if include_rating:
        for tag in dedupe_keep_order(rating_tags):
            final_tags.append(f"rating/{tag}")

    if not final_tags:
        final_tags.append("tag/untagged")

    xmp_path.write_bytes(build_xmp(final_tags))
    return "written"
