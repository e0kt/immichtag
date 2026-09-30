"""Downloading originals from the page: one file, or a selection as a streamed zip."""

from __future__ import annotations

import io
import json
import shutil
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

from immichtag import sidecar, web

PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")

FILES = {  # relative path -> fixture
    "u/aa/ねこ.jpg": "a.jpg",
    "u/aa/x.jpg": "a.jpg",
    "u/bb/x.jpg": "b.png",
    "u/bb/y.png": "b.png",
}


@pytest.fixture
def server(tmp_path, fixtures_dir):
    root = tmp_path / "upload"
    for rel, fixture in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(fixtures_dir / "images" / fixture, path)
        sidecar.sidecar_path(path).write_bytes(sidecar.build_xmp(["tag/cat"], PROV))
    state = web.AppState(root)
    state.holder.start()
    state.holder.require()
    httpd = web.bind("127.0.0.1", 0, web.make_handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{httpd.server_address[1]}", root
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def post(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def id_of(state, rel):
    index = state.holder.require()
    return next(i for i, p in enumerate(index.images) if p.relative_to(index.root).as_posix() == rel)


# -- single file ------------------------------------------------------------------------


def test_single_download_is_the_original(server):
    state, base, root = server
    image_id, gen = id_of(state, "u/bb/y.png"), state.holder.require().generation
    status, headers, body = get(f"{base}/api/download?id={image_id}&v={gen}")
    assert status == 200
    assert body == (root / "u/bb/y.png").read_bytes()
    assert int(headers["Content-Length"]) == len(body)
    assert headers["Content-Type"] == "image/png"
    assert headers["Content-Disposition"].startswith("attachment")


def test_single_download_names_non_ascii_files(server):
    state, base, _ = server
    image_id, gen = id_of(state, "u/aa/ねこ.jpg"), state.holder.require().generation
    _, headers, _ = get(f"{base}/api/download?id={image_id}&v={gen}")
    assert "filename*=UTF-8''%E3%81%AD%E3%81%93.jpg" in headers["Content-Disposition"]


def test_single_download_refuses_stale_missing_and_removed(server):
    state, base, root = server
    index = state.holder.require()
    gen = index.generation
    assert get(f"{base}/api/download?id=0&v={gen - 1}")[0] == 409

    gone = id_of(state, "u/aa/x.jpg")
    (root / "u/aa/x.jpg").unlink()
    status, _, body = get(f"{base}/api/download?id={gone}&v={gen}")
    assert status == 404 and b"no longer on disk" in body

    removed = id_of(state, "u/bb/y.png")
    index.remove({removed})
    assert get(f"{base}/api/download?id={removed}&v={gen}")[0] == 404


# -- bundles ----------------------------------------------------------------------------


def test_bundle_holds_exactly_the_selection(server):
    state, base, root = server
    gen = state.holder.require().generation
    picks = ["u/aa/ねこ.jpg", "u/bb/y.png"]
    status, data = post(f"{base}/api/bundle",
                        {"mode": "ids", "ids": [id_of(state, r) for r in picks], "generation": gen})
    assert status == 200
    assert data["count"] == 2 and data["warn"] is False
    assert data["bytes"] == sum((root / r).stat().st_size for r in picks)

    status, headers, body = get(base + data["url"])
    assert status == 200 and headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert sorted(zf.namelist()) == sorted(["ねこ.jpg", "y.png"])
        for rel in picks:
            info = zf.getinfo(rel.rsplit("/", 1)[-1])
            assert info.compress_type == zipfile.ZIP_STORED
            assert zf.read(info) == (root / rel).read_bytes()


def test_bundle_renames_collisions_and_lists_skipped(server):
    state, base, root = server
    gen = state.holder.require().generation
    _, data = post(f"{base}/api/bundle", {"mode": "all", "query": {"filters": []}, "generation": gen})
    assert data["count"] == 4
    (root / "u/bb/y.png").unlink()                 # gone between the POST and the GET
    _, _, body = get(base + data["url"])
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = set(zf.namelist())
        assert {"x.jpg", "x-1.jpg", "ねこ.jpg", "_skipped.txt"} == names
        assert zf.read("_skipped.txt").decode().startswith("y.png:")


def test_bundle_token_works_once(server):
    state, base, _ = server
    gen = state.holder.require().generation
    _, data = post(f"{base}/api/bundle", {"mode": "ids", "ids": [0], "generation": gen})
    assert get(base + data["url"])[0] == 200
    assert get(base + data["url"])[0] == 404
    assert get(f"{base}/api/bundle/nope.zip")[0] == 404


def test_bundle_warns_when_large(server, monkeypatch):
    state, base, _ = server
    monkeypatch.setattr(web, "BUNDLE_WARN_COUNT", 2)
    gen = state.holder.require().generation
    _, data = post(f"{base}/api/bundle", {"mode": "all", "query": {"filters": []}, "generation": gen})
    assert data["warn"] is True


def test_bundle_after_rescan_is_409(server):
    state, base, _ = server
    gen = state.holder.require().generation
    _, data = post(f"{base}/api/bundle", {"mode": "ids", "ids": [0], "generation": gen})
    state.reindex()
    state.holder.require()
    assert get(base + data["url"])[0] == 409
