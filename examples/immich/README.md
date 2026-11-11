# Immich companion files

| File | Use |
| --- | --- |
| [`../config.toml`](../config.toml) | immichtag's config: which folder to tag, Immich URL, API key, path map. Copy to `~/.config/immichtag/config.toml`. |
| [`docker-compose.override.yml`](docker-compose.override.yml) | Mounts an existing anime folder into Immich so it can be added as an External Library (setup B). |

Setup A, tagging Immich's own uploads, needs no change to Immich at all: point `[library] dir`
at the host folder behind `UPLOAD_LOCATION`. See the main README, section "Immich setup".
