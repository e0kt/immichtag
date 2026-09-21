import shutil
import time

import pytest

from immichtag import library, sidecar, web

from .fake_immich import API_KEY, FakeImmich

PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")


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
    for name in ("1.jpg", "2.jpg"):
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
        sidecar.sidecar_path(folder / name).write_bytes(sidecar.build_xmp(["tag/long_hair", "rating/general"], PROV))
        fake.add_asset(f"/data/upload/u/aa/{name}", ["tag/long_hair", "rating/general"])
    fake.add_tag("tag/bangs")
    fake.add_tag("character|abe_nana")
    fake.add_tag("my_album/trip")
    fake.busy_polls = 0
    monkeypatch.setenv("IMMICHTAG_URL", fake.url)
    monkeypatch.setenv("IMMICHTAG_API_KEY", API_KEY)
    state = web.AppState(root)
    state.holder.start()
    state.holder.require()
    library.PendingSync(state._state_dir(), root).append(folder / "1.jpg", had_sidecar=True)
    return state


def wait(app):
    for _ in range(200):
        job = app.sync_status()["job"]
        if not job["active"]:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_status_reports_immich(app):
    immich = app.status()["immich"]
    assert immich == {"configured": True, "reachable": True, "pending": 1}


def test_cleanup_preview_and_confirm(app, fake):
    preview = app.cleanup_preview()
    assert preview["count"] == 2
    assert set(preview["examples"]) == {"tag/bangs", "character|abe_nana"}
    with pytest.raises(ValueError):
        app.start_cleanup({})
    app.start_cleanup({"confirm": True})
    job = wait(app)
    assert job["state"] == "finished" and job["result"]["deletedTags"] == 2
    assert {t["value"] for t in fake.tags.values()} >= {"my_album", "my_album/trip", "tag/long_hair"}
    assert "tag/bangs" not in {t["value"] for t in fake.tags.values()}


def test_sync_job_runs_in_background(app, fake):
    app.start_sync({})
    job = wait(app)
    assert job["state"] == "finished", job
    assert job["result"]["refreshed"] == 1
    assert app.status()["immich"]["pending"] == 0


def test_not_configured(app, monkeypatch):
    monkeypatch.delenv("IMMICHTAG_API_KEY")
    assert app.status()["immich"]["configured"] is False
    with pytest.raises(ValueError):
        app.start_sync({})
