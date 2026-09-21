"""A small in-process stand-in for the Immich 3.2 endpoints immichtag uses."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

API_KEY = "test-key-123"


class FakeImmich:
    def __init__(self) -> None:
        self.assets: List[dict] = []            # {"id", "originalPath", "tags": set(tag ids)}
        self.tags: Dict[str, dict] = {}         # id -> {"id", "value", "parentId"}
        self.asset_jobs: List[dict] = []
        self.sidecar_jobs: List[dict] = []
        self.deleted: List[str] = []
        self.requests: List[tuple] = []
        self.fail_next = 0                      # answer this many requests with 500
        self.discovery_404 = False
        self.legacy_search = False              # reject the v3.2 search shape with 400
        self.busy_polls = 2                     # GET /api/jobs reports work this many times
        self.page_size_seen: List[int] = []
        self._server = None

    # -- fixtures --------------------------------------------------------------

    def add_tag(self, value: str) -> str:
        """Create `value` and its missing ancestors like Immich's upsertTags; return its id."""
        parent = None
        parts = value.split("/")
        for depth in range(1, len(parts) + 1):
            path = "/".join(parts[:depth])
            existing = next((t for t in self.tags.values() if t["value"] == path), None)
            if existing is None:
                tag_id = f"00000000-0000-4000-8000-{len(self.tags) + 1:012d}"
                existing = {"id": tag_id, "value": path, "parentId": parent, "name": parts[depth - 1]}
                self.tags[tag_id] = existing
            parent = existing["id"]
        return parent

    def add_asset(self, original_path: str, tag_values=()) -> str:
        asset_id = f"a0000000-0000-4000-8000-{len(self.assets) + 1:012d}"
        tag_ids = {self.add_tag(v) for v in tag_values}
        self.assets.append({"id": asset_id, "originalPath": original_path, "tags": tag_ids})
        return asset_id

    def descendants(self, tag_id: str) -> set:
        out = {tag_id}
        changed = True
        while changed:
            changed = False
            for t in self.tags.values():
                if t["parentId"] in out and t["id"] not in out:
                    out.add(t["id"])
                    changed = True
        return out

    # -- server ----------------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeImmich":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, payload=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                fake.requests.append((method, self.path, body, self.headers.get("x-api-key")))
                if fake.fail_next > 0:
                    fake.fail_next -= 1
                    return self._reply(500, {"message": "boom"})
                if self.path == "/api/server/version" and method == "GET":
                    return self._reply(200, {"major": 3, "minor": 2, "patch": 2})
                if self.headers.get("x-api-key") != API_KEY:
                    return self._reply(401, {"message": "Invalid API key"})
                return fake.route(self, method, self.path, body)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def route(self, h, method, path, body):
        if path == "/api/search/metadata" and method == "POST":
            new_shape = any(k in body for k in ("filter", "cursor", "orderBy"))
            if new_shape and self.legacy_search:
                return h._reply(400, {"message": "property filter should not exist"})
            size = body.get("size", 250)
            self.page_size_seen.append(size)
            if new_shape:
                tag_ids = ((body.get("filter") or {}).get("tagIds") or {}).get("any")
                start = int(body.get("cursor") or 0)
            else:
                tag_ids = body.get("tagIds")
                start = (int(body.get("page") or 1) - 1) * size
            items = self.assets
            if tag_ids:
                wanted = set()
                for tag_id in tag_ids:
                    wanted |= self.descendants(tag_id)
                items = [a for a in items if a["tags"] & wanted]
            page = items[start:start + size]
            more = start + size < len(items)
            assets = {"items": [{"id": a["id"], "originalPath": a["originalPath"]} for a in page],
                      "count": len(page)}
            if new_shape:
                assets["nextCursor"] = str(start + size) if more else None
            else:
                assets["nextPage"] = str(start // size + 2) if more else None
            return h._reply(200, {"assets": assets, "albums": {"items": []}})
        if path == "/api/assets/jobs" and method == "POST":
            self.asset_jobs.append(body)
            return h._reply(204)
        if path == "/api/jobs/sidecar" and method == "PUT":
            if self.discovery_404:
                return h._reply(404, {"message": "Cannot PUT"})
            self.sidecar_jobs.append(body)
            return h._reply(200, {})
        if path == "/api/jobs" and method == "GET":
            busy = 1 if self.busy_polls > 0 else 0
            self.busy_polls = max(0, self.busy_polls - 1)
            counts = {"active": busy, "waiting": busy, "completed": 0, "failed": 0, "delayed": 0, "paused": 0}
            status = {"isActive": bool(busy), "isPaused": False}
            return h._reply(200, {name: {"jobCounts": dict(counts), "queueStatus": status}
                                  for name in ("sidecar", "metadataExtraction", "thumbnailGeneration")})
        if path == "/api/tags" and method == "GET":
            return h._reply(200, list(self.tags.values()))
        if path.startswith("/api/tags/") and method == "DELETE":
            tag_id = path.rsplit("/", 1)[-1]
            if tag_id not in self.tags:
                return h._reply(404, {"message": "not found"})
            for gone in self.descendants(tag_id):     # Immich cascades to children
                self.tags.pop(gone, None)
                self.deleted.append(gone)
                for asset in self.assets:
                    asset["tags"].discard(gone)
            return h._reply(204)
        return h._reply(404, {"message": f"no route {method} {path}"})
