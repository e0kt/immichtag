---

description: "Task list for 001-pixai-full-retag"
---

# Tasks: 全库改用 PixAI 重新标注，彻底移除 WD14

**Input**: Design documents from `specs/001-pixai-full-retag/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: The spec does not ask for tests, but the plan requires them (research **R15**) for the risky logic: sidecar rewriting, Immich tag deletion and name escaping. They are listed first in each phase and must fail before the implementation lands.

**Organization**: Tasks are grouped by user story. US1 and US2 are both P1, US3 is P2, US4 is P3.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: US1–US4, from spec.md
- All paths are relative to the repository root `/Users/paul/programming/wd14`.

## Constants used throughout (from research.md)

- **Model**: `MODEL_REPO = "pixai-labs/pixai-tagger-v1.0"`, `MODEL_REVISION = "f33cfdb53c0c90b049bab9ce066eea1118970ef8"`
- **Model categories**:
  - `model.config.tags_split` order: `general 15043, character 8308, copyright 2460, style 4917, meta 145, rating 4`
  - Rating names: `rating:s rating:g rating:q rating:e`
- **Default thresholds**: general 0.34 · character 0.37 · copyright→`parody` 0.46 · meta 0.47 · rating argmax · style not written
- **Managed prefixes**: `character, parody, tag, meta, rating`. The legacy pipe form `character|…` / `tag|…` counts as managed for Immich cleanup only.
- **Name escape**: a `/` inside a managed tag's name is written as `∕` (U+2215)
- **Provenance**: XMP namespace `http://ns.animetag.local/1.0/`, prefix `animetag`, fields `model`, `revision`, `profile`, `taggedAt`
- **Paths**:
  - State dir: `~/Library/Application Support/animetag/<library id>/` on macOS, `$XDG_STATE_HOME/animetag/<id>/` elsewhere, where `<id>` = first 12 hex digits of the SHA-256 of the resolved root
  - Config: `~/.config/animetag/config.toml`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Version control, the rename, dependencies and the test scaffolding.

- [X] T001 Initialize git in `/Users/paul/programming/wd14`:
  - Add `.pytest_cache/` and `*.egg-info/` to `.gitignore`.
  - Run `git init && git add -A && git commit -m "Baseline before animetag migration"`, so the rename and every later change can be reviewed and reverted.
- [X] T002 Move the package `src/wd14/` → `src/animetag/` (`git mv`) and delete `src/animetag/__pycache__/`.
  - Replace every `wd14` module reference in `src/animetag/*.py` (`from wd14 import …`, `from wd14.x import …`, `wd14.cli`) with `animetag`.
  - Rewrite the docstrings in `src/animetag/__init__.py` ("animetag — anime image tagger → Immich-compatible XMP sidecars") and `src/animetag/__main__.py`.
  - At this point the code still runs WD14; only the names change.
