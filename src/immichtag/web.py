"""A local web UI for the tag filter.

`uv run immichtag-web` builds the sidecar index once, serves a page on 127.0.0.1 and opens
it. Everything the CLI takes as flags is a control on that page, plus the two things a
terminal cannot give you: thumbnails of what a filter actually selects, and a match
count that updates as you type.

The page can also run the tagger (PixAI Tagger v1.0) — the same selection and thresholds
as `uv run immichtag`: new images first, then any whose sidecar another model wrote — and
rebuilds the index when it is done, so new uploads go from untagged to filterable without
a terminal. It can sync the result into Immich and remove the tags nothing uses.

Results can be downloaded as originals — one file, or a selection as a zip streamed
straight to the socket — and deleted, which moves them to Immich's trash (never a
permanent delete, never a file removed behind Immich's back). Trashed files stay on disk
until the trash is emptied, so the index leaves out whatever Immich holds in its trash.

Stdlib http.server on purpose — this is a personal tool on loopback, and a web
framework would be a heavier dependency than the whole tagger.
"""

from __future__ import annotations

import errno
import io
import json
import os
import platform
import secrets
import subprocess
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image

from immichtag import config, filterer, immich, library, run, sidecar, vocab
from immichtag.trash import DeletionLog, TrashSet
from immichtag.index import IndexHolder, LibraryIndex

STATIC_DIR = Path(__file__).parent / "web"
#: Nothing well-known lives here. Still collidable, which is why serve() can move.
DEFAULT_PORT = 8788
#: How many ports past the default to try before giving up.
PORT_SEARCH = 20
THUMB_SIZE = 360
THUMB_CACHE_LIMIT = 600
#: Files are streamed in pieces this big, never read whole.
CHUNK = 1024 * 1024
#: Most ids one selection may name explicitly; "all matching" has no limit.
MAX_SELECTION_IDS = 100_000

#: A download bundle's link works once, and only for this long.
BUNDLE_TTL = 600
#: Past either of these the page asks again before starting the download.
BUNDLE_WARN_BYTES = 2 * 1024**3
BUNDLE_WARN_COUNT = 2000

#: A delete confirmation must be used within this long.
DELETE_PREVIEW_TTL = 600
#: Above this many images the page makes the user type the count.
DELETE_TYPE_THRESHOLD = 100
#: How many sample thumbnails the confirmation shows.
DELETE_SAMPLE = 12
#: Failures listed in a finished delete's report; the rest are counted.
DELETE_FAILURES_SHOWN = 50

MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif",
}


class HTTPStatusError(Exception):
    """An error the page should see with a specific status (404, 409) rather than 400."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def content_disposition(name: str) -> str:
    """`attachment` with the real name for browsers (RFC 5987) and an ASCII fallback."""
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


@dataclass
class ExportJob:
    """One export, run on a worker thread and polled by the page."""

    id: str
    total: int
    out_dir: str
    done: int = 0
    copied: int = 0
    compressed: int = 0
    over: int = 0
    failed: int = 0
    bytes_written: int = 0
    state: str = "running"  # running | finished | cancelled | error
    error: str | None = None
    messages: List[str] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "copied": self.copied,
            "compressed": self.compressed,
            "over": self.over,
            "failed": self.failed,
            "bytes": self.bytes_written,
            "size": filterer.human_size(self.bytes_written),
            "outDir": self.out_dir,
            "error": self.error,
            "messages": self.messages[-20:],
        }


@dataclass
class DownloadBundle:
    """A selection resolved for one zip download. The page gets its size first, then
    fetches the zip by token with a plain GET, so the browser's download manager — not
    the tab's memory — receives it."""

    token: str
    ids: List[int]
    generation: int
    count: int
    bytes: int
    created: float = field(default_factory=time.monotonic)
    used: bool = False


@dataclass
class DeletePreview:
    """Binds what the confirmation showed to what gets deleted: the ids are resolved once,
    here, and the start request must repeat their exact count."""

    token: str
    ids: List[int]
    generation: int
    count: int
    trash_enabled: bool | None
    created: float = field(default_factory=time.monotonic)


@dataclass
class DeleteJob:
    """One confirmed delete, moving images to Immich's trash. One at a time."""

    id: str
    total: int
    trash_enabled: bool | None
    state: str = "resolving"  # resolving | trashing | finishing | finished | error
    done: int = 0
    deleted: int = 0
    already: int = 0
    missing: int = 0
    failed: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    started: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    ended: str | None = None
    error: str | None = None
    #: The index revision once the deleted images are out of it; the page re-searches then.
    revision: int | None = None

    @property
    def active(self) -> bool:
        return self.state in ("resolving", "trashing", "finishing")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "active": self.active,
            "total": self.total,
            "done": self.done,
            "deleted": self.deleted,
            "already": self.already,
            "missing": self.missing,
            "failed": self.failed,
            "failures": self.failures[:DELETE_FAILURES_SHOWN],
            "moreFailures": max(0, len(self.failures) - DELETE_FAILURES_SHOWN),
            "trashEnabled": self.trash_enabled,
            "error": self.error,
            "revision": self.revision,
        }


