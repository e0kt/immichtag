# Data Model: 全库改用 PixAI 重新标注

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

This project has no database. The durable data lives in files: sidecars next to images, and a small state directory per library. The only other data is in Immich, which is reached through its API. Entities below are described by their fields, validation rules and state transitions.

---

## Image

A file under the library root with an extension in `IMAGE_EXTS` (`.jpg .jpeg .png .webp .bmp .avif`), outside `SKIP_DIRS`.

| Field | Type | Notes |
|---|---|---|
| `path` | absolute path | Identity within a run |
| `sidecar_path` | path | Always `path + ".xmp"`; Immich also looks here first |
| `status` | enum | Derived; see below |

**Status (derived on every scan, never stored):**

| Condition | Status | Handling |
|---|---|---|
| no sidecar file | `NEW` | tagged first |
| sidecar without provenance, or unreadable | `LEGACY` | re-tagged (every WD14 sidecar is here) |
| sidecar whose `model`+`revision` ≠ the running tagger's | `OUTDATED` | re-tagged (an older PixAI release) |
| sidecar whose `model`+`revision` = the running tagger's | `CURRENT` | skipped unless `--overwrite` |

- The tagging queue is ordered as follows (FR-004): NEW images first, then LEGACY and OUTDATED. Within each group, scan order is kept.
- An unreadable sidecar (I/O error or unparseable XML) counts as LEGACY, so it is re-tagged. The broken file is still included in the backup first.

---

## Sidecar

`<image>.xmp`, an XMP packet. [contracts/sidecar-xmp.md](./contracts/sidecar-xmp.md) defines the exact byte-level format; this section defines the logical content.

| Field | Type | Source | Notes |
|---|---|---|---|
| `tags` | ordered list of Tag | `digiKam:TagsList` (fallback: `lr:HierarchicalSubject`, then `dc:subject`) | Deduplicated, order preserved |
| `provenance` | Provenance or none | `animetag:*` attributes **or** child elements | None ⇒ LEGACY |
| `foreign_tags` | list of Tag | derived | Tags whose first segment is not a managed prefix; preserved across re-tags (FR-009) |

**Validation on write:**
- Every managed tag's name has `/` replaced with `∕` (R7), so the only `/` left is the prefix separator.
- The file is written atomically (R5).
- `TagsList` is an `rdf:Seq`; the two other bags carry identical values as `rdf:Bag`.
- If there are no managed tags, `tag/untagged` is written, as today.

**Merge rule on re-tag:** `new_tags = managed_tags_from_model + [t for t in old.tags if not t.is_managed]`. Managed tags are written first, in the order character, parody, tag, meta, rating; foreign tags keep their old relative order.

---

## Provenance

| Field | Type | Example | Rule |
|---|---|---|---|
| `model` | string | `pixai-labs/pixai-tagger-v1.0` | Must equal the running tagger's for CURRENT |
| `revision` | string (40-hex SHA) | `f33cfdb5…70ef8` | Must equal the pinned revision for CURRENT |
| `profile` | string | `v1` or `custom:g0.30,c0.37,p0.46,m0.47` | `v1` when all thresholds are default; informational only |
| `taggedAt` | ISO-8601 UTC | `2026-09-22T03:14:15Z` | Written once per tagging |

---

## Tag

A string of the form `<prefix>/<name>`, or a bare `<name>` for foreign tags that have no hierarchy.

| Prefix | Managed | Source category | Cardinality per image |
|---|---|---|---|
| `character` | ✓ | character ≥ 0.37 | 0..n |
| `parody` | ✓ | copyright ≥ 0.46 | 0..n |
| `tag` | ✓ | general ≥ 0.34, minus the junk list unless `--keep-common-tags` | 0..n (`tag/untagged` if nothing at all) |
| `meta` | ✓ | meta ≥ 0.47, whitelisted names only (R6) | 0..n |
| `rating` | ✓ | argmax over `{g,s,q,e}` → `general/sensitive/questionable/explicit` | exactly 1, unless `--no-rating` |
| anything else | ✗ | written by the user or Immich | preserved |

Legacy pipe forms (`character|…`, `tag|…`) are managed for **Immich cleanup purposes only**. They never appear in a new sidecar.