- [X] T003 Rewrite `pyproject.toml`:
  - Metadata: `name = "animetag"`, a new description, `requires-python = ">=3.11,<3.14"`.
  - Dependencies: keep `huggingface-hub>=0.24`, `lxml>=5.0`, `numpy>=1.24`, `pillow>=10.0`, `tqdm>=4.66`; remove `onnxruntime` and `pandas`; add `torch>=2.4`, `torchvision>=0.19`, `transformers>=4.46`, `timm>=1.0`, `safetensors>=0.4`. Keep the comment explaining version floors, not pins.
  - `[project.scripts]`: `animetag = "animetag.cli:main"`, `animetag-filter = "animetag.filter_cli:main"`, `animetag-web = "animetag.web_cli:main"`, `animetag-immich = "animetag.immich_cli:main"`, `animetag-backup = "animetag.backup_cli:main"`.
  - Build: `[tool.hatch.build.targets.wheel] packages = ["src/animetag"]`; a `[dependency-groups] dev = ["pytest>=8"]`.
  - Pytest: `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and `markers = ["integration: needs the cached PixAI model and an Apple GPU"]`.
- [X] T004 Regenerate `uv.lock` and the environment with `uv lock && uv sync` from the repository root, then check the result:
  - `uv run python -c "import torch, transformers, timm; assert torch.backends.mps.is_available()"` succeeds.
  - `uv run python -c "import onnxruntime"` fails.
  - Record the resolved versions in the commit message.
- [X] T005 [P] Create `tests/__init__.py` (empty), `tests/unit/__init__.py`, `tests/integration/__init__.py` and `tests/conftest.py`. The conftest provides:
  - a `fixtures_dir` fixture pointing to `tests/fixtures`
  - a `tmp_library` fixture that copies `tests/fixtures/images/*` into `tmp_path/lib/<uuid-like>/ab/cd/` and returns the root
  - an autouse fixture that sets `ANIMETAG_STATE_DIR` to `tmp_path/state`, so tests never touch the real state dir. `config.py` must honour this override (T011).
  - a `pytest_collection_modifyitems` hook that skips `@pytest.mark.integration` tests unless `~/.cache/huggingface/hub/models--pixai-labs--pixai-tagger-v1.0/snapshots/f33cfdb53c0c90b049bab9ce066eea1118970ef8/model.safetensors` exists and `torch.backends.mps.is_available()` is true.
- [X] T006 [P] Create the test fixtures in `tests/fixtures/`:
  - `tests/fixtures/wd14_legacy.xmp`: a copy of one real sidecar from `/Users/paul/docker/immich/library/upload/**/*.jpg.xmp`, containing `tag/bangs` and `rating/sensitive`, three `rdf:Bag`s and no provenance.
  - `tests/fixtures/animetag_current.xmp`: the exact example in [contracts/sidecar-xmp.md](./contracts/sidecar-xmp.md), including the foreign tag `my_album/trip`.
  - `tests/fixtures/immich_rewritten.xmp`: the exiftool-rewritten form from research R5, with the full 40-hex revision. Provenance is written as child elements in their own `rdf:Description`; it has `dc:description/rdf:Alt/rdf:li` = `hello`, `xmp:Rating` 0, a `TagsList` `rdf:Seq` with `parody/fate∕grand_order`, `tag/long_hair` and `my_album/trip`, and stale `HierarchicalSubject`/`dc:subject` bags without `my_album/trip`.
  - `tests/fixtures/images/`: `a.jpg` (64×48 RGB) and `b.png` (48×64 RGBA with a transparent corner), generated once with Pillow.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Configuration, tag vocabulary rules, the sidecar format and library scanning. Every story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests (write first, must fail)

- [X] T007 [P] Write `tests/unit/test_config.py`, covering:
  - root resolution order `--dir` > `ANIMETAG_DIR` > default
  - a set `WD14_DIR` alone is ignored and produces exactly one stderr notice (`note: WD14_DIR is no longer read; set ANIMETAG_DIR instead`)
  - `config.toml` values are overridden by `ANIMETAG_IMMICH_URL`, `ANIMETAG_IMMICH_API_KEY` and `ANIMETAG_IMMICH_PATH_MAP`
  - a group- or world-readable config file triggers a warning
  - the api key never appears in `repr(ImmichSettings)`
  - the state dir follows `ANIMETAG_STATE_DIR` and the library id is stable for the same resolved root
- [X] T008 [P] Write `tests/unit/test_vocab_core.py`, covering:
  - `escape_name("fate/grand_order") == "fate∕grand_order"`, and `unescape_name` reverses it
  - `is_managed()` is true for `tag/x`, `parody/x`, `character|x` and `tag|/_`, and false for `my_album/trip` and `untagged_foreign`
  - `split_tag()` returns `("parody", "fate∕grand_order")` for `parody/fate∕grand_order` and `(None, "my_album∕trip")` for `my_album/trip`
  - `normalize()` folds case, spaces and `-`, and folds `/`→`∕` only inside the name part (`"Parody/Fate/Grand Order"` → `"parody/fate∕grand_order"`)
  - `META_WHITELIST` has exactly the 28 names from research R6
  - `RATING_MAP` maps `g/s/q/e` → `general/sensitive/questionable/explicit`
- [X] T009 [P] Write `tests/unit/test_sidecar.py`, covering:
  - `build_xmp()` → `read_sidecar()` round trip: order preserved, `TagsList` is an `rdf:Seq`, the other two properties are `rdf:Bag` with identical values, and provenance comes back
  - `read_sidecar(immich_rewritten.xmp)` returns tags `[parody/fate∕grand_order, tag/long_hair, my_album/trip]` (TagsList precedence, `hello` never read) and the element-form provenance
  - `read_sidecar(wd14_legacy.xmp)` has no provenance and includes `tag/bangs`
  - a garbage file yields `(tags=[], provenance=None, error=...)`
  - `merge_tags(new_managed, old_tags)` keeps `my_album/trip` after the managed tags and drops old managed tags
  - `write_atomic()` leaves no `*.animetag-tmp` file and survives a simulated crash: monkeypatch `os.replace` to raise, then assert the original bytes are unchanged
- [X] T010 [P] Write `tests/unit/test_library.py`, covering:
  - `classify()` returns NEW (no sidecar), LEGACY (wd14 fixture, or an unreadable sidecar), OUTDATED (provenance with another revision) and CURRENT (matching model+revision, attribute or element form)
  - `tagging_queue()` orders all NEW first, then LEGACY/OUTDATED, each group in scan order, and `overwrite=True` also includes CURRENT
  - `remove_stale_tmp(root)` deletes `.*.xmp.animetag-tmp` files only
  - `PendingSync` append/read/remove round trip, with paths relative to the root

### Implementation

- [X] T011 [P] Create `src/animetag/config.py`, per research R12:
  - `DEFAULT_ROOT = Path("/Users/paul/docker/immich/library/upload")`
  - `resolve_root(cli_dir: str | None) -> Path`, which prints the one-time `WD14_DIR` notice
  - `state_dir(root) -> Path`, created on demand, honouring `ANIMETAG_STATE_DIR`
  - `library_id(root)`
  - `ImmichSettings(url, api_key, path_map)`, a dataclass whose `__repr__` masks the key
  - `load_immich_settings()`, reading `~/.config/animetag/config.toml` via `tomllib` with env overrides and warning when the file mode is looser than 0600
- [X] T012 [P] Create `src/animetag/vocab.py`:
  - `MANAGED_PREFIXES`
  - `CATEGORY_PREFIX = {"general": "tag", "character": "character", "copyright": "parody", "meta": "meta", "rating": "rating"}` (style absent)
  - `DEFAULT_THRESHOLDS = {"general": .34, "character": .37, "copyright": .46, "meta": .47}`
  - `JUNK_TAGS`, moved verbatim from the old `src/animetag/tagger.py`
  - `META_WHITELIST`, the 28 names in research R6
  - `RATING_MAP`, `ESCAPE = "∕"`
  - the functions `escape_name`, `unescape_name`, `is_managed`, `split_tag`, `normalize` and `leaf_of`
  - This is the single place the rules in [data-model.md → Tag](./data-model.md#tag) live.
- [X] T013 Create `src/animetag/sidecar.py`, which depends on T012. It implements [contracts/sidecar-xmp.md](./contracts/sidecar-xmp.md):
  - `Provenance(model, revision, profile, tagged_at)`
  - `SidecarData(tags, provenance, error)`
  - `sidecar_path(image)`
  - `read_sidecar(path)`:
    - A regex fast path applies only when the file contains `animetag:model="`; everything else goes through lxml with `recover=True`.
    - Read `rdf:li` elements only under the `digiKam:TagsList`, then `lr:HierarchicalSubject`, then `dc:subject` `Seq`/`Bag`.
    - Accept the provenance attribute and element forms.
  - `read_tags(path)`
  - `build_xmp(tags, provenance) -> bytes`, using lxml; `TagsList` as `rdf:Seq`, the other two as `rdf:Bag`
  - `merge_tags(managed, old)`
  - `write_atomic(path, data)`: write `.<name>.animetag-tmp` in the same directory, `fsync`, `os.replace`
