"""The trash set and deletion log kept in the state directory."""

from __future__ import annotations

import json

from immichtag.trash import DeletionLog, TrashSet


def test_trash_set_missing_or_corrupt_is_empty(tmp_path):
    trash = TrashSet(tmp_path)
    assert trash.load() == set()
    trash.path.write_text("{not json")
    assert trash.load() == set()


def test_trash_set_replace_and_add(tmp_path):
    trash = TrashSet(tmp_path)
    trash.replace({"u/a.jpg", "u/b.jpg"})
    assert trash.load() == {"u/a.jpg", "u/b.jpg"}
    trash.add({"u/c.jpg"})
    assert TrashSet(tmp_path).load() == {"u/a.jpg", "u/b.jpg", "u/c.jpg"}
    trash.replace({"u/c.jpg"})
    assert trash.load() == {"u/c.jpg"}
    assert [p.name for p in tmp_path.iterdir()] == ["trashed.json"]   # no tmp left behind


def test_deletion_log_appends_one_line_per_image(tmp_path):
    log = DeletionLog(tmp_path)
    log.append("job1", [{"path": "u/a.jpg", "assetId": "A", "outcome": "deleted", "reason": None}], True)
    log.append("job2", [{"path": "u/b.jpg", "assetId": None, "outcome": "missing", "reason": "not in Immich"},
                        {"path": "u/c.jpg", "assetId": "C", "outcome": "failed", "reason": "HTTP 400"}], False)
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [(e["job"], e["path"], e["outcome"]) for e in lines] == [
        ("job1", "u/a.jpg", "deleted"), ("job2", "u/b.jpg", "missing"), ("job2", "u/c.jpg", "failed")]
    assert set(lines[0]) == {"at", "job", "path", "assetId", "outcome", "reason", "trashEnabled"}
    assert lines[2]["trashEnabled"] is False
