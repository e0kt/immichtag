import json
import tarfile

from immichtag import backup, sidecar


def _sidecars(root, contents):
    paths = []
    for i, data in enumerate(contents):
        folder = root / "u" / f"{i:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"img{i}.jpg.xmp"
        path.write_bytes(data)
        paths.append(path)
    return paths


def test_backup_archives_bytes_with_manifest(tmp_path):
    root, state = tmp_path / "lib", tmp_path / "state"
    paths = _sidecars(root, [b"one", b"two \xe2\x88\x95"])
    backup_id = backup.create_backup(root, paths, state, reason="retag")
    assert backup_id

    folder = state / "backups" / backup_id
    with tarfile.open(folder / "sidecars.tar.gz") as tar:
        names = sorted(tar.getnames())
        assert names == sorted(str(p.relative_to(root)) for p in paths)
        assert tar.extractfile(str(paths[1].relative_to(root))).read() == b"two \xe2\x88\x95"

    manifest = [json.loads(line) for line in (folder / "manifest.jsonl").read_text().splitlines()]
    assert {m["path"] for m in manifest} == {str(p.relative_to(root)) for p in paths}
    assert all({"size", "mtime", "sha256"} <= m.keys() for m in manifest)
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["count"] == 2 and meta["reason"] == "retag" and meta["root"] == str(root)


def test_second_backup_skips_already_archived(tmp_path):
    root, state = tmp_path / "lib", tmp_path / "state"
    paths = _sidecars(root, [b"a", b"b"])
    assert backup.create_backup(root, paths, state)
    assert backup.create_backup(root, paths, state) is None     # nothing new
    paths[0].write_bytes(b"a changed")
    second = backup.create_backup(root, paths, state)
    assert second
    assert backup.list_backups(state)[0]["count"] == 1


def test_missing_files_are_ignored(tmp_path):
    root, state = tmp_path / "lib", tmp_path / "state"
    root.mkdir()
    assert backup.create_backup(root, [root / "gone.jpg.xmp"], state) is None


def test_restore_is_byte_identical_and_takes_safety_backup(tmp_path):
    root, state = tmp_path / "lib", tmp_path / "state"
    originals = [b"<wd14 one/>", b"<wd14 two/>"]
    paths = _sidecars(root, originals)
    backup_id = backup.create_backup(root, paths, state)
    for path in paths:
        sidecar.write_atomic(path, b"<immichtag new/>")

    assert sorted(backup.restore(root, backup_id, state, dry_run=True)) == sorted(paths)
    assert all(p.read_bytes() == b"<immichtag new/>" for p in paths)    # dry run wrote nothing

    restored = backup.restore(root, backup_id, state)
    assert sorted(restored) == sorted(paths)
    assert [p.read_bytes() for p in paths] == originals

    reasons = [b["reason"] for b in backup.list_backups(state)]
    assert "restore-safety" in reasons


def test_list_backups_newest_first(tmp_path):
    root, state = tmp_path / "lib", tmp_path / "state"
    paths = _sidecars(root, [b"x"])
    first = backup.create_backup(root, paths, state)
    paths[0].write_bytes(b"y")
    second = backup.create_backup(root, paths, state)
    listed = backup.list_backups(state)
    assert [b["id"] for b in listed] == [second, first]
    assert listed[0]["size"] > 0


def test_restore_rejects_path_traversal(tmp_path):
    import io
    import pytest
    root, state = tmp_path / "lib", tmp_path / "state"
    root.mkdir()
    folder = state / "backups" / "20260101T000000Z"
    folder.mkdir(parents=True)
    with tarfile.open(folder / "sidecars.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("../evil.xmp")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    (folder / "manifest.jsonl").write_text("")
    (folder / "meta.json").write_text(json.dumps({"root": str(root), "count": 1, "reason": "retag", "created": "x"}))
    with pytest.raises(ValueError):
        backup.restore(root, "20260101T000000Z", state)
    assert not (tmp_path / "evil.xmp").exists()
