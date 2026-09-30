"""Keeping Immich in step with the sidecars, through Immich's own API.

After a run rewrites sidecars, Immich still shows the old tags until it re-reads them;
tags no asset uses any more stay in its tag list forever. This module asks Immich to
re-read exactly the images that changed and then deletes the empty tags this tool owns.

Everything here was checked against the Immich 3.2.2 server source
(specs/001-pixai-full-retag/research.md R8/R9, contracts/immich-api.md). Two rules
matter most:

- Immich's own "tag cleanup" job deletes empty tags under *every* prefix, so it is never
  used; only managed prefixes are considered, and each tag is re-checked empty first.
- Deleting a tag cascades to its children, so tags are deleted deepest first and a
  parent is never deleted while anything under it is still in use.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from immichtag import filterer, library, sidecar, vocab
from immichtag.config import ImmichSettings

DISCOVERY_HINT = "discovery needed: Immich → Administration → Jobs → Sidecar metadata → Discover"

#: What each endpoint needs, for the error message when the key lacks it.
PERMISSIONS = {
    "/api/search/metadata": "asset.read",
    "/api/assets/jobs": "job.create + asset.update",
    "/api/assets": "asset.delete",
    "/api/jobs/sidecar": "job.create (admin key)",
    "/api/jobs": "job.read (admin key)",
    "/api/tags": "tag.read / tag.delete",
}


class ImmichError(Exception):
    pass


class ImmichUnavailable(ImmichError):
    """Connection refused, timed out, or kept failing with 5xx."""


class ImmichAuthError(ImmichError):
    def __init__(self, method: str, path: str, status: int) -> None:
        base = "/api/tags" if path.startswith("/api/tags") else path
        need = PERMISSIONS.get(base, "an admin key")
        super().__init__(f"Immich refused {method} {base} (HTTP {status}): the API key needs {need}")


class ImmichNotFound(ImmichError):
    pass


# -- client -------------------------------------------------------------------------


class ImmichClient:
    RETRY_DELAYS = (1, 4, 16)
    PAGE_SIZE = 1000
    JOB_BATCH = 500

    def __init__(self, settings: ImmichSettings, *, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 30, retry: bool = True) -> None:
        """`retry=False` gives up at the first connection error or 5xx — for callers that
        have a fallback and should not stall ~20 s on an Immich that is down."""
        if not settings.api_key:
            raise ImmichError("no Immich API key configured")
        self._url = settings.url.rstrip("/")
        self._key = settings.api_key
        self._sleep = sleep
        self._timeout = timeout
        self._delays = self.RETRY_DELAYS if retry else ()
        self._legacy_search = False

    def __repr__(self) -> str:  # never show the key
        return f"ImmichClient({self._url!r})"

    def _request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "x-api-key": self._key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        last = "no attempt"
        for attempt in range(len(self._delays) + 1):
            if attempt:
                self._sleep(self._delays[attempt - 1])
            request = urllib.request.Request(self._url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise ImmichAuthError(method, path, exc.code) from None
                if exc.code == 404:
                    raise ImmichNotFound(f"{method} {path}: HTTP 404") from None
                if exc.code >= 500:
                    last = f"HTTP {exc.code}"
                    continue
                detail = exc.read()[:300].decode("utf-8", "replace")
                raise ImmichError(f"{method} {path}: HTTP {exc.code} {detail}") from None
            except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError) as exc:
                last = str(getattr(exc, "reason", exc))
                continue
        raise ImmichUnavailable(f"Immich at {self._url} is not reachable ({last})")

    # -- endpoints -------------------------------------------------------------

    def version(self) -> Tuple[int, int, int]:
        data = self._request("GET", "/api/server/version")
        return int(data["major"]), int(data["minor"]), int(data["patch"])

    def _search(self, filter_: dict, size: int, cursor: str | None, page: int, legacy: dict) -> dict:
        """One page of POST /api/search/metadata. Uses the v3.2 filter/cursor shape, which
        includes trashed assets; falls back to the deprecated flat fields on older servers."""
        if not self._legacy_search:
            body = {"filter": filter_, "size": size}
            if cursor:
                body["cursor"] = cursor
            try:
                return self._request("POST", "/api/search/metadata", body)["assets"]
            except ImmichError as exc:
                if isinstance(exc, (ImmichAuthError, ImmichUnavailable)) or "HTTP 400" not in str(exc):
                    raise
                self._legacy_search = True
        body = {"size": size, "page": page, "withDeleted": True, **legacy}
        return self._request("POST", "/api/search/metadata", body)["assets"]

    def iter_assets(self) -> Iterator[Tuple[str, str]]:
        """(id, originalPath) of every asset of the key's user, trashed ones included."""
        cursor, page = None, 1
        while True:
            assets = self._search({}, self.PAGE_SIZE, cursor, page, {})
            for item in assets["items"]:
                yield item["id"], item["originalPath"]
            cursor = assets.get("nextCursor")
            next_page = assets.get("nextPage")
            if self._legacy_search:
                if not next_page:
                    return
                page = int(next_page)
            elif not cursor:
                return

    def assets_by_path(self, remote_paths: Iterable[str]) -> Dict[str, Tuple[str, bool]]:
        """originalPath -> (id, is trashed) for the given paths; missing paths are absent.
        Looked up 500 at a time; an older server that rejects the filter shape gets one
        full listing instead (trashed state then unknown, reported as False)."""
        paths = list(dict.fromkeys(remote_paths))
        found: Dict[str, Tuple[str, bool]] = {}
        if not self._legacy_search:
            try:
                for start in range(0, len(paths), self.JOB_BATCH):
                    chunk = paths[start:start + self.JOB_BATCH]
                    body = {"filter": {"originalPath": {"in": chunk}}, "size": self.JOB_BATCH}
                    for item in self._request("POST", "/api/search/metadata", body)["assets"]["items"]:
                        found[item["originalPath"]] = (item["id"], bool(item.get("isTrashed")))
                return found
            except ImmichError as exc:
                if isinstance(exc, (ImmichAuthError, ImmichUnavailable)) or "HTTP 400" not in str(exc):
                    raise
                self._legacy_search = True
        wanted = set(paths)
        for asset_id, original in self.iter_assets():
            if original in wanted:
                found[original] = (asset_id, False)
        return found

    def iter_trashed(self) -> Iterator[Tuple[str, str]]:
        """(id, originalPath) of every asset in the trash. v3.2 search shape only: an
        older server raises, and the caller keeps what it knew before."""
        if self._legacy_search:
            raise ImmichError("this Immich does not support listing the trash")
        cursor = None
        while True:
            body = {"filter": {"trashedAt": {"ne": None}}, "size": self.PAGE_SIZE}
            if cursor:
                body["cursor"] = cursor
            assets = self._request("POST", "/api/search/metadata", body)["assets"]
            for item in assets["items"]:
                yield item["id"], item["originalPath"]
            cursor = assets.get("nextCursor")
            if not cursor:
                return

    def features(self) -> dict:
        """Server feature flags; `trash` says whether deleted assets go to a trash."""
        return self._request("GET", "/api/server/features") or {}

    def trash(self, ids: List[str]) -> None:
        """Move assets to Immich's trash — never `force`, which deletes for good. Immich
        checks access to every id first, so one bad id fails its whole batch."""
        for start in range(0, len(ids), self.JOB_BATCH):
            self._request("DELETE", "/api/assets", {"ids": ids[start:start + self.JOB_BATCH], "force": False})

    def trash_each(self, ids: List[str]) -> Dict[str, str | None]:
        """One request per id, for a batch that failed: id -> None, or why it failed."""
        outcome: Dict[str, str | None] = {}
        for asset_id in ids:
            try:
                self.trash([asset_id])
                outcome[asset_id] = None
            except (ImmichAuthError, ImmichUnavailable):
                raise
            except ImmichError as exc:
                outcome[asset_id] = str(exc)
        return outcome

    def tag_has_assets(self, tag_id: str) -> bool:
        """Does any asset carry this tag or a tag below it? (Immich matches descendants.)"""
        assets = self._search({"tagIds": {"any": [tag_id]}}, 1, None, 1, {"tagIds": [tag_id]})
        return bool(assets["items"])

    def run_asset_job(self, ids: List[str], name: str = "refresh-metadata") -> None:
        for start in range(0, len(ids), self.JOB_BATCH):
            self._request("POST", "/api/assets/jobs", {"assetIds": ids[start:start + self.JOB_BATCH], "name": name})

    def start_sidecar_discovery(self, force: bool = False) -> bool:
        """Queue Immich's sidecar check for assets whose sidecar it has not registered.
        False when this Immich no longer has the (deprecated) endpoint."""
        try:
            self._request("PUT", "/api/jobs/sidecar", {"command": "start", "force": force})
        except ImmichNotFound:
            return False
        return True

    def queue_counts(self) -> Dict[str, int]:
        data = self._request("GET", "/api/jobs") or {}
        counts = {}
        for name, queue in data.items():
            jobs = queue.get("jobCounts", {})
            counts[name] = int(jobs.get("active", 0)) + int(jobs.get("waiting", 0))
        return counts

    def wait_idle(self, queues=("sidecar", "metadataExtraction"), timeout: float = 7200,
                  poll: float = 5, on_progress: Callable[[int], None] | None = None) -> bool:
        """Wait until Immich has worked through the queues. False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            counts = self.queue_counts()
            outstanding = sum(counts.get(q, 0) for q in queues)
            if on_progress:
                on_progress(outstanding)
            if outstanding == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def asset_tags(self, asset_id: str) -> List[str]:
        """The full tag values Immich has on one asset."""
        data = self._request("GET", f"/api/assets/{asset_id}") or {}
        return [tag["value"] for tag in data.get("tags") or []]

    def list_tags(self) -> List[dict]:
        return self._request("GET", "/api/tags") or []

    def delete_tag(self, tag_id: str) -> None:
        try:
            self._request("DELETE", f"/api/tags/{tag_id}")
        except ImmichNotFound:
            pass  # already gone


# -- paths --------------------------------------------------------------------------


@dataclass(frozen=True)
class PathMap:
    """Immich's view of a path (inside its container) <-> this machine's view."""

    remote: str
    local: Path

    @classmethod
    def parse(cls, text: str) -> "PathMap":
        remote, sep, local = text.partition("=")
        if not sep or not remote.strip() or not local.strip():
            raise ValueError(f"path_map must look like /data=/local/path, got {text!r}")
        return cls(remote=remote.strip().rstrip("/") or "/", local=Path(local.strip()).expanduser())

    def to_local(self, original_path: str) -> Path | None:
        prefix = self.remote.rstrip("/") + "/"
        if not original_path.startswith(prefix):
            return None
        return self.local / PurePosixPath(original_path[len(prefix):])

    def to_remote(self, local_path: Path) -> str:
        rel = Path(local_path).relative_to(self.local)
        return self.remote.rstrip("/") + "/" + PurePosixPath(rel).as_posix()

    @classmethod
    def autodetect(cls, remote_paths: Iterable[str], root: Path) -> Optional["PathMap"]:
        """Find the prefix whose remainder exists under `root`: /data/upload/u/x.jpg with
        root .../upload holding u/x.jpg means /data/upload <-> root."""
        root = Path(root)
        for remote in remote_paths:
            parts = PurePosixPath(remote).parts
            for cut in range(1, len(parts)):
                if (root / Path(*parts[cut:])).exists():
                    return cls(remote=str(PurePosixPath(*parts[:cut])), local=root)
        return None


def resolve_path_map(client: ImmichClient, settings: ImmichSettings, root: Path,
                     sample: Iterable[str] | None = None) -> Optional[PathMap]:
    """The configured path map, else one guessed from `sample` (Immich paths) or, without
    one, from the first 500 assets Immich lists."""
    if settings.path_map:
        return PathMap.parse(settings.path_map)
    if sample is None:
        sample = []
        for _, original in client.iter_assets():
            sample.append(original)
            if len(sample) >= 500:
                break
    return PathMap.autodetect(sample, root)


# -- cleanup ------------------------------------------------------------------------


def _ancestors(value: str) -> List[str]:
    parts = value.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def in_use_from_tags(tags: Iterable[str]) -> Set[str]:
    """The managed tags among `tags`, plus each one's ancestors — the tags Immich must keep."""
    used: Set[str] = set()
    for tag in tags:
        if tag not in used and vocab.is_managed(tag):
            used.add(tag)
            used.update(_ancestors(tag))
    return used


