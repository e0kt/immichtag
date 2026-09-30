# Contract: web API additions (`immichtag-web`)

These endpoints are served on the same loopback server as the existing API (`specs/001-pixai-full-retag/contracts/web-api.md`). Every error is `{"error": "<message>"}` with HTTP 400, except where a different status is listed. `Selection` is defined in [data-model.md](../data-model.md#selection-request-value).

**Common rule**: any request that carries a `generation` (or `v`) that differs from the current index answers **409** `{"error": "The library was rescanned — reload the results."}`.

## Changed

| Endpoint | Change |
|---|---|
| `GET /api/status` | `images` is now the live count (minus removed images). New fields: `revision` (int) and `immich.canDelete` (bool, meaning configured and reachable). New `busy`: `null` or the kind of job running (`"tag"`, `"sync"`, `"cleanup"`, `"export"` or `"delete"`). |
| `POST /api/search` | Adds `revision` to the response. |
| `POST /api/export`, `POST /api/tag`, `POST /api/immich/sync`, `POST /api/immich/cleanup`, `POST /api/reindex` | Refused with 409 `{"error": "A delete is in progress — wait for it to finish."}` while a DeleteJob is active. |

## Download

### `GET /api/download?id=<int>&v=<generation>`

This returns one original file as an attachment.

- **200**: headers `Content-Type` (from the extension, as `/api/image` does), `Content-Length`, `Content-Disposition: attachment; filename="<ascii>"; filename*=UTF-8''<pct-encoded>`, `Cache-Control: no-store`. The body is the file, byte for byte.
- **404**: the id is unknown or removed, or the file is gone from disk (`"<name> is no longer on disk"`).
- **409**: stale `v`.

### `POST /api/bundle`

The request body is a `Selection`.

**200**:

```json
{"token": "9c1e…", "count": 20, "bytes": 48213904, "size": "46.0 MB",
 "url": "/api/bundle/9c1e….zip", "warn": false}
```

`warn` is true when `bytes > 2 GiB` or `count > 2000`, and the page asks again before it navigates. The token expires after 10 minutes and works once.

### `GET /api/bundle/<token>.zip`

This streams a STORED ZIP (research R5).

- **200**: headers `Content-Type: application/zip`, `Content-Disposition: attachment; filename="immichtag-<count>-<stamp>.zip"`, `Connection: close`, and no `Content-Length`. The body ends when the connection closes.
- **404**: the token is unknown, expired or already used.
- **409**: the index generation changed since `POST /api/bundle`.

## Delete

Both `POST` endpoints answer **415** `{"error": "JSON required"}` unless `Content-Type` is `application/json`. A cross-site form can only post `text/plain` without a CORS preflight, which this server never grants (plan: Complexity Tracking).

### `POST /api/delete/preview`

The request body is a `Selection`.

**200**:

```json
{"token": "51ad…", "count": 342,
 "sample": [{"id": 17, "name": "x.jpg", "generation": 3}],
 "trashEnabled": true,
 "typeToConfirm": true}
```

- `sample` holds up to 12 images, for the dialog.
- `typeToConfirm` is `count > 100 || !trashEnabled`.
- `trashEnabled` is null if the features call failed; the page treats null as unknown and requires typing.

**Errors**:
- **409**: another job is busy.
- **400**: Immich isn't configured or isn't reachable. The message is the same text the page shows next to the disabled button.

### `POST /api/delete`

This starts the delete.

**Request**:

```json
{"token": "51ad…", "confirm": 342}
```

**200**: the response is `{"job": DeleteJob}` (see below).

**Errors**:
- **400**: `confirm` is missing or isn't exactly the preview's `count`, or the token is unknown or expired.
- **409**: a job is busy, the generation changed, or another process holds the tagging lock. For the lock, the message includes the holder, e.g. `"another tagging run is active (pid 4121, started …)"`.

A second POST while a delete runs returns the running job; it doesn't start another one.

### `GET /api/delete`

This returns `{"job": DeleteJob | null}`, the current or last delete. The page polls it every second while `active` and reattaches to it on load.

```json
{"id": "3f9a1c2b7d10", "state": "trashing", "active": true,
 "total": 342, "done": 200,
 "deleted": 198, "already": 1, "missing": 1, "failed": 0,
 "failures": [{"name": "y.png", "reason": "not in Immich"}], "moreFailures": 0,
 "trashEnabled": true, "error": null, "revision": 12}
```

- `state` is one of `resolving`, `trashing`, `finishing`, `finished` or `error`.
- `revision` is set once the job finishes, and the page re-runs its search.

## UI contract (`web/index.html`, `app.js`, `app.css`)

- **Thumbnail card**:
  - It gets a checkbox in the corner, shown on hover and always shown in selection mode. Clicking the checkbox toggles selection; clicking the image still opens the viewer.
  - Shift-click on a checkbox selects the range from the last one clicked.
  - A ⬇ icon starts `/api/download` for that card.
- **Selection controls**: they appear in the sticky results row (next to the match count) when at least one image is selected, so the grid never moves when a selection starts. It shows `N selected` and the buttons **Select all M matching**, **Clear**, **Download**, and **Delete…**. Delete is disabled, with its reason as a tooltip, when `!immich.canDelete` or `busy`.
- **Viewer**: it gets **Download** and **Delete…** buttons for the image being viewed. After a delete, the viewer closes.
- **Delete dialog**:
  - An in-page modal, never `confirm()`, showing the count, the sample thumbnails and "Moves N images to Immich's trash; restore them there within its retention period". If the trash is off or unknown, it shows a red "Immich's trash is off — this is permanent" instead.
  - When `typeToConfirm` is set, the Delete button stays disabled until the typed number equals N.
- **Delete progress**: a new `#delete-panel` in `.jobs`, like the export panel. When the job finishes, it shows the counts and a scrollable failure list, with a link to open the state folder (`/api/reveal`).
- **Results after a delete**: the grid, the match count and the tag autocomplete counts refresh automatically when a job finishes (a `revision` change).
