# Contract: Command-line interface

Five console scripts are installed by `pyproject.toml`. **No `wd14*` scripts remain** (FR-028).

Common to all commands:
- `--dir PATH` overrides the library root. Resolution order: `--dir`, then `$ANIMETAG_DIR`, then `/Users/paul/docker/immich/library/upload`.
- If `$WD14_DIR` is set and `$ANIMETAG_DIR` is not, the command prints once to stderr: `note: WD14_DIR is no longer read; set ANIMETAG_DIR instead` (FR-029).
- Exit codes: `0` success; `1` completed, but every item failed; `2` usage error; `3` another tagging run holds the lock; `130` interrupted.

---

## `animetag` — tag and re-tag

```text
animetag [--dir PATH] [--dry-run] [--limit N] [--overwrite]
         [--general-threshold F] [--character-threshold F]
         [--parody-threshold F] [--meta-threshold F]
         [--keep-common-tags] [--no-rating]
         [--device {auto,mps,cpu}] [--no-immich]
```

| Flag | Default | Behaviour |
|---|---|---|
| *(none)* | | Tag every `NEW`, `LEGACY` and `OUTDATED` image, in that order (FR-003, FR-004) |
| `--dry-run` | off | Print the counts per status and the ETA at the measured rate, then exit without loading the model |
| `--limit N` | none | Stop after N images, taken from the head of the queue |
| `--overwrite` | off | Also re-tag `CURRENT` images |
| `--general-threshold` | 0.34 | |
| `--character-threshold` | 0.37 | |
| `--parody-threshold` | 0.46 | Copyright category |
| `--meta-threshold` | 0.47 | Applied to the whitelist only |
| `--keep-common-tags` | off | Keep `1girl`, `solo`, … |
| `--no-rating` | off | Omit `rating/*` |
| `--device` | auto | auto means mps if available, else cpu, with a warning |
| `--no-immich` | off | Skip the post-run Immich sync; written images stay in the pending list |

Removed flag: `--rating-threshold` (rating is argmax). If it is passed, argparse's standard "unrecognized arguments" error is shown.

**Stdout (normal run):**

```text
Scanning /Users/paul/docker/immich/library/upload ...
87969 images: 12 new, 87957 to re-tag (87957 legacy, 0 outdated), 0 current.
Backing up 87957 sidecars -> ~/Library/Application Support/animetag/3fa1…/backups/20260922T031415Z (38.7 MB)
Loading pixai-labs/pixai-tagger-v1.0 @ f33cfdb on mps (fp16) ...
Tagging: 100%|████| 87969/87969 [13:34:10<00:00, 1.80img/s]
Done. written=87969 failed=0 total=87969
Immich: refreshed 87957, discovered 12, removed 7054 empty tags.
```

- On `--device cpu` or an auto fallback, stderr gets: `warning: GPU unavailable, running on CPU — expect ≈ N h for M images`.
- On a sync failure, the last line reads `Immich sync pending (N images): <reason>. Run: animetag-immich sync`, and the exit code is unaffected (FR-021).

---

## `animetag-immich` — Immich synchronisation

```text
animetag-immich sync    [--dir PATH] [--all] [--no-cleanup] [--dry-run]
animetag-immich cleanup [--dir PATH] [--dry-run]
animetag-immich status  [--dir PATH]
```

| Subcommand | Behaviour |
|---|---|
| `sync` | Process `pending-sync.jsonl`: refresh the known assets, discover new sidecars, wait for Immich's queues to drain, then run `cleanup` unless `--no-cleanup` |
| `sync --all` | The same for every image in the library, ignoring the pending list (FR-018) |
| `cleanup` | Delete the empty managed tags (FR-019); `--dry-run` prints `would delete N tags`, 20 examples and the prefix counts (FR-020) |
| `status` | Reachability, Immich version, key permissions check, pending count, managed empty-tag count |

Without a configured key, every subcommand exits 2 with the message `Immich API key not configured: set [immich].api_key in ~/.config/animetag/config.toml or ANIMETAG_IMMICH_API_KEY`.

---

## `animetag-backup` — sidecar backups

```text
animetag-backup list    [--dir PATH]
animetag-backup restore ID [--dir PATH] [--dry-run] [--no-immich]
```

| Subcommand | Behaviour |
|---|---|
| `list` | One line per backup: id, file count, compressed size and reason |
| `restore ID` | Take a safety backup, write the archived sidecars back atomically, add them to the pending sync list, then run `animetag-immich sync` unless `--no-immich` |

---

## `animetag-filter` — export

Same flags as today's `wd14-filter` (`--filter/-f`, `--exclude/-x`, `--match`, `--out/-o`, `--max-images/-n`, `--max-size`, `--no-compress`, `--order`, `--seed`, `--copy-sidecar`, `--list-tags`, `--dry-run`, `--dir`).

**Changed behaviour:**
- **Pattern rules** follow [data-model.md → Tag](../data-model.md#tag). Examples:
  - `-f "parody/fate/grand_order"` and `-f "fate/grand_order"` both work.
  - `-f "meta/ai-generated"` works.
  - `rating/*` names are unchanged.
- **Before scanning**, each plain pattern that is not in the new model's vocabulary (research R13) produces a warning on stderr with suggestions from that vocabulary. **After the scan**, the same pattern gets a second line with the number of scanned images that still carry it (a legacy tag), or `does not exist` when none do:

  ```text
  warning: 'bangs' is not in the new model's vocabulary. similar: blunt_bangs, swept_bangs, hair_between_eyes, parted_bangs, asymmetrical_bangs
  ...scan...
  warning: 'bangs' is a legacy tag — 42 images still carry it until re-tagged.
  warning: 'blue_hiar' does not exist in this library.
  ```

  When `--order scan` stops early at `--max-images`, the count is reported as `at least N`. Warnings never change the exit code or the selection.
- **Migration notice:** when the library still has `LEGACY`/`OUTDATED` images, the command prints `note: N images are not re-tagged yet; results may be incomplete` (FR-024).
- `--copy-sidecar` copies the sidecar byte for byte, including provenance (FR-025).

---

## `animetag-web` — browser UI

Same flags as today's `wd14-web` (`--dir`, `--port`, `--host`, `--no-browser`). The HTTP contract is in [web-api.md](./web-api.md).
