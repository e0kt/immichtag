# Contract: Immich endpoints consumed

**Target**: Immich server 3.2.2 (`ghcr.io/immich-app/immich-server:v3`). Every call is verified against the compiled server source in the running container (research R8, R9).

- **Auth**: header `x-api-key: <key>`. The key belongs to the admin user and needs the permissions `asset.read`, `asset.update`, `job.create`, `job.read`, `tag.read` and `tag.delete`.
- **Base URL**: `[immich].url`, default `http://127.0.0.1:2283`, with the `/api` prefix.

| Purpose | Method and path | Body / query | Permission | Notes |
|---|---|---|---|---|
| Version / reachability | `GET /api/server/version` | — | public | Warn if major ≠ 3 |
| List assets and paths | `POST /api/search/metadata` | `{"filter": {}, "size": 1000, "cursor": c}` | `asset.read` | v3.2 shape: page until `assets.nextCursor` is null; use `id` and `originalPath`. It applies no trash filter, so trashed assets are included |
| Check a tag is empty | `POST /api/search/metadata` | `{"filter": {"tagIds": {"any": [id]}}, "size": 1}` | `asset.read` | Matches the tag or any descendant (tag closure). Empty ⇔ `assets.items` is empty |
| Re-read known sidecars | `POST /api/assets/jobs` | `{"assetIds": [...≤500], "name": "refresh-metadata"}` | `job.create` + `asset.update` on the ids | 204. Queues `AssetExtractMetadata`, which replaces the asset's tags with the sidecar's |
| Discover new sidecars | `PUT /api/jobs/sidecar` | `{"command": "start", "force": false}` | admin + `job.create` | Deprecated since v2.4.0, present in 3.2.2. `SidecarCheck` → `AssetExtractMetadata` for assets without a registered sidecar. On 404: report the manual step, continue |
| Full re-sync (optional) | `PUT /api/jobs/sidecar` | `{"command": "start", "force": true}` | admin + `job.create` | Used only by `sync --all` when refresh by ids is unavailable |
| Queue progress | `GET /api/jobs` | — | admin + `job.read` | Poll every 5 s until `sidecar` and `metadataExtraction` have `active + waiting == 0`; give up after 2 h and report pending |
| List tags | `GET /api/tags` | — | `tag.read` | `id`, `value`, `parentId` |
| Delete a tag | `DELETE /api/tags/{id}` | — | `tag.delete` | **Cascades to descendants.** Delete deepest first, verified-empty only |

**Older servers**: if the v3.2 search shape is rejected with HTTP 400, the client switches to the deprecated flat fields (`page` + `nextPage`, `withDeleted: true`, `tagIds: [...]`) for the rest of the session. They are deprecated in 3.2 but still accepted, and the two shapes may not be mixed in one request.

**Explicitly not used:**
- `POST /api/jobs {"name":"tag-cleanup"}`: it deletes empty tags under every prefix, which violates FR-019.
- Any direct database access.

**Error policy:**
- A connection error or 5xx is retried 3 times with backoff (1, 4 and 16 s). After that the sync is marked pending, and the tagging result is unaffected (FR-021).
- 401/403 stops the sync at once with a message naming the missing permission. The key itself is never printed.
