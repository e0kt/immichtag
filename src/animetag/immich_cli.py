"""`animetag-immich` — make Immich re-read sidecars and drop the tags nothing uses.

`animetag` already does this after every run; this command is for finishing a sync that
could not run then (Immich down, no key yet), for a whole-library resync, and for
previewing what the tag cleanup would delete.
"""

from __future__ import annotations

import argparse
import sys

from animetag import config, immich, library

NO_KEY = ("Immich API key not configured: set [immich].api_key in ~/.config/animetag/config.toml "
          "or ANIMETAG_IMMICH_API_KEY")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animetag-immich",
        description="Sync the library's sidecars into Immich and remove the empty tags animetag manages.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(cmd):
        cmd.add_argument("--dir", default=None,
                         help=f"Library (default: $ANIMETAG_DIR, else {config.DEFAULT_ROOT})")
        return cmd

    sync = common(sub.add_parser("sync", help="Re-read the sidecars written since the last sync"))
    sync.add_argument("--all", action="store_true", help="Every image in the library, not just pending ones")
    sync.add_argument("--no-cleanup", action="store_true", help="Do not delete empty tags afterwards")
    sync.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing")

    cleanup = common(sub.add_parser("cleanup", help="Delete managed tags no image uses any more"))
    cleanup.add_argument("--dry-run", action="store_true", help="Only show what would be deleted")

    common(sub.add_parser("status", help="Connection, permissions and what is pending"))
    return parser


def print_cleanup(result: immich.CleanupResult, dry_run: bool) -> None:
    prefixes = ", ".join(f"{name} {count}" for name, count in sorted(result.by_prefix.items()))
    verb = "would delete" if dry_run else "deleted"
    count = result.count if dry_run else result.deleted
    print(f"Tag cleanup: {verb} {count} tags" + (f" ({prefixes})" if prefixes and dry_run else ""))
    if dry_run and result.examples:
        for value in result.examples:
            print(f"  {value}")
        if result.count > len(result.examples):
            print(f"  ... and {result.count - len(result.examples)} more")
    if result.skipped_nonempty or result.skipped_blocked:
        print(f"  kept {result.skipped_nonempty} still in use in Immich, "
              f"{result.skipped_blocked} above those")


def print_sync(result: immich.SyncResult, dry_run: bool) -> None:
    if not result.ok:
        print(f"Immich sync pending ({result.pending} images): {result.reason}", file=sys.stderr)
        return
    if dry_run:
        print(f"Would refresh {result.refreshed} images in Immich and discover {result.discovered} new sidecars.")
    else:
        print(f"Immich: refreshed {result.refreshed}, discovered {result.discovered}, "
              f"removed {result.deleted_tags} empty tags.")
    if result.unmatched:
        print(f"  {result.unmatched} images are not Immich assets; they stay pending.")
    if result.cleanup is not None:
        print_cleanup(result.cleanup, dry_run)
    if result.reason:
        print(f"Immich: {result.reason}")


def _progress():
    last = {"phase": None}

    def report(phase: str, done: int, total: int) -> None:
        if phase != last["phase"]:
            last["phase"] = phase
            label = {"mapping": "Reading Immich's asset list", "refresh": f"Asking Immich to re-read {total} images",
                     "discover": "Starting sidecar discovery", "waiting": "Waiting for Immich's queues",
                     "cleanup": "Removing empty tags"}.get(phase, phase)
            print(f"{label} ...", flush=True)
        elif phase == "waiting" and total:
            print(f"\r  {total} jobs left in Immich   ", end="", flush=True)

    return report


def cmd_sync(root, state_dir, settings, args) -> int:
    result = immich.sync(root, settings, state_dir, all=args.all, cleanup=not args.no_cleanup,
                         dry_run=args.dry_run, on_progress=_progress())
    print()
    print_sync(result, args.dry_run)
    return 0 if result.ok else 1


def cmd_cleanup(root, settings, args) -> int:
    try:
        client = immich.ImmichClient(settings)
        print("Collecting the tags the library's sidecars use ...", flush=True)
        in_use = immich.managed_in_use(root)
        plan = immich.plan_cleanup(client.list_tags(), in_use)
        if args.dry_run or not plan:
            print_cleanup(immich.run_cleanup(client, plan, dry_run=True), dry_run=True)
            return 0
        print_cleanup(immich.run_cleanup(client, plan, dry_run=True), dry_run=True)
        result = immich.run_cleanup(client, plan)
        print_cleanup(result, dry_run=False)
    except immich.ImmichError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def cmd_status(root, state_dir, settings) -> int:
    print(f"Library: {root}")
    print(f"Immich:  {settings.url}")
    pending = len(library.PendingSync(state_dir, root))
    print(f"Pending sync: {pending} images")
    try:
        client = immich.ImmichClient(settings)
        major, minor, patch = client.version()
        note = "" if major == 3 else "  (animetag was built against Immich 3.x)"
        print(f"Server version: {major}.{minor}.{patch}{note}")
    except immich.ImmichError as exc:
        print(f"Not reachable: {exc}", file=sys.stderr)
        return 1

    checks = (
        ("tag.read", lambda: client.list_tags()),
        ("job.read (admin)", lambda: client.queue_counts()),
        ("asset.read", lambda: client.tag_has_assets("00000000-0000-4000-8000-000000000000")),
    )
    ok = True
    for name, call in checks:
        try:
            call()
            print(f"  permission {name}: ok")
        except immich.ImmichAuthError:
            ok = False
            print(f"  permission {name}: MISSING")
        except immich.ImmichError as exc:
            print(f"  permission {name}: could not check ({exc})")
    print("  (asset.update, job.create and tag.delete are only exercised by a real sync)")

    print("Counting empty managed tags ...", flush=True)
    plan = immich.plan_cleanup(client.list_tags(), immich.managed_in_use(root))
    print(f"Empty managed tags in Immich: {len(plan)}")
    return 0 if ok else 1


def main() -> int:
    args = build_parser().parse_args()
    root = config.resolve_root(args.dir)
    if not root.is_dir():
        print(f"Directory does not exist: {root}", file=sys.stderr)
        return 2
    settings = config.load_immich_settings()
    if not settings.configured:
        print(NO_KEY, file=sys.stderr)
        return 2
    state_dir = config.state_dir(root)
    if args.command == "sync":
        return cmd_sync(root, state_dir, settings, args)
    if args.command == "cleanup":
        return cmd_cleanup(root, settings, args)
    return cmd_status(root, state_dir, settings)


if __name__ == "__main__":
    raise SystemExit(main())
