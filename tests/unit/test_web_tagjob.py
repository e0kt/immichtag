"""The web UI's Tag button runs the same TaggingRun as the CLI, with the stub model."""

import shutil
import time

import pytest

from immichtag import library, web

from .test_run import StubTagger


@pytest.fixture
def app(tmp_path, fixtures_dir):
    root = tmp_path / "lib"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name in ("new1.jpg", "new2.jpg", "old.jpg"):
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
    shutil.copy(fixtures_dir / "wd14_legacy.xmp", folder / "old.jpg.xmp")
    state = web.AppState(root)
    state.tagger_factory = lambda device: StubTagger()
    state.holder.start()
    state.holder.require()
    return state


def wait_for(app, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = app.tagging()["job"]
        if job and not job["active"]:
            return job
        time.sleep(0.02)
    raise AssertionError("tag job did not finish")


def test_run_through_the_web(app):
    assert app.pending_tagging() == {"new": 2, "legacy": 1, "outdated": 0, "current": 0}
    states = []
    app.start_tagging()
    for _ in range(500):
        job = app.tagging()["job"]
        states.append(job["state"])
        if not job["active"]:
            break
        time.sleep(0.005)
    job = wait_for(app)
    assert job["state"] == "finished"
    assert (job["totalNew"], job["totalRetag"], job["written"]) == (2, 1, 3)
    assert job["device"] == "cpu"
    assert job["backupId"]                              # the legacy sidecar was backed up
    assert set(states) <= {"scanning", "locking", "backing-up", "loading", "running", "syncing", "finished"}

    # the index was rebuilt before the job reported the end
    app.holder.require()
    assert app.pending_tagging() == {"new": 0, "legacy": 0, "outdated": 0, "current": 3}
    assert app.status()["retag"] == {"legacy": 0, "outdated": 0}


def test_refused_when_another_run_holds_the_lock(app):
    with library.TaggingLock(app._state_dir()):
        app.start_tagging()
        job = wait_for(app)
    assert job["state"] == "refused"
    assert "another tagging run is active" in job["error"]
    assert job["written"] == 0


def test_second_start_attaches_to_running_job(app):
    first = app.start_tagging()["job"]
    second = app.start_tagging()["job"]
    assert first["id"] == second["id"]
    wait_for(app)
