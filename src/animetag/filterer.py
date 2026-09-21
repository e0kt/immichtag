"""Core selection logic for the tag filter: read sidecars, match tags, copy/compress.

No CLI concerns here — argument parsing and the run loop live in animetag.filter_cli.
"""

from __future__ import annotations

import fnmatch
import io
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence, Tuple

from PIL import Image

from animetag import library, vocab
from animetag import sidecar as sidecar_io
from animetag.library import IMAGE_EXTS, SKIP_DIRS
from animetag.vocab import is_glob, leaf_of, normalize, split_tag

#: A filter is a group of tag patterns, so this caps the groups, not the tags in them.
MAX_FILTERS = 10

#: Compression floor: quality stops dropping here and the image is downscaled instead.
MIN_JPEG_QUALITY = 50
JPEG_QUALITIES = (92, 85, 80, 75, 70, 65, 60, 55, MIN_JPEG_QUALITY)
#: Never shrink the longest side below this, even if the size limit is missed.
MIN_LONG_SIDE = 640
DOWNSCALE_STEP = 0.75
MAX_DOWNSCALES = 8


def parse_size(text: str) -> int:
    """Accept 1MB, 900k, 1.5 MiB, or a plain byte count."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kmg]i?b?|b)?\s*", str(text), re.I)
    if not match:
        raise ValueError(f"Not a size: {text!r} (try 1MB, 900k, or a byte count)")
    value = float(match.group(1))
    unit = (match.group(2) or "b").lower().rstrip("b").rstrip("i")
    return int(value * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit])


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024 or unit == "GB":
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}GB"


@dataclass(frozen=True)
class TagFilter:
    """One `--filter` group: every `required` tag must be present, no `excluded` one may be.

    A pattern starting with a managed prefix (`character/hatsune_miku`, `rating/*`) is
    matched against the full tag; anything else against the name after the prefix, so
    `blue_hair` finds `tag/blue_hair` and `fate/grand_order` finds `parody/fate∕grand_order`.
    """

    source: str
    required: Tuple[str, ...]
    excluded: Tuple[str, ...]

    @classmethod
    def parse(cls, spec: str) -> "TagFilter":
        required: List[str] = []
        excluded: List[str] = []
        for raw in re.split(r"[,\n]", spec):
            item = raw.strip()
            if not item:
                continue
            if item.startswith(("-", "!")):
                item = item[1:].strip()
                if item:
                    excluded.append(normalize(item))
            else:
                required.append(normalize(item))
        if not required and not excluded:
            raise ValueError(f"Empty filter: {spec!r}")
        return cls(source=spec.strip(), required=tuple(required), excluded=tuple(excluded))

    def matches(self, full_tags: Sequence[str], leaf_tags: Sequence[str]) -> bool:
        for pattern in self.excluded:
            if _has_tag(pattern, full_tags, leaf_tags):
                return False
        for pattern in self.required:
            if not _has_tag(pattern, full_tags, leaf_tags):
                return False
        return True


def pattern_matches(pattern: str, full_tag: str, leaf_tag: str) -> bool:
    """Does one normalized pattern match one normalized tag?

    The single definition of the matching rule — the CLI walks tags with it and the
    web index expands it over the vocabulary, so the two can never drift apart.
    """
    target = full_tag if split_tag(pattern)[0] else leaf_tag
    if is_glob(pattern):
        return fnmatch.fnmatchcase(target, pattern)
    return pattern == target


def _has_tag(pattern: str, full_tags: Sequence[str], leaf_tags: Sequence[str]) -> bool:
    if not is_glob(pattern):
        # The overwhelmingly common case: a plain tag needs no per-tag walk.
        return pattern in (full_tags if split_tag(pattern)[0] else leaf_tags)
    return any(pattern_matches(pattern, full, leaf) for full, leaf in zip(full_tags, leaf_tags))


def parse_filters(specs: Iterable[str]) -> List[TagFilter]:
    filters = [TagFilter.parse(spec) for spec in specs]
    if len(filters) > MAX_FILTERS:
        raise ValueError(f"At most {MAX_FILTERS} filters, got {len(filters)}")
    return filters


def parse_exclusions(specs: Iterable[str]) -> TagFilter | None:
    """The global veto: tags no exported image may carry, whichever filter matched it.

    A filter's own `-tag` only rules that tag out of *that* group, so under `any`
    matching an image can still arrive through another group. This applies to the
    combined result instead. Returns None when nothing was asked for.
    """
    patterns: List[str] = []
    for spec in specs:
        for raw in re.split(r"[,\n]", spec):
            item = raw.strip().lstrip("-!").strip()  # a leading -/! here is redundant
            if item:
                patterns.append(normalize(item))
    if not patterns:
        return None
    return TagFilter(source=", ".join(patterns), required=(), excluded=tuple(patterns))


def parse_sidecar(sidecar_path: Path) -> List[str]:
    """The deduped tags in a sidecar, in file order (see sidecar.read_sidecar)."""
    return sidecar_io.read_tags(sidecar_path)


def sidecar_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if filename.lower().endswith(".xmp"):
                yield Path(dirpath) / filename


def image_for_sidecar(sidecar_path: Path) -> Path | None:
    """`image.jpg.xmp` -> `image.jpg`, if that image is actually there."""
    image_path = sidecar_path.with_suffix("")
    if image_path.suffix.lower() in IMAGE_EXTS and image_path.is_file():
        return image_path
    return None


@dataclass
class Match:
    image: Path
    sidecar: Path
    tags: List[str]


@dataclass
class ScanStats:
    """What a scan saw besides its matches: how many sidecars still need re-tagging,
    and how many carry each `watch` pattern (for the legacy-tag warnings)."""

    watch: Tuple[str, ...] = ()
    scanned: int = 0
    legacy: int = 0
    outdated: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    #: False when the scan stopped early at --max-images, so the numbers are lower bounds.
    complete: bool = True

    def observe(self, data: sidecar_io.SidecarData) -> None:
        self.scanned += 1
        status = library.status_of(data, vocab.MODEL_REPO, vocab.MODEL_REVISION)
        self.legacy += status is library.Status.LEGACY
        self.outdated += status is library.Status.OUTDATED
        if self.watch:
            full, leaf = normalized_tags(data.tags)
            for pattern in self.watch:
                if _has_tag(pattern, full, leaf):
                    self.counts[pattern] = self.counts.get(pattern, 0) + 1


def normalized_tags(tags: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(full, leaf) normalized forms, the two things a pattern can be matched against."""
    full = [normalize(t) for t in tags]
    return full, [leaf_of(t) for t in full]


