"""The model: PixAI Tagger v1.0 on the Apple GPU, and probabilities -> sidecar tags.

No I/O beyond reading the image; writing sidecars is run.py's job.

The model code is not vendored — the upstream repo declares no license — so it is
loaded from the Hub with `trust_remote_code`, pinned to one revision so the code that
runs can never change underneath us. The revision is also written into every sidecar,
which is how a later run tells current tags from ones to redo.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Sequence, Tuple

import numpy as np
import torch
from PIL import Image
from torchvision.transforms import functional as TF

from immichtag import vocab
from immichtag.vocab import MODEL_REPO, MODEL_REVISION  # noqa: F401  (re-exported)

INPUT_SIZE = 1008

# The library is the user's own images, not untrusted uploads, so Pillow's
# decompression-bomb guard only ever stops a huge scan or panorama from being tagged
# (the first full run lost a 223-megapixel image to it).
Image.MAX_IMAGE_PIXELS = None

#: Measured on an M5 end to end (decode + inference), for time estimates only.
EXPECTED_RATE = {"mps": 1.8, "cpu": 0.2}


def select_device(preference: str = "auto") -> str:
    if preference == "cpu":
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    if preference == "mps":
        raise RuntimeError("--device mps requested but the Apple GPU is not available")
    return "cpu"


class Tagger:
    """A loaded model plus the vocabulary its outputs index into."""

    def __init__(self, model: torch.nn.Module, device: str) -> None:
        self.model = model
        self.device = device
        self.dtype = torch.float16 if device == "mps" else torch.float32
        self.names: List[str] = list(model.config.tags)
        self.categories: List[str] = [
            category for category, count in model.config.tags_split for _ in range(count)
        ]
        if len(self.names) != len(self.categories):
            raise RuntimeError("model vocabulary and category split disagree")

    def preprocess(self, path: Path) -> torch.Tensor:
        return preprocess(path)

    @torch.inference_mode()
    def infer(self, pixels: torch.Tensor) -> np.ndarray:
        """Probabilities for one preprocessed image, as float32 in vocabulary order."""
        logits = self.model(pixels.to(device=self.device, dtype=self.dtype))
        return logits.float().sigmoid()[0].cpu().numpy()


def load_model(device: str = "auto") -> Tagger:
    from transformers import AutoModel
    from transformers.utils import logging as hf_logging

    hf_logging.disable_progress_bar()  # its per-weight bar would interleave with ours
    device = select_device(device)
    model = AutoModel.from_pretrained(MODEL_REPO, revision=MODEL_REVISION, trust_remote_code=True)
    # .to(device) first, then .half(). Never .to(dtype=...): that also casts the complex
    # RoPE buffers (freqs_cis) to a real dtype, silently dropping their imaginary part —
    # the model still runs and returns plausible-looking nonsense.
    model = model.eval().to(device)
    if device == "mps":
        model = model.half()
    rope = [buf for name, buf in model.named_buffers() if name.endswith("freqs_cis")]
    if not rope or not all(buf.is_complex() for buf in rope):
        raise RuntimeError("PixAI RoPE buffers lost their complex dtype while loading")
    return Tagger(model, device)


def preprocess(path: Path) -> torch.Tensor:
    """The model's own processor, reimplemented: keep the aspect ratio, bilinear resize so
    the long side is 1008, center-pad with 0, normalize to [-1, 1]. Transparency is
    composited onto white first."""
    with Image.open(path) as img:
        if img.mode != "RGB":
            img = img.convert("RGBA")
            canvas = Image.new("RGBA", img.size, (255, 255, 255))
            canvas.alpha_composite(img)
            img = canvas.convert("RGB")
        else:
            img.load()
        tensor = TF.to_tensor(img)

    height, width = tensor.shape[-2:]
    if height != INPUT_SIZE or width != INPUT_SIZE:
        ratio = min(INPUT_SIZE / height, INPUT_SIZE / width)
        new_h, new_w = int(height * ratio), int(width * ratio)
        pad_h, pad_w = INPUT_SIZE - new_h, INPUT_SIZE - new_w
        tensor = TF.resize(tensor, [new_h, new_w])
        tensor = TF.pad(tensor, [pad_w // 2, pad_h // 2, pad_w - pad_w // 2, pad_h - pad_h // 2], 0)
    tensor = TF.normalize(tensor, [0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
    return tensor.unsqueeze(0)


def iter_preprocessed(
    preprocess_fn: Callable[[Path], object], paths: Iterable[Path]
) -> Iterator[Tuple[Path, object]]:
    """(path, tensor or the exception it raised), decoding the next image while the GPU
    works on the current one."""
    paths = list(paths)
    if not paths:
        return

    def safe(path: Path):
        try:
            return preprocess_fn(path)
        except Exception as exc:  # reported per image by the caller
            return exc

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(safe, paths[0])
        for index, path in enumerate(paths):
            result = pending.result()
            if index + 1 < len(paths):
                pending = pool.submit(safe, paths[index + 1])
            yield path, result


def tags_from_probs(
    probs: np.ndarray,
    names: Sequence[str],
    categories: Sequence[str],
    thresholds: Dict[str, float] | None = None,
    keep_common: bool = False,
    include_rating: bool = True,
) -> List[str]:
    """Managed sidecar tags for one image: character, parody, tag, meta, then one rating.

    Within a category the most confident tag comes first. Style (artist names) is never
    written; meta only when whitelisted; rating is the single most likely one.
    """
    if not (len(probs) == len(names) == len(categories)):
        raise ValueError("probabilities, names and categories differ in length")
    limits = {**vocab.DEFAULT_THRESHOLDS, **(thresholds or {})}

    buckets: Dict[str, List[Tuple[float, str]]] = {"character": [], "copyright": [], "general": [], "meta": []}
    best_rating: Tuple[float, str] | None = None
    for name, category, prob in zip(names, categories, probs):
        prob = float(prob)
        if category == "rating":
            if best_rating is None or prob > best_rating[0]:
                best_rating = (prob, name)
            continue
        if category not in buckets or prob < limits[category]:
            continue
        if category == "general" and not keep_common and name in vocab.JUNK_TAGS:
            continue
        if category == "meta" and name not in vocab.META_WHITELIST:
            continue
        buckets[category].append((prob, name))

    tags: List[str] = []
    for category in ("character", "copyright", "general", "meta"):
        prefix = vocab.CATEGORY_PREFIX[category]
        tags.extend(vocab.managed_tag(prefix, name) for _, name in sorted(buckets[category], key=lambda p: -p[0]))
    if not tags:
        tags.append("tag/untagged")
    if include_rating and best_rating is not None:
        short = best_rating[1].split(":", 1)[-1]
        tags.append(f"rating/{vocab.RATING_MAP.get(short, short)}")
    return tags


def profile_name(thresholds: Dict[str, float]) -> str:
    """"v1" for the default thresholds, else a readable record of what was used."""
    limits = {**vocab.DEFAULT_THRESHOLDS, **thresholds}
    if limits == vocab.DEFAULT_THRESHOLDS:
        return "v1"
    return "custom:g{general:.2f},c{character:.2f},p{copyright:.2f},m{meta:.2f}".format(**limits)
