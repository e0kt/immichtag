import pytest

from animetag import immich
from animetag.config import ImmichSettings

from .fake_immich import API_KEY, FakeImmich


def tag(tag_id, value, parent=None):
    return {"id": tag_id, "value": value, "parentId": parent}


def test_plan_selects_only_managed_unused_tags():
    tags = [
        tag("1", "tag"), tag("2", "tag/bangs", "1"), tag("3", "tag/long_hair", "1"),
        tag("4", "character|abe_nana"), tag("5", "tag|/_"),
        tag("6", "my_album"), tag("7", "my_album/trip", "6"), tag("8", "vacation"),
        tag("9", "parody"), tag("10", "parody/fate∕grand_order", "9"),
        tag("11", "rating"), tag("12", "rating/questionable", "11"),
        tag("13", "meta"), tag("14", "meta/ai-generated", "13"),
    ]
    in_use = {"tag", "tag/long_hair", "parody", "parody/fate∕grand_order", "rating", "rating/general"}
    plan = immich.plan_cleanup(tags, in_use)
    values = [t["value"] for t in plan]
    assert set(values) == {"tag/bangs", "character|abe_nana", "tag|/_", "rating/questionable",
                           "meta", "meta/ai-generated"}
    for keep in ("my_album", "my_album/trip", "vacation", "tag", "parody", "rating"):
        assert keep not in values
    # deepest first: a child always comes before its parent
    assert values.index("meta/ai-generated") < values.index("meta")


def test_parent_of_kept_tag_is_kept_even_if_not_listed_in_use():
    # Deleting tag/old would cascade to tag/old/mine, which is in use. The Immich
    # hierarchy must protect it even when in_use does not name the parent.
    tags = [tag("1", "tag"), tag("2", "tag/old", "1"), tag("3", "tag/old/mine", "2"), tag("4", "tag/x", "1")]
    plan = immich.plan_cleanup(tags, in_use={"tag/old/mine"})
    assert [t["value"] for t in plan] == ["tag/x"]


def test_in_use_ancestor_is_never_a_candidate():
    tags = [tag("1", "parody"), tag("2", "parody/fate∕grand_order", "1")]
    plan = immich.plan_cleanup(tags, in_use={"parody/fate∕grand_order", "parody"})
    assert plan == []


@pytest.fixture
def fake():
    server = FakeImmich().start()
    yield server
    server.stop()


def test_run_cleanup_dry_run_and_real(fake):
    fake.add_asset("/data/upload/u/1.jpg", ["tag/long_hair", "vacation"])
    for value in ("tag/bangs", "character|abe_nana", "my_album/trip", "rating/questionable"):
        fake.add_tag(value)
    client = immich.ImmichClient(ImmichSettings(fake.url, API_KEY, None), sleep=lambda s: None)
    plan = immich.plan_cleanup(client.list_tags(), in_use={"tag", "tag/long_hair"})

    preview = immich.run_cleanup(client, plan, dry_run=True)
    assert preview.count == 4 and preview.deleted == 0
    assert preview.by_prefix == {"tag": 1, "legacy-pipe": 1, "rating": 2}
    assert set(preview.examples) == {"tag/bangs", "character|abe_nana", "rating/questionable", "rating"}
    assert fake.deleted == []

    result = immich.run_cleanup(client, plan)
    remaining = {t["value"] for t in fake.tags.values()}
    assert remaining == {"tag", "tag/long_hair", "vacation", "my_album", "my_album/trip"}
    assert result.deleted == 4


def test_run_cleanup_skips_tag_that_turns_out_non_empty(fake):
    fake.add_asset("/data/upload/u/1.jpg", ["tag/bangs"])       # Immich still uses it
    client = immich.ImmichClient(ImmichSettings(fake.url, API_KEY, None), sleep=lambda s: None)
    plan = immich.plan_cleanup(client.list_tags(), in_use=set())  # our index thinks it's unused
    result = immich.run_cleanup(client, plan)
    assert result.deleted == 0
    assert result.skipped_nonempty == 1
    assert {t["value"] for t in fake.tags.values()} == {"tag", "tag/bangs"}   # parent kept too
