# Contract: XMP sidecar

The sidecar is the interface shared by the tagger, which writes it, Immich (reads it, and rewrites it when the user edits tags in Immich), and the filter, index and export code, which read it.

## As written by `animetag`

```xml
<?xml version='1.0' encoding='UTF-8'?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:animetag="http://ns.animetag.local/1.0/"
        xmlns:digiKam="http://www.digikam.org/ns/1.0/"
        xmlns:lr="http://ns.adobe.com/lightroom/1.0/"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        animetag:model="pixai-labs/pixai-tagger-v1.0"
        animetag:revision="f33cfdb53c0c90b049bab9ce066eea1118970ef8"
        animetag:profile="v1"
        animetag:taggedAt="2026-09-22T03:14:15Z">
      <digiKam:TagsList>
        <rdf:Seq>
          <rdf:li>character/jeanne_d'arc_alter_(fate)</rdf:li>
          <rdf:li>parody/fate∕grand_order</rdf:li>
          <rdf:li>parody/fate_(series)</rdf:li>
          <rdf:li>tag/long_hair</rdf:li>
          <rdf:li>meta/official_art</rdf:li>
          <rdf:li>rating/sensitive</rdf:li>
          <rdf:li>my_album/trip</rdf:li>          <!-- foreign tag, preserved -->
        </rdf:Seq>
      </digiKam:TagsList>
      <lr:HierarchicalSubject><rdf:Bag><!-- same values --></rdf:Bag></lr:HierarchicalSubject>
      <dc:subject><rdf:Bag><!-- same values --></rdf:Bag></dc:subject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
```

**Guarantees:**
1. **Encoding and escaping**: UTF-8, with XML escaping done by lxml.
2. **Tag values**:
   - The only `/` in a managed tag is the one after the prefix; a `/` inside a name is written as `∕` (U+2215).
   - `|` is written verbatim.
   - Immich stores the value split on `/`, so `parody/fate∕grand_order` is the two-level tag `parody` → `fate∕grand_order`.
3. **Tag lists**: `TagsList` is an `rdf:Seq` and holds the full list, with managed tags first and then foreign tags in their previous order. The two `rdf:Bag`s mirror it exactly.
4. **Rating**: exactly one `rating/*` value, unless the tagging used `--no-rating`.
5. **Atomic write**: the file is replaced atomically. Readers never observe a partial file.

## As rewritten by Immich (must be accepted)

Immich 3.2.2's `SidecarWrite` uses exiftool 13.59. The following output was verified on a copy of the file above after a tag edit:
- The provenance **attributes** become **child elements** (`<animetag:model>…</animetag:model>`) of a separate `rdf:Description` in the same namespace.
- `digiKam:TagsList` is rewritten with the asset's full current tag list, as an `rdf:Seq`.
- `lr:HierarchicalSubject` and `dc:subject` are left as they were, so they may be stale.
- Other properties may appear, for example `dc:description` holding an `rdf:Alt`/`rdf:li`, and `xmp:Rating`.

**Reader rules:**
1. Tags come from `digiKam:TagsList` when present, otherwise from `lr:HierarchicalSubject`, otherwise from `dc:subject`. This is the same precedence as Immich's `getTagList`.
2. Only `rdf:li` elements that are direct children of the chosen property's `rdf:Seq` or `rdf:Bag` count as tags. `rdf:Alt` content (descriptions, titles) is never read as a tag.
3. Provenance is read from `animetag:*` attributes on any `rdf:Description`, or from `animetag:*` child elements. If both exist, the attribute form wins.
4. If the file cannot be parsed, it is treated as having no tags and status `LEGACY`.

## Legacy (WD14) sidecars

These have the same three properties, but each is an `rdf:Bag` with no provenance. They are read by the same rules; `TagsList` is present in every one of the 3,000 sampled files.
