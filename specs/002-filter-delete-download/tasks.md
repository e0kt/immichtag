---

description: "Task list for Delete and Download from Filter Results"
---

# Tasks: Delete and Download from Filter Results

**Input**: Design documents from `specs/002-filter-delete-download/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/web-api.md, contracts/immich-api.md, quickstart.md

**Tests**: included. The plan (Project Structure) and quickstart.md name the unit tests explicitly. They run with `uv run pytest tests/unit -q` against the in-process `tests/unit/fake_immich.py`, and the real Immich server is never used in tests.

**Organization**: tasks are grouped by user story:
- **US1**: single download (P1)
- **US2**: select + batch download (P2)
- **US3**: delete, single or batch (P2)

**Conventions for every task**:
- Match the surrounding code: stdlib only, `from __future__ import annotations`, the same docstring and comment density, and the same error style. Raise `ValueError` for a 400 in `_fail`.
- Every destructive or id-bearing endpoint must follow [contracts/web-api.md](contracts/web-api.md) exactly.
- "Research Rn" refers to [research.md](research.md), and "data-model" to [data-model.md](data-model.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 / US2 / US3

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: prepare the shared web plumbing that every story uses. There's no new project, dependency or config.

- [X] T001 In `src/immichtag/web.py`, add an `HTTPStatusError(Exception)` carrying a `status: int`. Make the handler's `_fail` send `exc.status` (409, 404) instead of 400 when given one. Add a module-level helper `content_disposition(name: str) -> str` that returns `attachment; filename="<ASCII fallback, non-ASCII replaced by _ and quotes stripped>"; filename*=UTF-8''<urllib.parse.quote(name, safe='')>` (research R4). Also move the extension→MIME dict out of `Handler._image` to a module-level `MIME_TYPES`, so the download code can reuse it.
- [X] T002 In `src/immichtag/web.py`, add a `Handler._stream_file(path, content_type, headers)` helper. It sends 200 with `Content-Length` from `path.stat().st_size` plus the extra headers, then copies the file to `self.wfile` in 1 MiB chunks. It must not call `read_bytes()`. If `path` is missing, it raises `HTTPStatusError(404, f"{path.name} is no longer on disk")` **before** any header is sent.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the index can hide and remove images, requests can be checked for staleness, and selections can be resolved. US2 and US3 depend on all of it, and US1 depends on T004.

**⚠️ CRITICAL**: complete this phase before starting Phase 4 or 5. Phase 3 (US1) needs only T004.

- [X] T003 [P] Write `tests/unit/test_index_remove.py`. Build a `LibraryIndex` with `index.build()` over a tmp library of 4 tagged images (reuse the `sidecar.build_xmp` + `PROV` pattern from `tests/unit/test_web_immich.py`) and assert:
  - (a) after `remove({1})`, `select([])` omits 1, `expand("long_hair")` omits 1, `tag_counts()` drops by one, `live_count == 3`, `image_count == 4`, `revision` went up by 1, and `generation` is unchanged;
  - (b) a glob expanded before `remove` doesn't return the removed id afterwards (the cache is cleared);
  - (c) `build(root, hidden={"u/aa/2.jpg"})` never assigns that image an id, and `image_count == 3`.
- [X] T004 In `src/immichtag/index.py`, extend `LibraryIndex` (data-model "LibraryIndex"):
  - fields `removed: Set[int]` and `revision: int = 0`;
  - property `live_count`;
  - method `remove(ids)`, which discards the ids from every set in `postings`, clears `self._expansion_cache`, updates `removed` and bumps `revision`;
  - in `select()`, "everything" becomes `set(range(len(self.images))) - self.removed` (both the no-filter path and the filter-with-no-required path).

  `tag_counts()` must skip tags whose posting set became empty. Makes T003 (a) and (b) pass.
- [X] T005 In `src/immichtag/index.py`, add `hidden: Set[str] | None = None` to `build()`. It holds library-relative POSIX paths, and images whose `image.relative_to(root).as_posix()` is in it are skipped before any id is assigned. `IndexHolder` must also accept `hidden_provider: Callable[[], Set[str]] | None` in `__init__` and call it at the start of each build, passing the result through. Read the rest of `IndexHolder` first and keep its threading and state logic unchanged. Makes T003 (c) pass.
- [X] T006 In `src/immichtag/web.py`, add `AppState.check_generation(value)`. It raises `HTTPStatusError(409, "The library was rescanned — reload the results.")` when `int(value)` differs from `self.holder.require().generation`.
- [X] T007 In `src/immichtag/web.py`, add `AppState.resolve_selection(payload) -> List[int]` per data-model "Selection".
  - It checks the generation (T006) first.
  - For `mode == "ids"`, it keeps the given order and drops duplicates. It rejects ids outside `0 ≤ id < image_count` or in `index.removed` with a `ValueError`, and caps at 100,000.
  - For `mode == "all"`, it runs `index.order(index.select(*self.parse_filters(query)), query.get("order") or "scan", query.get("seed"))`, ignores `maxImages`, and removes `except`.
  - An empty result raises `ValueError("Nothing selected.")`.
- [X] T008 In `src/immichtag/web.py`, add `revision` (`index.revision`) to the `search()` response. In `status()`, report `images` as `index.live_count` and add `revision`. Update `tests/unit/` only if an existing assertion reads `images`; run `uv run pytest tests/unit -q` to check.
- [X] T009 [P] Write `tests/unit/test_web_selection.py`, using an `AppState` over a tmp library of 4 images with two tag sets. Assert:
  - `resolve_selection` `ids` mode keeps the given order and drops duplicates;
  - `all` mode returns the same ids `search()` counts (`total`) for the same query, even with `maxImages: 1`;
  - `except` removes ids;
  - a wrong `generation` raises `HTTPStatusError` with status 409;
  - a removed id is rejected;
  - an empty result raises `ValueError`.

**Checkpoint**: `uv run pytest tests/unit -q` is green, the index supports removal and hiding, and selections resolve.

---

## Phase 3: User Story 1 - Download a single image from the results (Priority: P1) 🎯 MVP

**Goal**: one click on a thumbnail or in the viewer saves the original file under its original name (FR-001).

**Independent Test**: filter, click ⬇ on a card, and check that the saved file is byte-identical to the original and correctly named, and that nothing else on the page changed (spec US1).

### Tests for User Story 1

- [X] T010 [P] [US1] Write `tests/unit/test_web_download.py::test_single_*`. Start the real handler with `web.bind("127.0.0.1", 0, web.make_handler(state))` on a thread, and use `urllib.request`. Assert that `GET /api/download?id=0&v=<gen>`:
  - returns 200 with a body equal to the file's bytes and a matching `Content-Length`;
  - has a `Content-Disposition` containing `attachment`;
  - for an image named `ねこ.jpg`, contains `filename*=UTF-8''%E3%81%AD%E3%81%93.jpg`;
  - for a stale `v`, returns 409;
  - for a file deleted from disk after indexing, returns 404;
  - for a removed id (`index.remove`), returns 404.

### Implementation for User Story 1

- [X] T011 [US1] In `src/immichtag/web.py` `do_GET`, add the `/api/download` route.
  - It parses `id` and `v`, calls `state.check_generation(v)`, and raises `HTTPStatusError(404, ...)` if the id is out of range or in `index.removed`.
  - Then it calls `self._stream_file(path, MIME_TYPES.get(suffix, "application/octet-stream"), {"Content-Disposition": content_disposition(path.name), "Cache-Control": "no-store"})`.

  Makes T010 pass.
- [X] T012 [P] [US1] In `src/immichtag/web/app.js` `appendCards`, add a small `<a class="card-dl" href="/api/download?id=${item.id}&v=${generation}" download title="Download original">⬇</a>` inside each card. Its `onclick` calls `event.stopPropagation()`, so the viewer doesn't open. In `openViewer`, set the href of a new `#viewer-download` link the same way. Download failures (404/409) come back as a browser download error; that's acceptable, and no extra JS is needed.
- [X] T013 [P] [US1] In `src/immichtag/web/index.html`, add `<a class="btn" id="viewer-download" download>Download</a>` in `.viewer .side`, under `#viewer-name`.
- [X] T014 [P] [US1] In `src/immichtag/web/app.css`, style `.card-dl`: absolutely positioned top-right in `.card` (give `.card` `position: relative` if it isn't already), hidden until `.card:hover`, and with a visible focus style. Style `#viewer-download` like the existing `.btn`.

**Checkpoint**: US1 works on its own. You can test it manually as in [quickstart.md](quickstart.md) step 1.

---

## Phase 4: User Story 2 - Select several results and download them together (Priority: P2)

**Goal**: select results by hand or "all matching", see the count and size, and download them as one streamed ZIP (FR-002 to FR-006).

**Independent Test**: select 5 images, click Download, and check that one archive arrives containing exactly those 5 originals (spec US2).

### Tests for User Story 2

- [X] T015 [P] [US2] Add `test_bundle_*` tests to `tests/unit/test_web_download.py`, using the same live-handler fixture as T010. Assert:
  - `POST /api/bundle` with an `ids` selection returns `count`, `bytes` (the sum of the files' sizes), `url`, and `warn: false`;
  - `GET url` returns `application/zip`, and `zipfile.ZipFile(io.BytesIO(body))` lists exactly the selected names with byte-identical contents, stored with `ZIP_STORED`;
  - two images named `x.jpg` in different folders come out as `x.jpg` and `x-1.jpg`;
  - a file removed between the POST and the GET is listed in `_skipped.txt` and absent from the archive;
  - a second GET of the same token returns 404;
  - an `all` selection bundles every match;
  - `warn` is true when `count > 2000` (monkeypatch the threshold constant to 2 in the test).

### Implementation for User Story 2

- [X] T016 [US2] In `src/immichtag/web.py`, add a `DownloadBundle` dataclass per data-model: `token`, `ids`, `generation`, `count`, `bytes`, `created`, `used`. Also add the constants `BUNDLE_TTL = 600`, `BUNDLE_WARN_BYTES = 2 * 1024**3` and `BUNDLE_WARN_COUNT = 2000`. Add `AppState.bundles: Dict[str, DownloadBundle]` guarded by a lock, and `AppState.create_bundle(payload)`:
  - it resolves the selection (T007), drops expired bundles, and sums `stat().st_size`, skipping missing files;
  - it stores the bundle under `secrets.token_hex(12)`;
  - it returns `{token, count, bytes, size: filterer.human_size(bytes), url: f"/api/bundle/{token}.zip", warn}`.
- [X] T017 [US2] In `src/immichtag/web.py`, add `AppState.take_bundle(token) -> DownloadBundle`. It raises `HTTPStatusError(404)` if the token is unknown, expired or already used, then marks the bundle used, and raises 409 if the bundle's generation differs from the index's. Add `Handler._stream_bundle(bundle)`, which sends 200 with these headers:
  - `Content-Type: application/zip`
  - `Content-Disposition` built with `content_disposition(f"immichtag-{count}-{time.strftime('%Y%m%d-%H%M%S')}.zip")`
  - `Connection: close`
  - no `Content-Length`

  It then sets `self.close_connection = True`, opens `zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True)`, and for each id computes the archive name with `filterer.unique_stem(path.stem, used) + path.suffix`.
  - It copies each file with `zf.open(ZipInfo(name, date_time=<mtime>), "w", force_zip64=size > 2**31)` in 1 MiB chunks.
  - On `OSError` it records `name: reason` and continues. At the end it writes `_skipped.txt` if anything was skipped.
  - If the client disconnects (`BrokenPipeError` / `ConnectionResetError`), it returns quietly.

  Route `POST /api/bundle` → `create_bundle`, and `GET /api/bundle/<token>.zip` → `take_bundle` + `_stream_bundle`. Makes T015 pass.
- [X] T018 [P] [US2] In `src/immichtag/web/index.html`, add a `<div class="selection-bar" id="selection-bar" hidden>` at the top of `<main>`, above `.results-bar`. It contains:
  - `<span id="sel-count"></span>`
  - `<button class="btn" id="sel-all">`
  - `<button class="btn" id="sel-clear">Clear</button>`
  - `<button class="btn" id="sel-download">Download</button>`
  - `<button class="btn danger" id="sel-delete">Delete…</button>`: US3 wires it; until then it stays `hidden`.

  Also add a generic in-page modal `<div class="modal" id="modal" hidden><div class="modal-box"><div id="modal-body"></div><div class="actions" id="modal-actions"></div></div></div>` before `.toast`.
- [X] T019 [US2] In `src/immichtag/web/app.js`, add the selection state `state.sel = { mode: 'ids', ids: new Set(), except: new Set(), lastClicked: null }`, plus these helpers:
  - `selectionCount()`: in `ids` mode, `ids.size`; in `all` mode, `state.total - except.size`.
  - `selectionPayload()`: returns `{mode, ids:[...], generation}`, or `{mode:'all', query: searchPayload(), except:[...], generation}`. Reuse or extract the function that builds the `/api/search` body; read `search()` first.
  - `clearSelection()`
  - `renderSelection()`: toggles `#selection-bar`, sets the `#sel-count` text to "N selected", sets the `#sel-all` text to `Select all ${state.total.toLocaleString()} matching`, and toggles a `.selected` class on the visible cards.

  Store the generation of the current results in `state.generation` when `search()` gets a response. Call `clearSelection()` wherever the filters, match mode, exclusions or generation change: find every call site that triggers a fresh `search()` (not `loadMore`) and clear there (FR-004).
- [X] T020 [US2] In `src/immichtag/web/app.js` `appendCards`, add a `<input type="checkbox" class="card-check">` to each card.
  - Its click stops propagation and toggles the id in `sel.ids`, or in `sel.except` in `all` mode.
  - Shift-click selects the range between `lastClicked` and this card, by their position in the grid.
  - Cards loaded later in `all` mode render as checked unless they're in `except`.

  Call `renderSelection()` after each change. Wire `#sel-all` (`mode='all'`, clear `except`) and `#sel-clear`.
- [X] T021 [US2] In `src/immichtag/web/app.js`, add `async function downloadSelection()`:
  - it POSTs `/api/bundle` with `selectionPayload()` and shows the modal: "Download N images (size) as one zip?" with Cancel and Download buttons. When `warn` is set, it adds a line that it's large and may take a while.
  - On confirm it sets `window.location.href = data.url`, so the browser's download manager takes over (research R5).
  - A 409 error toasts "The library was rescanned — reload the results." and re-runs `search()`.

  Add a small `showModal(bodyNodes, actions)` / `hideModal()` pair next to the existing `immichActions` helper, reusing its `{label, primary, onclick, disabled}` shape; US3 reuses it. Wire `#sel-download`.
- [X] T022 [P] [US2] In `src/immichtag/web/app.css`, add styles for:
  - `.card-check` (top-left; visible on hover or when the body has a `.selecting` class, i.e. when any image is selected);
  - `.card.selected` (an accent outline);
  - `.selection-bar` (sticky at the top of `main`, flex, wrapping on narrow widths);
  - `.btn.danger`;
  - `.modal` (a fixed overlay above `.viewer`) and `.modal-box` (centered, max-width 560px).

  Use the existing CSS variables in `app.css`, and read its top to find them.

**Deviation (implemented)**: the selection controls sit in the existing results row, which is sticky, not in a separate bar above it. A separate bar pushed the grid down when a selection started, so a shift-click landed on the wrong card.

**Checkpoint**: US1 and US2 both work. You can test manually as in quickstart.md steps 2–3.

---

## Phase 5: User Story 3 - Delete one image or a selection (Priority: P2)

**Goal**: delete moves images to Immich's trash after a count-exact confirmation. It shows progress and a report, the results update without a rescan, trashed images stay hidden across rescans and reappear once restored, and every run is logged (FR-007 to FR-015).

**Independent Test**: delete one image from the viewer, then check that it's gone from the grid and count, that Immich shows it in the trash, and that restoring it and clicking Rescan brings it back (spec US3). Quickstart.md steps 4–9 cover the full check.

### Tests for User Story 3

- [X] T023 [P] [US3] Extend `tests/unit/fake_immich.py` per contracts/immich-api.md. Read the whole file first and follow its request-dispatch style. Add:
  - `self.trashed: set` of asset ids;
  - `self.features = {"trash": True}`;
  - `self.trash_calls: List[dict]`;
  - `self.deny_ids: set`: a `DELETE` containing any of these returns 400, all-or-nothing like `requireAccess`.

  Handle these requests:
  - `GET /api/server/features` returns `self.features`.
  - `DELETE /api/assets` records the body, returns 400 if any id is in `deny_ids` or unknown, returns 403 if `self.deny_delete_permission`, and otherwise adds the ids to `trashed` and returns 204. It asserts that `force` isn't true, so a test fails loudly if it ever is.
  - In `POST /api/search/metadata`: support `filter.originalPath.in` (exact match), support `filter.trashedAt.ne == null` (trashed only), and include `"isTrashed": id in self.trashed` in every item.

  Add a helper `restore(asset_id)`.
- [X] T024 [P] [US3] Write `tests/unit/test_immich_trash.py` against `FakeImmich` with an `ImmichClient`. Assert:
  - `features()["trash"]` is returned;
  - `assets_by_path([...])` maps paths to `(id, is_trashed)`, and 1,200 paths take 3 requests;
  - `iter_trashed()` yields only trashed assets;
  - `trash(ids)` sends batches of ≤500 with `force: false`;
  - `trash_each(ids)` returns per-id outcomes when one id is denied, and the other ids are still trashed;
  - a 403 raises `ImmichAuthError` whose message contains `asset.delete`;
  - `resolve_path_map` returns the configured map when `path_map` is set, and otherwise autodetects it.
- [X] T025 [P] [US3] Write `tests/unit/test_trash_state.py` for `src/immichtag/trash.py`. Assert:
  - `TrashSet.load()` returns an empty set for a missing or corrupt file;
  - `replace()` then `load()` round-trips;
  - `add()` unions and persists;
  - the file is written atomically, with no leftover tmp file;
  - `DeletionLog.append(job_id, records)` writes one JSON line per record, with the keys from data-model "DeletionLog", and appends without truncating.
- [X] T026 [P] [US3] Write `tests/unit/test_web_delete.py`, reusing the `fake` and `app` fixtures pattern from `tests/unit/test_web_immich.py` (4 images whose fake assets are at `/data/upload/...`, path map configured). Wait for jobs by polling `state.delete_status()` with a timeout. Assert:
  1. `delete_preview` returns `count`, up to 12 `sample` items, `trashEnabled`, and `typeToConfirm` (true when count > 100, monkeypatching the threshold, or when `fake.features["trash"] = False`).
  2. `start_delete` with a missing or wrong `confirm` raises `ValueError`, an unknown token raises, and a token can't be used twice.
  3. A full run: the fake has the assets trashed, and `force` is never true. The job ends `finished` with the correct `deleted` count. The images are gone from `state.search(...)` and from `/api/tags` counts. Their paths are in `trashed.json`, removed from `PendingSync` (seed an entry first), and written to `deletions.jsonl` with outcome `deleted`.
  4. Mixed outcomes: one asset already trashed → `already`; one path with no fake asset → `missing`; one id in `deny_ids` → `failed` with a reason. The rest are deleted.
  5. Busy guards: with an active export job in `state.jobs`, `delete_preview` and `start_delete` raise 409. During a delete (patch the worker to block on an event), `start_export`, `start_tagging` and `start_sync` raise 409.
  6. With `library.TaggingLock(state_dir)` held in the test, `start_delete` raises 409 whose message contains "another tagging run".
  7. Rebuild and restore: after a delete, `state.reindex()` (wait for `holder.ready`) still hides the image. After `fake.restore(id)` and another reindex, it's back.
  8. Not configured (monkeypatch `config.load_immich_settings` as `test_not_configured` does): `status()["immich"]["canDelete"]` is False, and `delete_preview` raises.

### Implementation for User Story 3

- [X] T027 [P] [US3] Create `src/immichtag/trash.py` with a module docstring explaining research R3 (trashed files stay on disk, so the index hides them). Add:
  - `class TrashSet`: `FILENAME = "trashed.json"`, `__init__(state_dir)`, `load() -> Set[str]` (empty on a missing file or a JSON error), `replace(paths, source="immich")` (writes `{"refreshedAt", "source", "paths": sorted}`), and `add(paths)` (load, union, write). Every write goes through `sidecar.write_atomic`.
  - `class DeletionLog`: `FILENAME = "deletions.jsonl"`, and `append(job_id, records, trash_enabled)`. Each record is `{"path", "assetId", "outcome", "reason"}`, and one line is written per record, adding `at` and `job`.

  Model it on `library.PendingSync`. Makes T025 pass.
- [X] T028 [US3] In `src/immichtag/immich.py`, extract the path-map logic from `sync()` into `resolve_path_map(client, settings, root, sample=None) -> PathMap | None`.
  - It uses `settings.path_map` when set.
  - Otherwise it autodetects from `sample`, or, when `sample` is None, from the first 500 paths of `client.iter_assets()`.

  Make `sync()` call it, passing its already-listed `assets` as the sample. `uv run pytest tests/unit/test_immich_sync.py -q` must stay green.
- [X] T029 [US3] In `src/immichtag/immich.py` `ImmichClient`, add these methods (contracts/immich-api.md):
  - `features() -> dict`: `GET /api/server/features`.
  - `assets_by_path(remote_paths) -> Dict[str, Tuple[str, bool]]`: `POST /api/search/metadata {"filter": {"originalPath": {"in": chunk}}, "size": 500}` per chunk of 500, returning `originalPath -> (id, isTrashed)`. If `self._legacy_search` is set, or the v3.2 shape answers 400, it falls back to one pass over `iter_assets()`. In that case trashed state is unknown (False).
  - `iter_trashed() -> Iterator[Tuple[str, str]]`: `filter {"trashedAt": {"ne": None}}`, paged by cursor like `iter_assets`. It raises `ImmichError` on a legacy server, and the caller keeps the saved set.
  - `trash(ids)`: `DELETE /api/assets {"ids": chunk, "force": False}` per chunk of 500.
  - `trash_each(ids) -> Dict[str, str | None]`: `trash([id])` per id, returning id → None on success or the error message. `ImmichAuthError` and `ImmichUnavailable` are re-raised, not swallowed.

  Add `"/api/assets": "asset.delete"` to `PERMISSIONS`. Check `ImmichAuthError`'s `base` computation: `/api/assets` must map to `asset.delete`, and the existing `/api/assets/jobs` entry must still win for job calls. Makes T024 pass together with T028.
- [X] T030 [US3] In `src/immichtag/web.py` `AppState.__init__`, construct `IndexHolder(root, hidden_provider=self._refresh_trash)` (T005). Add `_refresh_trash() -> Set[str]`:
  - When Immich is configured, it tries `ImmichClient(settings, timeout=10)`, `resolve_path_map`, and `iter_trashed()`. It maps each path to a library-relative POSIX path and discards paths outside the root, then calls `TrashSet(state_dir).replace(paths)` and returns them.
  - On any `ImmichError`, or when not configured, it returns `TrashSet(state_dir).load()`.

  It must never raise, because a failure here must not break indexing. Also make `reindex()` pass a new root correctly (read `IndexHolder.start`).
- [X] T031 [US3] In `src/immichtag/web.py`, add `AppState.busy() -> str | None`. It returns:
  - `"tag"` if `self.tag_job and self.tag_job.active`;
  - `"sync"` / `"cleanup"` if `self.sync_job` is running;
  - `"export"` if any `ExportJob` in `self.jobs` has `state == "running"`;
  - `"delete"` if `self.delete_job` is active;
  - otherwise None.

  In `start_export`, `start_tagging`, `_start_job` (sync/cleanup) and the new delete start, raise `HTTPStatusError(409, ...)` when a **different** kind is busy. The existing "attach to the running job of the same kind" behaviour stays. Add `busy` to `status()`.
- [X] T032 [US3] In `src/immichtag/web.py`, extend `immich_status()` with `"canDelete": configured and reachable`, plus `"deleteReason"`: `"No Immich API key configured"`, `"Immich is not reachable"`, or None.
- [X] T033 [US3] In `src/immichtag/web.py`, add a `DeletePreview` dataclass (token, ids, generation, count, created) and `AppState.delete_previews` (with a lock, TTL 600 s, and a `DELETE_TYPE_THRESHOLD = 100` constant). Add `AppState.delete_preview(payload)`:
  - It requires `canDelete`, else raises `ValueError(deleteReason)`, and requires `busy()` to be None, else raises 409.
  - It resolves the selection (T007) and calls `ImmichClient(settings, timeout=5).features().get("trash")`; on `ImmichError`, trash is None.
  - It stores the preview and returns `{token, count, sample: [{id, name, generation}] for the first 12, trashEnabled, typeToConfirm: count > DELETE_TYPE_THRESHOLD or trashEnabled is not True}`.
- [X] T034 [US3] In `src/immichtag/web.py`, add a `DeleteJob` dataclass with the fields in data-model "DeleteJob" and `as_dict()` per contracts/web-api.md `GET /api/delete`. `failures` is capped at 50, with `moreFailures` counting the rest, and `active` means `state in ("resolving", "trashing", "finishing")`. Add `AppState.delete_job`, `AppState.delete_lock`, and `delete_status() -> {"job": ...}`.
- [X] T035 [US3] In `src/immichtag/web.py`, add `AppState.start_delete(payload)`:
  - **Checks, under `delete_lock`**:
    - If a delete is active, return it; a second POST attaches.
    - Pop the preview by `payload["token"]`. If it's unknown or expired, raise `ValueError("This confirmation expired — try again.")`.
    - If `payload.get("confirm") != preview.count` (with `isinstance(..., int)` and not bool), raise `ValueError("Confirm the number of images to delete.")`.
    - Call `check_generation(preview.generation)`. If `busy()` returns a value, raise 409.
    - Acquire `library.TaggingLock(self._state_dir())`. On `LockBusy`, raise `HTTPStatusError(409, str(exc))`.
  - **Then** create the job and start the `_run_delete(job, ids, lock)` thread. The thread releases the lock in `finally`.
- [X] T036 [US3] In `src/immichtag/web.py`, implement `_run_delete(job, ids, lock)` per data-model "outcome decision" and research R2/R7:
  1. **`resolving`**:
     - Build a client and call `resolve_path_map`. If it's None, the job errors with "cannot match Immich's paths to this library; set [immich].path_map".
     - Map each id's path → remote path, then call `assets_by_path`.
     - Classify each image as `missing`, `already` or to-trash.
  2. **`trashing`**, in chunks of 500:
     - Call `client.trash(chunk)`. On `ImmichError` other than auth or unavailable, fall back to `trash_each(chunk)`.
     - Update `done`, `deleted` and `failed` as you go.
     - On `ImmichAuthError` or `ImmichUnavailable`, stop, set the job error, and mark the remaining images `failed` with that reason.
  3. **`finishing`**, for `deleted` + `already` paths (relative):
     - `TrashSet.add(...)`;
     - `index.remove(ids)`;
     - `PendingSync.remove(paths)`;
     - clear any cached thumbnails for those ids;
     - `DeletionLog.append(job.id, all records, job.trashEnabled)`.

     These steps run **even when step 2 stopped early**, for whatever was already trashed.
  4. Set `job.state` to `finished`, or to `error` if `job.error` is set, and set `job.revision`.

  Wrap the whole thing so an unexpected exception sets `state = "error"`, and `lock.release()` always runs. Makes T026 pass.
- [X] T037 [US3] In `src/immichtag/web.py`, add the routes: `GET /api/delete` → `delete_status()`, `POST /api/delete/preview` → `delete_preview(payload)`, and `POST /api/delete` → `start_delete(payload)`. For both POSTs, first reject any request whose `Content-Type` isn't `application/json` with `HTTPStatusError(415, "JSON required")` (plan: Complexity Tracking).
- [X] T038 [P] [US3] In `src/immichtag/web/index.html`, add:
  - `<button class="btn danger" id="viewer-delete">Delete…</button>` next to `#viewer-download`;
  - a `<div class="job" id="delete-panel" hidden>` in `.jobs`, with `#delete-title`, `#delete-pct`, a bar `#delete-bar`, `#delete-detail`, and `#delete-actions`, mirroring `#export-panel`.

  Un-hide `#sel-delete`.
- [X] T039 [US3] In `src/immichtag/web/app.js`, add `async function confirmDelete(payload)`:
  - It POSTs `/api/delete/preview` and shows the modal (T021) with:
    - the count;
    - up to 12 sample thumbnails (`/api/thumb?id=…&v=…`);
    - the text "Moves N images to Immich's trash. Restore them there within its retention period.", or, when `trashEnabled !== true`, a `.warn` line "Immich's trash is off or unknown — this delete may be permanent.".
  - When `typeToConfirm` is set, it adds a number input, and the Delete action stays disabled until `Number(input.value) === count`.
  - On Delete, it POSTs `/api/delete {token, confirm: count}`, hides the modal and starts polling (T040).
  - Errors toast the server message. For a 409 about a rescan, it also re-runs `search()`.

  Wire `#sel-delete` → `confirmDelete(selectionPayload())`, and `#viewer-delete` → `confirmDelete({mode:'ids', ids:[currentItem.id], generation: currentItem.generation})`. Store the open item in `openViewer`.
- [X] T040 [US3] In `src/immichtag/web/app.js`, add `pollDelete()`, modelled on the export panel's polling (read how the export job is polled and rendered first). It `GET`s `/api/delete` every 1 s while `job.active` and renders:
  - the phase, `done/total` and the bar;
  - when finished: "Deleted X · already in trash Y · not in Immich Z · failed W", and a scrollable list of `failures` (plus "and N more" when there are more);
  - a "Show log folder" action (`/api/reveal` with the state dir; add `stateDir` to `/api/status` in `web.py` if it isn't there) and a Dismiss action.

  When the job ends, it closes the viewer if its image was among those deleted, calls `clearSelection()`, re-runs `search()` and refreshes the status. On page load, if `/api/delete` returns an active job, it calls `pollDelete()` (reattach).
- [X] T041 [US3] In `src/immichtag/web/app.js`, wherever `/api/status` is applied, set `#sel-delete.disabled` and `#viewer-delete.disabled` from `!status.immich.canDelete || status.busy`. Set their `title` to `status.immich.deleteReason`, or `"Busy: ${status.busy}"` when a job is running. Download controls are never disabled by Immich state (FR-012).
- [X] T042 [P] [US3] In `src/immichtag/web/app.css`, style `#viewer-delete`, the modal's sample thumbnail strip (small square grid), the `.warn` line (a danger color from the existing variables), the type-to-confirm input, and `#delete-panel`'s failure list (max-height with scrolling).
- [X] T043 [US3] In `src/immichtag/immich_cli.py`, add `asset.delete` to the permissions that `status` prints (find where the required permissions are listed), noting "(for deleting from immichtag-web)".

**Checkpoint**: all three stories work independently, and `uv run pytest tests/unit -q` is green.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T044 [P] In `README.md`, extend the `immichtag-web` section with "Downloading and deleting". Cover:
  - the ⬇ single download of originals, selecting (including "all matching"), and zip downloads;
  - that Delete moves images to **Immich's trash** (restorable for its retention period; permanent if Immich's trash is disabled), that the key needs `asset.delete`, and the confirmation rules;
  - that trashed images stay hidden across rescans and reappear after a restore;
  - where `deletions.jsonl` lives.

  Also add `asset.delete` to the permissions list in the `## Immich` section, and strengthen the `--host` note, because the page can now delete.
- [X] T045 [P] Update the module docstring of `src/immichtag/web.py` to mention downloading and deleting (via Immich's trash).
- [ ] T046 Run `uv run pytest tests/unit -q` and fix any failures. Then walk [quickstart.md](quickstart.md) steps 1–9 against the real Immich, using the throwaway `zz-quickstart.jpg`, and record anything that differs in the spec's Assumptions or in research.md.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001–T002)**: must come first; both edit `web.py`, so they run in order.
- **Foundational (T003–T009)**:
  - T004 → T005, both in `index.py`, in order.
  - T006 → T007 → T008, in `web.py`, in order.
  - T003 and T009 (tests) can be written in parallel with the implementation.
- **US1 (T010–T014)**: needs T001, T002 and T004 (it uses `removed`), and T006. It doesn't need T005, T007 or T008.
- **US2 (T015–T022)**: needs all of Phase 2 and US1's T001 plumbing. It doesn't depend on US1's UI.
- **US3 (T023–T043)**: needs all of Phase 2. It reuses US2's modal helper (T021), the selection payload (T019) and the `#sel-delete` markup (T018). If US3 is built without US2, T039 must add the modal helper itself.
- **Polish (T044–T046)**: after the stories you ship.

### Within US3

- T023 (fake) blocks T024 and T026.
- T027 (`trash.py`) and T028 → T029 (`immich.py`) can run in parallel with each other.
- T030 needs T005, T027 and T029.
- In `web.py`: T031 → T032 → T033 → T034 → T035 → T036 → T037, one file, in order.
- The UI tasks T038 → T039 → T040 → T041 are in order in `app.js`. T042 (CSS) can run in parallel.

### Parallel opportunities

- **Phase 2**: T003 ∥ T009 ∥ (T004→T005) ∥ (T006→T007→T008). Different files, but the tests assert the implementations.
- **US1**: after T011, T012 ∥ T013 ∥ T014 (`app.js`, `index.html`, `app.css`).
- **US2**: T015 ∥ T018 ∥ T022. T019 → T020 → T021 in order (`app.js`). T016 → T017 in order (`web.py`).
- **US3**: T023 ∥ T025 ∥ T027 ∥ T028, then T024 ∥ T026. T038 ∥ T042 alongside the `web.py` chain.
- **Polish**: T044 ∥ T045.

### Parallel example: User Story 3 kick-off

```text
Task: "T023 Extend tests/unit/fake_immich.py with trash/features/originalPath.in"
Task: "T025 Write tests/unit/test_trash_state.py"
Task: "T027 Create src/immichtag/trash.py (TrashSet, DeletionLog)"
Task: "T028 Extract resolve_path_map() in src/immichtag/immich.py"
```

---

## Implementation Strategy

### MVP first (User Story 1 only)

1. Phase 1 (T001–T002), then T004 and T006 from Phase 2.
2. Phase 3 (T010–T014).
3. **Stop and validate**: run `uv run pytest tests/unit/test_web_download.py -q`, then quickstart step 1. It's safe to ship on its own, because nothing destructive has been added.

### Incremental delivery

1. **Setup + Foundational**: the index can remove and hide images, and selections resolve.
2. **+ US1**: single download (MVP).
3. **+ US2**: selection and zip download. It's still non-destructive.
4. **+ US3**: delete via Immich's trash. It needs `asset.delete` on the key, and is validated with quickstart steps 4–9 on a throwaway image.
5. **Polish**: README and docstring.

---

## Notes

- **[P]**: different files and no dependency on an incomplete task. Tasks editing the same file (`web.py`, `app.js`) are never marked [P] against each other.
- **Never send `force: true`** to `DELETE /api/assets`, and never remove files from the library. The fake server asserts the first (T023).
- **Commit after each checkpoint.** Ask before committing, per the repo's usual flow.
