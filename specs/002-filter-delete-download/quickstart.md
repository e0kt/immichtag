# Quickstart: validating delete and download

These scenarios prove the feature end to end. The endpoints and fields are in [contracts/web-api.md](contracts/web-api.md), and the saved files are in [data-model.md](data-model.md).

## Prerequisites

- Immich 3.2.x is running (`docker ps` shows `immich_server`), and `~/.config/immichtag/config.toml` has a key with `asset.delete` added to the permissions listed in 001's contract.
- The library has been tagged (`uv run immichtag --dry-run` reports nothing new).
- `uv sync` has been run.

## Automated tests

```bash
uv run pytest tests/unit -q
```

The new unit tests use `tests/unit/fake_immich.py`, extended with `DELETE /api/assets`, `originalPath.in`, `trashedAt.ne` and `/api/server/features`. They must cover:

- **Selection**: `ids` and `all` + `except` resolve to the same ids that `/api/search` counts, and a stale `generation` gets 409.
- **Download**: `/api/download` returns bytes identical to the file, and a Japanese name is correct in `filename*`.
- **Bundle**:
  - the archive contains exactly the selected files, byte-identical;
  - duplicate names come out as `name.jpg` and `name-1.jpg`;
  - a file deleted between the POST and the GET is listed in `_skipped.txt`;
  - the token works once.
- **Delete preview and start**:
  - a wrong or missing `confirm` gets 400;
  - a busy export or tagging job gets 409;
  - a held `TaggingLock` gets 409 and names the holder.
- **Delete job**:
  - outcomes come out as `deleted`, `already`, `missing` and `failed`;
  - one bad id in a 500-id batch still trashes the other 499;
  - `force` is never sent;
  - trashed images leave `select()`, the tag counts and `PendingSync`;
  - `deletions.jsonl` gets one line per image.
- **Rebuild**: an index built with the trash set hides those images. After the fake server "restores" one, a rebuild shows it again.

## Manual end-to-end (real Immich)

Use a throwaway image so nothing real is at risk:

```bash
cp tests/fixtures/images/a.jpg ~/docker/immich/library/upload/<your user dir>/zz-quickstart.jpg
# let Immich pick it up (or upload a.jpg through the Immich UI), then:
uv run immichtag            # tags it and syncs
uv run immichtag-web
```

1. **Single download.** Filter so the test image shows, then click ⬇ on its card.
   - **Expected**: the browser saves `zz-quickstart.jpg`, and `cmp` against the original reports no difference. Nothing else on the page changes.
2. **Batch download.** Tick 3 cards, then click **Download**.
   - **Expected**: the page shows "3 images, <size>", and one `immichtag-3-….zip` arrives. `unzip -l` lists the 3 files.
3. **Select all.** Click **Select all M matching** on a filter with more than 60 matches.
   - **Expected**: the bar says M, and downloading gives M files. Changing a filter clears the selection.
4. **Single delete.** Open the test image in the viewer and click **Delete…**, then confirm.
   - **Expected**: the viewer closes, and the match count drops by 1 within 2 s with no rescan. Immich → Trash shows the image, and `~/Library/Application Support/immichtag/<id>/deletions.jsonl` has a `deleted` line for it.
5. **Survives a rescan.** Click **Rescan**.
   - **Expected**: the image stays hidden, even though the file is still on disk (`ls` shows it).
6. **Restore.** Restore it in Immich's Trash, then click **Rescan**.
   - **Expected**: it's back in the results, with its tags.
7. **Large-batch guard.** Select more than 100 images and click **Delete…**, then click **Cancel** in the dialog.
   - **Expected**: the dialog requires typing the exact count before Delete is enabled. Cancelling deletes nothing, and Immich's trash is unchanged.
8. **Immich down.** Run `docker stop immich_server` and reload the page.
   - **Expected**: the Delete buttons are disabled, with "Immich is not reachable" as the reason. Download still works. Afterwards, run `docker start immich_server`.
9. **Overlap refused.** Start `uv run immichtag` on new images in a terminal, then try a delete in the page.
   - **Expected**: the page refuses the delete and names the running tagging process.

Finally, empty the test image from Immich's trash, or leave it to expire.
