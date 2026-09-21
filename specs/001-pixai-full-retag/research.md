# Research: 全库改用 PixAI 重新标注，彻底移除 WD14

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-09-21

Every item below resolves an unknown from the plan's Technical Context. Evidence is either a measurement on this machine (Apple M5, 32 GB) or the source code of the Immich server that is actually running (v3.2.2, read from `/usr/src/app/server/dist` inside `immich_server`).

---

## R1. Model runtime and precision

**Decision**: Run PixAI Tagger v1.0 with PyTorch on the Apple GPU (MPS) in fp16. Load it with `transformers.AutoModel.from_pretrained(..., trust_remote_code=True, revision=<pinned SHA>)`, then call `model.to(device).half()`. Fall back to CPU fp32 when MPS is unavailable, with a warning and an ETA.

**Rationale** (20 library images, time per image including decode):

| Configuration | Per image | Images/s | Full library (87,969) |
|---|---|---|---|
| MPS fp16 | 555 ms | 1.80 | ≈ 13.6 h |
| MPS bf16 | 560 ms | 1.79 | ≈ 13.7 h |
| MPS fp32 | 1.74 s | 0.58 | ≈ 42 h |

- fp16 is within 0.003 of fp32 on every score; bf16 drifts by up to 0.023. fp16 wins on accuracy at the same speed.
- Batch sizes 2, 4 and 8 gave no speedup, because the GPU is already saturated at batch 1. So one image at a time, matching today's loop.
- **Correctness trap (verified)**: `model.to(dtype=...)` also casts the complex64 RoPE buffers (`freqs_cis`) to a real dtype, which drops their imaginary part. The model still runs but produces plausible-looking garbage: no character was recognized on 20 images, even in fp32. `.half()` only touches real floating-point tensors. The loader must use `.to(device)` followed by `.half()`, and a unit test must assert that `freqs_cis` stays complex after loading.

**Alternatives considered**:
- *Export to ONNX to keep the current onnxruntime stack.* Rejected: RoPE uses `torch.view_as_complex` and complex multiplication, which the ONNX exporter does not support. The attention would have to be rewritten with real-valued ops and re-validated. That is a large, risky port for no measured gain.
- *CoreML conversion.* Rejected for the same complex-op reason. For the v0.9 ONNX model, the CoreML execution provider in MLProgram mode was 2× faster than CPU, but v1.0 is a different architecture.
- *PixAI v0.9 via ONNX + CoreML (380 ms/img).* Rejected: faster, but it has no rating or copyright head and noisier general tags (about 20% of its tags were scored below 0.10 by both other models). The spec requires v1.0 (FR-001).

## R2. Loading the model code: vendoring vs pinned remote code

**Decision**: Do not copy PixAI's `tagger_pipeline.py` into this repository. Load it from the Hugging Face repo at a pinned commit (`revision="f33cfdb53c0c90b049bab9ce066eea1118970ef8"`) with `trust_remote_code=True`. The revision is a constant in the tagger module and is also written into every sidecar's provenance (R5).

**Rationale**: The model repo declares no license (`card_data.license` is empty). Its code is adapted from SAM3, Detectron2 and others. Vendoring it would redistribute code of unclear license, whereas loading it for personal use is the upstream-documented path. Pinning the SHA means the executed code cannot change under us, which is the main security concern with `trust_remote_code`.

**Consequence**: Runtime dependencies are `torch`, `torchvision`, `transformers`, `timm` and `safetensors` (the remote module imports the first four). `onnxruntime` and `pandas` are removed, because they were used only by WD14.

**Alternatives considered**: Reimplementing the ViTDet backbone as our own `nn.Module` would remove `transformers` and `timm`, but it is a from-scratch port of a 32-block model with windowed attention and RoPE interpolation. Rejected as disproportionate.

## R3. Preprocessing

**Decision**: Reproduce the model's own processor exactly:
1. Composite onto white and convert to RGB.
2. `to_tensor` at full resolution.
3. Bilinear resize (torchvision, antialiased) so the long side is 1008, keeping the aspect ratio.
4. Center-pad to 1008×1008 with 0.
5. Normalize with mean 0.5 and std 0.5.

This is implemented locally with torch/torchvision, with no `transformers` image-processor object, and a unit test compares its output against the upstream processor on fixture images (max abs diff < 1e-5).

