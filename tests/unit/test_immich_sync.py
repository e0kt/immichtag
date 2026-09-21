import shutil
from pathlib import Path

import pytest

from animetag import immich, library, sidecar
from animetag.config import ImmichSettings

from .fake_immich import API_KEY, FakeImmich

PROV = sidecar.Provenance("pixai-labs/pixai-tagger-v1.0", "f" * 40, "v1", "2026-01-01T00:00:00Z")


@pytest.fixture
def fake():
    server = FakeImmich().start()
    yield server
    server.stop()


@pytest.fixture
def lib(tmp_path, fixtures_dir):
    """Local layout <tmp>/library/upload/u/aa/{1,2,3}.jpg; Immich sees /data/upload/..."""
    root = tmp_path / "library" / "upload"
    folder = root / "u" / "aa"
    folder.mkdir(parents=True)
    for name in ("1.jpg", "2.jpg", "3.jpg"):
        shutil.copy(fixtures_dir / "images" / "a.jpg", folder / name)
        sidecar.sidecar_path(folder / name).write_bytes(
            sidecar.build_xmp(["character/hatsune_miku", "parody/vocaloid", "tag/long_hair", "rating/general"], PROV))
    return root


def settings(fake, path_map=None):
    return ImmichSettings(url=fake.url, api_key=API_KEY, path_map=path_map)


def test_path_map_parse_and_round_trip(tmp_path):
    pm = immich.PathMap.parse(f"/data={tmp_path}")
    local = pm.to_local("/data/upload/u/aa/1.jpg")
    assert local == tmp_path / "upload" / "u" / "aa" / "1.jpg"
    assert pm.to_remote(local) == "/data/upload/u/aa/1.jpg"
    assert pm.to_local("/elsewhere/x.jpg") is None
    with pytest.raises(ValueError):
        immich.PathMap.parse("no-equals-sign")


def test_path_map_autodetect(lib):
    pm = immich.PathMap.autodetect(["/nope/zzz.jpg", "/data/upload/u/aa/2.jpg"], lib)
    assert pm is not None
    assert pm.to_local("/data/upload/u/aa/3.jpg") == lib / "u" / "aa" / "3.jpg"
    assert immich.PathMap.autodetect(["/nope/zzz.jpg"], lib) is None


def test_sync_pending_refreshes_known_and_discovers_new(fake, lib, tmp_path):
    ids = {n: fake.add_asset(f"/data/upload/u/aa/{n}", ["tag/bangs"]) for n in ("1.jpg", "2.jpg", "3.jpg")}
    fake.add_tag("character|abe_nana")
    state = tmp_path / "state"
    pending = library.PendingSync(state, lib)
    pending.append(lib / "u/aa/1.jpg", had_sidecar=True)
    pending.append(lib / "u/aa/2.jpg", had_sidecar=False)

    # Immich re-reads the sidecars: simulate by moving the assets onto their new tags
    for asset in fake.assets:
        asset["tags"] = {fake.add_tag(v) for v in ("character/hatsune_miku", "parody/vocaloid",
                                                   "tag/long_hair", "rating/general")}

    result = immich.sync(lib, settings(fake), state, poll=0)
    assert result.ok, result.reason
    assert result.refreshed == 2 and result.discovered == 1
    assert fake.asset_jobs == [{"assetIds": [ids["1.jpg"], ids["2.jpg"]], "name": "refresh-metadata"}]
    assert fake.sidecar_jobs == [{"command": "start", "force": False}]
    # cleanup ran: legacy and emptied managed tags are gone, used ones stay
    values = {t["value"] for t in fake.tags.values()}
    assert "tag/bangs" not in values and "character|abe_nana" not in values
    assert {"tag/long_hair", "parody/vocaloid", "character/hatsune_miku"} <= values
    assert result.deleted_tags == 2
    assert pending.entries() == []


def test_sync_without_new_images_skips_discovery(fake, lib, tmp_path):
    fake.add_asset("/data/upload/u/aa/1.jpg")
    state = tmp_path / "state"
    library.PendingSync(state, lib).append(lib / "u/aa/1.jpg", had_sidecar=True)
    result = immich.sync(lib, settings(fake, path_map=f"/data={lib.parent}"), state, cleanup=False, poll=0)
    assert result.ok and result.discovered == 0
    assert fake.sidecar_jobs == []


def test_sync_all_covers_every_image(fake, lib, tmp_path):
    for n in ("1.jpg", "2.jpg", "3.jpg"):
        fake.add_asset(f"/data/upload/u/aa/{n}")
    result = immich.sync(lib, settings(fake), tmp_path / "state", all=True, cleanup=False, poll=0)
    assert result.ok and result.refreshed == 3
    assert fake.sidecar_jobs == [{"command": "start", "force": False}]


def test_sync_server_down_keeps_pending(lib, tmp_path):
    state = tmp_path / "state"
    pending = library.PendingSync(state, lib)
    pending.append(lib / "u/aa/1.jpg", had_sidecar=True)
    down = ImmichSettings(url="http://127.0.0.1:9", api_key=API_KEY, path_map=None)
    result = immich.sync(lib, down, state, poll=0, sleep=lambda s: None)
    assert not result.ok
    assert "not reachable" in result.reason
    assert result.pending == 1
    assert len(pending.entries()) == 1


def test_sync_discovery_404_still_refreshes(fake, lib, tmp_path):
    fake.discovery_404 = True
    fake.add_asset("/data/upload/u/aa/1.jpg")
    state = tmp_path / "state"
    library.PendingSync(state, lib).append(lib / "u/aa/1.jpg", had_sidecar=False)
    result = immich.sync(lib, settings(fake), state, cleanup=False, poll=0)
    assert result.ok
    assert "Discover" in result.reason
    assert result.refreshed == 1 and result.discovered == 0


def test_sync_dry_run_writes_nothing(fake, lib, tmp_path):
    fake.add_asset("/data/upload/u/aa/1.jpg", ["tag/bangs"])
    state = tmp_path / "state"
    library.PendingSync(state, lib).append(lib / "u/aa/1.jpg", had_sidecar=False)
    result = immich.sync(lib, settings(fake), state, dry_run=True, poll=0)
    assert result.ok
    assert fake.asset_jobs == [] and fake.sidecar_jobs == [] and fake.deleted == []
    assert result.refreshed == 1
    assert len(library.PendingSync(state, lib).entries()) == 1


def test_unmatched_images_stay_pending(fake, lib, tmp_path):
    fake.add_asset("/data/upload/u/aa/1.jpg")          # 2.jpg is not an Immich asset
    state = tmp_path / "state"
    pending = library.PendingSync(state, lib)
    pending.append(lib / "u/aa/1.jpg", had_sidecar=True)
    pending.append(lib / "u/aa/2.jpg", had_sidecar=True)
    result = immich.sync(lib, settings(fake), state, cleanup=False, poll=0)
    assert result.ok and result.unmatched == 1
    assert [e["path"] for e in pending.entries()] == [str(Path("u/aa/2.jpg"))]


def test_managed_in_use_includes_ancestors(lib):
    used = immich.managed_in_use(lib)
    assert {"character", "character/hatsune_miku", "parody", "parody/vocaloid", "tag", "tag/long_hair",
            "rating", "rating/general"} == used
