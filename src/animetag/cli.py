"""`animetag` — tag new images and re-tag everything not written by the current model.

With no arguments it takes every image that has no sidecar (new uploads, first) and
every image whose sidecar came from another model — WD14's included — and writes fresh
sidecars. Safe to interrupt and re-run: finished images are skipped next time.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from tqdm import tqdm

from animetag import config, filterer, library, run, tagger, vocab

EXIT_OK, EXIT_ALL_FAILED, EXIT_USAGE, EXIT_LOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


def build_parser() -> argparse.ArgumentParser:
    t = vocab.DEFAULT_THRESHOLDS
    parser = argparse.ArgumentParser(
        prog="animetag",
        description=(
            "Tag anime images with PixAI Tagger v1.0 into Immich-compatible XMP sidecars. "
            "With no arguments, tags every new image and re-tags every image whose sidecar "
            f"was not written by the current model, under {config.DEFAULT_ROOT}."
        ),
    )
    parser.add_argument("--dir", default=None,
                        help=f"Library to tag (default: $ANIMETAG_DIR, else {config.DEFAULT_ROOT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be tagged and how long it would take, then exit")
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many images")
    parser.add_argument("--overwrite", action="store_true",
                        help="Also re-tag images already tagged by the current model")
    parser.add_argument("--general-threshold", type=float, default=t["general"])
    parser.add_argument("--character-threshold", type=float, default=t["character"])
    parser.add_argument("--parody-threshold", type=float, default=t["copyright"],
                        help="Threshold for copyright/series tags, written as parody/*")
    parser.add_argument("--meta-threshold", type=float, default=t["meta"],
                        help="Threshold for the whitelisted meta/* tags")
    parser.add_argument("--keep-common-tags", action="store_true",
                        help="Keep common tags such as 1girl, solo, looking_at_viewer")
    parser.add_argument("--no-rating", action="store_true", help="Do not write rating/* tags")
    parser.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto",
                        help="auto = the Apple GPU when available")
    parser.add_argument("--no-immich", action="store_true",
                        help="Do not sync to Immich afterwards; the images stay pending for "
                             "`animetag-immich sync`")
    return parser


def _hours(images: int, device: str) -> str:
    seconds = images / tagger.EXPECTED_RATE[device]
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _counts_line(total: int, counts: dict, queued_new: int, queued_retag: int) -> str:
    return (
        f"{total} images: {queued_new} new, {queued_retag} to re-tag "
        f"({counts.get('legacy', 0)} legacy, {counts.get('outdated', 0)} outdated), "
        f"{counts.get('current', 0)} current."
    )


def dry_run(root, args) -> int:
    entries = library.scan(root, tagger.MODEL_REPO, tagger.MODEL_REVISION)
    counts = library.count(entries)
    queue = library.tagging_queue(entries, overwrite=args.overwrite)
    if args.limit is not None:
        queue = queue[: args.limit]
    new = sum(1 for _, status in queue if status is library.Status.NEW)
    print(_counts_line(len(entries), counts, new, len(queue) - new))
    if queue:
        device = tagger.select_device(args.device)
        estimate = " (rough estimate)" if device == "cpu" else ""
        print(f"Would tag {len(queue)} images on {device}: about {_hours(len(queue), device)}{estimate}.")
    return EXIT_OK


def print_sync_result(result, pending_count: int) -> None:
    if result is None:
        if pending_count:
            print(f"Immich sync pending ({pending_count} images): no Immich API key configured. "
                  "Run: animetag-immich sync")
        return
    ok = getattr(result, "ok", None)
    if ok is None and isinstance(result, dict):
        ok = result.get("ok")
    if ok:
        print(f"Immich: refreshed {result.refreshed}, discovered {result.discovered}, "
              f"removed {result.deleted_tags} empty tags.")
        if result.reason:
            print(f"Immich: {result.reason}")
    else:
        reason = getattr(result, "reason", None) or (result.get("reason") if isinstance(result, dict) else "")
        print(f"Immich sync pending ({pending_count} images): {reason}. Run: animetag-immich sync")


def main() -> int:
    args = build_parser().parse_args()
    root = config.resolve_root(args.dir)
    if not root.is_dir():
        print(f"Directory does not exist: {root}", file=sys.stderr)
        return EXIT_USAGE
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return EXIT_USAGE

    print(f"Scanning {root} ...")
    if args.dry_run:
        return dry_run(root, args)

    state_dir = config.state_dir(root)
    options = run.RunOptions(
        thresholds={
            "general": args.general_threshold,
            "character": args.character_threshold,
            "copyright": args.parody_threshold,
            "meta": args.meta_threshold,
        },
        keep_common=args.keep_common_tags,
        include_rating=not args.no_rating,
        overwrite=args.overwrite,
        limit=args.limit,
        device=args.device,
        immich=not args.no_immich,
    )

    cancel = threading.Event()
    bar: dict = {}
    seen_states: set = set()

    def on_progress(progress: run.RunProgress) -> None:
        state = progress.state
        if state not in seen_states:
            seen_states.add(state)
            if state == "locking":
                total = sum(progress.counts.values())
                print(_counts_line(total, progress.counts, progress.total_new, progress.total_retag))
            elif state == "loading":
                if progress.backup_id:
                    where = state_dir / "backups" / progress.backup_id
                    print(f"Backed up {progress.backup_count} sidecars -> {where} "
                          f"({filterer.human_size(progress.backup_bytes)})")
                device = tagger.select_device(options.device)
                precision = "fp16" if device == "mps" else "fp32"
                print(f"Loading {tagger.MODEL_REPO} @ {tagger.MODEL_REVISION[:7]} on {device} ({precision}) ...")
            elif state == "running":
                if progress.device == "cpu":
                    print(f"warning: GPU unavailable, running on CPU — expect about "
                          f"{_hours(progress.total, 'cpu')} for {progress.total} images", file=sys.stderr)
                bar["bar"] = tqdm(total=progress.total, desc="Tagging", unit="img")
            elif state == "syncing":
                print("Syncing to Immich ...")
        if "bar" in bar and state == "running":
            bar["bar"].n = progress.done
            bar["bar"].refresh()
        elif "bar" in bar and state != "running":
            bar.pop("bar").close()

    def interrupt(signum, frame):
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        print("\nStopping after the current image (Ctrl-C again to abort) ...", file=sys.stderr)

    previous = signal.signal(signal.SIGINT, interrupt)
    sync_hook = run.immich_sync_hook(root, state_dir) if options.immich else None
    try:
        tagging = run.TaggingRun(root, options, on_progress=on_progress, cancel=cancel,
                                 state_dir=state_dir, sync_hook=sync_hook)
        progress = tagging.run()
    finally:
        signal.signal(signal.SIGINT, previous)
        if "bar" in bar:
            bar.pop("bar").close()

    if progress.state == "refused":
        print(f"{progress.error}", file=sys.stderr)
        return EXIT_LOCKED
    if progress.total == 0:
        total = sum(progress.counts.values())
        print(_counts_line(total, progress.counts, 0, 0))
        print("Nothing to tag.")
        return EXIT_OK

    for message in progress.messages[-20:]:
        print(f"Failed: {message}", file=sys.stderr)
    print(f"Done. written={progress.written} failed={progress.failed} "
          f"skipped={progress.skipped} total={progress.total}")
    if progress.state == "error":
        print(f"error: {progress.error}", file=sys.stderr)

    pending = len(library.PendingSync(state_dir, root))
    if options.immich:
        print_sync_result(progress.sync, pending)
    elif pending:
        print(f"Immich sync pending ({pending} images): skipped by --no-immich. Run: animetag-immich sync")

    if progress.state == "cancelled":
        return EXIT_INTERRUPTED
    if progress.state == "error" or (progress.failed and not progress.written):
        return EXIT_ALL_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