- [X] T014 Create `src/animetag/library.py`, which depends on T013:
  - `IMAGE_EXTS` and `SKIP_DIRS`, moved from the old tagger
  - `image_files(root)`
  - `Status` enum (`NEW`, `LEGACY`, `OUTDATED`, `CURRENT`)
  - `classify(image, model, revision)`
  - `scan(root, model, revision) -> list[(Path, Status)]`
  - `tagging_queue(entries, overwrite=False)`
  - `remove_stale_tmp(root)`
  - `PendingSync(state_dir)` with `append(path, had_sidecar)`, `entries()` and `remove(paths)`, backed by `pending-sync.jsonl`; paths are relative to the root, the file is rewritten atomically on remove
- [X] T015 Rewire `src/animetag/filterer.py`, `src/animetag/index.py`, `src/animetag/filter_cli.py` and `src/animetag/web_cli.py` onto the new foundation, so the package imports without the old tagger internals:
  - `src/animetag/filterer.py`: take `IMAGE_EXTS`/`SKIP_DIRS` from `library`; replace `parse_sidecar`/`LI_RE` with `sidecar.read_tags`; take `normalize` from `vocab` (the category-aware matching comes in US3).
  - `src/animetag/index.py`: use `sidecar.read_tags`.
  - `src/animetag/filter_cli.py` and `src/animetag/web_cli.py`: use `config.resolve_root` instead of `cli.DEFAULT_DIR`.
  - Then run `uv run pytest tests/unit -q`; T007–T010 must pass.

**Checkpoint**: Foundation ready. `uv run animetag-filter --list-tags 20` still works on the real library, now through the new reader.

---

## Phase 3: User Story 1 — 用新模型重新标注整个图库 (Priority: P1) 🎯 MVP

**Goal**: A single `animetag` run re-tags every image with PixAI v1.0: new images first, legacy after. It backs up before overwriting, writes atomically, can be resumed, and leaves no WD14 code path.

**Independent Test**: [quickstart.md](./quickstart.md) scenarios 1–4, on a throwaway copy with `--no-immich`.

### Tests for User Story 1

- [X] T016 [P] [US1] Write `tests/unit/test_tagger_postprocess.py` for `tags_from_probs(probs, names, categories, thresholds, keep_common, include_rating)` with a synthetic vocabulary. It covers:
  - per-category thresholds
  - style is never emitted
  - meta is emitted only when whitelisted
  - junk tags are dropped unless `keep_common`
  - exactly one rating by argmax, mapped through `RATING_MAP`
  - `include_rating=False` emits no rating
  - `/` in names is escaped
  - output order character, parody, tag, meta, rating
  - `tag/untagged` when nothing passes
- [X] T017 [P] [US1] Write `tests/unit/test_backup.py`, covering:
  - `create_backup()` archives the given sidecars byte for byte, with `manifest.jsonl` (path, size, mtime, sha256) and `meta.json`
  - a second backup skips files whose sha256 is already archived
  - `restore(id)` takes a `restore-safety` backup first and then reproduces the original bytes exactly (SC-007)
  - `list_backups()` returns newest first
- [X] T018 [P] [US1] Write `tests/unit/test_lock.py`:
  - `TaggingLock` acquired twice in one process raises `LockBusy` naming the holder's pid and start time.
  - A child process (`subprocess` running `python -c` that holds the lock and is killed) releases it on death.
- [X] T019 [P] [US1] Write `tests/unit/test_run.py` against `tmp_library`, with a stub tagger injected into `TaggingRun`. The stub returns fixed probabilities and needs no torch. It covers:
  - NEW images are processed before LEGACY
  - a backup exists before the first overwrite; setting the cancel event after the first image leaves every sidecar either untouched or complete
  - the resumed run skips CURRENT images
  - `PendingSync` receives one entry per written sidecar, with the right `had_sidecar`
  - a foreign tag in an old sidecar survives
  - an undecodable image is counted as failed and its sidecar is left untouched
  - an image deleted mid-run is counted as skipped
- [X] T020 [P] [US1] Write `tests/integration/test_tagger_model.py` (`@pytest.mark.integration`), covering:
  - after `load_model("mps")` every `blocks.*.attn.freqs_cis` buffer is still `torch.complex64` and the weights are fp16
  - on `tests/fixtures/images/a.jpg`, `tagger.preprocess()` matches the upstream `AutoImageProcessor` output (`trust_remote_code`, same revision) with max abs diff < 1e-5
  - on two images, fp16 probabilities are within 0.01 of an fp32 CPU reference

