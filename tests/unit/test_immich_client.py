import logging

import pytest

from animetag import immich
from animetag.config import ImmichSettings

from .fake_immich import API_KEY, FakeImmich


@pytest.fixture
def fake():
    server = FakeImmich().start()
    yield server
    server.stop()


def client_for(fake, key=API_KEY):
    return immich.ImmichClient(ImmichSettings(url=fake.url, api_key=key, path_map=None), sleep=lambda s: None)


def test_every_request_carries_the_key(fake):
    client = client_for(fake)
    client.list_tags()
    client.queue_counts()
    assert all(key == API_KEY for method, path, body, key in fake.requests)


def test_iter_assets_pages_with_cursor(fake):
    for i in range(2500):
        fake.add_asset(f"/data/upload/u/{i}.jpg")
    assets = list(client_for(fake).iter_assets())
    assert len(assets) == 2500
    assert assets[0] == (fake.assets[0]["id"], "/data/upload/u/0.jpg")
    assert fake.page_size_seen == [1000, 1000, 1000]
    bodies = [b for m, p, b, k in fake.requests if p == "/api/search/metadata"]
    assert "filter" in bodies[0] and "cursor" not in bodies[0] and bodies[1]["cursor"] == "1000"


def test_iter_assets_falls_back_to_legacy_paging(fake):
    fake.legacy_search = True
    for i in range(1200):
        fake.add_asset(f"/data/upload/u/{i}.jpg")
    assert len(list(client_for(fake).iter_assets())) == 1200
    legacy = [b for m, p, b, k in fake.requests if p == "/api/search/metadata" and "page" in b]
    assert [b["page"] for b in legacy] == [1, 2]
    assert all(b["withDeleted"] is True for b in legacy)


def test_tag_has_assets_uses_descendants(fake):
    fake.add_asset("/data/upload/u/1.jpg", ["parody/fate∕grand_order"])
    client = client_for(fake)
    tags = {t["value"]: t["id"] for t in client.list_tags()}
    assert client.tag_has_assets(tags["parody"])
    assert client.tag_has_assets(tags["parody/fate∕grand_order"])
    empty = fake.add_tag("tag/bangs")
    assert not client.tag_has_assets(empty)


def test_asset_jobs_are_batched(fake):
    ids = [f"id-{i}" for i in range(1200)]
    client_for(fake).run_asset_job(ids)
    assert [len(job["assetIds"]) for job in fake.asset_jobs] == [500, 500, 200]
    assert all(job["name"] == "refresh-metadata" for job in fake.asset_jobs)


def test_retries_5xx_then_gives_up(fake):
    client = client_for(fake)
    fake.fail_next = 2
    assert client.list_tags() == []                  # two 500s, then success
    fake.fail_next = 10
    with pytest.raises(immich.ImmichUnavailable):
        client.list_tags()


def test_unreachable_server():
    client = immich.ImmichClient(ImmichSettings(url="http://127.0.0.1:9", api_key=API_KEY, path_map=None),
                                 sleep=lambda s: None)
    with pytest.raises(immich.ImmichUnavailable) as info:
        client.list_tags()
    assert API_KEY not in str(info.value)


def test_auth_error_names_permission_and_hides_key(fake, caplog):
    client = client_for(fake, key="wrong-secret-key")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(immich.ImmichAuthError) as info:
            client.list_tags()
    message = str(info.value)
    assert "tag.read" in message and "/api/tags" in message
    assert "wrong-secret-key" not in message
    assert "wrong-secret-key" not in caplog.text


def test_discovery_404_reports_unavailable(fake):
    client = client_for(fake)
    assert client.start_sidecar_discovery() is True
    assert fake.sidecar_jobs == [{"command": "start", "force": False}]
    fake.discovery_404 = True
    assert client.start_sidecar_discovery() is False


def test_wait_idle_polls_until_queues_drain(fake):
    fake.busy_polls = 3
    seen = []
    assert client_for(fake).wait_idle(poll=0, on_progress=seen.append) is True
    assert len(seen) >= 3
    assert seen[-1] == 0


def test_wait_idle_times_out(fake):
    fake.busy_polls = 10**6
    assert client_for(fake).wait_idle(timeout=0.05, poll=0.01) is False


def test_delete_tag_and_version(fake):
    tag_id = fake.add_tag("tag/bangs")
    client = client_for(fake)
    client.delete_tag(tag_id)
    assert tag_id in fake.deleted
    client.delete_tag(tag_id)                        # already gone: not an error
    assert client.version() == (3, 2, 2)