**Matching (shared by the CLI and the web; `filterer.pattern_matches`):**
1. `normalize(s)`: strip, lowercase, fold spaces and `-` to `_`, as today. Then, in the **name part only**, fold `/` to `∕`.
2. **Split**: if the first `/`-segment of the pattern is a managed prefix, the pattern is a full path `(prefix, name)`. Otherwise the entire pattern is a name. This replaces today's "contains a `/`" rule.
3. **Leaf** of a stored tag: everything after the first `/` when the first segment is a managed prefix; otherwise the whole tag. This replaces today's `rsplit('/', 1)`.
4. Globs (`*`, `?`, `[`) apply to the same target (full path or name) as a plain pattern would.

Consequence: `fate/grand_order`, `parody/fate/grand_order` and `parody/fate∕grand_order` all match the stored `parody/fate∕grand_order`, and `rating/explicit` keeps its meaning.

---

## Vocabulary

| Field | Type | Notes |
|---|---|---|
| `model_tags` | map normalized name → (category, prefix or none) | From the pinned `config.json`; the style category maps to none, because it is not written |
| `library_tags` | map normalized full tag → image count | From the index |

Used only for suggestions and legacy detection (R13). A pattern is classified as follows:
- `matches` — it resolves to at least one image.
- `legacy` — it appears in `library_tags` but not in `model_tags`.
- `unknown` — it appears in neither.
- `empty` — it is in `model_tags` but no image has it yet.

---

## Tagging run

In memory. The web version is exposed through `/api/tag`.

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `total_new`, `total_retag` | int | Counted separately (FR-008) |
| `done`, `written`, `failed`, `skipped` | int | |
| `device` | `mps` or `cpu` | cpu ⇒ warning shown |
| `eta_seconds` | int or none | From the moving average of the last 50 images |
| `backup_id` | string or none | Set once the pre-overwrite backup is written |
| `sync` | Sync result or none | Filled after the run |
| `state` | enum | See below |

```text
scanning → locking ─(lock busy)→ refused
              │
              ▼
          backing-up → loading-model → running ─┬─(cancel/SIGINT)─► syncing → cancelled
                                                ├─(all done)──────► syncing → finished
                                                └─(fatal error)───► syncing → error
```

`syncing` runs whenever at least one sidecar was written, whatever the outcome. Its own failure is recorded in `sync`; it never changes the run's outcome (FR-021).

---

## Pending sync entry

`<state>/pending-sync.jsonl`, append-only. One line per written sidecar:

```json
{"path": "259c78e3-…/d5/92/d592baca-….jpg", "had_sidecar": true, "at": "2026-09-22T03:14:15Z"}
```

- `path` is relative to the library root.
- `had_sidecar: false` means the image was NEW, which makes Immich sidecar discovery necessary.
- When a sync succeeds, the file is rewritten without the entries it covered.
- `animetag-immich sync` with no arguments processes this file; `--all` ignores it and covers the whole library.

---

## Backup

`<state>/backups/<id>/`, where `<id>` is a UTC timestamp such as `20260922T031415Z`.

| File | Content |
|---|---|
| `sidecars.tar.gz` | Original bytes of every sidecar overwritten by the run, with archive paths relative to the library root |
| `manifest.jsonl` | One line per file: `{"path", "size", "mtime", "sha256"}` |
| `meta.json` | `{"root", "created", "count", "reason": "retag" or "restore-safety"}` |

**Rules:**
- A backup is complete before the first byte of any sidecar is replaced in that run. If writing the backup fails, the run aborts before touching anything.
- A file whose `(path, sha256)` pair appears in an earlier manifest is not archived again. The path is part of the key, because two images can have byte-identical sidecars.
- Restoring first takes a safety backup of the current sidecars (`reason: restore-safety`), then writes each archived file back atomically and appends it to the pending sync list.

---

## Immich tag (external, read and delete only)

From `GET /api/tags`: `{id, value, parentId, name}`. `value` is the full path, e.g. `tag/long_hair`.

**Derived fields:**
- `managed` — see Tag.
- `in_use` — the value is in the library's managed in-use set, or is an ancestor of such a value.
- `candidate` — `managed ∧ ¬in_use`.
- `verified_empty` — a search on `tagIds:[id]` with `size: 1` returns 0 assets.

**Deletion order:** candidates that are `verified_empty` are deleted deepest first, i.e. by number of `/` segments descending, so no cascade removes an unverified tag.

## Immich asset (external, read only)

From `POST /api/search/metadata`: `{id, originalPath}`. It is mapped to a local path through `path_map`. Assets whose mapped path does not exist locally are ignored.