### Implementation for User Story 1

- [X] T021 [P] [US1] Rewrite `src/animetag/tagger.py`, deleting all WD14/ONNX/pandas code. It provides:
  - `select_device(pref)`: `auto` means mps if available, else cpu
  - `load_model(device)`:
    - `AutoModel.from_pretrained(MODEL_REPO, revision=MODEL_REVISION, trust_remote_code=True).eval().to(device)`, then `.half()` on mps
    - **never** `.to(dtype=...)`, because it destroys the complex RoPE buffers (research R1)
    - assert `freqs_cis.is_complex()`
    - return a `Tagger` holding `model`, `device`, `names = model.config.tags` and `categories` expanded from `model.config.tags_split`
  - `preprocess(path) -> Tensor`, per research R3:
    - composite onto white and convert to RGB
    - `torchvision.transforms.functional.to_tensor`
    - with `r = min(1008/h, 1008/w)`, resize to `(int(h*r), int(w*r))` via `transforms.functional.resize`
    - pad `[left, top, right, bottom]` = `[pw//2, ph//2, pw-pw//2, ph-ph//2]` with value 0
    - normalize with mean and std `[0.5]*3`
  - `Tagger.infer(tensor) -> np.ndarray`: fp32 sigmoid under `torch.inference_mode()`
  - `tags_from_probs(...)`, per T016
  - `iter_preprocessed(paths)`: a one-thread `ThreadPoolExecutor` prefetch that yields `(path, tensor | exception)`
  - `profile_name(thresholds)`: `"v1"` when all defaults, else `"custom:g…,c…,p…,m…"`
