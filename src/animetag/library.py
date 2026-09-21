"""The library on disk: which images exist, which need (re-)tagging, and in what order.

An image's status is derived from its sidecar on every scan and never stored, so it can
never disagree with the files: no sidecar is NEW, a sidecar without our provenance
(every WD14 sidecar) is LEGACY, one from another model revision is OUTDATED.
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Tuple

from animetag import sidecar

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"}
#: Immich's own folders next to the uploads, plus the usual noise.
SKIP_DIRS = {"thumbs", "encoded-video", "profile", "backups", ".git", "__pycache__"}


class Status(str, Enum):
    NEW = "new"
    LEGACY = "legacy"
    OUTDATED = "outdated"
    CURRENT = "current"


def image_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in IMAGE_EXTS:
                yield path


def status_of(data: sidecar.SidecarData, model: str, revision: str) -> Status:
    """Status of an image whose sidecar exists and was read into `data`."""
    prov = data.provenance
    if data.error or prov is None:
        return Status.LEGACY
    if prov.model == model and prov.revision == revision:
        return Status.CURRENT
    return Status.OUTDATED


def classify(image: Path, model: str, revision: str) -> Status:
    path = sidecar.sidecar_path(image)
    if not path.exists():
        return Status.NEW
    return status_of(sidecar.read_sidecar(path), model, revision)


def scan(root: Path, model: str, revision: str) -> List[Tuple[Path, Status]]:
    return [(image, classify(image, model, revision)) for image in image_files(root)]


def count(entries: Iterable[Tuple[Path, Status]]) -> Dict[str, int]:
    totals = {status.value: 0 for status in Status}
    for _, status in entries:
        totals[status.value] += 1
    return totals


def tagging_queue(
    entries: List[Tuple[Path, Status]], overwrite: bool = False
) -> List[Tuple[Path, Status]]:
    """New uploads first — they have no tags at all — then everything to re-tag.

    Scan order is kept within each group, so an interrupted run resumes predictably.
    """
    new = [entry for entry in entries if entry[1] is Status.NEW]
    retag = {Status.LEGACY, Status.OUTDATED} | ({Status.CURRENT} if overwrite else set())
    rest = [entry for entry in entries if entry[1] in retag]
    return new + rest


def remove_stale_tmp(root: Path) -> int:
    """Delete temp files a killed run left behind. Returns how many."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if filename.startswith(".") and filename.endswith(sidecar.TMP_SUFFIX):
                try:
                    (Path(dirpath) / filename).unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PendingSync:
    """Sidecars written but not yet re-read by Immich, so a failed or skipped sync can be
    finished later by `animetag-immich sync`. Paths are relative to the library root."""

    FILENAME = "pending-sync.jsonl"

    def __init__(self, state_dir: Path, root: Path) -> None:
        self.path = Path(state_dir) / self.FILENAME
        self.root = Path(root)

    def append(self, image: Path, had_sidecar: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"path": str(Path(image).relative_to(self.root)), "had_sidecar": had_sidecar, "at": _now()}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> List[dict]:
        """One entry per path. A path ever seen without a sidecar keeps `had_sidecar`
        false: Immich still has to discover that sidecar, not just re-read it."""
        if not self.path.exists():
            return []
        merged: Dict[str, dict] = {}
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                previous = merged.get(entry["path"])
                if previous is not None:
                    entry["had_sidecar"] = previous["had_sidecar"] and entry["had_sidecar"]
                merged[entry["path"]] = entry
        return list(merged.values())

    def remove(self, paths: Iterable[str]) -> None:
        if not self.path.exists():
            return
        gone = set(paths)
        keep = [entry for entry in self.entries() if entry["path"] not in gone]
        data = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in keep)
        sidecar.write_atomic(self.path, data.encode("utf-8"))

    def __len__(self) -> int:
        return len(self.entries())


class LockBusy(RuntimeError):
    """Another tagging run holds the library's lock."""


class TaggingLock:
    """One writer per library, across processes: the CLI and the web server can never
    tag the same library at once. `flock` is released by the kernel when the holder dies,
    so a crash never leaves a stale lock behind."""

    FILENAME = "tagging.lock"

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / self.FILENAME
        self._fd: int | None = None

    def acquire(self) -> "TaggingLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = os.pread(fd, 200, 0).decode("utf-8", "replace").strip()
            os.close(fd)
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise
            raise LockBusy(f"another tagging run is active ({holder or 'unknown holder'})") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}, started {_now()}".encode(), 0)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> "TaggingLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
