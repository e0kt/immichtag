# Contract: Immich endpoints added for delete

These are added to the endpoints in `specs/001-pixai-full-retag/contracts/immich-api.md`. The target is Immich 3.2.2, and every call was checked against the running server's compiled source (research R1–R3). The auth, retry and 401/403 policies are unchanged.

The API key now also needs **`asset.delete`**.

| Purpose | Method and path | Body | Permission | Notes |
|---|---|---|---|---|
| Is the trash on? | `GET /api/server/features` | — | none | `trash: bool`. When false, trashed assets are purged at the next daily cleanup, and the page warns that the delete is permanent. |
| Find assets by path | `POST /api/search/metadata` | `{"filter": {"originalPath": {"in": [...≤500]}}, "size": 500}` | `asset.read` | Uses `id`, `originalPath` and `isTrashed`. Trashed assets are included. With a legacy server (400 on this shape), fall back to `iter_assets()` and a dict. |
| List the trash | `POST /api/search/metadata` | `{"filter": {"trashedAt": {"ne": null}}, "size": 1000, "cursor": c}` | `asset.read` | Refreshes `trashed.json` when the index is built. With a legacy server, the refresh is skipped and the saved file is kept, which is the same as when Immich is unreachable. |
| Move to trash | `DELETE /api/assets` | `{"ids": [...≤500], "force": false}` | `asset.delete` | 204. **All-or-nothing per request** (`requireAccess` checks every id). On a 4xx other than 401/403, retry that batch one id at a time. `force` is **never** true. |

**Explicitly not used**:
- `force: true`, because it's permanent.
- `POST /api/trash/empty`, `POST /api/trash/restore` and `POST /api/trash/restore/assets`. Emptying and restoring the trash stay in Immich's UI.

**Client additions** (`immich.ImmichClient`):
- `features() -> dict`
- `assets_by_path(remote_paths) -> dict[path, (id, is_trashed)]`
- `iter_trashed() -> Iterator[(id, originalPath)]`
- `trash(ids) -> None`: batches of 500, and raises the existing error types.
- `PERMISSIONS["/api/assets"] = "asset.delete"`.

The `PathMap` resolution the sync builds inline is factored out into `immich.resolve_path_map(client, settings, root)`, so the sync and the delete share it.
