"""A local web UI for the tag filter.

`uv run animetag-web` builds the sidecar index once, serves a page on 127.0.0.1 and opens
it. Everything the CLI takes as flags is a control on that page, plus the two things a
terminal cannot give you: thumbnails of what a filter actually selects, and a match
count that updates as you type.

The page can also run the tagger over images that have no sidecar yet — the same
selection and thresholds as `uv run animetag` — and rebuilds the index when it is done, so
new uploads go from untagged to filterable without a terminal.

Stdlib http.server on purpose — this is a personal tool on loopback, and a web
framework would be a heavier dependency than the whole tagger.
"""

from __future__ import annotations

import errno
import io
import json
import platform
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

from PIL import Image

from animetag import config, filterer, immich, library, run, sidecar
from animetag.index import IndexHolder, LibraryIndex

STATIC_DIR = Path(__file__).parent / "web"
#: Nothing well-known lives here. Still collidable, which is why serve() can move.
DEFAULT_PORT = 8788
#: How many ports past the default to try before giving up.
PORT_SEARCH = 20
THUMB_SIZE = 360
THUMB_CACHE_LIMIT = 600


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
class TagJob:
    """One run of the tagger over the images with no sidecar yet. One at a time."""

    id: str
    total: int = 0
    done: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    state: str = "scanning"  # scanning | loading | running | finished | cancelled | error
    error: str | None = None
    #: time.monotonic() when inference began, for the time-left estimate.
    started: float | None = None
    messages: List[str] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def active(self) -> bool:
        return self.state not in ("finished", "cancelled", "error", "refused")

    def as_dict(self) -> Dict[str, Any]:
        eta = None
        if self.state == "running" and self.started is not None and self.done:
            elapsed = time.monotonic() - self.started
            eta = round(elapsed / self.done * (self.total - self.done))
        return {
            "id": self.id,
            "state": self.state,
            "active": self.active,
            "total": self.total,
            "done": self.done,
            "written": self.written,
            "skipped": self.skipped,
            "failed": self.failed,
            "eta": eta,
            "error": self.error,
            "messages": self.messages[-20:],
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
        self.holder = IndexHolder(root)
        self.jobs: Dict[str, ExportJob] = {}
        self.tag_job: TagJob | None = None
        self.tag_lock = threading.Lock()
        self.thumbs: Dict[Tuple[int, int], bytes] = {}
        self.thumb_lock = threading.Lock()
        self.sync_job: SyncJob | None = None
        self.sync_lock = threading.Lock()
        self._reachable: Tuple[float, bool] = (0.0, False)

    # -- index ---------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        holder = self.holder
        index = holder.index
        return {
            "state": holder.state,
            "root": str(holder.root),
            "scanned": holder.scanned,
            "images": index.image_count if index else 0,
            "tags": index.tag_count if index else 0,
            "error": holder.error,
            "immich": self.immich_status(),
        }

    def reindex(self, root: Path | None = None) -> None:
        with self.thumb_lock:
            self.thumbs.clear()
        self.holder.start(root)

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

    # -- tagging -------------------------------------------------------------

    def untagged_count(self) -> Dict[str, Any]:
        """How many images a tagging run would take on right now."""
        root = self.holder.root
        return {"count": sum(1 for path in library.image_files(root)
                             if not sidecar.sidecar_path(path).exists())}

    def tagging(self) -> Dict[str, Any]:
        job = self.tag_job
        return {"job": job.as_dict() if job else None}

    def start_tagging(self) -> Dict[str, Any]:
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
            job.total = progress.total
            job.done = progress.done
            job.written = progress.written
            job.skipped = progress.skipped
            job.failed = progress.failed
            job.messages = progress.messages
            job.error = progress.error
            if progress.state == "running" and job.started is None:
                job.started = time.monotonic()
            # Final states are set below, after the index rebuild has been started.
            if progress.active:
                job.state = progress.state

        tagging = run.TaggingRun(root, run.RunOptions(), on_progress=on_progress, cancel=job.cancel)
        progress = tagging.run()
        on_progress(progress)

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
            return {"configured": False, "reachable": False, "pending": pending}
        checked, reachable = self._reachable
        if time.monotonic() - checked > 30:  # status is polled often; Immich need not be
            try:
                immich.ImmichClient(settings, timeout=3).version()
                reachable = True
            except immich.ImmichError:
                reachable = False
            self._reachable = (time.monotonic(), reachable)
        return {"configured": True, "reachable": reachable, "pending": pending}

    def _in_use(self) -> set:
        """Managed tags the library uses, from the index rather than 100k file reads."""
        index = self.holder.require()
        return immich.in_use_from_tags(tag for tags in index.display_tags for tag in tags)

    def sync_status(self) -> Dict[str, Any]:
        job = self.sync_job
        return {"job": job.as_dict() if job else None}

    def _start_job(self, kind: str, target) -> Dict[str, Any]:
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
        server_version = "animetag-web"
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
                elif url.path == "/api/untagged":
                    self._json(state.untagged_count())
                elif url.path == "/api/tag":
                    self._json(state.tagging())
                elif url.path == "/api/immich/sync":
                    self._json(state.sync_status())
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
                elif url.path == "/api/pick-folder":
                    self._json(state.pick_folder())
                elif url.path == "/api/reveal":
                    state.reveal(str(payload.get("path") or ""))
                    self._json({"ok": True})
                elif url.path == "/api/reindex":
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
            kinds = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif",
            }
            content_type = kinds.get(path.suffix.lower(), "application/octet-stream")
            self._send(200, path.read_bytes(), content_type, "max-age=3600")

        def _fail(self, exc: Exception) -> None:
            message = str(exc) or exc.__class__.__name__
            if not isinstance(exc, (ValueError, RuntimeError, KeyError, IndexError, OSError, immich.ImmichError)):
                traceback.print_exc()
            try:
                self._json({"error": message}, 400)
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
            f"Free one, or pick another with:  uv run animetag-web --port 9000"
        )
    raise SystemExit(
        f"Port {port} on {host} is already in use{detail}.\n"
        f"Pick another with:  uv run animetag-web --port {port + 1}"
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
    print(f"animetag-web serving {root}")
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