**Rationale**: Decode plus preprocessing measured 27 ms per image, against 530 ms of inference, so exactness costs nothing worth optimizing. A one-image prefetch thread overlaps decoding with GPU work, which recovers most of the 27 ms.

**Alternatives considered**: Pre-shrinking with PIL (JPEG draft mode) before `to_tensor` would be faster, but changes the pixels the model sees. Rejected for a gain of about 5%.

## R4. Default thresholds and category mapping

**Decision**: Use the model's per-category micro-F1 operating points, a single "precision-balanced" profile named `v1`:

| Model category | Prefix | Threshold | Notes |
|---|---|---|---|
| general | `tag/` | 0.34 | Avg 37.3 tags/img on the 20-image sample (range 24–53); FR-013 wants 25–45 |
| character | `character/` | 0.37 | 16/20 images, identical to 0.27 on the sample |
| copyright | `parody/` | 0.46 | 19/20 images |
| meta | `meta/` | 0.47 | Whitelist only (R6) |
| rating | `rating/` | argmax | Exactly one per image (FR-012), mapped `g→general`, `s→sensitive`, `q→questionable`, `e→explicit` |
| style | — | — | Not written (FR-015) |

The CLI keeps `--general-threshold`, `--character-threshold` and `--parody-threshold` (the last is now meaningful) and adds `--meta-threshold`. `--rating-threshold` is removed, because rating is argmax. The junk list (`1girl`, `solo`, …) and `--keep-common-tags` are unchanged.

**Rationale**: The pipeline's default macro-F1 thresholds (general 0.17) give about 51 general tags per image, well over the 25–45 band, and about 60% more Immich tag links than today. The micro-F1 points are published by the model authors and land inside the band. Argmax rating matches today's behaviour better than a threshold: WD14 at 0.50 sometimes wrote two ratings (Immich has 87,986 rating links for 87,755 tagged assets).

## R5. Sidecar format, provenance and parsing

