"""Deleting from the page: confirmation, Immich's trash, and what the index remembers."""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest

from immichtag import config, library, sidecar, web

from .fake_immich import API_KEY, FakeImmich

PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")
NAMES = ("1.jpg", "2.jpg", "3.jpg", "4.jpg")


@pytest.fixture
def fake():
    server = FakeImmich().start()
    yield server
    server.stop()


@pytest.fixture
def app(tmp_path, fixtures_dir, fake, monkeypatch):
    root = tmp_path / "library" / "upload"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name in NAMES:
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
        sidecar.sidecar_path(folder / name).write_bytes(sidecar.build_xmp(["tag/long_hair", "rating/general"], PROV))
        fake.add_asset(f"/data/upload/u/aa/{name}", ["tag/long_hair"])
    monkeypatch.setenv("IMMICHTAG_URL", fake.url)
    monkeypatch.setenv("IMMICHTAG_API_KEY", API_KEY)
    state = web.AppState(root)
    state.holder.start()
    state.holder.require()
    return state


def asset_id(fake, name):
    return next(a["id"] for a in fake.assets if a["originalPath"].endswith("/" + name))


def image_id(app, name):
    return next(i for i, p in enumerate(app.holder.require().images) if p.name == name)


def select(app, *names):
    return {"mode": "ids", "ids": [image_id(app, n) for n in names], "generation": app.holder.require().generation}


def select_all(app):
    return {"mode": "all", "query": {"filters": []}, "generation": app.holder.require().generation}


def run_delete(app, selection):
    preview = app.delete_preview(selection)
    app.start_delete({"token": preview["token"], "confirm": preview["count"]})
    for _ in range(500):
        job = app.delete_status()["job"]
        if not job["active"]:
            return job
        time.sleep(0.01)
    raise AssertionError("delete did not finish")


def visible(app):
    index = app.holder.require()
    return {index.images[i].name for i in index.select([])}


def state_dir(app):
    return config.state_dir(app.holder.root)


def test_preview_describes_the_delete(app, fake, monkeypatch):
    preview = app.delete_preview(select_all(app))
    assert preview["count"] == 4 and len(preview["sample"]) == 4
    assert preview["trashEnabled"] is True and preview["typeToConfirm"] is False
    monkeypatch.setattr(web, "DELETE_TYPE_THRESHOLD", 3)
    assert app.delete_preview(select_all(app))["typeToConfirm"] is True
    monkeypatch.setattr(web, "DELETE_TYPE_THRESHOLD", 100)
    fake.features = {"trash": False}
    preview = app.delete_preview(select_all(app))
    assert preview["trashEnabled"] is False and preview["typeToConfirm"] is True


def test_start_needs_the_exact_count_and_a_fresh_token(app, fake):
    preview = app.delete_preview(select(app, "1.jpg", "2.jpg"))
    for confirm in (None, 1, 3, True, "2"):
        with pytest.raises(ValueError):
            app.start_delete({"token": preview["token"], "confirm": confirm})
        preview = app.delete_preview(select(app, "1.jpg", "2.jpg"))   # a refused start spends the token
    with pytest.raises(ValueError):
        app.start_delete({"token": "nope", "confirm": 2})
    assert fake.trash_calls == []


def test_delete_trashes_and_forgets(app, fake):
    pending = library.PendingSync(state_dir(app), app.holder.root)
    pending.append(app.holder.root / "u/aa/1.jpg", had_sidecar=True)
    before_tags = dict(app.holder.require().tag_counts())

    job = run_delete(app, select(app, "1.jpg", "2.jpg"))

    assert job["state"] == "finished", job
    assert job["deleted"] == 2 and job["failed"] == 0 and job["revision"] == 1
    assert fake.trashed == {asset_id(fake, "1.jpg"), asset_id(fake, "2.jpg")}
    assert all(call["force"] is False for call in fake.trash_calls)
    assert visible(app) == {"3.jpg", "4.jpg"}
    assert app.search({"filters": []})["total"] == 2
    assert dict(app.holder.require().tag_counts())["tag/long_hair"] == before_tags["tag/long_hair"] - 2
    assert len(pending) == 0
    trashed = json.loads((state_dir(app) / "trashed.json").read_text())["paths"]
    assert trashed == ["u/aa/1.jpg", "u/aa/2.jpg"]
    log = [json.loads(line) for line in (state_dir(app) / "deletions.jsonl").read_text().splitlines()]
    assert {(e["path"], e["outcome"]) for e in log} == {("u/aa/1.jpg", "deleted"), ("u/aa/2.jpg", "deleted")}
    assert app.status()["images"] == 2


