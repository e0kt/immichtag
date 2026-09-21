"""Check that Immich shows exactly the tags the sidecars hold (SC-003).

Samples random images that have a sidecar, looks each up in Immich, and compares the
asset's tags with the sidecar's TagsList. Read-only on both sides. Exits 1 on any
mismatch.

    uv run python scripts/verify_immich.py --sample 100
"""

from __future__ import annotations

import argparse
import random
import sys

from immichtag import config, immich, library, sidecar, vocab


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=None, help="Library")
    parser.add_argument("--sample", type=int, default=100)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--current-only", action="store_true",
                        help="Only images whose sidecar the current model wrote (i.e. already re-tagged)")
    args = parser.parse_args()

    root = config.resolve_root(args.dir)
    settings = config.load_immich_settings()
    if not settings.configured:
        print("No Immich API key configured.", file=sys.stderr)
        return 2
    client = immich.ImmichClient(settings)

    assets = list(client.iter_assets())
    path_map = (immich.PathMap.parse(settings.path_map) if settings.path_map
                else immich.PathMap.autodetect((p for _, p in assets[:500]), root))
    if path_map is None:
        print("Cannot match Immich paths to this library; set [immich].path_map.", file=sys.stderr)
        return 2
    by_local = {path_map.to_local(original): asset_id for asset_id, original in assets}

    tagged = [image for image in library.image_files(root) if sidecar.sidecar_path(image).exists()]
    if args.current_only:
        tagged = [image for image in tagged
                  if library.classify(image, vocab.MODEL_REPO, vocab.MODEL_REVISION) is library.Status.CURRENT]
        print(f"{len(tagged)} images carry current sidecars")
    sample = random.Random(args.seed).sample(tagged, min(args.sample, len(tagged)))

    mismatches = missing = 0
    for image in sample:
        asset_id = by_local.get(image)
        if asset_id is None:
            missing += 1
            print(f"not in Immich: {image}")
            continue
        want = set(sidecar.read_tags(sidecar.sidecar_path(image)))
        have = set(client.asset_tags(asset_id))
        if want != have:
            mismatches += 1
            print(f"MISMATCH {image.name}")
            for tag in sorted(want - have):
                print(f"  only in sidecar: {tag}")
            for tag in sorted(have - want):
                print(f"  only in Immich:  {tag}")

    checked = len(sample) - missing
    print(f"{checked - mismatches}/{checked} match"
          + (f", {missing} not Immich assets" if missing else ""))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
