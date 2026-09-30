# Implementation Plan: Delete and Download from Filter Results

**Branch**: `002-filter-delete-download` (spec directory; work is currently on `main`, no branch hook configured) | **Date**: 2026-09-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-filter-delete-download/spec.md`

## Summary

This feature adds three things to the `immichtag-web` results grid and viewer:

- a one-click download of a single original file,
- a selection mechanism (pick images by hand, or "all matching" the current filters),
- batch download (one streamed ZIP) and delete, for one image or for the selection.

**How delete works**: delete goes through **Immich's trash**, `DELETE /api/assets` with `force: false`, so it's reversible, and Immich's database never points at a missing file.

**Why the index needs a trash set**: Immich's trash only changes the asset's database row, and the files stay on disk (research R3). So the web index learns which images are trashed from Immich, and hides them both in the live index and across rescans. Images restored in Immich come back on the next rescan.

**Safety**:
- Every delete needs a server-checked confirmation of the exact count, and more than 100 images also needs the user to type that count.
- Every delete is logged locally.
- A delete never overlaps tagging, sync, cleanup or export, in this process or any other.

## Technical Context

**Language/Version**: Python 3.11–3.13 (`requires-python = ">=3.11,<3.14"`). The page is plain browser JavaScript and CSS, with no build step.

**Primary Dependencies**: only what's already in the project and the Python standard library.
- `http.server` for the server.
- `zipfile` (writes to a non-seekable stream) for bundles.
- `urllib` for the Immich client.
- `secrets` for tokens.

There are **no new dependencies**.

**Storage**: files in the library's state directory (`config.state_dir(root)`, outside the library):
- `trashed.json`, the trash set;
- `deletions.jsonl`, the deletion log;
- the existing `pending-sync.jsonl`, edited only by removing entries.

Everything else is in memory in `AppState`.

**Testing**: pytest (`uv run pytest tests/unit`), using the in-process `tests/unit/fake_immich.py`, extended with the endpoints in [contracts/immich-api.md](contracts/immich-api.md). The page itself is verified manually with [quickstart.md](quickstart.md).

**Target Platform**: macOS (Apple silicon), where Immich runs in Docker on the same machine, and the browser page is served on loopback (127.0.0.1).

**Project Type**: a single Python package with CLIs and a local web UI (`src/immichtag`).

**Performance Goals**:
- The count and grid update less than 2 s after a delete (SC-006). This is done by in-place index removal, not a rebuild of 88k images.
- A 1,000-image delete finishes in seconds: about 2 path-lookup calls plus 2 trash calls of 500 each.
- A single download starts immediately and is streamed.
- A batch ZIP streams with memory bounded to one chunk, at any size.

**Constraints**:
- Never permanent delete (`force: true` is never sent).
- Never touch files in Immich's upload folder.
- Server-side count confirmation.
- A stale-generation guard on every id-bearing request, so a rescan can't make an id mean a different image.
- The same no-auth, loopback-only model as today (see Complexity Tracking).

**Scale/Scope**: about 88k images, and a selection can hold up to the whole library. There are 5 new endpoints and 4 changed ones. The work touches `web.py`, `index.py`, `immich.py`, `library.py` (reuse only), `web/app.js`, `web/index.html` and `web/app.css`, plus the README.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template, so it has no ratified principles to gate on. **Result: PASS (vacuous).**

In its place, the design was checked against the conventions this repository already enforces, which were set in feature 001:

| Convention (from existing code/README) | This design |
|---|---|
| Stdlib `http.server`, no web framework (`web.py` docstring) | ✅ The new endpoints go in the same handler. Streaming uses stdlib `zipfile`. |
| Immich is touched only through its API, verified against the running server's source | ✅ R1–R3 were read from `immich_server` dist. There's no DB access and no file removal. |
| State lives outside the library (`config.state_dir`) | ✅ `trashed.json` and `deletions.jsonl` are there. |
| One writer per library across processes (`TaggingLock`) | ✅ The delete holds it (R7). |
| A destructive action is previewed, then confirmed (the tag cleanup's `cleanup-preview` → `confirm`) | ✅ `delete/preview` → `delete {confirm: count}`. |
| Tested against `fake_immich.py`, not the real server | ✅ The fake is extended. |

**Post-design re-check** (after Phase 1): still PASS. The design adds no dependency, framework or storage engine.

## Project Structure

### Documentation (this feature)

```text
specs/002-filter-delete-download/
├── plan.md              # This file
├── spec.md
├── research.md          # Phase 0: R1–R8
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── web-api.md       # new/changed immichtag-web endpoints + UI contract
│   └── immich-api.md    # Immich endpoints added (trash, lookup by path, features)
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks, not created here)
```

### Source Code (repository root)

```text
src/immichtag/
├── web.py          # AppState: selection resolving, busy(), bundles, delete previews,
│                   #   DeleteJob + worker; handler routes /api/download, /api/bundle*,
│                   #   /api/delete*; generation (409) guard; streaming helpers
├── index.py        # LibraryIndex.remove()/removed/revision/live_count;
│                   #   build(..., hidden=) ; IndexHolder passes the trash set
├── immich.py       # ImmichClient.features/assets_by_path/iter_trashed/trash;
│                   #   resolve_path_map() factored out of sync(); PERMISSIONS entry
├── trash.py        # NEW, small: TrashSet (trashed.json load/refresh/add) and
│                   #   DeletionLog (deletions.jsonl append)
├── immich_cli.py   # `status` lists asset.delete among required permissions
└── web/
    ├── index.html  # selection bar, viewer buttons, delete dialog, #delete-panel
    ├── app.js      # selection state, card checkboxes, download/bundle/delete flows,
    │               #   revision-driven refresh, reattach to running delete
    └── app.css     # card checkbox/selected state, bar, modal