def test_mixed_outcomes_do_not_stop_the_rest(app, fake):
    fake.trashed.add(asset_id(fake, "1.jpg"))                      # already in the trash
    fake.assets = [a for a in fake.assets if not a["originalPath"].endswith("/2.jpg")]   # unknown to Immich
    fake.deny_ids.add(asset_id(fake, "3.jpg"))                     # spoils its batch

    job = run_delete(app, select_all(app))

    assert job["state"] == "finished", job
    assert (job["deleted"], job["already"], job["missing"], job["failed"]) == (1, 1, 1, 1)
    assert {f["name"] for f in job["failures"]} == {"2.jpg", "3.jpg"}
    assert asset_id(fake, "4.jpg") in fake.trashed
    assert visible(app) == {"2.jpg", "3.jpg"}                      # only what is in the trash is hidden


def test_immich_refusing_permission_stops_with_a_reason(app, fake):
    fake.deny_delete_permission = True
    job = run_delete(app, select(app, "1.jpg"))
    assert job["state"] == "error" and "asset.delete" in job["error"]
    assert job["failed"] == 1 and visible(app) == set(NAMES)


def test_no_delete_while_another_job_runs(app):
    app.jobs["x"] = web.ExportJob(id="x", total=1, out_dir="/tmp")
    with pytest.raises(web.HTTPStatusError) as err:
        app.delete_preview(select(app, "1.jpg"))
    assert err.value.status == 409
    app.jobs["x"].state = "finished"
    preview = app.delete_preview(select(app, "1.jpg"))
    app.jobs["x"].state = "running"
    with pytest.raises(web.HTTPStatusError):
        app.start_delete({"token": preview["token"], "confirm": 1})


def test_nothing_else_starts_during_a_delete(app, monkeypatch, tmp_path):
    release = threading.Event()

    def held(job, ids, lock):
        release.wait(5)
        lock.release()
        job.state = "finished"

    monkeypatch.setattr(app, "_run_delete", held)
    preview = app.delete_preview(select(app, "1.jpg"))
    app.start_delete({"token": preview["token"], "confirm": 1})
    try:
        assert app.status()["busy"] == "delete"
        for start in (lambda: app.start_export({"outDir": str(tmp_path / "out")}),
                      app.start_tagging,
                      lambda: app.start_sync({})):
            with pytest.raises(web.HTTPStatusError):
                start()
    finally:
        release.set()


def test_refused_while_a_terminal_run_holds_the_lock(app, fake):
    preview = app.delete_preview(select(app, "1.jpg"))
    with library.TaggingLock(state_dir(app)):
        with pytest.raises(web.HTTPStatusError, match="another tagging run") as err:
            app.start_delete({"token": preview["token"], "confirm": 1})
    assert err.value.status == 409 and fake.trash_calls == []


def rescan(app):
    app.reindex()
    app.holder.require()


def test_trashed_stay_hidden_across_rescans_until_restored(app, fake):
    run_delete(app, select(app, "1.jpg"))
    rescan(app)
    assert "1.jpg" not in visible(app)
    fake.restore(asset_id(fake, "1.jpg"))
    rescan(app)
    assert "1.jpg" in visible(app)


def test_trashed_in_immich_itself_are_hidden_too(app, fake):
    fake.trashed.add(asset_id(fake, "4.jpg"))
    rescan(app)
    assert "4.jpg" not in visible(app)


def test_saved_trash_is_used_when_immich_is_down(app, fake):
    run_delete(app, select(app, "2.jpg"))
    fake.stop()
    rescan(app)
    assert "2.jpg" not in visible(app)


def test_not_configured(app, monkeypatch):
    monkeypatch.delenv("IMMICHTAG_API_KEY")
    status = app.status()["immich"]
    assert status["canDelete"] is False and status["deleteReason"]
    with pytest.raises(ValueError):
        app.delete_preview(select(app, "1.jpg"))


def test_delete_endpoints_want_json(app):
    httpd = web.bind("127.0.0.1", 0, web.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = json.dumps(select(app, "1.jpg")).encode()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/api/delete/preview"
        plain = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(plain, timeout=5)
        assert err.value.code == 415
        proper = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(proper, timeout=5) as response:
            assert json.loads(response.read())["count"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
