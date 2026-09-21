"""Command line entry point for the tag filter.

`uv run animetag-filter --filter blue_hair,long_hair --out ./picked` scans the library
for tagged images matching the filters and copies them out, shrinking anything over
the size limit on the way.
"""

from __future__ import annotations

import argparse
import random
from collections import Counter
from pathlib import Path

from tqdm import tqdm

from animetag import config, filterer
from animetag.filterer import MAX_FILTERS

EPILOG = """\
examples:
  # one filter, three tags: images having all of them
  animetag-filter -f "blue_hair,long_hair,school_uniform" -o ./picked

  # two filters: an image matching EITHER one is taken
  animetag-filter -f "character/hatsune_miku" -f "parody/k-on!" -o ./picked

  # exclude within one filter with a leading '-', glob with '*', cap the output
  animetag-filter -f "cat_ears,-rating/explicit,blue_*" -n 200 -o ./picked

  # -x vetoes across every filter, so neither group can bring explicit images in
  animetag-filter -f "cat_ears" -f "character/hatsune_miku" -x "rating/explicit,monochrome" -o ./picked

  # see what tags the library actually has before writing a filter
  animetag-filter --list-tags 40
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animetag-filter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Filter already-tagged images by their .xmp tags and copy the matches to an "
            "output folder, compressing any image over the size limit."
        ),
        epilog=EPILOG,
    )
    parser.add_argument(
        "--filter",
        "-f",
        action="append",
        default=[],
        metavar="TAGS",
        help=(
            "Comma-separated tags that must ALL be present. Prefix a tag with '-' to "
            "exclude it, use '*' to glob. Repeat up to %d times; an image matching ANY "
            "filter is taken (see --match)." % MAX_FILTERS
        ),
    )
    parser.add_argument(
        "--exclude",
        "-x",
        action="append",
        default=[],
        metavar="TAGS",
        help=(
            "Comma-separated tags NO exported image may carry, whichever filter matched "
            "it. Repeatable. Unlike a '-tag' inside one --filter, this applies to the "
            "whole selection"
        ),
    )
    parser.add_argument(
        "--match",
        choices=("any", "all"),
        default="any",
        help="Combine multiple --filter groups with OR (any, default) or AND (all)",
    )
    parser.add_argument(
        "--out",
        "-o",
        default=None,
        help="Output folder; created if missing (required unless --dry-run/--list-tags)",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help=f"Directory to scan (default: $ANIMETAG_DIR, else {config.DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--max-images",
        "-n",
        type=int,
        default=None,
        metavar="N",
        help="Stop after copying N images (default: no limit)",
    )
    parser.add_argument(
        "--max-size",
        default="1MB",
        help="Compress images larger than this, e.g. 1MB, 500k (default: 1MB)",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Copy every image untouched, however large it is",
    )
    parser.add_argument(
        "--order",
        choices=("scan", "random", "newest", "oldest"),
        default="scan",
        help=(
            "Which matches to take when --max-images cuts the list: scan order "
            "(default, and the only one that can stop early), random, or by file date"
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="Seed for --order random")
    parser.add_argument(
        "--copy-sidecar",
        action="store_true",
        help="Also copy each image's .xmp next to the output image",
    )
    parser.add_argument(
        "--list-tags",
        type=int,
        nargs="?",
        const=50,
        default=None,
        metavar="N",
        help="Print the N most common tags among matching images and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many images match, then exit without copying",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    root = config.resolve_root(args.dir)
    if not root.is_dir():
        raise SystemExit(f"Directory does not exist: {root}")

    try:
        filters = filterer.parse_filters(args.filter)
        exclude = filterer.parse_exclusions(args.exclude)
        limit_bytes = filterer.parse_size(args.max_size)
    except ValueError as exc:
        raise SystemExit(str(exc))

    listing = args.list_tags is not None
    if not args.dry_run and not listing and not args.out:
        raise SystemExit("--out is required (or use --dry-run / --list-tags)")
    if args.max_images is not None and args.max_images < 1:
        raise SystemExit("--max-images must be at least 1")

    if filters:
        print(f"Filters ({args.match} of {len(filters)}):")
        for tag_filter in filters:
            parts = list(tag_filter.required) + [f"NOT {t}" for t in tag_filter.excluded]
            print(f"  - {' AND '.join(parts)}")
    else:
        print("No filters given: every tagged image matches.")
    if exclude:
        print(f"Excluding everywhere: {' '.join(exclude.excluded)}")

    # Only scan order can stop at the cap; the others have to see every match first.
    stop_after = args.max_images if (args.order == "scan" and not listing) else None

    print(f"Scanning {root} ...")
    matches = []
    progress = tqdm(desc="Matched", unit="img")
    try:
        for match in filterer.find_matches(
            root, filters, args.match == "all", stop_after, exclude
        ):
            matches.append(match)
            progress.update(1)
    except KeyboardInterrupt:
        progress.close()
        print("\nInterrupted during scan.")
        return 130
    progress.close()

    print(f"{len(matches)} matching images.")
    if not matches:
        return 0

    if listing:
        counter = Counter(tag for match in matches for tag in match.tags)
        width = max(len(tag) for tag, _ in counter.most_common(args.list_tags))
        for tag, count in counter.most_common(args.list_tags):
            print(f"  {tag:<{width}}  {count}")
        print(f"({len(counter)} distinct tags across {len(matches)} images)")
        return 0

    if args.order == "random":
        random.Random(args.seed).shuffle(matches)
    elif args.order in ("newest", "oldest"):
        matches.sort(key=lambda m: m.image.stat().st_mtime, reverse=args.order == "newest")

    if args.max_images is not None:
        matches = matches[: args.max_images]

    if args.dry_run:
        print(f"Would copy {len(matches)} images.")
        for match in matches[:10]:
            print(f"  {match.image}")
        if len(matches) > 10:
            print(f"  ... and {len(matches) - 10} more")
        return 0

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    used_stems: set = set()
    copied = 0
    compressed = 0
    failed = 0
    over = 0
    total_bytes = 0

    progress = tqdm(matches, desc="Copying", unit="img")
    try:
        for match in progress:
            try:
                dest, was_compressed = filterer.export(
                    match=match,
                    out_dir=out_dir,
                    used_stems=used_stems,
                    limit_bytes=limit_bytes,
                    compress=not args.no_compress,
                    copy_sidecar=args.copy_sidecar,
                )
                size = dest.stat().st_size
                copied += 1
                compressed += int(was_compressed)
                over += int(size > limit_bytes)
                total_bytes += size
            except Exception as exc:
                failed += 1
                progress.write(f"Failed: {match.image} -> {exc}")
    except KeyboardInterrupt:
        progress.close()
        print(f"\nInterrupted. copied={copied}, compressed={compressed}, failed={failed}")
        return 130
    progress.close()

    print(
        f"Done. copied={copied}, compressed={compressed}, failed={failed} -> {out_dir} "
        f"({filterer.human_size(total_bytes)} total)"
    )
    if over and not args.no_compress:
        print(f"Note: {over} file(s) are still over {args.max_size}; they could not be shrunk further.")
    return 1 if failed and not copied else 0
