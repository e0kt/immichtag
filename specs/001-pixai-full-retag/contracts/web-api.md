# Contract: `immichtag-web` HTTP API (loopback only)

The API keeps today's routes and shapes unless listed below. All responses are JSON unless noted. Errors return `400 {"error": "..."}`, as today.

## Changed

### `GET /api/status`

Three fields are added:

```json
{
  "state": "ready", "root": "...", "scanned": 87969, "images": 87969, "tags": 13520, "error": null,
  "retag": {"legacy": 87957, "outdated": 0},
  "immich": {"configured": true, "reachable": true, "pending": 0},
  "model": {"name": "pixai-labs/pixai-tagger-v1.0", "revision": "f33cfdb"}
}
```

- `retag` drives the "N images not re-tagged yet" banner (FR-024), which is hidden when both counts are 0.
- `immich.pending` drives a "Sync to Immich" button, shown when the count is above 0.

### `POST /api/search`

Same request as today. The response gains `warnings`:

```json
{
  "generation": 3, "total": 120, "selected": 120, "offset": 0, "items": [ ... ],
  "warnings": [
    {"pattern": "bangs", "kind": "legacy", "count": 42,
     "similar": ["blunt_bangs", "swept_bangs", "hair_between_eyes"]},
    {"pattern": "blue_hiar", "kind": "unknown", "count": 0, "similar": ["blue_hair"]}
  ]
}
```

- Each warning's `kind` is `legacy`, `unknown` or `empty`; see [data-model.md → Vocabulary](../data-model.md#vocabulary). Only plain patterns that match nothing are reported.
- The page renders each warning under the filter row it came from. Clicking a suggestion replaces the pattern.

### `GET /api/untagged` → renamed `GET /api/pending-tagging`

```json
{"new": 12, "legacy": 87957, "outdated": 0, "current": 0}
```

The old route is removed; the page is updated at the same time.

### `POST /api/tag` and `GET /api/tag`

The same single-job semantics, with an extended job object:

```json
{"job": {
  "id": "…", "state": "running", "active": true,
  "totalNew": 12, "totalRetag": 87957, "total": 87969,
  "done": 5120, "written": 5118, "skipped": 0, "failed": 2,
  "device": "mps", "eta": 44870, "backupId": "20260922T031415Z",
  "sync": null, "error": null, "messages": ["…"]
}}
```

- `state` is one of `scanning | locking | backing-up | loading | running | syncing | finished | cancelled | error | refused`.
- `refused` means another run (for example a CLI run) holds the lock. `error` then says who holds it.
- `sync`, once filled: `{"ok": true, "refreshed": 87957, "discovered": 12, "deletedTags": 7054}` or `{"ok": false, "reason": "…", "pending": 5120}`.

## New

### `POST /api/immich/sync`

Body: `{"all": false}`. Starts a sync of the pending list, or of the whole library when `all` is true, as a background job; one sync at a time.

Response:

```json
{"job": {"id": "…", "state": "running", "phase": "refresh|discover|waiting|cleanup", "done": 0, "total": 5120}}
```

### `GET /api/immich/sync`

The current or last sync job, same shape as above.

### `GET /api/immich/cleanup-preview`

```json
{"count": 7054, "byPrefix": {"legacy-pipe": 6816, "tag": 230, "character": 8}, "examples": ["tag/bangs", "character|abe_nana"]}
```

### `POST /api/immich/cleanup`

Body: `{"confirm": true}`. Without `confirm: true` the request is rejected with 400 (FR-020).

## Removed

- `GET /api/untagged` (renamed, see above).
