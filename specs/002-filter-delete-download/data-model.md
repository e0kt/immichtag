# Data Model: Delete and Download from Filter Results

There's no database: every entity is either held in memory by the web server (`AppState`) or stored as a small file in the library's state directory (`config.state_dir(root)`, never inside the library).

## Selection (request value)

This is what the page sends to say which images an action applies to. It's resolved on the server into an ordered list of index ids (research R6).

| Field | Type | Rules |
|---|---|---|
| `mode` | `"ids"` \| `"all"` | Required. |
| `generation` | int | Required. Must equal the index's current `generation`, or the server answers 409 (R4). |
| `ids` | int[] | `mode = "ids"` only. 1 to 100,000 entries, each `0 ≤ id < image_count` and not removed. Duplicates are dropped. |
| `query` | search payload | `mode = "all"` only. The same `filters` / `matchAll` / `excludeAll` / `order` / `seed` as `/api/search`. `maxImages` is ignored. |
| `except` | int[] | `mode = "all"` only, optional. These ids are left out of the resolved list. |

**Resolution**: `ids` stay in the order given. `all` → `index.order(index.select(...))` minus `except`. An empty result is an error ("Nothing selected").

**Lifetime on the page**: the selection is cleared when the filters, match mode or exclusions change, when the generation changes, and after a delete finishes.

## LibraryIndex (existing, extended)

| New field | Type | Meaning |
|---|---|---|
| `removed` | set[int] | Ids deleted since this index was built. They're absent from every posting set and from "everything". |
| `revision` | int | Bumped by every `remove()`. It's returned by `/api/search` and `/api/status`, so the page knows to refresh without the thumbnail cache being invalidated. |

- **New operation `remove(ids)`**: it discards the ids from every posting set, clears `_expansion_cache`, adds them to `removed`, and bumps `revision`.
- **`image_count`**: it keeps meaning "slots", so ids don't change. A new `live_count` (`len(images) - len(removed)`) is what `/api/status` reports as `images`.
- **`build()` gains an optional `hidden: set[str]` parameter**, the relative paths from the trash set. Those images are skipped entirely, so they never get an id.

## TrashSet (file: `<state_dir>/trashed.json`)

These are the library-relative paths Immich currently holds in its trash (R3).

```json
{"refreshedAt": "2026-09-29T10:15:00Z", "source": "immich", "paths": ["u/ab/cd/x.jpg", "..."]}
```

- It's **replaced wholesale** whenever the index build can reach Immich (`source: "immich"`).
- A delete job **adds** to it as soon as Immich accepts a batch (`source` stays the same).
- If the file is missing or unreadable, the set is empty. The images then show until the next successful refresh; that's safe, because nothing is deleted.
- It's written atomically with `sidecar.write_atomic`, the same as `PendingSync`.

## DownloadBundle (in memory: `AppState.bundles`)

| Field | Type | Meaning |
|---|---|---|
| `token` | str | 24 hex characters from `secrets.token_hex(12)`. It's the path segment in `/api/bundle/<token>.zip`. |
| `ids` | int[] | Resolved at creation. |
| `generation` | int | The index generation at creation. If a rescan happened in between, streaming fails with 409. |
| `count`, `bytes` | int | Shown before the download starts (FR-006). |
| `created` | float | `time.monotonic()`. The bundle expires after 600 s. |
| `used` | bool | Set to true when streaming starts. A token works once. |

**Archive layout**: the original files are at the archive root under their own names, with collisions renamed by `filterer.unique_stem`. The archive has a `_skipped.txt` only if some file couldn't be read, with one `name: reason` per line. The archive file name is `immichtag-<count>-<yyyymmdd-hhmmss>.zip`.

## DeletePreview (in memory: `AppState.delete_previews`)

This is a one-use token that binds what the user saw in the confirm dialog to what gets deleted (R7).

| Field | Type | Meaning |
|---|---|---|
| `token` | str | From `secrets.token_hex(12)`. |
| `ids` | int[] | Resolved at preview time. |
| `generation` | int | Must still match when the delete starts. |
| `count` | int | `len(ids)`. The start request must send `confirm == count`. |
| `created` | float | The token expires after 600 s. |

## DeleteJob (in memory: `AppState.delete_job`, one at a time)

| Field | Type | Meaning |
|---|---|---|
| `id` | str | 12 hex characters. |
| `state` | str | `resolving` → `trashing` → `finishing` → `finished` \| `error` |
| `total` | int | The number of images in the job. |
| `done` | int | The number of images with an outcome so far. |
| `deleted` | int | Outcome counts: the images trashed by this job. |
| `already` | int | Outcome counts: the images Immich already had in the trash. |
| `missing` | int | Outcome counts: the images with no Immich asset at that path. |
| `failed` | int | Outcome counts: the images that couldn't be trashed. |
| `failures` | `{name, reason}`[] | Every `missing` and `failed` image. The page shows the first 50 and says how many more there are. |
| `trashEnabled` | bool | From `/api/server/features`, recorded in the log. |
| `started`, `ended` | ISO str | |
| `error` | str \| null | A job-level failure, e.g. Immich unreachable or path map unknown. Nothing was trashed after this point. |

**How an image's outcome is decided**:

```text
            ┌─ no asset at mapped path ─────────────► missing
resolve ────┼─ asset.isTrashed ─────────────────────► already
            └─ asset found ──► DELETE batch ──ok────► deleted
                                   │ batch 4xx
                                   └► per-id retry ─ok► deleted
                                                   └──► failed(reason)
```

**When the job ends**, the `deleted` and `already` paths are:
- added to the TrashSet,
- removed from the live index (`remove`),
- removed from `PendingSync`.

The job is then appended to the DeletionLog.

**Invariant**: the job holds `TaggingLock` from `resolving` until it ends. While `state ∉ {finished, error}`, no export, tagging, sync or cleanup can start in this server.

## DeletionLog (file: `<state_dir>/deletions.jsonl`, append-only)

Each job writes one line per image, so `grep` finds any file:

```json
{"at": "2026-09-29T10:15:02Z", "job": "3f9a1c2b7d10", "path": "u/ab/cd/x.jpg", "assetId": "…", "outcome": "deleted", "reason": null, "trashEnabled": true}
```

`outcome` is one of `deleted`, `already`, `missing` or `failed`. The file is never rewritten or truncated by immichtag.
