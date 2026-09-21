# wd14

WD14 anime tagger that writes Immich-compatible XMP sidecars (`image.jpg` → `image.jpg.xmp`)
with `character/`, `parody/`, `tag/` and `rating/` hierarchies.

## Usage

```bash
uv run wd14
```

That is the whole normal workflow: it scans the library, finds every image that has no
`.xmp` sidecar yet, and tags those. Already-tagged images are never touched, so the command
is safe to re-run and an interrupted run resumes where it stopped.

The default library is `/Users/paul/docker/immich/library/upload`. Point it elsewhere with
`WD14_DIR=/some/path uv run wd14`, or per-run with `--dir`.

## Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir` | `$WD14_DIR`, else the path above | Directory to scan |
| `--dry-run` | off | Report how many new images there are, then exit |
| `--limit N` | none | Stop after N images — a quick trial run |
| `--overwrite` | off | Re-tag images that already have a sidecar |
| `--keep-common-tags` | off | Keep `1girl`, `solo`, `looking_at_viewer`, … |
| `--no-rating` | off | Skip `rating/*` tags |
| `--general-threshold` | 0.40 | |
| `--character-threshold` | 0.75 | |
| `--parody-threshold` | 0.60 | |
| `--rating-threshold` | 0.50 | |

The model (`SmilingWolf/wd-v1-4-convnextv2-tagger-v2`) is pulled from the Hugging Face Hub
on first use and cached under `~/.cache/huggingface`.

## Browsing and filtering in a browser — `wd14-web`

```bash
uv run wd14-web
```

Opens a local page (127.0.0.1:8788) with the same filtering as `wd14-filter` (below), but
you can see what you are selecting:

- **tag autocomplete** with a count next to every tag, so you know how big a selection is
  before you make it
- **live match count and thumbnails** — the number and the grid update as you add tags
- **click any thumbnail** for the full image and its complete tag list; clicking a tag
  there adds it to the current filter (shift-click excludes it), which is the fastest way
  to find the tag you actually meant
- **a "Never include" box** — the `--exclude` veto, applied across every filter at once
- **the output controls** — folder, max images, size limit, ordering — as fields, with a
  progress bar and a cancel button during the export
- **Tag new** in the header — shows how many images have no sidecar yet and runs the
  tagger over them, the same selection and default thresholds as `uv run wd14`, with
  progress, time left and a cancel button. Reloading the page reattaches to a run in
  progress. Non-default thresholds, `--overwrite` and `--keep-common-tags` still need the CLI

The whole library is indexed into memory once at startup (about 17 s and 360 MB for 100k
images), after which a filter is answered by set arithmetic in around a millisecond rather
than a 16-second disk scan. A tagging run started from the page rebuilds the index when it
finishes; **Rescan** does the same after tagging from the terminal.

Only two buttons write anything. **Tag new** writes sidecars next to images that have none
and never touches an existing one. **Export** writes only into its output folder, and does
exactly what the CLI does — the two share the matching, compression and naming code, so the
same filters give the same files.

| Flag | Default | Purpose |
| --- | --- | --- |
| `--dir` | `$WD14_DIR`, else the library | Directory to index |
| `--port` | `8788` | Port to serve on. Left to the default it moves to the next free port if 8788 is taken; given explicitly it fails rather than starting somewhere you aren't looking |
| `--host` | `127.0.0.1` | Interface to bind — loopback, i.e. this machine only |
| `--no-browser` | off | Do not open a browser window |

The server has no authentication, so leave `--host` alone unless you have a reason to
expose it.

## Filtering tagged images — `wd14-filter`

A separate program that goes the other way: it reads the sidecars, picks the images whose
tags you want, and copies them into a folder — shrinking anything over 1 MB on the way.

```bash
uv run wd14-filter --filter "blue_hair,long_hair" --max-images 200 --out ./picked
```

### Filters

A `--filter` (`-f`) is a comma-separated group of tags that must **all** be present:

```bash
-f "blue_hair,long_hair,school_uniform"     # all three
```

Repeat `--filter` for up to **10** groups. An image matching **any** group is taken;
`--match all` requires every group instead.

```bash
-f "character/hatsune_miku" -f "parody/k-on!"    # either character
```

Inside a group:

- `-tag` or `!tag` excludes, but **only from that group** — `-f "cat_ears,-rating/explicit"`
- `*` and `?` glob — `-f "blue_*"`, `-f "character/*"`
- a bare tag matches the leaf, so `blue_hair` finds `tag/blue_hair`; write the prefix
  (`rating/general`) when you want to pin the hierarchy
- case, spaces and dashes are all folded, so `Blue Hair` == `blue_hair`

Since a leading `-` looks like an option to the shell, write an exclusion-only filter as
`-f '!rating/explicit'` or `--filter=-rating/explicit`.

### Excluding everywhere — `--exclude` / `-x`

A `-tag` inside a `--filter` only rules that tag out of *that* group, so under `--match any`
an image can still arrive through another group. `--exclude` vetoes the combined result:

```bash
# without -x, explicit Miku images still come in through the second filter
wd14-filter -f "cat_ears,-rating/explicit" -f "character/hatsune_miku" -o ./picked   # 6857

# -x applies to every group at once
wd14-filter -f "cat_ears" -f "character/hatsune_miku" -x "rating/explicit" -o ./picked  # 6815
```

It takes the same comma-separated lists and globs as `--filter`, is repeatable, and works
with no `--filter` at all — `-x "rating/explicit"` alone means "everything but those". No
leading `-` is needed here; the flag already means exclude.

Not sure what to filter on? `uv run wd14-filter --list-tags 40` prints the most common
tags in the library (add filters to see the common tags *within* a selection).

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
| `--dir` | `$WD14_DIR`, else the library | Directory to scan |
| `--order` | `scan` | Which matches `--max-images` keeps: `scan`, `random`, `newest`, `oldest` |
| `--seed` | none | Reproducible `--order random` |
| `--copy-sidecar` | off | Also copy each `.xmp` next to the output image |
| `--list-tags [N]` | off | Print the N most common tags among the matches, then exit |
| `--dry-run` | off | Report how many images match, then exit without copying |

Only `--order scan` can stop the scan early at `--max-images`; the other orders have to
see every match before they can rank them, so they always walk the whole library.

### Compression

An image already under `--max-size` is copied byte-for-byte. A larger one is re-encoded as
JPEG: quality drops from 92 toward 50 first, and only then does the image get scaled down
(never below 640 px on the long side). Transparency is flattened onto white, since JPEG has
no alpha channel. If even that misses the limit the smallest attempt is still written, and
the run reports how many files stayed over.

Output names are flattened into one folder, so `a/img.jpg` and `b/img.jpg` land as
`img.jpg` and `img-1.jpg`.

## Layout

Each program is a core module with no CLI concerns plus a thin entry point:

| | Core | Entry point |
| --- | --- | --- |
| `wd14` | `tagger.py` — model, inference, XMP output | `cli.py` |
| `wd14-filter` | `filterer.py` — sidecar parsing, tag matching, copy/compress | `filter_cli.py` |
| `wd14-web` | `web.py` — HTTP routes, tagging and export jobs, thumbnails | `web_cli.py` |

`index.py` holds the in-memory inverted index the web UI filters against, and `web/` the
page it serves. The web UI never reimplements filtering: it parses its rows into the same
`TagFilter` objects the CLI builds, and both resolve a tag pattern through the one
`filterer.pattern_matches`, so the two cannot drift apart.
