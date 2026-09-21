import os

import pytest
from lxml import etree

from immichtag import sidecar

REVISION = "f33cfdb53c0c90b049bab9ce066eea1118970ef8"
PROV = sidecar.Provenance(
    model="pixai-labs/pixai-tagger-v1.0", revision=REVISION, profile="v1",
    tagged_at="2026-09-22T03:14:15Z",
)
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


def test_build_and_read_round_trip(tmp_path):
    tags = ["character/a_(b)", "parody/fate∕grand_order", "tag/long_hair", "meta/official_art",
            "rating/sensitive", "my_album/trip", "tag/<&>"]
    path = tmp_path / "x.jpg.xmp"
    path.write_bytes(sidecar.build_xmp(tags, PROV))

    data = sidecar.read_sidecar(path)
    assert data.error is None
    assert data.tags == tags
    assert data.provenance == PROV

    root = etree.fromstring(path.read_bytes())
    tags_list = root.find(f".//{{{sidecar.DIGIKAM}}}TagsList")
    assert tags_list[0].tag == f"{{{RDF}}}Seq"
    for local, ns in (("HierarchicalSubject", sidecar.LR), ("subject", sidecar.DC)):
        prop = root.find(f".//{{{ns}}}{local}")
        assert prop[0].tag == f"{{{RDF}}}Bag"
        assert [li.text for li in prop[0]] == tags


def test_read_immich_rewritten(fixtures_dir):
    data = sidecar.read_sidecar(fixtures_dir / "immich_rewritten.xmp")
    assert data.tags == ["parody/fate∕grand_order", "tag/long_hair", "my_album/trip"]
    assert "hello" not in data.tags
    assert data.provenance == PROV


def test_read_current_fixture(fixtures_dir):
    data = sidecar.read_sidecar(fixtures_dir / "immichtag_current.xmp")
    assert data.provenance == PROV
    assert data.tags[-1] == "my_album/trip"
    assert "parody/fate∕grand_order" in data.tags


def test_read_wd14_legacy(fixtures_dir):
    data = sidecar.read_sidecar(fixtures_dir / "wd14_legacy.xmp")
    assert data.provenance is None
    assert data.error is None
    assert "tag/bangs" in data.tags
    assert "rating/sensitive" in data.tags
    assert len(data.tags) == len(set(data.tags))


def test_read_garbage(tmp_path):
    path = tmp_path / "bad.xmp"
    path.write_bytes(b"\x00\x01 not xml at all")
    data = sidecar.read_sidecar(path)
    assert data.tags == []
    assert data.provenance is None
    assert data.error


def test_read_missing_file(tmp_path):
    data = sidecar.read_sidecar(tmp_path / "nope.xmp")
    assert data.tags == [] and data.error


def test_tagslist_takes_precedence_over_stale_bags(tmp_path):
    path = tmp_path / "p.xmp"
    path.write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="%s">'
        '<rdf:Description xmlns:lr="http://ns.adobe.com/lightroom/1.0/" xmlns:digiKam="http://www.digikam.org/ns/1.0/">'
        '<lr:HierarchicalSubject><rdf:Bag><rdf:li>tag/old</rdf:li></rdf:Bag></lr:HierarchicalSubject>'
        '<digiKam:TagsList><rdf:Seq><rdf:li>tag/new</rdf:li></rdf:Seq></digiKam:TagsList>'
        "</rdf:Description></rdf:RDF></x:xmpmeta>" % RDF,
        encoding="utf-8",
    )
    assert sidecar.read_tags(path) == ["tag/new"]


def test_falls_back_to_hierarchical_subject(tmp_path):
    path = tmp_path / "p.xmp"
    path.write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="%s">'
        '<rdf:Description xmlns:lr="http://ns.adobe.com/lightroom/1.0/">'
        '<lr:HierarchicalSubject><rdf:Bag><rdf:li>tag/only</rdf:li></rdf:Bag></lr:HierarchicalSubject>'
        "</rdf:Description></rdf:RDF></x:xmpmeta>" % RDF,
        encoding="utf-8",
    )
    assert sidecar.read_tags(path) == ["tag/only"]


def test_merge_keeps_foreign_after_managed():
    old = ["tag/bangs", "rating/sensitive", "my_album/trip", "character|legacy", "vacation"]
    new = ["character/x", "tag/long_hair", "rating/general"]
    assert sidecar.merge_tags(new, old) == ["character/x", "tag/long_hair", "rating/general",
                                            "my_album/trip", "vacation"]


def test_sidecar_path():
    from pathlib import Path
    assert sidecar.sidecar_path(Path("/a/b/img.jpg")) == Path("/a/b/img.jpg.xmp")


def test_write_atomic_replaces_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "img.jpg.xmp"
    path.write_bytes(b"old")
    sidecar.write_atomic(path, b"new")
    assert path.read_bytes() == b"new"
    assert not list(tmp_path.glob("*immichtag-tmp"))


def test_write_atomic_crash_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "img.jpg.xmp"
    path.write_bytes(b"original")

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        sidecar.write_atomic(path, b"new content")
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob("*immichtag-tmp"))