- [X] T022 [P] [US1] Create `src/animetag/backup.py`, per research R10 and [data-model.md → Backup](./data-model.md#backup):
  - `create_backup(root, sidecars, state_dir, reason) -> str | None`: skips when every sidecar is already archived; writes `backups/<UTC %Y%m%dT%H%M%SZ>/{sidecars.tar.gz, manifest.jsonl, meta.json}`; raises before returning if any write fails
  - `list_backups(state_dir)`
  - `restore(root, backup_id, state_dir, dry_run=False) -> list[Path]`: takes a safety backup, then calls `sidecar.write_atomic` for each file
- [X] T023 [P] [US1] Add `TaggingLock` and `LockBusy` to `src/animetag/library.py`, per research R11:
  - `fcntl.flock(LOCK_EX|LOCK_NB)` on `<state>/tagging.lock`
  - the pid and ISO start time are written into the file for error messages
  - usable as a context manager
- [X] T024 [US1] Create `src/animetag/run.py`, which depends on T021–T023. It is the single tagging workflow shared by the CLI and the web:
  - `RunOptions`: thresholds, `keep_common`, `include_rating`, `overwrite`, `limit`, `device`, `immich` flag
  - `RunProgress`: `total_new`, `total_retag`, `done`, `written`, `failed`, `skipped`, `device`, `eta_seconds` from a moving average of the last 50 images, `backup_id`, `state`, `messages`, `sync`
  - `TaggingRun(root, options, tagger_factory=tagger.load_model, on_progress=None, cancel: threading.Event)` with `run()`, which proceeds as follows:
    1. `remove_stale_tmp`, then scan and queue, honouring `limit`
    2. acquire `TaggingLock`
    3. `create_backup` of the queued items that have a sidecar
    4. load the model
    5. iterate `iter_preprocessed`: infer → `tags_from_probs` → `merge_tags` with the old sidecar's foreign tags → `build_xmp` with provenance (`MODEL_REPO`, `MODEL_REVISION`, `profile_name`, UTC now) → `write_atomic` → `PendingSync.append`
    6. count `FileNotFoundError` as skipped and other exceptions as failed, with a message
    7. stop cleanly on `cancel`
    8. call the post-run hook `self.after_write(progress)`, a no-op until US2
  - Returns the final `RunProgress`.
- [X] T025 [US1] Rewrite `src/animetag/cli.py` for the `animetag` command per [contracts/cli.md](./contracts/cli.md#animetag--tag-and-re-tag):
  - Flags and defaults: `--dir --dry-run --limit --overwrite --general-threshold --character-threshold --parody-threshold --meta-threshold --keep-common-tags --no-rating --device --no-immich`. `--rating-threshold` is gone.
  - `--dry-run` prints the counts per status and an ETA at 1.8 img/s (mps) or 0.2 img/s (cpu, marked as an estimate) without loading the model.
  - Print the scan, backup and model lines, then run a tqdm bar driven by `on_progress`.
  - On a cpu device, print a warning with an ETA.
  - SIGINT sets the cancel event and exits 130 after the run unwinds.
  - `LockBusy` exits 3.
  - Exit 1 when failed > 0 and written == 0.
- [X] T026 [P] [US1] Create `src/animetag/backup_cli.py` for `animetag-backup list|restore ID [--dir] [--dry-run] [--no-immich]`, per contracts/cli.md:
  - `list` prints the id, count, compressed size and reason.
  - `restore` calls `backup.restore`, then appends the restored paths to `PendingSync`. The Immich sync is wired in T037.
- [X] T027 [US1] Keep `src/animetag/web.py` working. Replace `AppState._run_tagging`'s use of `tagger.load_model`/`tagger.process_one` with a `run.TaggingRun` using default `RunOptions`, and map `RunProgress` onto the existing `TagJob` fields (`total`, `done`, `written`, `skipped`, `failed`, `eta`). The full job state machine comes in US4 (T050).
- [X] T028 [US1] Remove every WD14 remnant from `src/animetag/`:
  - `rg -n "onnxruntime|pandas|SmilingWolf|selected_tags|wd-v1-4|process_one|preprocess_image" src/` must return nothing.
  - The old threshold constants (`DEFAULT_PARODY_THRESHOLD`, `DEFAULT_RATING_THRESHOLD`) and the category ids 0/3/4/9 are deleted.
  - Then run `uv run pytest -q`; unit tests pass, and integration tests pass when the model is cached.

**Checkpoint**: Run quickstart scenarios 1–4 on a copy. US1 is fully functional without Immich: written images are listed in the pending sync file.

---

## Phase 4: User Story 2 — Immich 里的标签与新 sidecar 保持一致 (Priority: P1)

**Goal**: After every run, and on demand, Immich re-reads the written sidecars and loses every empty *managed* tag, including the 6,816 legacy pipe tags. Nothing non-managed is ever deleted, and an unreachable Immich never fails a run.

**Independent Test**: This works even on the current WD14 sidecars. `animetag-immich cleanup --dry-run` lists the legacy pipe tags and no non-managed tag; `animetag-immich sync --all` then makes Immich's tags equal each sidecar's `TagsList` (quickstart scenario 7).

### Tests for User Story 2

- [X] T029 [P] [US2] Write `tests/unit/test_immich_client.py` against a stdlib `ThreadingHTTPServer` fake implementing the endpoints in [contracts/immich-api.md](./contracts/immich-api.md). It covers:
  - every request carries `x-api-key`
  - `iter_assets()` pages until `nextPage` is null
  - `run_asset_job()` batches 1,200 ids into 500/500/200
  - 5xx is retried three times (patch the sleep), then `ImmichUnavailable` is raised
  - 401/403 raises `ImmichAuthError` naming the endpoint and permission
  - a 404 on `PUT /api/jobs/sidecar` returns `discovery_unavailable=True`
  - `wait_idle()` polls until `active+waiting==0` for `sidecar` and `metadataExtraction`
  - no exception message or log line contains the key
- [X] T030 [P] [US2] Write `tests/unit/test_immich_cleanup.py` for `plan_cleanup(tags, in_use)`. It covers:
  - a managed tag not in use is a candidate, and so is a legacy `character|x`
  - a non-managed tag (`my_album/trip`, `vacation`) is never a candidate
  - an ancestor of an in-use value (`parody` when `parody/fate∕grand_order` is in use) is never a candidate
  - candidates come back deepest first
  - `run_cleanup(dry_run=True)` deletes nothing and reports the count, `byPrefix` and 20 examples
  - a candidate whose verify search returns an asset is skipped, not deleted
- [X] T031 [P] [US2] Write `tests/unit/test_immich_sync.py` with the fake server and a `tmp_library`. It covers:
  - `PathMap.parse("/data=/x")` and the `to_local`/`to_remote` round trip
  - autodetect from one asset path
  - pending entries with `had_sidecar=True` → `refresh-metadata` for exactly those asset ids
  - any `had_sidecar=False` → one sidecar discovery call
  - after `wait_idle`, cleanup runs and the pending entries are removed
  - with the server down, `SyncResult.ok` is false, the reason is set and the pending entries are kept
  - `sync --all` covers every local image that maps to an asset

### Implementation for User Story 2

- [X] T032 [US2] Create `src/animetag/immich.py`, part 1 (client), per contracts/immich-api.md:
  - `ImmichClient(settings)` on `urllib.request`, with JSON bodies, a 30 s timeout, and retries at 1, 4 and 16 s on connection errors and 5xx
  - `ImmichUnavailable`, and `ImmichAuthError(endpoint, permission)`
  - `version()`
  - `iter_assets()` via `POST /api/search/metadata {page, size:1000, withDeleted:true}`, yielding `(id, originalPath)`
  - `tag_has_assets(id)`
  - `run_asset_job(ids, name="refresh-metadata")` in batches of 500
  - `start_sidecar_discovery(force=False)`, returning False on 404
  - `queue_counts()` from `GET /api/jobs`
  - `wait_idle(timeout=7200, poll=5, on_progress)`
  - `list_tags()` and `delete_tag(id)`
  - `PathMap` with `parse`, `to_local`, `to_remote` and `autodetect(client, root)`
- [X] T033 [US2] Add part 2 (cleanup) to `src/animetag/immich.py`, per research R9:
  - `managed_in_use(root) -> set[str]`: every managed tag value (normalized) across all sidecars via `sidecar.read_tags`, plus all their ancestor paths
  - `plan_cleanup(tags, in_use) -> list[tag]`, deepest first
  - `run_cleanup(client, candidates, dry_run, on_progress) -> CleanupResult(count, deleted, skipped_nonempty, by_prefix, examples)`, which verifies each candidate is empty before `delete_tag`
- [X] T034 [US2] Add part 3 (sync) to `src/animetag/immich.py`, per research R8. `sync(root, settings, state_dir, all=False, cleanup=True, on_progress=None) -> SyncResult(ok, reason, refreshed, discovered, deleted_tags, pending)`:
  1. map the local paths to asset ids with `iter_assets` and `PathMap`
  2. refresh the known assets
  3. run discovery if any entry is NEW (or always with `all`)
  4. `wait_idle`
  5. `run_cleanup`, unless disabled
  6. `PendingSync.remove` the covered entries only on success

  It catches `ImmichUnavailable`/`ImmichAuthError` into a `SyncResult`. A discovery 404 sets `reason = "discovery needed: Immich → Administration → Jobs → Sidecar metadata → Discover"` but still counts as ok for the refreshed entries.
- [X] T035 [US2] Create `src/animetag/immich_cli.py` for `animetag-immich sync [--all] [--no-cleanup] [--dry-run] | cleanup [--dry-run] | status`, per contracts/cli.md:
  - Without a key: exit 2 with the exact message from the contract.
  - `status`: prints the version, reachability, pending count, managed-empty count, and a permission check made by calling `GET /api/tags`, `GET /api/jobs` and a size-1 search.
  - `sync --dry-run`: prints what would be refreshed, discovered and deleted without calling any write endpoint.
- [X] T036 [US2] Wire automatic sync into the tagging run in `src/animetag/run.py` and `src/animetag/cli.py`:
  - `src/animetag/run.py`: `after_write` calls `immich.sync(root, settings, state_dir)` when `options.immich` is on, a key is configured and at least one sidecar was written. The result goes in `RunProgress.sync`; `state` becomes `syncing` meanwhile.
  - `src/animetag/cli.py`: print `Immich: refreshed N, discovered M, removed K empty tags.`, or `Immich sync pending (N images): <reason>. Run: animetag-immich sync`. The exit code is unchanged (FR-021).
- [X] T037 [US2] In `src/animetag/backup_cli.py`, make `restore` run `immich.sync` for the restored paths unless `--no-immich`, and print the same result lines as T036.
- [X] T038 [US2] Add the Immich routes to `src/animetag/web.py`, per [contracts/web-api.md](./contracts/web-api.md#new):
  - `POST/GET /api/immich/sync`: one background `SyncJob` at a time, with `phase` in refresh|discover|waiting|cleanup, `done` and `total`
  - `GET /api/immich/cleanup-preview`
  - `POST /api/immich/cleanup`: 400 without `{"confirm": true}`
  - `/api/status` gains `immich: {configured, reachable, pending}`
- [X] T039 [US2] Add the Immich UI to `src/animetag/web/index.html`, `src/animetag/web/app.js` and `src/animetag/web/app.css`:
  - a "Sync to Immich (N)" header button, visible when `status.immich.pending > 0`, with phase progress from polling `GET /api/immich/sync`
  - a "Clean up Immich tags" panel that shows the preview (count, by prefix, examples) and enables a Delete button only after the preview has loaded; it posts `confirm: true`
  - no `alert`/`confirm` dialogs

**Checkpoint**: quickstart scenario 7 passes. For 20 re-tagged assets, Immich's tags equal their sidecar's `TagsList`. The legacy pipe tags are gone and no non-managed tag was touched.

---

## Phase 5: User Story 3 — 导出与筛选适配新标签 (Priority: P2)

**Goal**: The filter CLI and the web match the new scheme:
- category-aware patterns, including `parody/` and `meta/` and names containing `/`
- unchanged `rating/*` names
- warnings with suggestions for legacy or unknown tags
- a migration notice while any image is not re-tagged
- the CLI and the web still give identical results

**Independent Test**: [quickstart.md](./quickstart.md) scenario 5 on a re-tagged copy. On the current (legacy) library, `-f bangs` must also warn "legacy" with a count.

### Tests for User Story 3

- [X] T040 [P] [US3] Write `tests/unit/test_filterer_patterns.py` for `pattern_matches` and `TagFilter`. It covers:
  - `fate/grand_order`, `parody/fate/grand_order` and `Parody/Fate∕Grand Order` all match the stored `parody/fate∕grand_order`
  - `grand_order` alone does not
  - `rating/explicit` matches only `rating/explicit`
  - `rating/*` and `character/*` are full-path globs; `blue_*` is a leaf glob
  - a bare `long_hair` matches `tag/long_hair`
  - a foreign `my_album/trip` is matched by `my_album/trip` as a whole name
  - a legacy `tag/\||/` is matched by `\||/`
  - `-tag` exclusions and `--exclude` keep today's semantics
- [X] T041 [P] [US3] Write `tests/unit/test_filter_parity.py`. Build a small library in `tmp_path` whose sidecars cover slash names, legacy WD14 tags, meta and parody tags. For 12 filter/exclude combinations, `index.build(root).select(...)` returns exactly the same images as `filterer.find_matches(...)` (FR-026).
- [X] T042 [P] [US3] Write `tests/unit/test_vocab_warnings.py` with a fake model vocabulary (`blunt_bangs`, `swept_bangs`, `hair_between_eyes`, `blue_hair`) and library counts `{tag/bangs: 42}`. It covers:
  - `classify_pattern("bangs")` → `legacy` with count 42
  - `"blue_hiar"` → `unknown` with `blue_hair` suggested
  - a vocabulary tag with no images → `empty`
  - a glob is never classified
  - `suggest()` returns at most 5 names, close matches first
  - `load_model_vocabulary()` returns None, not an exception, when offline (patch `hf_hub_download` to raise)

### Implementation for User Story 3

- [X] T043 [US3] Make matching category-aware in `src/animetag/filterer.py`, per data-model.md → Tag:
  - `pattern_matches(pattern, full, leaf)` targets the full tag when `vocab.split_tag(pattern)[0]` is a managed prefix, and the leaf otherwise.
  - `_has_tag` uses the same rule.
  - `find_matches` computes leaves with `vocab.leaf_of` instead of `rsplit("/", 1)`.
  - `TagFilter.parse` and `parse_exclusions` normalize through `vocab.normalize`.
  - `find_matches` gains an optional `stats` collector that counts sidecar statuses and the occurrences of requested patterns, for T046.
  - Update the docstrings that describe the old "contains a `/`" rule.
- [X] T044 [P] [US3] Extend `src/animetag/vocab.py`:
  - `load_model_vocabulary()`: `hf_hub_download(MODEL_REPO, "config.json", revision=MODEL_REVISION)`, mapping each normalized name → (category, prefix or None for style). Cached per process; returns None when unavailable.
  - `classify_pattern(pattern, library_counts, model_vocab) -> Warning | None` with kinds `legacy`, `unknown` and `empty`
  - `suggest(name, candidates, n=5)` via `difflib.get_close_matches` plus substring matches
- [X] T045 [US3] Update `src/animetag/index.py`:
  - leaves via `vocab.leaf_of`, and `by_leaf` keyed on the new leaf
  - during `build()`, record each image's `library.Status` from its sidecar provenance, exposed as `retag_counts() -> {"legacy": n, "outdated": n}`
  - `warnings_for(filters, exclude) -> list[dict]`, returning `{pattern, kind, count, similar}` for plain patterns that expand to nothing, using `vocab.classify_pattern` with the index's tag counts and the model vocabulary
- [X] T046 [US3] Update `src/animetag/filter_cli.py`, per [contracts/cli.md → animetag-filter](./contracts/cli.md#animetag-filter--export):
  - Before scanning: warn about plain patterns missing from the model vocabulary, with suggestions.
  - After scanning: print the legacy count, or `does not exist`, from the `stats` collector (`at least N` when stopped early).
  - Print `note: N images are not re-tagged yet; results may be incomplete` when the scan saw LEGACY/OUTDATED sidecars.
  - Update the `EPILOG` examples to tags that exist in the new vocabulary (e.g. `parody/fate/grand_order`, `meta/ai-generated`, `-x rating/explicit`).
- [X] T047 [US3] Update `src/animetag/web.py`:
  - `/api/search` adds `warnings` from `index.warnings_for`.
  - `/api/status` adds `retag` (from `index.retag_counts()`) and `model: {name, revision[:7]}`.
  - `GET /api/untagged` is renamed to `GET /api/pending-tagging`, returning `{"new","legacy","outdated","current"}` from `library.scan`.
  - Update the module docstring to describe animetag and PixAI.
- [X] T048 [US3] Update `src/animetag/web/index.html`, `src/animetag/web/app.js` and `src/animetag/web/app.css`:
  - The `<title>` and `<h1>` become `animetag`.
  - A dismissible banner "N images not re-tagged yet — results may be incomplete" shows when `status.retag` totals > 0.
  - Warnings render under the filter row whose pattern produced them; clicking a suggestion replaces that pattern and re-runs the search.
  - The header tag button uses `/api/pending-tagging` and shows "Tag N new · re-tag M".
  - The autocomplete shows `parody/` and `meta/` tags like the others, with no special-casing needed.

**Checkpoint**: quickstart scenario 5 passes, and the CLI and web counts are identical for the same filters.

---

## Phase 6: User Story 4 — 日常标注新上传图片 (Priority: P3)

**Goal**: After the migration, one command or one click tags only the new uploads and syncs Immich. The web and the CLI can never write concurrently.

**Independent Test**: With a fully re-tagged copy plus 5 new images:
- one `animetag` run touches exactly those 5 and syncs them
- a CLI run started while the web job runs exits 3 without writing (quickstart scenario 6)

### Tests for User Story 4

- [X] T049 [P] [US4] Write `tests/unit/test_web_tagjob.py` against `AppState` with a stub tagger factory injected into `run.TaggingRun`. It covers:
  - job states progress through `scanning → … → finished`, with `totalNew`/`totalRetag` correct
  - when another process holds `TaggingLock`, the job ends in `refused` with the holder in `error`
  - a second `start_tagging()` attaches to the running job
  - after a run with writes the index is rebuilt and `pending-tagging` reports 0 new

### Implementation for User Story 4

- [X] T050 [US4] Rebuild `TagJob` in `src/animetag/web.py` on `run.TaggingRun` with the full state set from [contracts/web-api.md](./contracts/web-api.md#post-apitag-and-get-apitag):
  - states `scanning | locking | backing-up | loading | running | syncing | finished | cancelled | error | refused`
  - fields `totalNew`, `totalRetag`, `device`, `eta`, `backupId`, `sync`
  - cancellation through `RunOptions`' cancel event
  - `LockBusy` maps to `refused`
  - the index is rebuilt after any run with writes, before the state is set final, preserving the existing ordering guarantee in `_run_tagging`
- [X] T051 [US4] Update the tag panel in `src/animetag/web/app.js`:
  - show new versus re-tag counts, the device (with a CPU warning), the ETA, the backup id and the post-run Immich result line
  - show `refused` with the holder message
  - reattach after a reload, as today

**Checkpoint**: quickstart scenario 6 passes and the US4 acceptance scenarios hold.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T052 [P] Rewrite `README.md` for animetag (FR-027, FR-028). Cover:
  - the five commands and their flags, with a migration section: the old `wd14*` commands and `WD14_DIR` are gone
  - Immich setup: an admin API key with `asset.read, asset.update, job.create, job.read, tag.read, tag.delete`; `~/.config/animetag/config.toml` (chmod 600) with `path_map`
  - what the automatic sync and cleanup do and what they never touch
  - the tag scheme: prefixes, `meta/` whitelist, one rating, `∕` in names, and new-vocabulary differences such as `bangs` → `blunt_bangs`/`swept_bangs`/`hair_between_eyes`
  - backups and `animetag-backup restore`
  - performance (≈1.8 img/s on the M5, ≈14 h for the library)
  - removing the old WD14 cache
  - an updated Layout table with the new modules
- [X] T053 [P] Create `scripts/bench20.py`:
  - reads a list of image paths (default: 20 random images with `--seed 20`, read-only)
  - times preprocess and inference per image with `animetag.tagger`
  - prints img/s, images with a character, and the average number of `tag/` tags
  - never writes sidecars
- [X] T054 [P] Create `scripts/verify_immich.py`:
  - samples `--sample N` random local images that have sidecars
  - maps them to Immich assets via `immich.PathMap`/`iter_assets`
  - fetches `GET /api/assets/{id}` tags and compares them as sets with `sidecar.read_tags`
  - prints the mismatches and exits 1 if any exist (SC-003)
- [X] T055 Audit the rename and dependency removal across `src/`, `tests/` and `pyproject.toml`:
  - `rg -n -i "wd14" --glob '!specs/**' --glob '!README.md' .` returns only the `WD14_DIR` notice in `src/animetag/config.py` and its test.
  - `rg -n "onnxruntime|pandas" src tests pyproject.toml` returns nothing.
  - `uv run animetag --help`, `animetag-filter --help`, `animetag-web --help`, `animetag-immich --help` and `animetag-backup --help` all work.
- [ ] T056 *(Status 2026-09-21: 132 tests pass, including integration; quickstart 1–6 verified on library copies. Scenario 7 is still open: it needs the user's Immich API key.)* Run `uv run pytest -q` over `tests/` (all unit tests, plus integration tests with the model cached), then quickstart scenarios 1–7 end to end, and fix any failures before the full run.
- [ ] T057 *(Status: waiting for the user.)* Operational step, run by the user, about 13–15 h (scenario 8 in `specs/001-pixai-full-retag/quickstart.md`):
  1. Take a fresh Immich DB backup, then run `uv run animetag`; it can be resumed.
  2. Verify the success criteria:
     - `uv run animetag --dry-run` shows 0 to re-tag (SC-001)
     - `uv run animetag-immich status` shows pending 0 and 0 empty managed tags (SC-003)
     - `uv run python scripts/verify_immich.py --sample 100` passes (SC-003)
     - `uv run python scripts/bench20.py` gives ≥ 15/20 with a character and an average of 25–45 `tag/` tags (SC-004, SC-005)
  3. Keep the first backup until you have accepted the result.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001–T006)**: T001 → T002 → T003 → T004. T005 and T006 run in parallel after T001.
- **Foundational (T007–T015)**: needs Setup. Tests T007–T010 in parallel, then T011 and T012 in parallel, then T013 → T014 → T015. **Blocks every story.**
- **US1 (T016–T028)**: needs Foundational.
- **US2 (T029–T039)**: needs Foundational.
  - The client, cleanup and sync (T029–T035) are independent of US1 and can start in parallel with it. That is useful, because the cleanup of legacy tags works on today's sidecars.
  - T036 needs T024/T025 (run.py, cli.py). T037 needs T026. T038/T039 need T027 (web.py already on run.py).
- **US3 (T040–T048)**: needs Foundational. Independent of US1 and US2, except that T047/T048 edit `web.py`/`web/*`, which T038/T039 also edit, so do them in sequence, not in parallel.
- **US4 (T049–T051)**: needs US1 (run.py, lock) and US2 (sync inside the run), and follows T047/T048 on the same files.
- **Polish (T052–T057)**: after the stories you intend to ship. T057 needs everything.

### Story completion order

```text
Setup → Foundational ─┬─► US1 (MVP) ──┬─► US4
                      ├─► US2 ────────┤
                      └─► US3 ────────┘──► Polish → full run (T057)
```

### Within each story

Tests first, and they must fail. Then new modules, then the CLI and web wiring, then the checkpoint scenario.

---

## Parallel Examples

**Foundational**: T007, T008, T009 and T010 (four test files) together, then T011 (`config.py`) and T012 (`vocab.py`) together.

**US1**: T016–T020 (five test files) together, then T021 (`tagger.py`), T022 (`backup.py`) and T023 (lock in `library.py`) together, then T024 → T025, with T026 (`backup_cli.py`) in parallel with T025.

**US2 alongside US1**: once Foundational is done, T029–T031 (tests) and then T032 → T033 → T034 → T035 (`immich.py`, `immich_cli.py`) can proceed while US1 is being built. They touch no US1 file.

**US3**: T040–T042 (tests) together; T044 (`vocab.py`) in parallel with T043 (`filterer.py`).

---

## Implementation Strategy

### MVP first (US1)

1. Setup, then Foundational.
2. Phase 3 (US1). **Stop and validate** quickstart 1–4 on a copy.
3. At this point the library *can* be re-tagged, but Immich will not see the new tags until US2. Do not start the 14-hour full run before US2 is done.

### Recommended delivery

1. Setup + Foundational.
2. US1 and US2 together (both P1). Run US2's cleanup dry run against the live Immich early: it proves the deletion rules on the 6,816 real legacy tags before any re-tag.
3. US3: filter and web adapt to the new scheme.
4. US4: the web tag job on the shared run, plus the lock.
5. Polish, then T057 (the full library run).

### Notes

- Never run the model with `.to(dtype=...)`; see T021 and research R1.
- Nothing in these tasks writes into the Immich upload tree except sidecars and their short-lived `*.animetag-tmp` files.
- Commit after each task or logical group, now possible thanks to T001.
