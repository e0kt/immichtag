"""What immichtag remembers about deleted images.

Deleting from the web page moves an image to Immich's trash, which only flips the
asset's database row: the file and its sidecar stay in the upload folder until the trash
is emptied (30 days by default). A rescan would bring every deleted image straight back,
so the index leaves out the paths Immich holds in its trash — refreshed from Immich when
it is reachable, and remembered here for when it is not.

Every delete is also logged, one line per image, so what to restore can be found later.
Both files live in the library's state directory, never in the library itself.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Iterable, List, Set

from immichtag import sidecar


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TrashSet:
    """Library-relative paths of the images in Immich's trash."""

    FILENAME = "trashed.json"

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / self.FILENAME

    def load(self) -> Set[str]:
        """Empty when missing or unreadable: the images then show until the next refresh,
        which is the safe way to be wrong."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(p) for p in data.get("paths", [])}
        except (OSError, ValueError, AttributeError):
            return set()

    def replace(self, paths: Iterable[str], source: str = "immich") -> None:
        self._write(set(paths), source)

    def add(self, paths: Iterable[str]) -> None:
        source = "immich"
        try:
            source = json.loads(self.path.read_text(encoding="utf-8")).get("source", source)
        except (OSError, ValueError, AttributeError):
            pass
        self._write(self.load() | set(paths), source)

    def _write(self, paths: Set[str], source: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"refreshedAt": _now(), "source": source, "paths": sorted(paths)}
        sidecar.write_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


class DeletionLog:
    """Append-only record of every image a delete from the page touched."""

    FILENAME = "deletions.jsonl"

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / self.FILENAME

    def append(self, job_id: str, records: List[dict], trash_enabled: bool | None) -> None:
        """`records` are {"path", "assetId", "outcome", "reason"}."""
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        at = _now()
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                entry = {"at": at, "job": job_id, "path": record["path"], "assetId": record.get("assetId"),
                         "outcome": record["outcome"], "reason": record.get("reason"),
                         "trashEnabled": trash_enabled}
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
