# animetag

Anime image tagger for an Immich library. [PixAI Tagger v1.0] writes Immich-compatible XMP
sidecars (`image.jpg` → `image.jpg.xmp`) with `character/`, `parody/`, `tag/`, `meta/` and
`rating/` hierarchies. Tools here filter and export images by those tags, and keep Immich in step
with the sidecars.

[PixAI Tagger v1.0]: https://huggingface.co/pixai-labs/pixai-tagger-v1.0

## Usage

```bash
uv run animetag
```

That is the whole normal workflow. The command scans the library, tags every image that has no
sidecar yet (new uploads come first), and re-tags every image whose sidecar was written by
another model. That includes every old WD14 sidecar. When it is done, it asks Immich to re-read
the new sidecars and deletes the tags nothing uses any more.

The command is safe to interrupt and re-run. Sidecars are written one at a time and atomically,
and finished images are skipped the next time. Before anything is overwritten, the old sidecars
are backed up (see [Backups](#backups)).

The default library is `/Users/paul/docker/immich/library/upload`. Point it elsewhere with
`ANIMETAG_DIR=/some/path uv run animetag`, or per run with `--dir`.

### Speed

The command runs on the Mac's GPU in half precision at about 1.7–1.8 images/s on an M5. Re-tagging
the full ~88k-image library takes about 14 hours, and you can spread that over several runs.
Without the GPU it falls back to the CPU and warns you, at a fraction of the speed.
`uv run animetag --dry-run` shows what would be tagged and an estimate, without loading the
model.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir` | `$ANIMETAG_DIR`, else the path above | Library to tag |
| `--dry-run` | off | Counts and time estimate, then exit |
| `--limit N` | none | Stop after N images: a quick trial run |
| `--overwrite` | off | Also re-tag images already tagged by the current model |
| `--keep-common-tags` | off | Keep `1girl`, `solo`, `looking_at_viewer`, … |
| `--no-rating` | off | Skip `rating/*` tags |
| `--general-threshold` | 0.34 | `tag/*` |
| `--character-threshold` | 0.37 | `character/*` |
| `--parody-threshold` | 0.46 | `parody/*` (series / copyright) |
| `--meta-threshold` | 0.47 | `meta/*` (whitelisted tags only) |
| `--device` | `auto` | `mps` (Apple GPU), `cpu`, or `auto` |
| `--no-immich` | off | Don't sync to Immich afterwards; the images stay pending |

The thresholds are the model authors' per-category operating points. They give about 35–40
`tag/*` tags per image.

## The tags

| Prefix | What | Notes |
| --- | --- | --- |
| `character/` | Named characters | e.g. `character/hatsune_miku` |
| `parody/` | Series, games, franchises | e.g. `parody/blue_archive`, `parody/original` |
| `tag/` | Everything visible | common filler like `1girl`/`solo` dropped unless `--keep-common-tags` |
| `meta/` | A short whitelist only | AI-generated, official art, screenshots, photos, traditional media |
| `rating/` | Exactly one per image | `general`, `sensitive`, `questionable`, `explicit`, the same names as before |

- **Artist/style labels are not written.** The model predicts them from drawing style, not
  provenance.
- **A `/` inside a name is written as `∕`** (U+2215). For example, the series `fate/grand_order`
  is stored as `parody/fate∕grand_order`, because Immich splits tags on `/` into levels. When
  filtering, you can type either form.
- **Your own tags are kept.** A tag you add in Immich is written back into the sidecar by Immich,
  and it survives re-tagging as long as it isn't under one of the five prefixes above.
- Every sidecar records which model and revision wrote it. That record is how a run knows what is
  current.

### What changed from WD14

The new model uses a newer Danbooru vocabulary:

- Some old tags no longer exist. The most common is `bangs`: use `blunt_bangs`, `swept_bangs`,
  `parted_bangs` or `hair_between_eyes`. `black_footwear` became `black_shoes`.
- `parody/` is actually filled now; WD14 had no series category at all.
- Characters from newer games are recognised far more often.

The filter and the web UI warn when you use a tag the new model never writes, and suggest the
likely replacement.

## Immich

The sync needs an Immich API key. Create one in Immich (Account Settings → API Keys) on the admin
account, with these permissions:

`asset.read`, `asset.update`, `job.create`, `job.read`, `tag.read`, `tag.delete`

Put it in `~/.config/animetag/config.toml` and make the file readable only by you
(`chmod 600`). animetag warns if it isn't.

```toml
[immich]
url = "http://127.0.0.1:2283"
api_key = "..."
# Immich's path for the library, then this Mac's. Optional: guessed when left out.
path_map = "/data=/Users/paul/docker/immich/library"
```

The environment variables `ANIMETAG_IMMICH_URL`, `ANIMETAG_IMMICH_API_KEY` and
`ANIMETAG_IMMICH_PATH_MAP` override the file.

After a run, animetag does the following:

1. It asks Immich to re-read the sidecars it wrote (and to discover the ones for new uploads).
   Immich replaces each image's tags with the sidecar's.
2. It waits for Immich's queues to empty.
3. It deletes the tags nothing uses any more, such as `tag/bangs` once every image has been
   re-tagged, and the thousands of old `character|…` leftovers.

The cleanup is deliberately narrow:

- It only touches tags under `character`, `parody`, `tag`, `meta` and `rating`, plus their old
  `|` forms. It never touches any other tag, even an empty one.
- It never deletes a tag while anything below it is still in use.
- Each tag is checked empty again, right before it is deleted.

(Immich's own "tag cleanup" job is not used: it deletes every empty tag, yours included.)

If Immich is down or no key is set, tagging still completes, and the images are remembered as
pending. `animetag-immich` finishes the job:

```bash
uv run animetag-immich status              # connection, permissions, what is pending
uv run animetag-immich sync                # the pending images
uv run animetag-immich sync --all          # every image
uv run animetag-immich cleanup --dry-run   # show which tags would be deleted
uv run animetag-immich cleanup
```

## Backups

Before a run overwrites any sidecar, the originals go into a backup:
`~/Library/Application Support/animetag/<library id>/backups/`. A file already in an earlier
backup is not stored twice. For the whole WD14 library, the backup is roughly 20 MB (measured: about 240 bytes per sidecar, compressed).

```bash
uv run animetag-backup list
uv run animetag-backup restore 20260922T031415Z   # puts those sidecars back, byte for byte
```

A restore first backs up the sidecars it replaces, then syncs Immich. The restored WD14 sidecars
count as "not current", so the next `animetag` run would re-tag them again.

## Browsing and filtering in a browser — `animetag-web`

```bash
uv run animetag-web
```

This opens a local page (127.0.0.1:8788) with the same filtering as `animetag-filter` (below),
but you can see what you are selecting:

- **Tag autocomplete** with a count next to every tag, so you know how big a selection is
  before you make it.
- **Live match count and thumbnails.** The number and the grid update as you add tags.
- **Warnings under a filter** when a tag is a leftover from the old model or doesn't exist.
  Click a suggestion to swap it in.
- **Click any thumbnail** for the full image and its complete tag list. Clicking a tag there adds
  it to the current filter (shift-click excludes it).
- **A "Never include" box**: the `--exclude` veto, applied across every filter at once.
- **The output controls** (folder, max images, size limit, ordering) as fields, with a progress
  bar and a cancel button during the export.
- **Tag** in the header shows how many images are new or still need re-tagging, and runs the
  same tagging as `animetag`: backup, progress, time left, cancel, then the Immich sync.
  Reloading the page reattaches to a run in progress. A run from the terminal and one from the
  page can never overlap: whichever starts second is refused.
- **Sync to Immich (N)** appears when images are pending. **Clean up Immich tags** shows exactly
  which tags would be deleted before it lets you delete them.
- **A banner** while images are still not re-tagged, since filter results are incomplete until
  they are.

The whole library is indexed into memory once at startup, after which a filter is answered by
set arithmetic in around a millisecond. A tagging run started from the page rebuilds the index
when it finishes. **Rescan** does the same after tagging from the terminal.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir` | `$ANIMETAG_DIR`, else the library | Directory to index |
| `--port` | `8788` | Port to serve on. Left at the default, it moves to the next free port if 8788 is taken; given explicitly, it fails rather than starting somewhere you aren't looking |
| `--host` | `127.0.0.1` | Interface to bind: loopback, i.e. this machine only |
| `--no-browser` | off | Do not open a browser window |

The server has no authentication, so leave `--host` alone unless you have a reason to expose it.

## Filtering tagged images — `animetag-filter`

This program goes the other way: it reads the sidecars, picks the images whose tags you want,
and copies them into a folder, shrinking anything over 1 MB on the way.

```bash
uv run animetag-filter --filter "blue_hair,long_hair" --max-images 200 --out ./picked
```

### Filters

A `--filter` (`-f`) is a comma-separated group of tags that must **all** be present:

```bash
-f "blue_hair,long_hair,school_uniform"     # all three
```

Repeat `--filter` for up to **10** groups. An image matching **any** group is taken; with
`--match all`, every group must match.

```bash
-f "character/hatsune_miku" -f "parody/fate/grand_order"    # either
```

Inside a group:

- `-tag` or `!tag` excludes, but **only from that group**: `-f "cat_ears,-rating/explicit"`.
- `*` and `?` are globs: `-f "blue_*"`, `-f "character/*"`.
- A bare tag matches the name under any prefix, so `blue_hair` finds `tag/blue_hair` and
  `fate/grand_order` finds `parody/fate∕grand_order`. Start with a prefix (`rating/general`,
  `meta/*`) when you want to pin the category.
- Case, spaces and dashes are all folded, so `Blue Hair` == `blue_hair`.

Since a leading `-` looks like an option to the shell, write an exclusion-only filter as
`-f '!rating/explicit'` or `--filter=-rating/explicit`.

When a tag in a filter is one the new model never writes, the command says so before scanning
and suggests replacements. After scanning, it reports how many images still carry the tag. If
images are not re-tagged yet, it notes that the results may be incomplete.

### Excluding everywhere — `--exclude` / `-x`

A `-tag` inside a `--filter` only rules that tag out of *that* group, so under `--match any` an
image can still arrive through another group. `--exclude` vetoes the combined result:

```bash
animetag-filter -f "cat_ears" -f "character/hatsune_miku" -x "rating/explicit" -o ./picked
```

`--exclude` takes the same comma-separated lists and globs as `--filter`, is repeatable, and
works with no `--filter` at all: `-x "rating/explicit"` alone means "everything but those".

Not sure what to filter on? `uv run animetag-filter --list-tags 40` prints the most common tags
in the library. Add filters to see the common tags *within* a selection.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--filter` / `-f` | none | Tag group; repeat up to 10 times. No filters = every tagged image |
| `--exclude` / `-x` | none | Tags no image may carry, across all filters. Repeatable |
| `--match` | `any` | Combine the groups with OR (`any`) or AND (`all`) |
| `--out` / `-o` | — | Output folder, created if missing |
| `--max-images` / `-n` | no limit | Stop after N images |
| `--max-size` | `1MB` | Compress anything larger; accepts `900k`, `1.5MiB`, a byte count |
| `--no-compress` | off | Copy every image untouched, however large |
| `--dir` | `$ANIMETAG_DIR`, else the library | Directory to scan |
| `--order` | `scan` | Which matches `--max-images` keeps: `scan`, `random`, `newest`, `oldest` |
| `--seed` | none | Reproducible `--order random` |
| `--copy-sidecar` | off | Also copy each `.xmp` next to the output image |
| `--list-tags [N]` | off | Print the N most common tags among the matches, then exit |
| `--dry-run` | off | Report how many images match, then exit without copying |

### Compression

- An image already under `--max-size` is copied byte for byte.
- A larger one is re-encoded as JPEG: quality drops from 92 toward 50 first, and only then is the
  image scaled down (never below 640 px on the long side).
- Transparency is flattened onto white, since JPEG has no alpha channel.
- If even that misses the limit, the smallest attempt is still written, and the run reports how
  many files stayed over.

Output names are flattened into one folder, so `a/img.jpg` and `b/img.jpg` land as `img.jpg` and
`img-1.jpg`.

## Moving from wd14

The `wd14`, `wd14-filter` and `wd14-web` commands are gone. They are now `animetag`,
`animetag-filter` and `animetag-web`. `WD14_DIR` is no longer read (you get a one-line notice if
it is still set); use `ANIMETAG_DIR`.

Once you are happy with the new tags, you can delete the old WD14 model (747 MB):

```bash
rm -rf ~/.cache/huggingface/hub/models--SmilingWolf--wd-v1-4-convnextv2-tagger-v2
```

## Layout

Each program is a core module with no CLI concerns, plus a thin entry point:

| | Core | Entry point |
| --- | --- | --- |
| `animetag` | `run.py` (one tagging run), `tagger.py` (the model), `sidecar.py` (XMP read/write), `library.py` (scan, status, lock) | `cli.py` |
| `animetag-immich` | `immich.py` (API client, sync, tag cleanup) | `immich_cli.py` |
| `animetag-backup` | `backup.py` | `backup_cli.py` |
| `animetag-filter` | `filterer.py` (tag matching, copy/compress) | `filter_cli.py` |
| `animetag-web` | `web.py` (HTTP routes, jobs, thumbnails) | `web_cli.py` |

Other modules:

- `vocab.py`: the tag scheme (prefixes, thresholds, whitelist, escaping, matching rules,
  suggestions).
- `config.py`: where things live.
- `index.py`: the in-memory index the web UI filters against.
- `web/`: the page it serves.

The web UI never reimplements filtering. It parses its rows into the same `TagFilter` objects the
CLI builds, and both resolve a pattern through the one `filterer.pattern_matches`, so the two
cannot drift apart.

The model code is loaded from the pinned upstream revision rather than copied into this project.
The PixAI repository declares no license.

## Development

```bash
uv run pytest                              # unit tests; model tests run when the model is cached
uv run python scripts/bench20.py           # 20-image speed and quality check (read-only)
uv run python scripts/verify_immich.py     # Immich tags == sidecar tags, on a random sample
```