tests/unit/
├── fake_immich.py          # + DELETE /api/assets, originalPath.in, trashedAt.ne,
│                           #   /api/server/features, isTrashed
├── test_index_remove.py    # NEW: remove(), hidden build, select() after removal
├── test_immich_trash.py    # NEW: client lookup/trash batching/per-id fallback
├── test_web_download.py    # NEW: /api/download, /api/bundle, zip content, 409
└── test_web_delete.py      # NEW: preview/confirm, busy/lock refusal, outcomes,
                            #   pending-sync + log + trash-set effects, rebuild/restore
```

**Structure Decision**: extend the existing single package and keep its current module boundaries:
- The HTTP code and jobs stay in `web.py`.
- The index logic stays in `index.py`.
- The Immich calls stay in `immich.py`.

`trash.py` is the only new module. It keeps the two state files' formats in one place, so both the index build and the delete job use them, the same way `library.PendingSync` owns its file.

## Implementation Notes (ordering)

1. **Index removal and hidden build** (`index.py`) is pure logic and has no Immich dependency. Both P2 stories need it.
2. **Single download** (P1, Story 1): `/api/download` and the ⬇ in the card and viewer. It ships alone.
3. **Selection and bundle** (P2, Story 2): the selection resolver, `/api/bundle`, the ZIP streaming, and the selection bar.
4. **Immich client additions and `trash.py`**.
5. **Delete** (P2, Story 3): the preview and confirmation, the DeleteJob, the busy and lock guards, the effects, the panel and dialog, and `canDelete` in the status.
6. **README**: a new "Deleting and downloading" part of the web section. It covers the Immich trash semantics, `asset.delete`, the trash-off warning, and where the log lives. It also strengthens the "leave `--host` alone" note, because the page can now delete.

## Complexity Tracking

There are no constitution violations. One deliberate risk is recorded:

| Risk accepted | Why | Mitigation |
|---|---|---|
| The delete endpoints have no authentication | The server is single-user and loopback-only by default, as the existing Immich sync and cleanup endpoints already are. Adding auth is out of scope (spec Assumptions). | The destructive path needs a one-use token from a preview **and** the exact count. Any other site's page is blocked from reading the preview response (same-origin), so it can't obtain a token. The delete endpoints also require `Content-Type: application/json`. That forces a CORS preflight, which this server never grants. The trash keeps everything reversible, and the README warns against `--host`. |
