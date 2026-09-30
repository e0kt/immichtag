"""Client calls for deleting: finding assets by path, the trash, and trashing."""

from pathlib import Path

import pytest

from immichtag import immich
from immichtag.config import ImmichSettings

from .fake_immich import API_KEY, FakeImmich


@pytest.fixture
def fake():
    server = FakeImmich().start()
    yield server
    server.stop()


def client_for(fake, path_map=None):
    return immich.ImmichClient(ImmichSettings(url=fake.url, api_key=API_KEY, path_map=path_map),
                               sleep=lambda s: None)


def searches(fake):
    return [b for m, p, b, k in fake.requests if p == "/api/search/metadata"]


def test_features(fake):
    fake.features = {"trash": False}
    assert client_for(fake).features()["trash"] is False


def test_assets_by_path_in_chunks(fake):
    ids = [fake.add_asset(f"/data/u/{i}.jpg") for i in range(1200)]
    fake.trashed.add(ids[5])
    wanted = [f"/data/u/{i}.jpg" for i in range(1200)] + ["/data/u/missing.jpg"]
    found = client_for(fake).assets_by_path(wanted)
    assert len(found) == 1200
    assert found["/data/u/0.jpg"] == (ids[0], False)
    assert found["/data/u/5.jpg"] == (ids[5], True)
    assert len(searches(fake)) == 3
    assert all(len(b["filter"]["originalPath"]["in"]) <= 500 for b in searches(fake))


def test_assets_by_path_on_a_legacy_server(fake):
    fake.legacy_search = True
    asset = fake.add_asset("/data/u/a.jpg")
    fake.add_asset("/data/u/b.jpg")
    assert client_for(fake).assets_by_path(["/data/u/a.jpg"]) == {"/data/u/a.jpg": (asset, False)}


def test_iter_trashed_yields_only_the_trash(fake):
    ids = [fake.add_asset(f"/data/u/{i}.jpg") for i in range(4)]
    fake.trashed.update({ids[1], ids[3]})
    assert sorted(client_for(fake).iter_trashed()) == sorted([(ids[1], "/data/u/1.jpg"), (ids[3], "/data/u/3.jpg")])


def test_trash_batches_and_never_forces(fake):
    ids = [fake.add_asset(f"/data/u/{i}.jpg") for i in range(700)]
    client_for(fake).trash(ids)
    assert [len(c["ids"]) for c in fake.trash_calls] == [500, 200]
    assert all(c["force"] is False for c in fake.trash_calls)
    assert fake.trashed == set(ids)


def test_one_bad_id_fails_the_batch_but_not_the_others_one_by_one(fake):
    ids = [fake.add_asset(f"/data/u/{i}.jpg") for i in range(3)]
    fake.deny_ids.add(ids[1])
    client = client_for(fake)
    with pytest.raises(immich.ImmichError):
        client.trash(ids)
    assert fake.trashed == set()
    outcome = client.trash_each(ids)
    assert outcome[ids[0]] is None and outcome[ids[2]] is None
    assert "HTTP 400" in outcome[ids[1]]
    assert fake.trashed == {ids[0], ids[2]}


def test_missing_permission_names_asset_delete(fake):
    fake.deny_delete_permission = True
    asset = fake.add_asset("/data/u/a.jpg")
    with pytest.raises(immich.ImmichAuthError, match="asset.delete"):
        client_for(fake).trash([asset])
    with pytest.raises(immich.ImmichAuthError):
        client_for(fake).trash_each([asset])


def test_resolve_path_map(fake, tmp_path):
    root = tmp_path / "upload"
    (root / "u").mkdir(parents=True)
    (root / "u" / "a.jpg").write_bytes(b"x")
    fake.add_asset("/data/upload/u/a.jpg")
    detected = immich.resolve_path_map(client_for(fake),
                                       ImmichSettings(url=fake.url, api_key=API_KEY, path_map=None), root)
    assert detected == immich.PathMap("/data/upload", root)
    configured = immich.resolve_path_map(client_for(fake),
                                         ImmichSettings(url=fake.url, api_key=API_KEY, path_map="/x=/y"), root)
    assert configured == immich.PathMap("/x", Path("/y"))