def find_matches(
    root: Path,
    filters: Sequence[TagFilter],
    match_all: bool = False,
    stop_after: int | None = None,
    exclude: TagFilter | None = None,
    stats: ScanStats | None = None,
) -> Iterator[Match]:
    """Yield tagged images passing the filters, minus anything `exclude` vetoes.

    With no filters every tagged image matches, which is what makes `--list-tags`
    and a bare `--max-images` sample useful on their own.
    """
    found = 0
    for sidecar in sidecar_files(root):
        image = image_for_sidecar(sidecar)
        if image is None:
            continue
        data = sidecar_io.read_sidecar(sidecar)
        tags = data.tags
        if stats is not None:
            stats.observe(data)

        if filters or exclude:
            full, leaf = normalized_tags(tags)
            if exclude and not exclude.matches(full, leaf):
                continue
            if filters:
                results = (f.matches(full, leaf) for f in filters)
                if not (all(results) if match_all else any(results)):
                    continue

        yield Match(image=image, sidecar=sidecar, tags=tags)
        found += 1
        if stop_after is not None and found >= stop_after:
            if stats is not None:
                stats.complete = False
            return


def unique_stem(stem: str, used: set) -> str:
    """Flattening a tree into one folder can collide; keep the name and number it."""
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem) or "image"
    candidate = safe
    counter = 1
    while candidate.lower() in used:
        candidate = f"{safe}-{counter}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _encode_jpeg(img: Image.Image, quality: int, optimize: bool = False) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=optimize, progressive=True)
    return buffer.getvalue()


