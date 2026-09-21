"""One tagging run, shared by `animetag` and the web UI's Tag button.

scan -> lock -> back up what will be overwritten -> load the model -> tag, one sidecar
at a time -> hand the written images to the Immich sync.

Every image is written atomically and recorded as pending for Immich on its own, so an
interrupted run is just a shorter one: the next run's scan sees those images as current
and skips them.
"""

from __future__ import annotations

import collections
import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from animetag import backup, config, library, sidecar, tagger, vocab


@dataclass
class RunOptions:
    thresholds: Dict[str, float] = field(default_factory=lambda: dict(vocab.DEFAULT_THRESHOLDS))
    keep_common: bool = False
    include_rating: bool = True
    overwrite: bool = False
    limit: int | None = None
    device: str = "auto"
    #: Sync the written images to Immich when the run ends.
    immich: bool = True


@dataclass
class RunProgress:
    #: scanning | locking | backing-up | loading | running | syncing
    #: | finished | cancelled | error | refused
    state: str = "scanning"
    counts: Dict[str, int] = field(default_factory=dict)
    total_new: int = 0
    total_retag: int = 0
    done: int = 0
    written: int = 0
    failed: int = 0
    skipped: int = 0
    device: str | None = None
    eta_seconds: int | None = None
    backup_id: str | None = None
    backup_count: int = 0
    backup_bytes: int = 0
    messages: List[str] = field(default_factory=list)
    sync: Any = None
    error: str | None = None

    @property
    def total(self) -> int:
        return self.total_new + self.total_retag

    @property
    def active(self) -> bool:
        return self.state not in ("finished", "cancelled", "error", "refused")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaggingRun:
    """Run it with `run()`. Progress is reported through `on_progress(progress)` after
    every state change and every image; set `cancel` to stop after the current image."""

    ETA_WINDOW = 50

    def __init__(
        self,
        root: Path,
        options: RunOptions | None = None,
        *,
        tagger_factory: Callable[[str], Any] = tagger.load_model,
        on_progress: Callable[[RunProgress], None] | None = None,
        cancel: threading.Event | None = None,
        state_dir: Path | None = None,
        sync_hook: Callable[[RunProgress], Any] | None = None,
    ) -> None:
        self.root = Path(root)
        self.options = options or RunOptions()
        self.tagger_factory = tagger_factory
        self.on_progress = on_progress
        self.cancel = cancel or threading.Event()
        self.state_dir = Path(state_dir) if state_dir else config.state_dir(self.root)
        self.sync_hook = sync_hook
        self.progress = RunProgress()

    # -- plumbing ----------------------------------------------------------------

    def _emit(self) -> None:
        if self.on_progress:
            self.on_progress(self.progress)

    def _state(self, state: str) -> None:
        self.progress.state = state
        self._emit()

    # -- the run -----------------------------------------------------------------

    def run(self) -> RunProgress:
        progress = self.progress
        opts = self.options
        try:
            self._state("scanning")
            library.remove_stale_tmp(self.root)
            entries = library.scan(self.root, tagger.MODEL_REPO, tagger.MODEL_REVISION)
            progress.counts = library.count(entries)
            queue = library.tagging_queue(entries, overwrite=opts.overwrite)
            if opts.limit is not None:
                queue = queue[: opts.limit]
            progress.total_new = sum(1 for _, status in queue if status is library.Status.NEW)
            progress.total_retag = len(queue) - progress.total_new
            if not queue:
                self._state("finished")
                return progress

            self._state("locking")
            try:
                lock = library.TaggingLock(self.state_dir).acquire()
            except library.LockBusy as exc:
                progress.error = str(exc)
                self._state("refused")
                return progress
            try:
                outcome = self._tag(queue)
            finally:
                lock.release()
        except Exception as exc:
            progress.error = f"{type(exc).__name__}: {exc}"
            outcome = "error"

        if progress.written and self.sync_hook is not None:
            self._state("syncing")
            try:
                progress.sync = self.sync_hook(progress)
            except Exception as exc:  # a sync problem never undoes the tagging (FR-021)
                progress.sync = {"ok": False, "reason": str(exc)}
        self._state(outcome)
        return progress

    def _tag(self, queue: List[tuple]) -> str:
        progress = self.progress
        opts = self.options

        # Back up everything this run may overwrite before the first byte changes.
        self._state("backing-up")
        existing = [sidecar.sidecar_path(image) for image, status in queue if status is not library.Status.NEW]
        progress.backup_id = backup.create_backup(self.root, existing, self.state_dir, reason="retag")
        if progress.backup_id:
            info = next(b for b in backup.list_backups(self.state_dir) if b["id"] == progress.backup_id)
            progress.backup_count, progress.backup_bytes = info["count"], info["size"]

        self._state("loading")
        model = self.tagger_factory(opts.device)
        progress.device = getattr(model, "device", None)

        self._state("running")
        pending = library.PendingSync(self.state_dir, self.root)
        provenance_profile = tagger.profile_name(opts.thresholds)
        status_of = dict(queue)
        durations: collections.deque = collections.deque(maxlen=self.ETA_WINDOW)
        started = time.monotonic()

        for image, pixels in tagger.iter_preprocessed(model.preprocess, [image for image, _ in queue]):
            if self.cancel.is_set():
                return "cancelled"
            try:
                if isinstance(pixels, FileNotFoundError):
                    progress.skipped += 1    # removed since the scan, e.g. deleted in Immich
                elif isinstance(pixels, Exception):
                    raise pixels
                else:
                    self._write(image, pixels, model, status_of[image], pending, provenance_profile)
                    progress.written += 1
            except FileNotFoundError:
                progress.skipped += 1
            except Exception as exc:
                progress.failed += 1
                progress.messages.append(f"{image.name}: {type(exc).__name__}: {exc}")
            progress.done += 1

            now = time.monotonic()
            durations.append(now - started)
            started = now
            remaining = progress.total - progress.done
            progress.eta_seconds = round(sum(durations) / len(durations) * remaining) if remaining else 0
            self._emit()
        return "finished"

    def _write(self, image: Path, pixels: Any, model: Any, status: library.Status,
               pending: library.PendingSync, profile: str) -> None:
        opts = self.options
        probs = model.infer(pixels)
        managed = tagger.tags_from_probs(
            probs, model.names, model.categories,
            thresholds=opts.thresholds, keep_common=opts.keep_common,
            include_rating=opts.include_rating,
        )
        path = sidecar.sidecar_path(image)
        old = sidecar.read_sidecar(path).tags if path.exists() else []
        if not image.exists():
            raise FileNotFoundError(image)
        provenance = sidecar.Provenance(
            model=tagger.MODEL_REPO, revision=tagger.MODEL_REVISION, profile=profile, tagged_at=_now(),
        )
        sidecar.write_atomic(path, sidecar.build_xmp(sidecar.merge_tags(managed, old), provenance))
        pending.append(image, had_sidecar=status is not library.Status.NEW)
