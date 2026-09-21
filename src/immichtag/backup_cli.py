"""`immichtag-backup` — list the sidecar backups a run took, or put one back."""

from __future__ import annotations

import argparse
import sys

from immichtag import backup, config, filterer, library


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="immichtag-backup",
        description="List or restore the sidecar backups taken before sidecars were overwritten.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("list", "List backups, newest first"), ("restore", "Restore one backup")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--dir", default=None,
                         help=f"Library (default: $IMMICHTAG_DIR, else {config.DEFAULT_ROOT})")
        if name == "restore":
            cmd.add_argument("id", help="Backup id, as shown by `immichtag-backup list`")
            cmd.add_argument("--dry-run", action="store_true", help="Show what would be restored")
            cmd.add_argument("--no-immich", action="store_true", help="Do not sync to Immich afterwards")
    return parser


def cmd_list(root, state_dir) -> int:
    backups = backup.list_backups(state_dir)
    if not backups:
        print(f"No backups for {root}.")
        return 0
    for item in backups:
        print(f"{item['id']:<20}  {item['count']:>6} sidecars  {filterer.human_size(item['size']):>9}  {item['reason']}")
    return 0


def cmd_restore(root, state_dir, args) -> int:
    try:
        paths = backup.restore(root, args.id, state_dir, dry_run=args.dry_run)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"Would restore {len(paths)} sidecars from {args.id}.")
        for path in paths[:10]:
            print(f"  {path}")
        return 0

    pending = library.PendingSync(state_dir, root)
    for path in paths:
        image = path.with_name(path.name[: -len(".xmp")])
        if image.exists():
            pending.append(image, had_sidecar=True)
    print(f"Restored {len(paths)} sidecars from {args.id}. The sidecars they replaced were backed up first.")
    return sync_after_restore(root, state_dir, args)


def sync_after_restore(root, state_dir, args) -> int:
    pending = len(library.PendingSync(state_dir, root))
    settings = config.load_immich_settings()
    if args.no_immich or not settings.configured:
        why = "skipped by --no-immich" if args.no_immich else "no Immich API key configured"
        if pending:
            print(f"Immich sync pending ({pending} images): {why}. Run: immichtag-sync sync")
        return 0
    from immichtag import immich, immich_cli

    print("Syncing to Immich ...")
    immich_cli.print_sync(immich.sync(root, settings, state_dir), dry_run=False)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    root = config.resolve_root(args.dir)
    state_dir = config.state_dir(root)
    if args.command == "list":
        return cmd_list(root, state_dir)
    return cmd_restore(root, state_dir, args)


if __name__ == "__main__":
    raise SystemExit(main())
