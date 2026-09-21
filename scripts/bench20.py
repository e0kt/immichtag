"""Reproduce the 20-image speed and quality check that justified PixAI v1.0.

Read-only: runs the model on sample images and reports; never writes a sidecar.

    uv run python scripts/bench20.py                 # 20 random library images, seed 20
    uv run python scripts/bench20.py --paths list.txt --device cpu
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
from pathlib import Path

from animetag import config, library, tagger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=None, help="Library to sample from")
    parser.add_argument("--paths", type=Path, help="File with one image path per line instead of sampling")
    parser.add_argument("-n", type=int, default=20, help="Sample size (default 20)")
    parser.add_argument("--seed", type=int, default=20)
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto")
    args = parser.parse_args()

    if args.paths:
        images = [Path(line.strip()) for line in args.paths.read_text().splitlines() if line.strip()]
    else:
        root = config.resolve_root(args.dir)
        everything = sorted(library.image_files(root))
        images = random.Random(args.seed).sample(everything, min(args.n, len(everything)))

    started = time.perf_counter()
    model = tagger.load_model(args.device)
    load_s = time.perf_counter() - started
    model.infer(model.preprocess(images[0]))  # warm-up, not timed

    per_image, with_character, general_counts = [], 0, []
    wall = time.perf_counter()
    for image in images:
        t0 = time.perf_counter()
        probs = model.infer(model.preprocess(image))
        per_image.append(time.perf_counter() - t0)
        tags = tagger.tags_from_probs(probs, model.names, model.categories)
        with_character += any(t.startswith("character/") for t in tags)
        general_counts.append(sum(t.startswith("tag/") for t in tags))
    wall_s = time.perf_counter() - wall

    print(f"model load {load_s:.1f} s on {model.device}")
    print(f"{len(images)} images: {statistics.mean(per_image) * 1000:.0f} ms/img mean, "
          f"{len(images) / wall_s:.2f} img/s")
    print(f"images with a character: {with_character}/{len(images)}")
    print(f"average tag/ count: {statistics.mean(general_counts):.1f} "
          f"(min {min(general_counts)}, max {max(general_counts)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
