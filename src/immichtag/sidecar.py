"""The XMP sidecar: the file the tagger writes and Immich, the filter and the web read.

`image.jpg` -> `image.jpg.xmp`, holding the tags in three properties Immich and other
readers understand, plus a provenance record saying which model wrote them. The exact
format, including what Immich's exiftool turns it into when a user edits tags there, is
specs/001-pixai-full-retag/contracts/sidecar-xmp.md.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Sequence

from lxml import etree

from immichtag import vocab

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DIGIKAM = "http://www.digikam.org/ns/1.0/"
LR = "http://ns.adobe.com/lightroom/1.0/"
DC = "http://purl.org/dc/elements/1.1/"
IMMICHTAG = "http://ns.immichtag.local/1.0/"
NSMAP = {"x": "adobe:ns:meta/", "rdf": RDF, "immichtag": IMMICHTAG, "digiKam": DIGIKAM, "lr": LR, "dc": DC}

#: Where tags are read from, in Immich's own order of precedence (`getTagList`): Immich
#: rewrites only TagsList when a user edits tags, so the other two can be stale.
TAG_PROPERTIES = ((DIGIKAM, "TagsList"), (LR, "HierarchicalSubject"), (DC, "subject"))
PROVENANCE_FIELDS = ("model", "revision", "profile", "taggedAt")

#: Suffix of the temporary file a write goes through; a run removes strays on start.
TMP_SUFFIX = ".immichtag-tmp"

# Fast path for files this tool wrote itself, which are uniform. Over a 100k-image
# library a regex is many times faster than an XML parse; anything else — notably files
# exiftool rewrote for Immich — goes through lxml.
_FAST_TAGSLIST = re.compile(rb"<digiKam:TagsList>\s*<rdf:(?:Seq|Bag)>(.*?)</rdf:(?:Seq|Bag)>", re.DOTALL)
_FAST_LI = re.compile(rb"<rdf:li>(.*?)</rdf:li>", re.DOTALL)
_FAST_PROV = re.compile(rb'immichtag:(model|revision|profile|taggedAt)="([^"]*)"')


@dataclass(frozen=True)
class Provenance:
    model: str
    revision: str
    profile: str = ""
    tagged_at: str = ""


@dataclass
class SidecarData:
    tags: List[str] = field(default_factory=list)
    provenance: Provenance | None = None
    error: str | None = None


def sidecar_path(image: Path) -> Path:
    return image.with_name(image.name + ".xmp")


def read_sidecar(path: Path) -> SidecarData:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return SidecarData(error=str(exc))
    fast = _read_fast(data)
    if fast is not None:
        return fast
    return _read_xml(data)


def read_tags(path: Path) -> List[str]:
    return read_sidecar(path).tags


def _read_fast(data: bytes) -> SidecarData | None:
    # exiftool marks its output with x:xmptk; those files have a different layout.
    if b"xmptk" in data:
        return None
    block = _FAST_TAGSLIST.search(data)
    if block is None:
        return None
    texts = [html.unescape(m.group(1).decode("utf-8", "replace")) for m in _FAST_LI.finditer(block.group(1))]
    fields = {k.decode(): html.unescape(v.decode("utf-8", "replace")) for k, v in _FAST_PROV.findall(data)}
    return SidecarData(tags=_dedupe(texts), provenance=_provenance(fields))


def _read_xml(data: bytes) -> SidecarData:
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(recover=True, resolve_entities=False))
    except (etree.XMLSyntaxError, ValueError) as exc:
        return SidecarData(error=f"unparseable XMP: {exc}")
    if root is None:
        return SidecarData(error="unparseable XMP")

    tags: List[str] = []
    for namespace, local in TAG_PROPERTIES:
        for prop in root.iter(f"{{{namespace}}}{local}"):
            for container in prop:
                if container.tag in (f"{{{RDF}}}Seq", f"{{{RDF}}}Bag"):
                    tags.extend(li.text for li in container if li.tag == f"{{{RDF}}}li" and li.text)
        if tags:
            break

    fields: dict = {}
    for element in root.iter(f"{{{RDF}}}Description"):
        for name in PROVENANCE_FIELDS:
            value = element.get(f"{{{IMMICHTAG}}}{name}")
            if value is not None:
                fields.setdefault(name, value)
    for name in PROVENANCE_FIELDS:
        if name not in fields:
            element = next(root.iter(f"{{{IMMICHTAG}}}{name}"), None)
            if element is not None and element.text:
                fields[name] = element.text.strip()
    return SidecarData(tags=_dedupe(tags), provenance=_provenance(fields))


def _provenance(fields: dict) -> Provenance | None:
    if not fields.get("model") or not fields.get("revision"):
        return None
    return Provenance(
        model=fields["model"], revision=fields["revision"],
        profile=fields.get("profile", ""), tagged_at=fields.get("taggedAt", ""),
    )


def _dedupe(tags: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for tag in tags:
        tag = tag.strip()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def build_xmp(tags: Sequence[str], provenance: Provenance | None) -> bytes:
    """TagsList is an rdf:Seq (digiKam's spec, and what exiftool would "fix" it to);
    HierarchicalSubject and dc:subject mirror it for other XMP readers."""
    x = NSMAP["x"]
    xmpmeta = etree.Element(f"{{{x}}}xmpmeta", nsmap={"x": x})
    rdf = etree.SubElement(xmpmeta, f"{{{RDF}}}RDF", nsmap={"rdf": RDF})
    desc = etree.SubElement(
        rdf, f"{{{RDF}}}Description",
        nsmap={"immichtag": IMMICHTAG, "digiKam": DIGIKAM, "lr": LR, "dc": DC},
    )
    desc.set(f"{{{RDF}}}about", "")
    if provenance is not None:
        desc.set(f"{{{IMMICHTAG}}}model", provenance.model)
        desc.set(f"{{{IMMICHTAG}}}revision", provenance.revision)
        desc.set(f"{{{IMMICHTAG}}}profile", provenance.profile)
        desc.set(f"{{{IMMICHTAG}}}taggedAt", provenance.tagged_at)

    values = _dedupe(tags)
    for (namespace, local), kind in zip(TAG_PROPERTIES, ("Seq", "Bag", "Bag")):
        prop = etree.SubElement(desc, f"{{{namespace}}}{local}")
        container = etree.SubElement(prop, f"{{{RDF}}}{kind}")
        for value in values:
            etree.SubElement(container, f"{{{RDF}}}li").text = value

    return etree.tostring(xmpmeta, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def merge_tags(managed: Sequence[str], old: Sequence[str]) -> List[str]:
    """The new managed tags, then every old tag that is not ours, in its old order.

    Old managed tags are dropped: a re-tag replaces them. A tag the user added in
    Immich (which Immich writes back into TagsList) survives.
    """
    keep = [tag for tag in old if not vocab.is_managed(tag)]
    return _dedupe(list(managed) + keep)


def write_atomic(path: Path, data: bytes) -> None:
    """Never leave a half-written sidecar: a reader sees the old file or the new one."""
    tmp = path.with_name(f".{path.name}{TMP_SUFFIX}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
