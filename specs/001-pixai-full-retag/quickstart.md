# Quickstart & validation: 全库改用 PixAI 重新标注

This guide proves the feature end to end. Scenarios 1–7 use a **throwaway copy** of a few library folders and never touch the real library or Immich's data. Scenario 8 is the real full-library run.

Behaviour is defined in [contracts/cli.md](./contracts/cli.md), [contracts/sidecar-xmp.md](./contracts/sidecar-xmp.md), [contracts/immich-api.md](./contracts/immich-api.md) and [data-model.md](./data-model.md).

## Prerequisites

- An Apple Silicon Mac (the GPU must be available) and `uv`.
- Immich 3.2.x running at `http://127.0.0.1:2283`, with a fresh database backup (`~/docker/immich/immich-db-backup-*.sql.gz`).
- An Immich API key belonging to the admin user, with the permissions `asset.read`, `asset.update`, `job.create`, `job.read`, `tag.read` and `tag.delete`:

  ```toml
  # ~/.config/immichtag/config.toml  (chmod 600)
  [immich]
  url = "http://127.0.0.1:2283"
  api_key = "<key>"
  path_map = "/data=~/docker/immich/library"
  ```

- Install and run the tests:

  ```bash
  uv sync
  uv run pytest -q                # unit tests; integration tests run only if the model is cached
  ```

## 1. Dry run on the real library (read-only)

```bash
uv run immichtag --dry-run
```

**Expect**: `87969 images: N new, ~87957 to re-tag (… legacy, 0 outdated), 0 current.` and an ETA of about 13–15 h. The model is not loaded and no file changes.

## 2. Tagging a copy (FR-001 – FR-009)

```bash
T=$(mktemp -d)
rsync -a --include='*/' --include='*.jpg' --include='*.jpg.xmp' --exclude='*' \
  "~/docker/immich/library/upload/<user-id>/d5/" "$T/"
rm "$(ls $T/*/*.jpg | head -1).xmp"               # make one image NEW
uv run immichtag --dir "$T" --no-immich
```

**Expect**:
- The NEW image is processed first.
- A backup is announced before any overwrite, and `immichtag-backup list --dir "$T"` shows it.
- Every sidecar has `immichtag:model="pixai-labs/pixai-tagger-v1.0"`, exactly one `rating/*`, and a `TagsList` of type `rdf:Seq`.
- No `tag/bangs` appears anywhere; `parody/*` appears on images with a known character.
- Running the command again reports `0 to re-tag` and exits immediately (SC-002 resume).

## 3. Interruption and atomicity (FR-005, FR-006)

```bash
uv run immichtag --dir "$T" --no-immich --overwrite &  sleep 20; kill -INT %1
uv run immichtag --dir "$T" --no-immich
```

**Expect**:
- The second run processes only the remainder.
- `find "$T" -name '*immichtag-tmp'` prints nothing.
- Every sidecar parses.

## 4. Foreign tags and Immich-rewritten sidecars (FR-009, the reader rules)

Copy the fixture `tests/fixtures/immich_rewritten.xmp` (the exiftool output captured in research R5) next to an image in `$T`, then re-tag with `--overwrite`.

**Expect**:
- `my_album/trip` survives.
- `hello` (the description) never appears as a tag.
- Provenance written as child elements is recognized as `CURRENT` before the overwrite.

## 5. Filtering and export (FR-022 – FR-027)

```bash
uv run immichtag-filter --dir "$T" -f "fate/grand_order" --dry-run
uv run immichtag-filter --dir "$T" -f "bangs" --dry-run            # legacy/unknown warning
uv run immichtag-filter --dir "$T" -x "rating/explicit" -n 5 -o "$T/out" --copy-sidecar
uv run immichtag-web --dir "$T"
```

**Expect**:
- A slash-containing name matches with or without the `parody/` prefix.
- `bangs` prints a warning with suggestions and exits 0.
- The exported `.xmp` files are byte-identical to the originals.
- The web page:
  - shows the same counts as the CLI for the same filters (FR-026)
  - shows the warning under the filter row
  - hides the re-tag banner, because nothing is left to re-tag

## 6. Single writer (FR-010)

While `immichtag-web --dir "$T"` is running a tagging job, run `uv run immichtag --dir "$T" --overwrite`.

**Expect**: exit code 3 with `another tagging run is active (pid …)`, and no sidecar is written by the second process.

## 7. Immich sync on a small batch (FR-017 – FR-021)

The re-tag of a handful of real library images needs no copy, because it is exactly what the full run will do:

```bash
uv run immichtag-sync status
uv run immichtag-sync cleanup --dry-run       # preview first: nothing is deleted
uv run immichtag --limit 20                     # re-tags the first 20 queue items, syncs, then deletes the previewed empty tags
```

**Expect**:
- `status` shows the server as reachable, version 3.x and every permission as OK.
- The cleanup preview lists the ~6,816 legacy `character|…`/`tag|…` tags and the ~238 empty `/` tags, and 0 non-managed tags. Review it before the second command, which deletes them.
- For those 20 assets, Immich's tags (`GET /api/assets/{id}`) equal their sidecar's `TagsList`.
- Stopping Immich (`docker stop immich_server`) and tagging again prints `Immich sync pending (N images)` with exit code 0. After `docker start immich_server`, `uv run immichtag-sync sync` clears the pending list.

## 8. Full library (SC-001 – SC-003, SC-005)

```bash
uv run immichtag                                  # ≈ 13–15 h; safe to Ctrl-C and rerun
uv run immichtag --dry-run                        # expect: 0 to re-tag
uv run immichtag-sync status                    # expect: pending 0, empty managed tags 0
uv run python scripts/verify_immich.py --sample 100   # compares 100 random assets' Immich tags with their sidecars
uv run python scripts/bench20.py                 # expect ≥ 15/20 with a character, avg general tags 25–45
```

## Rollback

```bash
uv run immichtag-backup list
uv run immichtag-backup restore <id>              # restores the WD14 sidecars byte for byte, then syncs Immich
```

`restore` does not reinstall WD14. Restoring is for inspecting the old tags or undoing a bad run. A later `immichtag` run would re-tag the restored files again.

## Clean-up after acceptance

- Remove the old WD14 model cache (747 MB): `rm -rf ~/.cache/huggingface/hub/models--SmilingWolf--wd-v1-4-convnextv2-tagger-v2`
- Keep at least the first backup until you are satisfied with the new tags. It is about 40 MB.