**Decision** (tested against Immich's own exiftool 13.59 inside the container):

- **Provenance** goes in a custom XMP namespace `http://ns.animetag.local/1.0/` (prefix `animetag`), with `model`, `revision`, `profile` and `taggedAt`. It is written as attributes on `rdf:Description`.
  - Verified: when Immich rewrites a sidecar (its `SidecarWrite` job, triggered when the user edits tags in Immich), exiftool **keeps** the namespace but turns the attributes into child elements. The reader must accept both forms.
- **"Current" check**: a sidecar is current when `model` and `revision` equal the running tagger's. `profile` is informational only, so a run with custom thresholds is not undone by a later default run. A sidecar with no provenance is **legacy**; every WD14 sidecar falls into this group.
- **`digiKam:TagsList` is the source of truth**, written as `rdf:Seq`.
  - Verified: exiftool reports "Fixed incorrect list type for XMP-digiKam:TagsList" on today's `rdf:Bag`.
  - Immich's `getTagList` reads `TagsList` first, and its `SidecarWrite` updates only `TagsList`, leaving `lr:HierarchicalSubject` and `dc:subject` stale.
  - Our reader therefore uses `TagsList` when it is present and falls back to the other two bags only when it is absent, the same precedence as Immich. `lr:HierarchicalSubject` and `dc:subject` are still written with the same values for other XMP readers.
- **Parser bug fixed**: today's regex collects every `<rdf:li>` in the file.
  - Verified: after an Immich rewrite, the file contains `dc:description/rdf:Alt/rdf:li`, which the current parser would read as a tag.
  - The new parser only reads `rdf:li` inside the three tag properties. The regex fast path is kept for files we wrote ourselves, recognized by the provenance attribute form; anything else goes through lxml.
- **Foreign tags are preserved (FR-009)**: on re-tag, the new sidecar's tag list is our managed tags plus every existing tag whose top-level segment is not a managed prefix.
- **Atomic write (FR-006)**: write `.<name>.xmp.animetag-tmp` in the same directory, `fsync`, then `os.replace`. Stray temp files from a killed run are removed at the next run's start. Scans ignore them, because they end in `-tmp` and not `.xmp`.

**Alternatives considered**: Tracking provenance in a separate database instead of the sidecar was rejected: it would drift from the files, and the sidecar already travels with exports (FR-025).

## R6. `meta/` whitelist

**Decision**: A fixed list in code (`vocab.META_WHITELIST`) covering the categories the user approved:

- **AI**: `ai-generated`, `ai-assisted`
- **Official art**: `official_art`, `official_wallpaper`, `promotional_art`, `key_visual`, `novel_illustration`, `game_asset`, `visual_novel_cg`, `production_art`, `concept_art`, `artbook`
- **Screenshots**: `anime_screenshot`, `game_screenshot`, `screenshot`
- **Photos**: `photo_(medium)`, `cosplay_photo`
- **Traditional media**: `traditional_media`, `watercolor_(medium)`, `oil_painting_(medium)`, `acrylic_paint_(medium)`, `graphite_(medium)`, `colored_pencil_(medium)`, `marker_(medium)`, `pen_(medium)`, `ink_(medium)`, `pastel_(medium)`, `painting_(medium)`

Everything else in the 145-tag meta category is dropped. That includes resolution tags (`highres`, `absurdres`), Danbooru maintenance tags (`md5_mismatch`, `check_*`, `duplicate`, `corrupted_twitter_file`), and software-used tags (`photoshop_(medium)`, `clip_studio_paint_(medium)`, …).

**Rationale**: On the sample, the unfiltered meta output was dominated by `absurdres`, `highres` and `corrupted_twitter_file`, which are noise in a photo library. The list is small and reviewable, and changing it only affects future runs.

## R7. Tag names containing `/` or `|`

**Decision**: Inside a tag's name, `/` is replaced with `∕` (U+2215 DIVISION SLASH) on write; `|` is left alone.
- About 86 tags in the vocabulary contain `/`: 33 character, 32 copyright, 19 general, 2 style. Examples: `fate/grand_order`, `excalibur_(fate/stay_night)`.
- The replacement happens in every bag. The prefix separator stays a real `/`.

**Rationale**:
- Immich splits `TagsList` values on `/` to build its hierarchy, so `parody/fate/grand_order` would become three levels.
- `|` is literal in `TagsList` (Immich only splits on `|` in `HierarchicalSubject`, which it reads only when `TagsList` is absent), so it needs no escape.
- U+2215 renders like a slash, survived an exiftool round trip unchanged (verified), and is a single fixed, reversible substitution.

**Filtering**: the matching rule becomes category-aware (see [data-model.md](./data-model.md#tag)).
- A pattern is a full-path pattern only when its first segment is a known category. Otherwise the whole pattern is a name, with `/` folded to `∕`.
- So `fate/grand_order`, `parody/fate/grand_order` and `parody/fate∕grand_order` all match the same tag.
- `normalize()` gains the `/`→`∕` fold for the name part. The CLI and the web still share `filterer.pattern_matches`, so FR-026 holds.

**Alternatives considered**:
- `_`: collides with real names and is not reversible.
- U+FF0F full-width solidus: visibly wider in Immich's UI.
- Percent-encoding: ugly in Immich.

## R8. Making Immich re-read sidecars (FR-017, FR-018)

**Findings from Immich 3.2.2 source**:
- `MetadataService.applyTagList` upserts the tags read from the sidecar and then calls `tagRepository.replaceAssetTags`. The asset's tags are **replaced** by the sidecar's, not merged. `lockedProperties` is empty for every asset (queried), so nothing is shielded.
- `POST /api/assets/jobs {assetIds, name: "refresh-metadata"}` queues `AssetExtractMetadata` for exactly those assets.
  - Permission: `job.create`, plus `asset.update` access to the ids.
  - It reads the sidecar **already registered** in `asset_file`. It does not discover a sidecar that Immich has never seen.
- Sidecar discovery is `SidecarCheck`, whose completion always queues `AssetExtractMetadata` (see `JobService.onDone`).
  - It can only be started for the whole library: `PUT /api/jobs/sidecar {"command":"start","force":false}`.
  - With `force: false` it touches only assets without a registered sidecar; `force: true` re-checks all of them.
  - The endpoint is marked deprecated since v2.4.0 but is present and working in 3.2.2. It requires an admin key with `job.create`.
- Queue progress: `GET /api/jobs` (admin, `job.read`) reports active and waiting counts per queue.

**Decision**: After a tagging run that wrote N sidecars:
1. `refresh-metadata` for the written assets that Immich already knows, in batches of 500 ids.
2. `PUT /api/jobs/sidecar` with `force:false` if any written image had no sidecar before the run (new uploads).
3. Poll `GET /api/jobs` until the `sidecar` and `metadataExtraction` queues are idle.
4. Run tag cleanup (R9).

`animetag-immich sync --all` does the same for every image. The 13-hour re-tag syncs incrementally at the end of each run, so an interrupted run's work is still synced.

If the deprecated endpoint disappears in a future Immich version (HTTP 404), the tool reports "discovery needed: Immich → Administration → Jobs → Sidecar metadata → Discover" and continues. Refresh still covers every asset whose sidecar Immich already knows, which is all 87,755 existing ones.

**Asset lookup**: `POST /api/search/metadata {page, size: 1000, withDeleted: true}` returns `id` and `originalPath` for every asset. That is about 88 requests for the whole library, cached per run.

**Path mapping**: Immich sees `/data/upload/<user>/…` (queried); this Mac sees `<UPLOAD_LOCATION>/upload/<user>/…`. The mapping is taken from config (`immich.path_map = "/data=/Users/paul/docker/immich/library"`). When the setting is absent, it is auto-detected by matching the longest common suffix of one known asset path against the local library root.

**Alternatives considered**:
- Writing to Immich's Postgres directly: rejected, because it bypasses Immich's own tag closure table and is unsupported.
- Only `force:true` whole-library sidecar sync after every run: correct, but re-extracts all 88k assets after a 5-image daily run.

## R9. Removing emptied tags from Immich (FR-019, FR-020)

**Findings**:
- Immich has a manual `tag-cleanup` job (`POST /api/jobs {"name":"tag-cleanup"}`). Its SQL (`TagRepository.deleteEmptyTags`) deletes **every** tag with no asset on itself or any descendant, **regardless of prefix**. That violates FR-019's "never delete non-managed tags".
- It is not part of the nightly jobs.
- `DELETE /api/tags/{id}` cascades to descendants (`tag_parentId_fkey … ON DELETE CASCADE`), so deleting a parent silently deletes children.
- Current state (queried):
  - 6,816 empty legacy root tags of the form `character|…` or `tag|…`
  - 238 empty `/` tags
  - 3 empty roots (`character`, `tag`, `rating`), which are only structural
  - 1 odd root `tag|/_`

**Decision**: Do the cleanup client-side with the API:
1. `GET /api/tags` (`tag.read`) → all tags with `id`, `value` and `parentId`.
2. A tag is **managed** when its value's first `/`-segment is in `{character, parody, tag, rating, meta}`, or when it starts with `<managed>|` (the legacy pipe form).
3. **In-use set** = every managed tag value present in any sidecar in the library, from the index. This includes managed tags that Immich wrote back into sidecars.
4. Candidates = managed tags whose value is not in the in-use set and is not an ancestor of any in-use value.
5. Each candidate is verified empty with `POST /api/search/metadata {tagIds:[id], size:1}` before deletion. This is about 7k requests the first time and only a handful after that.
6. Delete deepest first, so no cascade ever removes a tag that has not been checked itself.

`--dry-run` prints the count and 20 examples (FR-020). The web UI shows the same preview before the delete button is enabled.

**Rationale**: This guarantees FR-019 by construction. The verify step covers any drift between our index and Immich, such as a sync that has not finished.

## R10. Backup and restore (FR-007)

**Decision**: Before the first overwrite in a run, write `backups/<UTC timestamp>.tar.gz` into the state directory (R12). It contains every sidecar the run is about to overwrite, byte for byte, with archive paths relative to the library root. A `manifest.jsonl` records each file's path, size, mtime and SHA-256.
- Files whose SHA-256 is already in an earlier backup are skipped, so resuming an interrupted re-tag does not re-archive the same legacy files.
- `animetag-backup list` shows the backups. `animetag-backup restore <id> [--dry-run]` writes the files back atomically (R5), then marks them pending for Immich sync.

**Sizing**: The average sidecar is 4.6 KB (2,000 sampled), so the full library is about 405 MB raw. XML with a repetitive vocabulary compresses about 10×, so roughly 40 MB.

**Alternatives considered**: `.bak` files next to each sidecar were rejected, because they would add 88k stray files inside Immich's upload tree.

## R11. Single writer (FR-010)

**Decision**: `fcntl.flock(LOCK_EX | LOCK_NB)` on `<state dir>/<library id>/tagging.lock` for the whole run.
- The CLI exits with "another tagging run is active (pid N, started …)".
- The web server's tag job reports the same and attaches to progress only for runs it started itself. A CLI run started while the web job is active is refused.

**Rationale**: `flock` is released by the kernel when the process dies, so there are no stale locks after a crash, and it works across processes on macOS.

## R12. Configuration and state locations

**Decision**:

- **Library root**:
  - Resolution order: `--dir`, then `ANIMETAG_DIR`, then `/Users/paul/docker/immich/library/upload`.
  - `WD14_DIR` no longer has any effect. If it is set and `ANIMETAG_DIR` is not, a one-line notice is printed per run (FR-029).
- **Immich settings**: `~/.config/animetag/config.toml`, parsed with `tomllib`:

  ```toml
  [immich]
  url = "http://127.0.0.1:2283"
  api_key = "…"
  path_map = "/data=/Users/paul/docker/immich/library"
  ```

  - Environment variables `ANIMETAG_IMMICH_URL`, `ANIMETAG_IMMICH_API_KEY` and `ANIMETAG_IMMICH_PATH_MAP` override the file.
  - The key is never printed or logged. The file should be mode 0600, and the tool warns if it is group- or world-readable.
- **State directory**:
  - macOS: `~/Library/Application Support/animetag/<library id>/`; elsewhere: `$XDG_STATE_HOME/animetag/<library id>/`.
  - `<library id>` is the first 12 hex digits of the SHA-256 of the resolved root path.
  - It holds `tagging.lock`, `pending-sync.jsonl` and `backups/`.
- **Required Immich API key permissions**: `asset.read`, `asset.update`, `job.create`, `job.read`, `tag.read` and `tag.delete`, on an admin user. The library has exactly one user.

**Rationale**: `tomllib` is in the standard library from Python 3.11, so `requires-python` moves from `>=3.10` to `>=3.11`; the project already runs 3.12. Keeping the key out of the repository and out of the Immich upload tree avoids leaking it with an export.

## R13. Unknown-tag warnings (FR-023)

**Decision**: The **vocabulary** is the model's tag list, read from the pinned `config.json` (694 KB, downloaded without the weights), mapped to prefixes, plus every tag currently in the library index.

For each plain (non-glob) pattern that matches nothing:
- **Legacy tag** (in the library but not in the model vocabulary, e.g. `bangs`): "`bangs` is a legacy tag — N images still carry it until they are re-tagged; the new model has no such tag. Similar: `blunt_bangs`, `swept_bangs`, `hair_between_eyes`".
- **Unknown tag**: "`xyz` does not exist. Similar: …".

Suggestions come from `difflib.get_close_matches` over the names, plus substring matches, with up to 5 shown. The CLI prints the warnings before scanning. The web returns them with every `/api/search` response, and the page shows them under the filter row.

**Rationale**: Uses the standard library only. The model vocabulary lets the CLI warn before a 16-second disk scan, and the library vocabulary covers the migration period.

## R14. Renaming (FR-028)

**Decision**:
- The Python package `wd14` → `animetag` (`src/animetag/`).
- Console scripts:
  - `animetag`, `animetag-filter` and `animetag-web`, which replace the three existing commands
  - new `animetag-immich` and `animetag-backup`
- `pyproject.toml` `name = "animetag"`; the web page title becomes "animetag"; the README is rewritten.
- No `wd14*` scripts, modules or aliases remain.
- The repository directory (`~/programming/wd14`) is not renamed, because that is outside the project's control.
- The existing `~/.cache/huggingface` WD14 model (747 MB) is not deleted automatically; the README tells the user how to remove it.

## R15. Testing

**Decision**: Add `pytest` as a dev dependency with the layout `tests/unit`, `tests/integration` and `tests/fixtures`. Unit tests cover:
- the sidecar round trip, including the exiftool-rewritten fixture captured in R5 and the `/` escape
- provenance classification
- ordering: new images before legacy ones
- category mapping, the meta whitelist and argmax rating
- the pattern rules
- unknown-tag suggestions
- selection of cleanup candidates, which must never touch a non-managed tag or an in-use ancestor
- the backup and restore round trip
- the lock
- the Immich client, against a stdlib fake HTTP server

Integration tests, which are skipped unless the model is cached, check two things:
- one real image produces the same tags as the fp32 reference within 0.01
- `freqs_cis` stays complex after loading

A benchmark script (`scripts/bench20.py`) reproduces the 20-image numbers.

**Rationale**: The project has no tests today. The risky parts (sidecar rewriting, Immich deletions, escaping) are pure functions or thin HTTP calls that are cheap to test without the model.
