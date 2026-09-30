# Research: Delete and Download from Filter Results

Each item resolves a question from the plan's Technical Context. The evidence is the compiled source of the Immich server that is actually running (`immich_server`, image `immich-server:v3`, read from `/usr/src/app/server/dist` on 2026-09-29), the same method as `specs/001-pixai-full-retag/research.md` R8 and R9. Where the evidence is this repository's code, the file is named.

## R1. How to "delete" in Immich

**Decision**: `DELETE /api/assets` with body `{"ids": [...], "force": false}`, in batches of at most 500 ids. It needs the `asset.delete` permission and answers 204.

**Findings** (`controllers/asset.controller.js`, `services/asset.service.js` `deleteAll`):
- `force: false` sets `status = trashed` and `deletedAt = now`, then emits `AssetTrashAll`. `force: true` would set `status = deleted`, which is permanent. The spec (FR-008) forbids that, so the client never sends `force: true`.
- The handler first calls `requireAccess(..., ids)` for the whole list, so the call is **all-or-nothing**. One id the key can't access fails the whole batch with a 400.
- Restoring is `POST /api/trash/restore/assets {"ids": [...]}` (same `asset.delete` permission). It's done by the user in Immich; immichtag doesn't call it.
- The trash is emptied automatically after `trash.days` days (30 by default, `dtos/config.dto.js`). If `trash.enabled` is false, `trashedDays = 0` (`asset.service.js:177`) and trashed assets are purged at the next daily cleanup. `GET /api/server/features` reports `trash: true|false` without needing any permission.

**Consequences**:
- A batch failure falls back to deleting that batch one id at a time, so a single bad id can't block the other 499 (FR-010).
- If the features endpoint says the trash is off, the confirm dialog says a delete will be **permanent within a day**, and the typed confirmation is required at any count.

**Alternatives rejected**:
- **Removing files from disk.** Immich would keep an asset pointing at a missing file, which violates SC-004.
- **Direct database access.** It's already excluded by 001's Immich contract.
- **`force: true`.** It isn't reversible.

## R2. Mapping index images to Immich asset ids

**Decision**: map local paths to Immich's paths with `PathMap` (from `[immich].path_map`, or autodetected as the sync already does). Look the assets up with `POST /api/search/metadata {"filter": {"originalPath": {"in": [<≤500 paths>]}}, "size": 500}`. On an older server that rejects the v3.2 shape, fall back to one full `iter_assets()` scan and a dict.

**Findings**:
- `originalPath` is a `StringPatternFilter`, which includes `eq`, `ne`, `in` and `notIn` (`dtos/search.dto.js:134-155`).
- The v3.2 filter search applies no trash filter, so an asset that is already trashed is still found. Its `isTrashed` field comes from `mapAsset` (`asset-response.dto.js:162`).

**Consequences**: each image gets exactly one outcome:
- `deleted`
- `already_trashed`: it's reported and not sent again.
- `not_in_immich`: no asset has that path. This happens with uploads Immich hasn't discovered yet.
- `failed` with a reason.

Resolving 10,000 images takes 20 search requests, compared with about 88 pages to scan the whole 88k library.

**Alternatives rejected**: scanning every asset on every delete. It's correct, but it takes several seconds for a one-image delete, so it's kept only as the fallback for older servers.

## R3. Trashed files stay on disk: hiding them from the index

**Finding**: trashing only changes the asset's row in the database (R1). The image and its `.xmp` sidecar stay in the upload folder until the trash is emptied. A plain rescan would therefore bring every deleted image straight back into the results, which violates FR-011.

**Decision**: keep a **trash set**, the library-relative paths that Immich has in its trash:
- When the index is built, and Immich is reachable, the set is refreshed from `POST /api/search/metadata {"filter": {"trashedAt": {"ne": null}}}`, paged by cursor and mapped to local paths. It's saved to `<state_dir>/trashed.json`.
- When Immich isn't reachable, the saved `trashed.json` is used.
- A delete job adds its `deleted` and `already_trashed` paths to the set and to the file as soon as Immich accepts them.
- Images in the set are left out of the index build. This also hides images trashed from Immich's own UI, which is consistent: they're deleted as far as the user is concerned.
- **Restore** (spec, Story 3, scenario 6): after the user restores an image in Immich, the next rebuild no longer finds it in Immich's trash, so it reappears.

**Deleting from the live index without a rebuild** (FR-011, SC-006): `LibraryIndex.remove(ids)` removes the ids from every posting set, clears the glob-expansion cache, and adds them to a `removed` set. `select()` treats `removed` as absent when "no filters" means "everything". Ids aren't reassigned, so `generation` stays the same, thumbnails already cached remain valid, and the rest of the grid keeps working. A new `revision` counter is bumped so the page knows to re-run its search.

**Alternatives rejected**:
- **A full rebuild after each delete.** It takes tens of seconds on 88k images, which fails SC-006.
- **Hiding only through a local log of deletions.** Restores done in Immich would never show up again.

## R4. Single-image download

**Decision**: `GET /api/download?id=<id>&v=<generation>`:
- It answers with `Content-Disposition: attachment; filename="<ascii fallback>"; filename*=UTF-8''<percent-encoded name>` (RFC 6266 / 5987), because many names in the library are Japanese.
- It sets `Content-Length` from `stat` and streams the file in 1 MiB chunks. It never reads the whole file into memory, unlike `/api/image` today.
- The page triggers it with a plain `<a href download>`, so the browser's own download manager handles it.