def managed_in_use(root: Path) -> Set[str]:
    """in_use_from_tags over every sidecar in the library, read from TagsList — the same
    property Immich reads."""
    def all_tags() -> Iterator[str]:
        for sidecar_file in filterer.sidecar_files(root):
            if filterer.image_for_sidecar(sidecar_file) is not None:
                yield from sidecar.read_tags(sidecar_file)

    return in_use_from_tags(all_tags())


def _prefix_label(value: str) -> str:
    head = value.split("/", 1)[0]
    if "|" in head:
        return "legacy-pipe"
    return head


def plan_cleanup(tags: List[dict], in_use: Set[str]) -> List[dict]:
    """Managed Immich tags that no sidecar uses, deepest first.

    A tag is excluded when anything below it in Immich's own hierarchy is kept, because
    deleting it would cascade to that child.
    """
    by_id = {t["id"]: t for t in tags}
    children: Dict[str, List[str]] = {}
    for t in tags:
        if t.get("parentId"):
            children.setdefault(t["parentId"], []).append(t["id"])

    def wanted(t: dict) -> bool:
        return vocab.is_managed(t["value"]) and t["value"] not in in_use

    memo: Dict[str, bool] = {}

    def deletable(tag_id: str) -> bool:
        if tag_id in memo:
            return memo[tag_id]
        memo[tag_id] = False  # guards against cycles
        tag = by_id[tag_id]
        ok = wanted(tag) and all(deletable(child) for child in children.get(tag_id, []) if child in by_id)
        memo[tag_id] = ok
        return ok

    def depth(tag_id: str) -> int:
        seen, level, current = set(), 0, by_id[tag_id]
        while current.get("parentId") in by_id and current["id"] not in seen:
            seen.add(current["id"])
            current = by_id[current["parentId"]]
            level += 1
        return level

    candidates = [t for t in tags if deletable(t["id"])]
    candidates.sort(key=lambda t: (-depth(t["id"]), t["value"]))
    return candidates