@dataclass
class TagJob:
    """One tagging run (run.TaggingRun) started from the page. One at a time."""

    id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: run.RunProgress = field(default_factory=run.RunProgress)
    #: Mirrors progress.state, except that the final state is only set once the index
    #: rebuild has started — the page reloads its index when it sees the run end.
    state: str = "scanning"

    @property
    def active(self) -> bool:
        return self.state not in ("finished", "cancelled", "error", "refused")

    def as_dict(self) -> Dict[str, Any]:
        p = self.progress
        sync = p.sync.as_dict() if hasattr(p.sync, "as_dict") else p.sync
        return {
            "id": self.id,
            "state": self.state,
            "active": self.active,
            "totalNew": p.total_new,
            "totalRetag": p.total_retag,
            "total": p.total,
            "done": p.done,
            "written": p.written,
            "skipped": p.skipped,
            "failed": p.failed,
            "device": p.device,
            "eta": p.eta_seconds if self.state == "running" and p.done else None,
            "backupId": p.backup_id,
            "sync": sync,
            "error": p.error,
            "messages": p.messages[-20:],
        }


@dataclass
class SyncJob:
    """An Immich sync or tag cleanup, run on a worker thread. One at a time."""

    id: str
    kind: str  # sync | cleanup
    state: str = "running"  # running | finished | error
    phase: str = "starting"
    done: int = 0
    total: int = 0
    result: Dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "state": self.state, "active": self.state == "running",
                "phase": self.phase, "done": self.done, "total": self.total,
                "result": self.result, "error": self.error}