**Stale ids**: every request that names ids also carries `v` (the generation). The server rejects a mismatch with 409 "the library was rescanned — reload the results" rather than serve or delete a different image under a reused id. This matters most for delete.

## R5. Batch download: a single streamed ZIP

**Decision**: a two-step process:
1. `POST /api/bundle` with a selection returns `{token, count, bytes, skipped}`. `bytes` is the sum of `stat` sizes, and the page shows it before downloading (FR-006). The page asks for a second confirmation above 2 GB or 2,000 images.
2. The page then navigates to `GET /api/bundle/<token>.zip`, which streams a ZIP written by `zipfile.ZipFile(stream, "w", ZIP_STORED)` straight to the socket.

**Findings and rationale**:
- `zipfile` supports writing to a non-seekable stream: it uses data descriptors, and ZIP64 turns on automatically past 4 GiB. **STORED** (no compression) is right for JPEG, PNG and WebP, which don't compress further, and it keeps CPU near zero. Memory stays at one chunk.
- There's no `Content-Length`, so the response uses `Connection: close` and the end of the stream marks the end of the body. The server is `ThreadingHTTPServer`, so the stream doesn't block other requests.
- A GET URL is what lets the browser's download manager own the transfer: progress, pause, and the Downloads folder. A `fetch` + Blob approach would buffer the whole archive in the tab.
- Tokens are held in memory, used once, and expire after 10 minutes. A token holds the ids already resolved, so the archive contains exactly what was confirmed.
- **Names inside the archive**: files keep their own names, and collisions are resolved with the existing `filterer.unique_stem` (`name-1.jpg`), the same rule Export uses. Files that disappeared or can't be read are skipped and listed in `_skipped.txt` at the root of the archive.

**Alternatives rejected**:
- **Building the zip in a temporary file first.** It doubles the disk use for large batches, and there's a long wait before the download starts.
- **Deflate compression.** It costs CPU and gains nothing on image formats.
- **One browser download per file.** Browsers block or prompt on many downloads at once.

## R6. Selection model

**Decision**: the page sends a selection in one of two forms:
- `{"mode": "ids", "ids": [...], "generation": g}`, for images picked by hand.
- `{"mode": "all", "query": <search payload>, "except": [...], "generation": g}`, for "select all matching", optionally minus images the user unticked.

The server resolves the selection with the same `index.select` + `order` used for search. The page's `maxImages` cap is ignored here: "all matching" means every match, and the page states the count.

The page clears the selection on any change to the filters, match mode or exclusions (FR-004), and after a rescan (the generation changes).

**Rationale**: "all matching" can mean 60,000 ids, most of which the page has never loaded (the grid is paged 60 at a time). Sending the query instead of the ids keeps the request small, and it's exactly what the user saw counted.

## R7. Delete job, confirmation, and mutual exclusion

**Decision**: a two-step process that mirrors the existing tag cleanup (`cleanup-preview`, then `cleanup` with `confirm`):
1. `POST /api/delete/preview` with a selection returns `{token, count, sample: [≤12 items], trashEnabled, immich}`.
2. `POST /api/delete {"token": t, "confirm": <count>}` starts the job.
   - The server requires `confirm` to equal the count held by the token, **at every size**. That's the server-side guarantee behind SC-005.
   - For more than 100 images (or when the trash is off), the page makes the user type the number, and only then sends `confirm`.

**Job**: a `DeleteJob` runs on a worker thread and is polled at `GET /api/delete`. Reloading the page reattaches to the running job, as it does for tagging. The phases are `resolving`, `trashing`, `finishing`, then `finished` or `error`. There's no cancel: once batches are sent to Immich they're already in the trash, and a partial stop would only complicate the report. A batch is 500 ids, so a 1,000-image delete takes seconds.

**Mutual exclusion** (FR-013):
- **Inside the web server**: a delete can't start while a tagging, sync, cleanup or export job is active, and none of those can start while a delete is active. A single `AppState.busy()` check covers every start path.
- **Across processes**: the delete holds `library.TaggingLock` for its whole duration, so a terminal `immichtag` run and a delete can never overlap. Whichever starts second is refused with the holder's details, the same message tagging already uses.

**When the job ends**:
- Deleted and already-trashed paths are:
  - added to the trash set (R3),
  - removed from the live index (R3),
  - removed from `PendingSync` (FR-015).
- The run is appended to `<state_dir>/deletions.jsonl` (FR-014), which is never inside the library.

## R8. Immich availability and permission

**Decision**:
- `/api/status` (`immich_status`) gains a `canDelete` field: configured and reachable. The page disables Delete with the reason when `canDelete` is false (FR-012). Download never looks at Immich.
- A missing `asset.delete` permission surfaces as an `ImmichAuthError`, and its message names the permission (add `"/api/assets": "asset.delete"` to `immich.PERMISSIONS`). `immichtag-sync status` lists `asset.delete` among the permissions the key should have.

**Rationale**: the key permissions aren't probed ahead of time, because Immich has no read-only way to test for `asset.delete`. The first failed call gives a precise message, and nothing has been trashed at that point.
