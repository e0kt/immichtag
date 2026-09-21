"""Command line entry point.

`uv run wd14` with no arguments tags every NEW image in the default library —
"new" meaning no .xmp sidecar sits next to it yet. Everything else is an override.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from tqdm import tqdm

from wd14 import tagger

#: Scanned when --dir is not given. Set WD14_DIR to point somewhere else without
#: editing this file; --dir still wins over both.
DEFAULT_DIR = Path(
    os.environ.get("WD14_DIR", "/Users/paul/docker/immich/library/upload")
).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wd14",
        description=(
            "WD14 anime tagger -> Immich-compatible XMP sidecars with character and "
            "parody/source tags. With no arguments, tags every image under "
            f"{DEFAULT_DIR} that has no sidecar yet."
        ),
    )
    parser.add_argument(
        "--dir",
        default=None,
        help=f"Directory to scan (default: $WD14_DIR, else {DEFAULT_DIR})",
    )
    parser.add_argument("--general-threshold", type=float, default=tagger.DEFAULT_GENERAL_THRESHOLD)
    parser.add_argument("--character-threshold", type=float, default=tagger.DEFAULT_CHARACTER_THRESHOLD)
    parser.add_argument("--parody-threshold", type=float, default=tagger.DEFAULT_PARODY_THRESHOLD)
    parser.add_argument("--rating-threshold", type=float, default=tagger.DEFAULT_RATING_THRESHOLD)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-tag images that already have a .xmp sidecar",
    )
    parser.add_argument(
        "--keep-common-tags",
        action="store_true",
        help="Keep common tags such as 1girl, solo, looking_at_viewer instead of filtering them",
    )
    parser.add_argument(
        "--no-rating",
        action="store_true",
        help="Do not write rating/safe, rating/questionable, etc. tags",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many images; useful for a quick trial run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many new images would be tagged, then exit without running the model",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    root = Path(args.dir).expanduser().resolve() if args.dir else DEFAULT_DIR.resolve()
    if not root.is_dir():
        raise SystemExit(f"Directory does not exist: {root}")

    print(f"Scanning {root} ...")
    all_files = list(tagger.image_files(root))

    # Selecting the work up front — rather than skipping inside the loop — is what makes
    # the progress bar count the images actually being tagged. Against a library of ~100k
    # already-tagged files, a bar that sat at 96k instant skips told you nothing.
    if args.overwrite:
        todo = all_files
    else:
        todo = [p for p in all_files if tagger.is_untagged(p)]

    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"Found {len(all_files)} images, {len(todo)} to tag.")
    if args.dry_run:
        return 0
    if not todo:
        print("Nothing new to tag.")
        return 0

    session, tags_df = tagger.load_model()

    written = 0
    skipped = 0
    failed = 0

    progress = tqdm(todo, desc="Tagging", unit="img")
    try:
        for image_path in progress:
            try:
                result = tagger.process_one(
                    image_path=image_path,
                    session=session,
                    tags_df=tags_df,
                    general_threshold=args.general_threshold,
                    character_threshold=args.character_threshold,
                    parody_threshold=args.parody_threshold,
                    rating_threshold=args.rating_threshold,
                    overwrite=args.overwrite,
                    keep_common_tags=args.keep_common_tags,
                    include_rating=not args.no_rating,
                )
                if result == "written":
                    written += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                progress.write(f"Failed: {image_path} -> {exc}")
    except KeyboardInterrupt:
        # Sidecars are written per image, so an interrupted run is simply a partial one:
        # the next `uv run wd14` picks up exactly where this one stopped.
        progress.close()
        print(f"\nInterrupted. written={written}, skipped={skipped}, failed={failed}")
        return 130

    progress.close()
    print(f"Done. written={written}, skipped={skipped}, failed={failed}, total={len(todo)}")
    return 1 if failed and not written else 0