def _best_quality(img: Image.Image, limit_bytes: int) -> Tuple[bytes | None, bytes]:
    """Highest ladder quality whose encoding fits, found by bisection.

    Walking the ladder top-down costs one encode per rung — the dominant cost of an
    export. Bisecting costs ~3 for the same answer, since "fits" is monotonic in
    quality. Probes skip Huffman optimization and the winner is re-encoded with it,
    which only ever shrinks the file, so a probe that fit still fits.

    Returns (the encoding that fits or None, the smallest encoding seen).
    """
    smallest: bytes | None = None
    winner: int | None = None

    low, high = 0, len(JPEG_QUALITIES) - 1
    while low <= high:
        middle = (low + high) // 2
        data = _encode_jpeg(img, JPEG_QUALITIES[middle])
        if smallest is None or len(data) < len(smallest):
            smallest = data
        if len(data) <= limit_bytes:
            winner = middle
            high = middle - 1  # a fit leaves room to try better quality
        else:
            low = middle + 1

    if winner is None:
        return None, smallest or b""
    final = _encode_jpeg(img, JPEG_QUALITIES[winner], optimize=True)
    if len(final) > limit_bytes:  # optimization should shrink it, but never assume
        final = _encode_jpeg(img, JPEG_QUALITIES[winner])
    return final, min(final, smallest or final, key=len)


def compress_to_limit(image_path: Path, limit_bytes: int) -> bytes:
    """Re-encode as JPEG under limit_bytes: drop quality first, then downscale.

    Transparency is flattened onto white — the same background the tagger composites
    on — because JPEG has no alpha channel.
    """
    img = Image.open(image_path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img).convert("RGB")
    else:
        img = img.convert("RGB")

    best: bytes | None = None
    for _ in range(MAX_DOWNSCALES + 1):
        fitting, smallest = _best_quality(img, limit_bytes)
        if best is None or len(smallest) < len(best):
            best = smallest
        if fitting is not None:
            return fitting

        width, height = img.size
        if max(width, height) * DOWNSCALE_STEP < MIN_LONG_SIDE:
            break
        img = img.resize(
            (max(1, int(width * DOWNSCALE_STEP)), max(1, int(height * DOWNSCALE_STEP))),
            Image.Resampling.LANCZOS,
        )

    # Missed the target: the smallest encoding still beats copying the original.
    return best or b""


def export(
    match: Match,
    out_dir: Path,
    used_stems: set,
    limit_bytes: int,
    compress: bool = True,
    copy_sidecar: bool = False,
) -> Tuple[Path, bool]:
    """Copy one image into out_dir, compressing it if it exceeds the size limit.

    Returns the written path and whether it had to be re-encoded.
    """
    stem = unique_stem(match.image.stem, used_stems)
    size = match.image.stat().st_size

    if not compress or size <= limit_bytes:
        dest = out_dir / (stem + match.image.suffix)
        shutil.copy2(match.image, dest)
        compressed = False
    else:
        try:
            data = compress_to_limit(match.image, limit_bytes)
        except Exception:
            data = b""
        if data and len(data) < size:
            dest = out_dir / (stem + ".jpg")
            dest.write_bytes(data)
            compressed = True
        else:
            # Unreadable by Pillow, or already smaller than anything we can produce.
            dest = out_dir / (stem + match.image.suffix)
            shutil.copy2(match.image, dest)
            compressed = False

    if copy_sidecar:
        shutil.copy2(match.sidecar, dest.with_suffix(dest.suffix + ".xmp"))

    return dest, compressed