@dataclass
class CleanupResult:
    count: int = 0
    deleted: int = 0
    skipped_nonempty: int = 0
    skipped_blocked: int = 0
    by_prefix: Dict[str, int] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)


def run_cleanup(client: ImmichClient, candidates: List[dict], dry_run: bool = False,
                on_progress: Callable[[int, int], None] | None = None) -> CleanupResult:
    """Delete the planned tags, each re-checked empty first. A tag found in use blocks
    every ancestor from deletion (Immich would cascade)."""
    result = CleanupResult(count=len(candidates))
    for tag in candidates:
        label = _prefix_label(tag["value"])
        result.by_prefix[label] = result.by_prefix.get(label, 0) + 1
    result.examples = [t["value"] for t in candidates[:20]]
    if dry_run:
        return result

    by_id = {t["id"]: t for t in candidates}
    blocked: Set[str] = set()
    parents = {t["id"]: t.get("parentId") for t in candidates}
    for index, tag in enumerate(candidates, 1):
        if tag["id"] in blocked:
            result.skipped_blocked += 1
        elif client.tag_has_assets(tag["id"]):
            result.skipped_nonempty += 1
            parent = parents.get(tag["id"])
            while parent:
                blocked.add(parent)
                parent = parents.get(parent) if parent in by_id else None
        else:
            client.delete_tag(tag["id"])
            result.deleted += 1
        if on_progress:
            on_progress(index, len(candidates))
    return result


