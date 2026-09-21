"""Backups of sidecars taken before a run overwrites them, and restoring them.

A backup lives in the library's state dir (never inside Immich's upload tree):

    backups/<UTC id>/sidecars.tar.gz   original bytes, paths relative to the library root
    backups/<UTC id>/manifest.jsonl    {"path", "size", "mtime", "sha256"} per file
    backups/<UTC id>/meta.json         {"root", "created", "count", "reason"}

A file already archived with the same path and content is not archived again, so
resuming an interrupted re-tag does not re-archive the same 88k legacy sidecars. The
key is (path, sha256), not the content alone: two images with identical sidecars are
still two files to restore.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Set, Tuple

from animetag import sidecar


def _backups_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "backups"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archived(state_dir: Path) -> Set[Tuple[str, str]]:
    known: Set[Tuple[str, str]] = set()
    base = _backups_dir(state_dir)
    if not base.is_dir():
        return known
    for manifest in base.glob("*/manifest.jsonl"):
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                known.add((entry["path"], entry["sha256"]))
    return known


def _new_id(base: Path) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate, counter = stamp, 1
    while (base / candidate).exists() or (base / f".{candidate}.partial").exists():
        candidate = f"{stamp}-{counter}"
        counter += 1
    return candidate


def create_backup(root: Path, sidecars: Iterable[Path], state_dir: Path, reason: str = "retag") -> str | None:
    """Archive the given sidecars as they are now. Returns the backup id, or None when
    every one is already archived (or none exists). Raises on failure — before any
    sidecar has been touched, which is the point."""
    root = Path(root)
    known = _archived(state_dir)
    items: List[Tuple[str, bytes, dict]] = []
    for path in sidecars:
        path = Path(path)
        try:
            data = path.read_bytes()
            stat = path.stat()
        except FileNotFoundError:
            continue
        rel = PurePosixPath(path.relative_to(root)).as_posix()
        digest = _sha256(data)
        if (rel, digest) in known:
            continue
        known.add((rel, digest))
        items.append((rel, data, {"path": rel, "size": len(data), "mtime": stat.st_mtime, "sha256": digest}))
    if not items:
        return None

    base = _backups_dir(state_dir)
    base.mkdir(parents=True, exist_ok=True)
    backup_id = _new_id(base)
    # Written under a hidden name and renamed at the end: a backup either exists whole
    # or not at all.
    partial = base / f".{backup_id}.partial"
    partial.mkdir()
    try:
        with tarfile.open(partial / "sidecars.tar.gz", "w:gz") as tar:
            for rel, data, entry in items:
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mtime = int(entry["mtime"])
                tar.addfile(info, io.BytesIO(data))
        with (partial / "manifest.jsonl").open("w", encoding="utf-8") as handle:
            for _, _, entry in items:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        meta = {
            "root": str(root),
            "created": dt.datetime.now(dt.timezone.utc).isoformat(),
            "count": len(items),
            "reason": reason,
        }
        (partial / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        partial.rename(base / backup_id)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return backup_id


def list_backups(state_dir: Path) -> List[Dict]:
    """Newest first: id, count, compressed size in bytes, reason, created."""
    base = _backups_dir(state_dir)
    if not base.is_dir():
        return []
    result = []
    for folder in base.iterdir():
        meta_path = folder / "meta.json"
        if folder.name.startswith(".") or not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        archive = folder / "sidecars.tar.gz"
        result.append({
            "id": folder.name,
            "count": meta.get("count", 0),
            "size": archive.stat().st_size if archive.exists() else 0,
            "reason": meta.get("reason", ""),
            "created": meta.get("created", ""),
            "root": meta.get("root", ""),
        })
    result.sort(key=lambda b: (b["created"], b["id"]), reverse=True)
    return result


def _members(archive: Path) -> List[Tuple[str, bytes]]:
    members = []
    with tarfile.open(archive, "r:gz") as tar:
        for info in tar.getmembers():
            if not info.isfile():
                continue
            rel = PurePosixPath(info.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"refusing unsafe path in backup: {info.name}")
            handle = tar.extractfile(info)
            members.append((info.name, handle.read() if handle else b""))
    return members


def restore(root: Path, backup_id: str, state_dir: Path, dry_run: bool = False) -> List[Path]:
    """Put a backup's sidecars back, byte for byte. The sidecars about to be replaced
    are backed up first (reason "restore-safety"), so a restore can itself be undone."""
    root = Path(root)
    folder = _backups_dir(state_dir) / backup_id
    archive = folder / "sidecars.tar.gz"
    if not archive.is_file():
        raise ValueError(f"no backup {backup_id!r} in {_backups_dir(state_dir)}")
    members = _members(archive)
    targets = [(root / rel, data) for rel, data in members]
    if dry_run:
        return [path for path, _ in targets]

    create_backup(root, [path for path, _ in targets if path.exists()], state_dir, reason="restore-safety")
    restored = []
    for path, data in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_atomic(path, data)
        restored.append(path)
    return restored
