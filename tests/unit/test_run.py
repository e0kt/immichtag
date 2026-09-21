"""A whole tagging run against a tiny library, with a stub model (no torch involved)."""

import shutil
import threading

import numpy as np
import pytest
from PIL import Image

from immichtag import backup, library, sidecar
from immichtag.run import RunOptions, TaggingRun
from immichtag.tagger import MODEL_REPO, MODEL_REVISION

NAMES = ["long_hair", "hatsune_miku", "vocaloid", "official_art", "tony_taka",
         "rating:g", "rating:s", "rating:q", "rating:e"]
CATS = ["general", "character", "copyright", "meta", "style", "rating", "rating", "rating", "rating"]


class StubTagger:
    device = "cpu"
    names = NAMES
    categories = CATS

    def __init__(self, vanish=()):
        self.vanish = set(vanish)
        self.inferred = []

    def preprocess(self, path):
        if path.name in self.vanish:
            raise FileNotFoundError(path)
        with Image.open(path) as img:
            img.load()
        return path

    def infer(self, x):
        self.inferred.append(x.name)
        return np.array([0.9, 0.95, 0.9, 0.9, 0.99, 0.1, 0.8, 0.05, 0.05], dtype=np.float32)


@pytest.fixture
def library_dir(tmp_path, fixtures_dir):
    """legacy1, new1, legacy2, new2 — in that walk order within one folder."""
    root = tmp_path / "lib"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name in ("a_legacy.jpg", "b_new.jpg", "c_legacy.jpg", "d_new.jpg"):
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
    shutil.copy(fixtures_dir / "wd14_legacy.xmp", folder / "a_legacy.jpg.xmp")
    shutil.copy(fixtures_dir / "immich_rewritten.xmp", folder / "c_legacy.jpg.xmp")
    # make c legacy (strip provenance) but keep its foreign tag my_album/trip
    text = (folder / "c_legacy.jpg.xmp").read_text(encoding="utf-8")
    text = text.replace("immichtag:model", "other:model").replace("immichtag:revision", "other:revision")
    (folder / "c_legacy.jpg.xmp").write_text(text, encoding="utf-8")
    return root


def _run(root, tagger, **kwargs):
    state_dir = root.parent / "state"
    run = TaggingRun(root, RunOptions(immich=False), tagger_factory=lambda device: tagger,
                     state_dir=state_dir, **kwargs)
    return run, run.run()


def test_full_run(library_dir):
    stub = StubTagger()
    run, progress = _run(library_dir, stub)
    folder = library_dir / "u" / "aa"

    assert progress.state == "finished"
    assert (progress.total_new, progress.total_retag) == (2, 2)
    assert progress.written == 4 and progress.failed == 0
    # new images first, each group in scan order
    new_first = [n for n in stub.inferred]
    assert set(new_first[:2]) == {"b_new.jpg", "d_new.jpg"}
    assert set(new_first[2:]) == {"a_legacy.jpg", "c_legacy.jpg"}

    for image in folder.glob("*.jpg"):
        data = sidecar.read_sidecar(sidecar.sidecar_path(image))
        assert data.provenance.model == MODEL_REPO
        assert data.provenance.revision == MODEL_REVISION
        assert data.provenance.profile == "v1"
        assert data.tags[:5] == ["character/hatsune_miku", "parody/vocaloid", "tag/long_hair",
                                 "meta/official_art", "rating/sensitive"]
        assert "tag/bangs" not in data.tags
        assert library.classify(image, MODEL_REPO, MODEL_REVISION) is library.Status.CURRENT

    assert "my_album/trip" in sidecar.read_tags(folder / "c_legacy.jpg.xmp")

    # backed up before overwrite: both legacy sidecars, byte for byte
    backups = backup.list_backups(run.state_dir)
    assert len(backups) == 1 and backups[0]["count"] == 2
    assert progress.backup_id == backups[0]["id"]

    pending = {e["path"].split("/")[-1]: e["had_sidecar"] for e in library.PendingSync(run.state_dir, library_dir).entries()}
    assert pending == {"a_legacy.jpg": True, "b_new.jpg": False, "c_legacy.jpg": True, "d_new.jpg": False}

    # a second run finds nothing to do
    stub2 = StubTagger()
    _, again = _run(library_dir, stub2)
    assert again.state == "finished" and again.total == 0 and stub2.inferred == []


def test_cancel_then_resume(library_dir):
    cancel = threading.Event()
    before = {p: p.read_bytes() for p in (library_dir / "u" / "aa").glob("*.xmp")}

    def stop_after_first(progress):
        if progress.written >= 1:
            cancel.set()

    stub = StubTagger()
    _, progress = _run(library_dir, stub, cancel=cancel, on_progress=stop_after_first)
    assert progress.state == "cancelled"
    assert progress.written == 1
    # every sidecar is either untouched or complete and current
    for path in (library_dir / "u" / "aa").glob("*.xmp"):
        data = sidecar.read_sidecar(path)
        assert data.error is None
        if path in before and path.read_bytes() != before[path]:
            assert data.provenance and data.provenance.model == MODEL_REPO
    assert not list(library_dir.rglob("*immichtag-tmp"))

    stub2 = StubTagger()
    _, resumed = _run(library_dir, stub2)
    assert resumed.written == 3
    assert len(stub2.inferred) == 3


def test_undecodable_and_vanished_images(library_dir):
    folder = library_dir / "u" / "aa"
    (folder / "e_broken.jpg").write_bytes(b"this is not a jpeg")
    shutil.copy(folder / "a_legacy.jpg.xmp", folder / "e_broken.jpg.xmp")
    broken_before = (folder / "e_broken.jpg.xmp").read_bytes()

    stub = StubTagger(vanish={"d_new.jpg"})
    _, progress = _run(library_dir, stub)
    assert progress.failed == 1
    assert progress.skipped == 1
    assert progress.written == 3
    assert any("e_broken.jpg" in m for m in progress.messages)
    assert (folder / "e_broken.jpg.xmp").read_bytes() == broken_before
    assert not (folder / "d_new.jpg.xmp").exists()


def test_limit_and_overwrite(library_dir):
    stub = StubTagger()
    run = TaggingRun(library_dir, RunOptions(immich=False, limit=1), tagger_factory=lambda d: stub,
                     state_dir=library_dir.parent / "state")
    progress = run.run()
    assert progress.written == 1 and stub.inferred[0] in ("b_new.jpg", "d_new.jpg")

    _run(library_dir, StubTagger())                    # finish the rest
    stub3 = StubTagger()
    run = TaggingRun(library_dir, RunOptions(immich=False, overwrite=True), tagger_factory=lambda d: stub3,
                     state_dir=library_dir.parent / "state")
    assert run.run().written == 4


def test_lock_busy_is_refused(library_dir):
    state_dir = library_dir.parent / "state"
    with library.TaggingLock(state_dir):
        stub = StubTagger()
        run = TaggingRun(library_dir, RunOptions(immich=False), tagger_factory=lambda d: stub,
                         state_dir=state_dir)
        progress = run.run()
    assert progress.state == "refused"
    assert "another tagging run is active" in progress.error
    assert stub.inferred == []


def test_sync_hook_called_after_writes(library_dir):
    calls = []

    def hook(progress):
        calls.append(progress.written)
        return {"ok": True}

    run = TaggingRun(library_dir, RunOptions(immich=False), tagger_factory=lambda d: StubTagger(),
                     state_dir=library_dir.parent / "state", sync_hook=hook)
    progress = run.run()
    assert calls == [4]
    assert progress.sync == {"ok": True}
