import shutil

from immichtag import library, sidecar

MODEL = "pixai-labs/pixai-tagger-v1.0"
REVISION = "f33cfdb53c0c90b049bab9ce066eea1118970ef8"


def _images(root):
    return sorted(library.image_files(root))


def _write(image, revision=REVISION, model=MODEL):
    prov = sidecar.Provenance(model=model, revision=revision, profile="v1", tagged_at="2026-01-01T00:00:00Z")
    sidecar.sidecar_path(image).write_bytes(sidecar.build_xmp(["tag/x", "rating/general"], prov))


def test_classify_each_status(tmp_library, fixtures_dir):
    a, b = _images(tmp_library)
    assert library.classify(a, MODEL, REVISION) is library.Status.NEW

    shutil.copy(fixtures_dir / "wd14_legacy.xmp", sidecar.sidecar_path(a))
    assert library.classify(a, MODEL, REVISION) is library.Status.LEGACY

    sidecar.sidecar_path(a).write_bytes(b"\x00garbage")
    assert library.classify(a, MODEL, REVISION) is library.Status.LEGACY

    _write(a, revision="0" * 40)
    assert library.classify(a, MODEL, REVISION) is library.Status.OUTDATED

    _write(a)
    assert library.classify(a, MODEL, REVISION) is library.Status.CURRENT

    # The element form Immich's exiftool produces is CURRENT too.
    shutil.copy(fixtures_dir / "immich_rewritten.xmp", sidecar.sidecar_path(b))
    assert library.classify(b, MODEL, REVISION) is library.Status.CURRENT


def test_queue_puts_new_first_in_scan_order(tmp_path, fixtures_dir):
    root = tmp_path / "lib"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    names = ["1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.jpg"]
    for name in names:
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
    shutil.copy(fixtures_dir / "wd14_legacy.xmp", folder / "1.jpg.xmp")
    _write(folder / "3.jpg")                       # current
    _write(folder / "4.jpg", revision="0" * 40)     # outdated
    # 2 and 5 are new

    entries = library.scan(root, MODEL, REVISION)
    statuses = {p.name: s for p, s in entries}
    assert statuses == {
        "1.jpg": library.Status.LEGACY, "2.jpg": library.Status.NEW,
        "3.jpg": library.Status.CURRENT, "4.jpg": library.Status.OUTDATED,
        "5.jpg": library.Status.NEW,
    }

    scan_order = [p.name for p, _ in entries]
    queue = [p.name for p, _ in library.tagging_queue(entries)]
    new = [n for n in scan_order if n in ("2.jpg", "5.jpg")]
    rest = [n for n in scan_order if n in ("1.jpg", "4.jpg")]
    assert queue == new + rest

    with_current = [p.name for p, _ in library.tagging_queue(entries, overwrite=True)]
    assert with_current[:2] == new
    assert sorted(with_current[2:]) == ["1.jpg", "3.jpg", "4.jpg"]


def test_remove_stale_tmp(tmp_library):
    a, _ = _images(tmp_library)
    stale = a.parent / f".{a.name}.xmp{sidecar.TMP_SUFFIX}"
    stale.write_bytes(b"half")
    keep = a.parent / "notes.immichtag-tmp.txt"
    keep.write_bytes(b"x")
    assert library.remove_stale_tmp(tmp_library) == 1
    assert not stale.exists()
    assert keep.exists()


def test_pending_sync_round_trip(tmp_library, tmp_path):
    a, b = _images(tmp_library)
    pending = library.PendingSync(tmp_path / "state", tmp_library)
    assert pending.entries() == []
    pending.append(a, had_sidecar=True)
    pending.append(b, had_sidecar=False)
    pending.append(a, had_sidecar=True)

    entries = pending.entries()
    assert [e["path"] for e in entries] == [str(a.relative_to(tmp_library)), str(b.relative_to(tmp_library))]
    assert {e["path"]: e["had_sidecar"] for e in entries}[str(b.relative_to(tmp_library))] is False

    pending.remove([str(a.relative_to(tmp_library))])
    assert [e["path"] for e in pending.entries()] == [str(b.relative_to(tmp_library))]


def test_pending_new_wins_over_retag(tmp_library, tmp_path):
    a, _ = _images(tmp_library)
    pending = library.PendingSync(tmp_path / "state", tmp_library)
    pending.append(a, had_sidecar=False)
    pending.append(a, had_sidecar=True)
    assert pending.entries()[0]["had_sidecar"] is False