# -- sync ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    ok: bool
    reason: str | None = None
    refreshed: int = 0
    discovered: int = 0
    deleted_tags: int = 0
    pending: int = 0
    unmatched: int = 0
    cleanup: CleanupResult | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "refreshed": self.refreshed,
                "discovered": self.discovered, "deletedTags": self.deleted_tags,
                "pending": self.pending, "unmatched": self.unmatched}


def _all_entries(root: Path) -> List[dict]:
    return [
        {"path": str(image.relative_to(root)), "had_sidecar": True}
        for image in library.image_files(root)
        if sidecar.sidecar_path(image).exists()
    ]


def sync(root: Path, settings: ImmichSettings, state_dir: Path, *, all: bool = False,
         cleanup: bool = True, dry_run: bool = False,
         on_progress: Callable[[str, int, int], None] | None = None,
         poll: float = 5, timeout: float = 7200,
         sleep: Callable[[float], None] = time.sleep,
         in_use: Callable[[], Set[str]] | None = None) -> SyncResult:
    """Make Immich re-read the sidecars written since the last sync (or all of them),
    wait for it, then delete the managed tags nothing uses any more.

    Never raises for an unreachable or misconfigured Immich: the result says why, and
    the pending list is kept for the next attempt. `in_use` may supply the library's
    managed tags (e.g. from the web UI's index) instead of re-reading every sidecar.
    """
    used = in_use or (lambda: managed_in_use(root))
    root = Path(root)
    pending = library.PendingSync(state_dir, root)
    entries = _all_entries(root) if all else pending.entries()

    def phase(name: str, done: int = 0, total: int = 0) -> None:
        if on_progress:
            on_progress(name, done, total)

    try:
        client = ImmichClient(settings, sleep=sleep)
        phase("mapping")
        assets = list(client.iter_assets())
        path_map = resolve_path_map(client, settings, root, sample=[p for _, p in assets[:500]])
        if path_map is None:
            return SyncResult(ok=False, pending=len(entries),
                              reason="cannot match Immich's paths to this library; set [immich].path_map")
        by_local = {}
        for asset_id, original in assets:
            local = path_map.to_local(original)
            if local is not None:
                by_local[local] = asset_id

        ids: List[str] = []
        covered: List[str] = []
        new_count = 0
        unmatched = 0
        for entry in entries:
            local = root / entry["path"]
            asset_id = by_local.get(local)
            if asset_id is None:
                if local.exists():
                    unmatched += 1
                else:
                    covered.append(entry["path"])   # image is gone; nothing left to sync
                continue
            ids.append(asset_id)
            covered.append(entry["path"])
            if not entry["had_sidecar"]:
                new_count += 1

        needs_discovery = all or new_count > 0
        result = SyncResult(ok=True, refreshed=len(ids), unmatched=unmatched)
        if dry_run:
            result.discovered = new_count
            if cleanup:
                plan = plan_cleanup(client.list_tags(), used())
                result.cleanup = run_cleanup(client, plan, dry_run=True)
            result.pending = len(entries)
            return result

        phase("refresh", 0, len(ids))
        client.run_asset_job(ids)
        if needs_discovery:
            phase("discover")
            if client.start_sidecar_discovery(force=False):
                result.discovered = new_count
            else:
                result.reason = DISCOVERY_HINT
        # The refresh is queued in Immich now; these entries need nothing more from us.
        if covered:
            pending.remove(covered)

        phase("waiting")
        idle = client.wait_idle(poll=poll, timeout=timeout,
                                on_progress=lambda left: phase("waiting", 0, left))
        if not idle:
            note = "Immich is still re-reading sidecars; run `immichtag-sync cleanup` later"
            result.reason = f"{result.reason}; {note}" if result.reason else note
        elif cleanup:
            phase("cleanup")
            plan = plan_cleanup(client.list_tags(), used())
            result.cleanup = run_cleanup(client, plan,
                                         on_progress=lambda d, t: phase("cleanup", d, t))
            result.deleted_tags = result.cleanup.deleted
        result.pending = len(pending)
        return result
    except ImmichError as exc:
        return SyncResult(ok=False, reason=str(exc), pending=len(pending))