class AppState:
    def __init__(self, root: Path) -> None:
        self.holder = IndexHolder(root, hidden_provider=self._refresh_trash)
        self.jobs: Dict[str, ExportJob] = {}
        self.tag_job: TagJob | None = None
        self.tag_lock = threading.Lock()
        self.thumbs: Dict[Tuple[int, int], bytes] = {}
        self.thumb_lock = threading.Lock()
        self.sync_job: SyncJob | None = None
        #: None = the real model; tests put a stub here.
        self.tagger_factory = None
        self.sync_lock = threading.Lock()
        self.bundles: Dict[str, DownloadBundle] = {}
        self.bundle_lock = threading.Lock()
        self.delete_previews: Dict[str, DeletePreview] = {}
        self.delete_job: DeleteJob | None = None
        self.delete_lock = threading.Lock()
        self._reachable: Tuple[float, bool] = (0.0, False)

    # -- index ---------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        holder = self.holder
        index = holder.index
        return {
            "state": holder.state,
            "root": str(holder.root),
            "scanned": holder.scanned,
            "images": index.live_count if index else 0,
            "revision": index.revision if index else 0,
            "tags": index.tag_count if index else 0,
            "error": holder.error,
            "retag": index.retag_counts() if index else {"legacy": 0, "outdated": 0},
            "model": {"name": vocab.MODEL_REPO, "revision": vocab.MODEL_REVISION[:7]},
            "immich": self.immich_status(),
            "busy": self.busy(),
            "stateDir": str(self._state_dir()),
        }

    def reindex(self, root: Path | None = None) -> None:
        with self.thumb_lock:
            self.thumbs.clear()
        self.holder.start(root)

    def _refresh_trash(self) -> Set[str]:
        """Paths (relative to the library) of the images in Immich's trash, for the index
        build to leave out: asked of Immich when it answers, else what was saved last time.
        Never raises — a failure here must not stop the library from being indexed."""
        root = self.holder.root
        store = TrashSet(config.state_dir(root))
        try:
            settings = config.load_immich_settings()
            if not settings.configured:
                return store.load()
            client = immich.ImmichClient(settings, timeout=10, retry=False)
            path_map = immich.resolve_path_map(client, settings, root)
            if path_map is None:
                return store.load()
            paths = set()
            for _, original in client.iter_trashed():
                local = path_map.to_local(original)
                if local is None:
                    continue
                try:
                    paths.add(local.relative_to(root).as_posix())
                except ValueError:
                    continue  # another library's asset
            store.replace(paths)
            return paths
        except Exception:
            return store.load()

    def busy(self) -> str | None:
        """Which kind of job is running, if any. A delete never overlaps another job."""
        if self.delete_job is not None and self.delete_job.active:
            return "delete"
        if self.tag_job is not None and self.tag_job.active:
            return "tag"
        if self.sync_job is not None and self.sync_job.state == "running":
            return self.sync_job.kind
        if any(job.state == "running" for job in self.jobs.values()):
            return "export"
        return None

    def refuse_during_delete(self) -> None:
        if self.busy() == "delete":
            raise HTTPStatusError(409, "A delete is in progress — wait for it to finish.")

    def check_generation(self, value: Any) -> LibraryIndex:
        """The index `value` names must still be the current one: every rebuild reassigns
        ids, and acting on a stale id would download — or delete — a different image."""
        index = self.holder.require()
        try:
            same = int(value) == index.generation
        except (TypeError, ValueError):
            same = False
        if not same:
            raise HTTPStatusError(409, "The library was rescanned — reload the results.")
        return index

    def resolve_selection(self, payload: Dict[str, Any]) -> List[int]:
        """The ids a selection from the page means (data-model: Selection)."""
        index = self.check_generation(payload.get("generation"))
        mode = payload.get("mode")
        if mode == "ids":
            raw = payload.get("ids") or []
            if len(raw) > MAX_SELECTION_IDS:
                raise ValueError(f"Too many images named at once (over {MAX_SELECTION_IDS:,}).")
            ids: List[int] = []
            seen: set = set()
            for value in raw:
                image_id = int(value)
                if not 0 <= image_id < index.image_count or image_id in index.removed:
                    raise ValueError(f"No image {image_id} in the index — reload the results.")
                if image_id not in seen:
                    seen.add(image_id)
                    ids.append(image_id)
        elif mode == "all":
            query = payload.get("query") or {}
            filters, match_all, exclude = self.parse_filters(query)
            ids = index.order(index.select(filters, match_all, exclude),
                              query.get("order") or "scan", query.get("seed"))
            skip = {int(v) for v in payload.get("except") or []}
            if skip:
                ids = [i for i in ids if i not in skip]
        else:
            raise ValueError("A selection needs mode \"ids\" or \"all\".")
        if not ids:
            raise ValueError("Nothing selected.")
        return ids

    def parse_filters(
        self, payload: Dict[str, Any]
    ) -> Tuple[List[filterer.TagFilter], bool, filterer.TagFilter | None]:
        """Turn the page's rows into the same objects the CLI builds."""
        specs: List[str] = []
        for row in payload.get("filters", []):
            include = [str(t) for t in row.get("include", []) if str(t).strip()]
            exclude = [f"!{t}" for t in row.get("exclude", []) if str(t).strip()]
            spec = ",".join(include + exclude)
            if spec:
                specs.append(spec)
        never = [str(t) for t in payload.get("excludeAll", []) if str(t).strip()]
        return (
            filterer.parse_filters(specs),
            bool(payload.get("matchAll")),
            filterer.parse_exclusions(never),
        )

    def search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        index = self.holder.require()
        filters, match_all, exclude = self.parse_filters(payload)
        ids = index.select(filters, match_all, exclude)
        total = len(ids)

        ids = index.order(ids, payload.get("order") or "scan", payload.get("seed"))
        limit = payload.get("maxImages")
        if isinstance(limit, int) and limit > 0:
            ids = ids[:limit]

        offset = int(payload.get("offset") or 0)
        page = int(payload.get("page") or 60)
        window = ids[offset : offset + page]

        return {
            "generation": index.generation,
            "revision": index.revision,
            "warnings": index.warnings_for(filters, exclude),
            "total": total,
            "selected": len(ids),
            "offset": offset,
            "items": [
                {
                    "id": image_id,
                    "name": index.images[image_id].name,
                    "tags": list(index.tags_of(image_id)),
                }
                for image_id in window
            ],
        }

    # -- thumbnails ----------------------------------------------------------

    def image_path(self, image_id: int, index: LibraryIndex | None = None) -> Path:
        if index is None:
            index = self.holder.require()
        if not 0 <= image_id < index.image_count:
            raise ValueError(f"No image {image_id} in the index — try rescanning.")
        return index.images[image_id]

    def thumbnail(self, image_id: int) -> bytes:
        index = self.holder.require()
        key = (index.generation, image_id)
        with self.thumb_lock:
            cached = self.thumbs.get(key)
        if cached is not None:
            return cached

        with Image.open(self.image_path(image_id, index)) as img:
            img.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))  # cheap JPEG downscale
            img = img.convert("RGB")
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=78)
        data = buffer.getvalue()

        with self.thumb_lock:
            if len(self.thumbs) >= THUMB_CACHE_LIMIT:
                self.thumbs.clear()  # a scroll-back is cheap to rebuild; memory isn't
            self.thumbs[key] = data
        return data

    # -- export --------------------------------------------------------------

    def start_export(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.refuse_during_delete()
        index = self.holder.require()
        filters, match_all, exclude = self.parse_filters(payload)

        out = str(payload.get("outDir") or "").strip()
        if not out:
            raise ValueError("Choose an output folder first.")
        out_dir = Path(out).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path.home() / out_dir).resolve()

        limit_bytes = filterer.parse_size(payload.get("maxSize") or "1MB")
        compress = not payload.get("noCompress")
        copy_sidecar = bool(payload.get("copySidecar"))

        ids = index.order(
            index.select(filters, match_all, exclude),
            payload.get("order") or "scan",
            payload.get("seed"),
        )
        limit = payload.get("maxImages")
        if isinstance(limit, int) and limit > 0:
            ids = ids[:limit]
        if not ids:
            raise ValueError("Nothing matches these filters.")

        out_dir.mkdir(parents=True, exist_ok=True)
        job = ExportJob(id=uuid.uuid4().hex[:12], total=len(ids), out_dir=str(out_dir))
        self.jobs[job.id] = job

        thread = threading.Thread(
            target=self._run_export,
            args=(job, ids, out_dir, limit_bytes, compress, copy_sidecar),
            daemon=True,
        )
        thread.start()
        return job.as_dict()

    def _run_export(
        self,
        job: ExportJob,
        ids: List[int],
        out_dir: Path,
        limit_bytes: int,
        compress: bool,
        copy_sidecar: bool,
    ) -> None:
        index = self.holder.require()
        used_stems: set = set()
        try:
            for image_id in ids:
                if job.cancel.is_set():
                    job.state = "cancelled"
                    return
                match = index.match(image_id)
                try:
                    dest, was_compressed = filterer.export(
                        match=match,
                        out_dir=out_dir,
                        used_stems=used_stems,
                        limit_bytes=limit_bytes,
                        compress=compress,
                        copy_sidecar=copy_sidecar,
                    )
                    size = dest.stat().st_size
                    job.copied += 1
                    job.compressed += int(was_compressed)
                    job.over += int(compress and size > limit_bytes)
                    job.bytes_written += size
                except Exception as exc:
                    job.failed += 1
                    job.messages.append(f"{match.image.name}: {exc}")
                finally:
                    job.done += 1
            job.state = "finished"
        except Exception as exc:
            job.state = "error"
            job.error = str(exc)

    # -- downloads -----------------------------------------------------------

    def create_bundle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        ids = self.resolve_selection(payload)
        index = self.holder.require()
        total = 0
        for image_id in ids:
            try:
                total += index.images[image_id].stat().st_size
            except OSError:
                pass  # listed in the zip's _skipped.txt instead
        bundle = DownloadBundle(token=secrets.token_hex(12), ids=ids, generation=index.generation,
                                count=len(ids), bytes=total)
        now = time.monotonic()
        with self.bundle_lock:
            for token in [t for t, b in self.bundles.items() if now - b.created > BUNDLE_TTL]:
                del self.bundles[token]
            self.bundles[bundle.token] = bundle
        return {
            "token": bundle.token,
            "count": bundle.count,
            "bytes": total,
            "size": filterer.human_size(total),
            "url": f"/api/bundle/{bundle.token}.zip",
            "warn": total > BUNDLE_WARN_BYTES or len(ids) > BUNDLE_WARN_COUNT,
        }

    def take_bundle(self, token: str) -> DownloadBundle:
        with self.bundle_lock:
            bundle = self.bundles.pop(token, None)
        if bundle is None or bundle.used or time.monotonic() - bundle.created > BUNDLE_TTL:
            raise HTTPStatusError(404, "This download link has expired — start the download again.")
        bundle.used = True
        self.check_generation(bundle.generation)
        return bundle

    # -- tagging -------------------------------------------------------------

    def pending_tagging(self) -> Dict[str, Any]:
        """What a tagging run would take on right now: new images (no sidecar) from a quick
        walk, re-tags from the index, which read every sidecar's provenance already."""
        root = self.holder.root
        new = sum(1 for path in library.image_files(root) if not sidecar.sidecar_path(path).exists())
        index = self.holder.index
        retag = index.retag_counts() if index else {"legacy": 0, "outdated": 0}
        current = (index.image_count - retag["legacy"] - retag["outdated"]) if index else 0
        return {"new": new, "legacy": retag["legacy"], "outdated": retag["outdated"], "current": current}

    def tagging(self) -> Dict[str, Any]:
        job = self.tag_job
        return {"job": job.as_dict() if job else None}

    def start_tagging(self) -> Dict[str, Any]:
        self.refuse_during_delete()
        with self.tag_lock:
            job = self.tag_job
            # One run at a time: a second press, or a second tab, attaches to the first.
            if job is None or not job.active:
                job = self.tag_job = TagJob(id=uuid.uuid4().hex[:12])
                thread = threading.Thread(
                    target=self._run_tagging, args=(job, self.holder.root), daemon=True
                )
                thread.start()
        return {"job": job.as_dict()}

    def _run_tagging(self, job: TagJob, root: Path) -> None:
        def on_progress(progress: run.RunProgress) -> None:
            if progress.active:
                job.state = progress.state

        state_dir = self._state_dir()
        tagging = run.TaggingRun(
            root, run.RunOptions(), on_progress=on_progress, cancel=job.cancel, state_dir=state_dir,
            sync_hook=run.immich_sync_hook(root, state_dir),
            **({"tagger_factory": self.tagger_factory} if self.tagger_factory else {}),
        )
        job.progress = tagging.progress
        progress = tagging.run()

        if progress.written:
            # The new sidecars are invisible until the index is rebuilt. A rebuild already
            # under way may have walked past them, so let it finish, then start another.
            self.holder.ready.wait()
            self.reindex()
        # Only now: the page reloads its index when it sees the run end, and must not
        # catch the old index still marked ready.
        job.state = progress.state

    # -- immich --------------------------------------------------------------

    def _state_dir(self) -> Path:
        return config.state_dir(self.holder.root)

    def immich_status(self) -> Dict[str, Any]:
        settings = config.load_immich_settings()
        pending = len(library.PendingSync(self._state_dir(), self.holder.root))
        if not settings.configured:
            return {"configured": False, "reachable": False, "pending": pending,
                    "canDelete": False, "deleteReason": "No Immich API key configured (see the README)."}
        checked, reachable = self._reachable
        if time.monotonic() - checked > 30:  # status is polled often; Immich need not be
            try:
                immich.ImmichClient(settings, timeout=3).version()
                reachable = True
            except immich.ImmichError:
                reachable = False
            self._reachable = (time.monotonic(), reachable)
        return {"configured": True, "reachable": reachable, "pending": pending, "canDelete": reachable,
                "deleteReason": None if reachable else "Immich is not reachable."}

    def _in_use(self) -> set:
        """Managed tags the library uses, from the index rather than 100k file reads."""
        index = self.holder.require()
        return immich.in_use_from_tags(tag for tags in index.display_tags for tag in tags)

    def sync_status(self) -> Dict[str, Any]:
        job = self.sync_job
        return {"job": job.as_dict() if job else None}

    def _start_job(self, kind: str, target) -> Dict[str, Any]:
        self.refuse_during_delete()
        settings = config.load_immich_settings()
        if not settings.configured:
            raise ValueError("No Immich API key configured (see the README: [immich] in config.toml).")
        with self.sync_lock:
            job = self.sync_job
            if job is None or job.state != "running":
                job = self.sync_job = SyncJob(id=uuid.uuid4().hex[:12], kind=kind)
                threading.Thread(target=self._run_job, args=(job, target, settings), daemon=True).start()
        return {"job": job.as_dict()}

    def _run_job(self, job: SyncJob, target, settings) -> None:
        def progress(phase: str, done: int = 0, total: int = 0) -> None:
            job.phase, job.done, job.total = phase, done, total
        try:
            job.result = target(settings, progress)
            ok = job.result.get("ok", True)
            job.error = None if ok else job.result.get("reason")
            job.state = "finished" if ok else "error"
        except Exception as exc:
            job.error = str(exc)
            job.state = "error"

    def start_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        full = bool(payload.get("all"))
        root, state_dir = self.holder.root, self._state_dir()

        def target(settings, progress):
            result = immich.sync(root, settings, state_dir, all=full, on_progress=progress, in_use=self._in_use)
            return result.as_dict()

        return self._start_job("sync", target)

    def cleanup_preview(self) -> Dict[str, Any]:
        settings = config.load_immich_settings()
        if not settings.configured:
            raise ValueError("No Immich API key configured.")
        client = immich.ImmichClient(settings)
        plan = immich.plan_cleanup(client.list_tags(), self._in_use())
        preview = immich.run_cleanup(client, plan, dry_run=True)
        return {"count": preview.count, "byPrefix": preview.by_prefix, "examples": preview.examples}

    def start_cleanup(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("confirm") is not True:
            raise ValueError("Deleting Immich tags needs {\"confirm\": true} — load the preview first.")

        def target(settings, progress):
            client = immich.ImmichClient(settings)
            progress("planning")
            plan = immich.plan_cleanup(client.list_tags(), self._in_use())
            result = immich.run_cleanup(client, plan, on_progress=lambda d, t: progress("cleanup", d, t))
            return {"ok": True, "deletedTags": result.deleted, "keptInUse": result.skipped_nonempty,
                    "count": result.count}

        return self._start_job("cleanup", target)

    # -- delete --------------------------------------------------------------

    def delete_preview(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        status = self.immich_status()
        if not status["canDelete"]:
            raise ValueError(status["deleteReason"])
        busy = self.busy()
        if busy:
            raise HTTPStatusError(409, f"Wait for the running {busy} to finish.")
        ids = self.resolve_selection(payload)
        index = self.holder.require()
        try:
            client = immich.ImmichClient(config.load_immich_settings(), timeout=5, retry=False)
            trash_enabled = client.features().get("trash")
            trash_enabled = trash_enabled if isinstance(trash_enabled, bool) else None
        except immich.ImmichError:
            trash_enabled = None
        preview = DeletePreview(token=secrets.token_hex(12), ids=ids, generation=index.generation,
                                count=len(ids), trash_enabled=trash_enabled)
        now = time.monotonic()
        with self.delete_lock:
            for token in [t for t, p in self.delete_previews.items() if now - p.created > DELETE_PREVIEW_TTL]:
                del self.delete_previews[token]
            self.delete_previews[preview.token] = preview
        return {
            "token": preview.token,
            "count": preview.count,
            "sample": [{"id": i, "name": index.images[i].name, "generation": index.generation}
                       for i in ids[:DELETE_SAMPLE]],
            "trashEnabled": trash_enabled,
            "typeToConfirm": preview.count > DELETE_TYPE_THRESHOLD or trash_enabled is not True,
        }

    def delete_status(self) -> Dict[str, Any]:
        job = self.delete_job
        return {"job": job.as_dict() if job else None}

    def start_delete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.delete_lock:
            job = self.delete_job
            if job is not None and job.active:
                return {"job": job.as_dict()}  # a second press attaches to the first
            preview = self.delete_previews.pop(str(payload.get("token") or ""), None)
            if preview is None or time.monotonic() - preview.created > DELETE_PREVIEW_TTL:
                raise ValueError("This confirmation expired — try again.")
            confirm = payload.get("confirm")
            if isinstance(confirm, bool) or not isinstance(confirm, int) or confirm != preview.count:
                raise ValueError("Confirm the number of images to delete.")
            self.check_generation(preview.generation)
            busy = self.busy()
            if busy:
                raise HTTPStatusError(409, f"Wait for the running {busy} to finish.")
            # The same lock a tagging run takes, so a run from the terminal and a delete
            # can never overlap; released by the worker when it is done.
            lock = library.TaggingLock(self._state_dir())
            try:
                lock.acquire()
            except library.LockBusy as exc:
                raise HTTPStatusError(409, str(exc)) from None
            job = self.delete_job = DeleteJob(id=uuid.uuid4().hex[:12], total=preview.count,
                                              trash_enabled=preview.trash_enabled)
            threading.Thread(target=self._run_delete, args=(job, preview.ids, lock), daemon=True).start()
        return {"job": job.as_dict()}

    def _run_delete(self, job: DeleteJob, ids: List[int], lock: library.TaggingLock) -> None:
        """Resolve each image to its Immich asset, trash them 500 at a time, then take the
        trashed ones out of the index, the pending sync and future rescans."""
        index = self.holder.require()
        root = index.root
        records: Dict[int, Dict[str, Any]] = {}

        def record(image_id: int, outcome: str, asset_id: str | None = None, reason: str | None = None) -> None:
            path = index.images[image_id]
            records[image_id] = {"path": path.relative_to(root).as_posix(), "assetId": asset_id,
                                 "outcome": outcome, "reason": reason}
            setattr(job, outcome, getattr(job, outcome) + 1)
            if outcome in ("missing", "failed"):
                job.failures.append({"name": path.name, "reason": reason or outcome})
            job.done += 1

        try:
            settings = config.load_immich_settings()
            client = immich.ImmichClient(settings)
            path_map = immich.resolve_path_map(client, settings, root)
            if path_map is None:
                raise immich.ImmichError("cannot match Immich's paths to this library; set [immich].path_map")
            remote: Dict[int, str] = {}
            for image_id in ids:
                try:
                    remote[image_id] = path_map.to_remote(index.images[image_id])
                except ValueError:
                    record(image_id, "missing", reason="outside the path Immich stores")
            found = client.assets_by_path(remote.values())

            queue: List[Tuple[int, str]] = []
            for image_id, path in remote.items():
                hit = found.get(path)
                if hit is None:
                    record(image_id, "missing", reason="not in Immich")
                elif hit[1]:
                    record(image_id, "already", asset_id=hit[0])
                else:
                    queue.append((image_id, hit[0]))

            job.state = "trashing"
            batch = immich.ImmichClient.JOB_BATCH
            for start in range(0, len(queue), batch):
                chunk = queue[start:start + batch]
                asset_ids = [asset_id for _, asset_id in chunk]
                try:
                    client.trash(asset_ids)
                    outcome: Dict[str, str | None] = dict.fromkeys(asset_ids)
                except (immich.ImmichAuthError, immich.ImmichUnavailable):
                    raise
                except immich.ImmichError:
                    outcome = client.trash_each(asset_ids)  # find the one that spoiled the batch
                for image_id, asset_id in chunk:
                    reason = outcome.get(asset_id)
                    record(image_id, "failed" if reason else "deleted", asset_id=asset_id, reason=reason)
        except Exception as exc:
            if not isinstance(exc, immich.ImmichError):
                traceback.print_exc()
            job.error = str(exc) or exc.__class__.__name__
            for image_id in ids:
                if image_id not in records:
                    record(image_id, "failed", reason=f"not attempted: {job.error}")
        finally:
            job.state = "finishing"
            try:
                self._after_delete(job, index, records)
            except Exception as exc:
                traceback.print_exc()
                job.error = job.error or f"trashed in Immich, but updating immichtag failed: {exc}"
            finally:
                lock.release()
                job.ended = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                job.state = "error" if job.error else "finished"

    def _after_delete(self, job: DeleteJob, index: LibraryIndex, records: Dict[int, Dict[str, Any]]) -> None:
        """Everything that must follow what Immich actually trashed — even when the job
        stopped part way, for the batches that did go through."""
        state_dir = config.state_dir(index.root)
        gone = {image_id: r["path"] for image_id, r in records.items() if r["outcome"] in ("deleted", "already")}
        if gone:
            TrashSet(state_dir).add(gone.values())
            index.remove(gone)
            library.PendingSync(state_dir, index.root).remove(gone.values())
            with self.thumb_lock:
                for image_id in gone:
                    self.thumbs.pop((index.generation, image_id), None)
        DeletionLog(state_dir).append(job.id, list(records.values()), job.trash_enabled)
        job.revision = index.revision

    # -- misc ----------------------------------------------------------------

    def pick_folder(self) -> Dict[str, Any]:
        """Native folder chooser. Only macOS has one we can shell out to."""
        if platform.system() != "Darwin":
            return {"path": None, "reason": "Folder picker is macOS-only; type the path."}
        script = 'POSIX path of (choose folder with prompt "Output folder")'
        try:
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=300
            )
        except Exception as exc:
            return {"path": None, "reason": str(exc)}
        if result.returncode != 0:
            return {"path": None, "reason": None}  # user cancelled
        return {"path": result.stdout.strip().rstrip("/")}

    def reveal(self, path: str) -> None:
        system = platform.system()
        opener = {"Darwin": ["open"], "Windows": ["explorer"], "Linux": ["xdg-open"]}
        if system in opener:
            subprocess.Popen(opener[system] + [path])


def make_handler(state: AppState) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "immichtag-web"
        protocol_version = "HTTP/1.1"

        # -- plumbing --------------------------------------------------------

        def log_message(self, *args) -> None:  # noqa: D102 - silence per-request noise
            pass

        def _send(self, code: int, body: bytes, content_type: str, cache: str | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"), "application/json", "no-store")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        def _static(self, name: str, content_type: str) -> None:
            path = STATIC_DIR / name
            if not path.is_file():
                self._json({"error": f"missing asset {name}"}, 404)
                return
            self._send(200, path.read_bytes(), content_type, "no-store")

        # -- routes ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    self._static("index.html", "text/html; charset=utf-8")
                elif url.path == "/app.js":
                    self._static("app.js", "text/javascript; charset=utf-8")
                elif url.path == "/app.css":
                    self._static("app.css", "text/css; charset=utf-8")
                elif url.path == "/api/status":
                    self._json(state.status())
                elif url.path == "/api/tags":
                    self._json(self._tags(query))
                elif url.path == "/api/thumb":
                    image_id = int(query.get("id", ["-1"])[0])
                    self._send(200, state.thumbnail(image_id), "image/jpeg", "max-age=3600")
                elif url.path == "/api/image":
                    self._image(int(query.get("id", ["-1"])[0]))
                elif url.path == "/api/download":
                    self._download(query)
                elif url.path.startswith("/api/bundle/") and url.path.endswith(".zip"):
                    token = url.path[len("/api/bundle/"):-len(".zip")]
                    self._stream_bundle(state.take_bundle(token))
                elif url.path == "/api/pending-tagging":
                    self._json(state.pending_tagging())
                elif url.path == "/api/tag":
                    self._json(state.tagging())
                elif url.path == "/api/immich/sync":
                    self._json(state.sync_status())
                elif url.path == "/api/delete":
                    self._json(state.delete_status())
                elif url.path == "/api/immich/cleanup-preview":
                    self._json(state.cleanup_preview())
                elif url.path.startswith("/api/export/"):
                    job = state.jobs.get(url.path.rsplit("/", 1)[-1])
                    self._json(job.as_dict() if job else {"error": "no such job"}, 200 if job else 404)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._fail(exc)

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            try:
                payload = self._body()
                if url.path == "/api/search":
                    self._json(state.search(payload))
                elif url.path == "/api/export":
                    self._json(state.start_export(payload))
                elif url.path == "/api/bundle":
                    self._json(state.create_bundle(payload))
                elif url.path in ("/api/delete/preview", "/api/delete"):
                    # JSON only: a cross-site form can post text/plain without a CORS
                    # preflight; it can never send application/json.
                    kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if kind != "application/json":
                        raise HTTPStatusError(415, "JSON required")
                    if url.path == "/api/delete/preview":
                        self._json(state.delete_preview(payload))
                    else:
                        self._json(state.start_delete(payload))
                elif url.path == "/api/pick-folder":
                    self._json(state.pick_folder())
                elif url.path == "/api/reveal":
                    state.reveal(str(payload.get("path") or ""))
                    self._json({"ok": True})
                elif url.path == "/api/reindex":
                    state.refuse_during_delete()
                    root = str(payload.get("root") or "").strip()
                    new_root = Path(root).expanduser() if root else state.holder.root
                    if not new_root.is_dir():
                        raise ValueError(f"Not a directory: {new_root}")
                    state.reindex(new_root)
                    self._json(state.status())
                elif url.path == "/api/tag":
                    self._json(state.start_tagging())
                elif url.path == "/api/immich/sync":
                    self._json(state.start_sync(payload))
                elif url.path == "/api/immich/cleanup":
                    self._json(state.start_cleanup(payload))
                elif url.path == "/api/tag/cancel":
                    job = state.tag_job
                    if job:
                        job.cancel.set()
                    self._json({"ok": bool(job)})
                elif url.path.startswith("/api/cancel/"):
                    job = state.jobs.get(url.path.rsplit("/", 1)[-1])
                    if job:
                        job.cancel.set()
                    self._json({"ok": bool(job)})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._fail(exc)

        # -- helpers ---------------------------------------------------------

        def _tags(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
            index = state.holder.require()
            needle = filterer.normalize(query.get("q", [""])[0])
            limit = int(query.get("limit", ["30"])[0])
            pairs = index.tag_counts()
            if needle:
                starts = [p for p in pairs if p[0].rsplit("/", 1)[-1].startswith(needle)]
                contains = [p for p in pairs if needle in p[0] and p not in starts]
                pairs = starts + contains
            return {"tags": [{"tag": tag, "count": count} for tag, count in pairs[:limit]]}

        def _image(self, image_id: int) -> None:
            path = state.image_path(image_id)
            content_type = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
            self._send(200, path.read_bytes(), content_type, "max-age=3600")

        def _download(self, query: Dict[str, List[str]]) -> None:
            index = state.check_generation(query.get("v", [""])[0])
            image_id = int(query.get("id", ["-1"])[0])
            if not 0 <= image_id < index.image_count or image_id in index.removed:
                raise HTTPStatusError(404, "That image is no longer in the results — reload them.")
            path = index.images[image_id]
            self._stream_file(path, MIME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                              {"Content-Disposition": content_disposition(path.name),
                               "Cache-Control": "no-store"})

        def _stream_bundle(self, bundle: DownloadBundle) -> None:
            """The selection as a STORED zip, written straight to the socket. zipfile copes
            with a stream it cannot seek (data descriptors, ZIP64 past 4 GiB), so memory
            stays at one chunk however large the selection; the end of the body is the
            end of the connection."""
            index = state.holder.require()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             content_disposition(f"immichtag-{bundle.count}-{stamp}.zip"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            used: set = set()
            skipped: List[str] = []
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True,
                                 strict_timestamps=False) as zf:
                for image_id in bundle.ids:
                    path = index.images[image_id]
                    try:
                        handle = path.open("rb")
                    except OSError as exc:
                        skipped.append(f"{path.name}: {exc.strerror or exc}")
                        continue
                    with handle:
                        size = os.fstat(handle.fileno()).st_size
                        name = filterer.unique_stem(path.stem, used) + path.suffix
                        info = zipfile.ZipInfo.from_file(path, arcname=name, strict_timestamps=False)
                        info.compress_type = zipfile.ZIP_STORED
                        with zf.open(info, "w", force_zip64=size > 2**31) as entry:
                            while True:
                                chunk = handle.read(CHUNK)
                                if not chunk:
                                    break
                                entry.write(chunk)
                if skipped:
                    zf.writestr("_skipped.txt", "\n".join(skipped) + "\n")

        def _stream_file(self, path: Path, content_type: str, headers: Dict[str, str]) -> None:
            """Send a file in CHUNK pieces. A missing file is a 404 before any header."""
            try:
                handle = path.open("rb")
            except FileNotFoundError:
                raise HTTPStatusError(404, f"{path.name} is no longer on disk") from None
            with handle:
                size = os.fstat(handle.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command == "HEAD":
                    return
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

        def _fail(self, exc: Exception) -> None:
            message = str(exc) or exc.__class__.__name__
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return  # the browser went away mid-download; nothing to tell it
            if not isinstance(exc, (ValueError, RuntimeError, KeyError, IndexError, OSError,
                                    immich.ImmichError, HTTPStatusError)):
                traceback.print_exc()
            try:
                self._json({"error": message}, getattr(exc, "status", 400))
            except Exception:
                pass

    return Handler


def port_holder(host: str, port: int) -> str | None:
    """Who is listening on this port? Best-effort, for the error message only."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP@{host}:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    lines = result.stdout.splitlines()[1:]
    if not lines:
        return None
    fields = lines[0].split()
    return f"{fields[0]} (pid {fields[1]})" if len(fields) > 1 else None


def bind(host: str, port: int, handler: type, search: int = 0) -> ThreadingHTTPServer:
    """Bind `port`, or the next free one when `search` allows moving.

    A port the user asked for explicitly is never silently swapped — that would start
    the server somewhere they aren't looking. Only the default may drift.
    """
    for candidate in range(port, port + search + 1):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or candidate == port + search:
                break

    holder = port_holder(host, port)
    detail = f" — held by {holder}" if holder else ""
    if search:
        raise SystemExit(
            f"Ports {port}-{port + search} on {host} are all in use{detail}.\n"
            f"Free one, or pick another with:  uv run immichtag-web --port 9000"
        )
    raise SystemExit(
        f"Port {port} on {host} is already in use{detail}.\n"
        f"Pick another with:  uv run immichtag-web --port {port + 1}"
    )


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    allow_other_port: bool = False,
) -> int:
    state = AppState(root)
    state.holder.start()

    httpd = bind(host, port, make_handler(state), PORT_SEARCH if allow_other_port else 0)
    if httpd.server_address[1] != port:
        print(f"Port {port} was busy, using {httpd.server_address[1]} instead.")
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"immichtag-web serving {root}")
    print(f"  {url}   (ctrl-c to stop)")

    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0
