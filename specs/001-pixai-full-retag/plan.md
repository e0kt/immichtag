# Implementation Plan: 全库改用 PixAI 重新标注，彻底移除 WD14

**Branch**: `001-pixai-full-retag` | **Date**: 2026-09-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-pixai-full-retag/spec.md`

## Summary

Replace the WD14 tagger with PixAI Tagger v1.0 and re-tag the whole ~88k-image Immich library with it. The project is renamed to `immichtag`.

**Technical approach**:
- **Model**: PixAI v1.0 runs on the Mac's GPU (PyTorch MPS, fp16; ≈ 555 ms/img, ≈ 13.6 h for the library). The model code is loaded from a pinned upstream revision.
- **Sidecars**:
  - Every sidecar records provenance in its own XMP namespace, so the default command re-tags anything not written by the current model: new images first, then legacy.
  - Sidecars are backed up before any overwrite and written atomically.
  - Foreign tags are preserved; names containing `/` are escaped so Immich keeps them as one tag.
- **Immich**: after every run the tool calls Immich's API to re-read the written sidecars. It then deletes only *managed*, verified-empty tags, deepest first. That also clears the 7,000+ legacy leftovers.
- **Filter and web**:
  - Category-aware matching adds `parody/` and `meta/`; the web API gains `/api/immich/*` routes.
  - Legacy and unknown tags produce warnings with suggestions.
  - Both keep sharing a single matching implementation.

## Technical Context

**Language/Version**: Python 3.12 (`requires-python` raised to `>=3.11,<3.14` for `tomllib`)

**Primary Dependencies**:
- Added: `torch`, `torchvision`, `transformers`, `timm`, `safetensors`
- Kept: `huggingface-hub`, `lxml`, `numpy`, `pillow`, `tqdm`
- Removed: `onnxruntime`, `pandas`
- Dev: `pytest`
- Immich is reached with the standard library `urllib`, so no HTTP library is needed.

**Storage**: Files only.
- XMP sidecars next to the images.
- A state directory per library, holding the lock, the pending-sync list and backups.
- `~/.config/immichtag/config.toml` for the Immich URL, key and path map.

**Testing**: `pytest`.
- Unit tests cover the sidecar, pattern, cleanup-selection, backup, lock and Immich-client logic, with a stdlib fake server for the client.
- Integration tests are skipped unless the model is cached.
- A 20-image benchmark script covers speed.

**Target Platform**: macOS on Apple Silicon (M5, 32 GB), with the GPU via MPS; a CPU fallback exists but is slow. The Immich 3.2.2 Docker stack runs on the same machine.

**Project Type**: CLI tools plus a local loopback web UI, in a single Python package.

**Performance Goals**:
- ≥ 1.6 images/s end to end on the M5 (measured 1.80 in fp16).
- Full re-tag ≤ 17 h of run time (SC-002).
- Immich sync of the full library completes within Immich's own queue throughput.
- Filter latency in the web stays at about 1 ms (the index is unchanged).

**Constraints**:
- Interrupting a run loses at most one image.
- No partial sidecars ever exist.
- Never delete a non-managed Immich tag, and never delete a tag without first verifying that it is empty.
- The Immich key never appears in output.
- The Immich upload tree only ever receives `*.xmp` files and short-lived `*-tmp` files.

**Scale/Scope**:
- 87,969 images (134 GB), 87,755 existing sidecars, 4.6 KB each on average.
- Immich: 14,140 tags, of which about 7,054 are empty or legacy; about 2.82M tag links.
- New vocabulary: 30,877 tags in total, 26k of them writable (style excluded).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template: no principles are ratified. There are no formal gates to evaluate; running `/speckit-constitution` would make them explicit.

In its place, this plan is checked against the conventions the existing codebase and README already commit to:

| De-facto rule (source) | Pre-research | Post-design |
|---|---|---|
| Core module with no CLI concerns, plus a thin entry point (README "Layout") | ✅ New `sidecar`, `library`, `backup`, `immich` and `vocab` modules are pure. `*_cli.py` files only parse args and print | ✅ |
| The CLI and the web never reimplement filtering; both use `filterer.pattern_matches` (README, `index.py` docstring) | ✅ The category-aware rule changes that single function | ✅ `/api/search` warnings reuse the same pattern classification |
| Only explicit actions write. The tagger writes sidecars, export writes only into its output folder (README "Only two buttons write") | ⚠️ Adds new write paths: Immich tag deletion, backups and the state dir | ✅ Justified: deletions are limited to managed, verified-empty tags, with a preview and confirmation (web) or `--dry-run` (CLI). Backups and state live outside the library |
| Stdlib `http.server` on loopback, no framework (web.py docstring) | ✅ Unchanged; new routes are added to the same handler | ✅ |
| Interrupted runs are just partial runs (cli.py) | ✅ Kept, and strengthened with atomic writes and a lock | ✅ |

There are no unjustified violations. The new dependencies are covered under Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/001-pixai-full-retag/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── cli.md
│   ├── sidecar-xmp.md
│   ├── web-api.md
│   └── immich-api.md
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
pyproject.toml                 # name=immichtag; 5 console scripts; deps per Technical Context
README.md                      # rewritten for immichtag; tag-name changes; Immich setup
src/immichtag/                  # moved from src/wd14/; no wd14 module remains
├── __init__.py
├── __main__.py
├── config.py        NEW       # library root resolution (IMMICHTAG_DIR, WD14_DIR notice), config.toml, state dir
├── vocab.py         NEW       # model vocabulary (pinned config.json), categories→prefixes, META_WHITELIST,
│                              #   JUNK_TAGS, rating map, name escaping, pattern classification & suggestions
├── tagger.py        REWRITE   # PixAI v1.0 load (pinned revision, .to(device).half()), preprocessing,
│                              #   inference → per-category tags; no I/O besides reading the image
├── sidecar.py       NEW       # XMP read (TagsList precedence, attr/element provenance), build, merge
│                              #   foreign tags, atomic write — moved out of tagger.py
├── library.py       NEW       # scan + status classification (NEW/LEGACY/OUTDATED/CURRENT), queue order,
│                              #   flock-based single-writer lock, pending-sync list
├── backup.py        NEW       # pre-overwrite tar.gz + manifest, dedupe by sha256, restore
├── immich.py        NEW       # urllib client, asset/path mapping, refresh/discover/wait, managed cleanup
├── run.py           NEW       # one tagging run (shared by CLI and web): lock → backup → tag → sync
├── cli.py           REWRITE   # `immichtag`
├── immich_cli.py    NEW       # `immichtag-sync`
├── backup_cli.py    NEW       # `immichtag-backup`
├── filterer.py      CHANGE    # category-aware pattern_matches/leaf, new sidecar reader, warnings hook
├── filter_cli.py    CHANGE    # warnings + migration notice
├── index.py         CHANGE    # new leaf rule, per-image status counts, vocabulary for warnings
├── web.py           CHANGE    # TagJob → run.py; /api/pending-tagging; /api/immich/*; warnings
├── web_cli.py       CHANGE    # rename only
└── web/                       # title, re-tag banner, warnings, Immich sync/cleanup controls
    ├── index.html
    ├── app.js
    └── app.css

scripts/
├── bench20.py       NEW       # reproduces the 20-image speed/quality comparison
└── verify_immich.py NEW       # samples N assets, compares Immich tags with sidecar TagsList

tests/
├── fixtures/                  # wd14_legacy.xmp, immichtag_current.xmp, immich_rewritten.xmp, tiny images
├── unit/                      # test_sidecar, test_vocab, test_filterer, test_library, test_backup,
│                              #   test_immich (fake server), test_config, test_lock
└── integration/               # test_tagger_model (skipped unless model cached), test_run_smoke
```

**Structure Decision**:
- **Layout**: Single Python package under `src/`, keeping the existing "core module plus thin CLI" layout. The package directory is moved from `src/wd14/` to `src/immichtag/`. The project is not under git today; running `git init` before implementation is recommended, so the rename and every later change can be reviewed and reverted.
- **`run.py`**: The tagging workflow is shared by `cli.py` and `web.py`, so it lives in its own module. Today that loop is duplicated between the two files.

## Phase 0 — Research (complete)

See [research.md](./research.md). Every Technical Context unknown is resolved:

| # | Topic | Decision |
|---|---|---|
| R1 | Runtime | torch MPS fp16, batch 1; `.to(device).half()`, never `.to(dtype=)` (RoPE trap, verified) |
| R2 | Model code | Pinned-revision remote code; not vendored (the upstream repo declares no license) |
| R3 | Preprocessing | The model's own resize-and-pad, reimplemented and checked against upstream; 1-image prefetch |
| R4 | Thresholds | micro-F1 points: general 0.34 (≈37 tags/img), character 0.37, copyright 0.46, meta 0.47; rating argmax |
| R5 | Sidecar | Provenance namespace (attribute or element form); `TagsList` `rdf:Seq` as the source of truth; parser bug fix; atomic write |
| R6 | meta whitelist | 28 names across AI, official art, screenshots, photos and traditional media |
| R7 | `/` in names | → `∕` (U+2215); category-aware matching |
| R8 | Immich re-read | `refresh-metadata` by ids + `PUT /api/jobs/sidecar` discover; poll `GET /api/jobs` |
| R9 | Immich cleanup | Client-side, managed-only, verified-empty, deepest first; Immich's own `tag-cleanup` rejected |
| R10 | Backup | tar.gz + manifest in the state dir, deduplicated by sha256 (~40 MB for the library) |
| R11 | Single writer | `fcntl.flock` in the state dir |
| R12 | Config | `config.toml` + env overrides; state dir under Application Support |
| R13 | Warnings | Model vocabulary + library vocabulary, difflib suggestions |
| R14 | Rename | `immichtag*` scripts, no aliases |
| R15 | Tests | pytest unit + gated integration + benchmark script |

## Phase 1 — Design (complete)

- [data-model.md](./data-model.md): image status, sidecar, provenance, tag prefixes and matching rules, vocabulary, tagging-run state machine, pending sync, backup, and the external Immich tag and asset.
- [contracts/cli.md](./contracts/cli.md): the five commands, their flags, output and exit codes.
- [contracts/sidecar-xmp.md](./contracts/sidecar-xmp.md): the written format, the Immich-rewritten format, and the reader rules.
- [contracts/web-api.md](./contracts/web-api.md): changed and new routes.
- [contracts/immich-api.md](./contracts/immich-api.md): every Immich endpoint used, with permissions and error policy.
- [quickstart.md](./quickstart.md): eight validation scenarios, rollback, and clean-up.

**Delivery order**. Each step is independently testable and follows the spec's priorities:
1. **Rename and dependencies** (`immichtag`, removal of WD14/onnxruntime/pandas). Nothing else works until this lands.
2. **P1 · US1 (re-tag)**: `vocab`, `tagger`, `sidecar`, `library`, `backup`, `run`, `cli`. Validated by quickstart 1–4 and 6 on a copy.
3. **P1 · US2 (Immich)**: `immich`, `immich_cli`, the sync hook in `run`. Validated by quickstart 7.
4. **P2 · US3 (filter/export)**: `filterer`, `index`, `filter_cli`, `web` and the page. Validated by quickstart 5.
5. **P3 · US4 (daily flow)**: the web tag job moves onto `run.py`, and the lock is shared. Validated by quickstart 6.
6. **Full-library run**: quickstart 8. This is a 13–15 h operational step, not a code change.

**Post-design Constitution Check**: re-evaluated in the table above; all rows pass. The one ⚠️ row (new write paths) is justified and bounded.

## Complexity Tracking

| Addition | Why needed | Simpler alternative rejected because |
|---|---|---|
| PyTorch + torchvision + transformers + timm (≈ 1 GB installed) replace onnxruntime | PixAI v1.0 ships only as PyTorch weights with custom model code | ONNX/CoreML export fails on complex-number RoPE ops, so the model would have to be rewritten (R1). Hand-porting the backbone to drop transformers/timm is a 32-block reimplementation (R2) |
| `trust_remote_code` | The model class exists only in the upstream repo | Vendoring code with no declared license (R2); the risk is contained by pinning the revision SHA |
| Client-side Immich tag cleanup (≈ 7k verify calls the first time) | FR-019 forbids deleting non-managed tags | Immich's built-in `tag-cleanup` deletes empty tags under every prefix (R9) |
| State directory (lock, pending list, backups) outside the library | FR-007, FR-010 and FR-021 need durable state that must not land in Immich's upload tree | Files next to the sidecars would add 88k strays inside Immich's storage |
| Two new CLIs (`immichtag-sync`, `immichtag-backup`) | FR-018 (separate sync) and FR-007 (restore) need user-invocable entry points | Subcommands on `immichtag` would break its "no arguments = tag everything" contract |
