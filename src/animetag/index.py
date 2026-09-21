"""An in-memory inverted index over the library's sidecars.

The CLI can afford to re-read 100k sidecars per run — it does one run. A UI cannot:
every keystroke would re-scan the disk. So the sidecars are read once into
`tag -> {image ids}` postings, after which a filter is set arithmetic and answers
in microseconds.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Set, Tuple

from animetag import filterer, library, sidecar, vocab
from animetag.filterer import Match, TagFilter


@dataclass
class LibraryIndex:
    """Tags of every tagged image under `root`, addressable by integer id.

    Ids are positions in `images`, which is os.walk order — so a sorted id list is
    the same "scan order" the CLI produces.
    """

    root: Path
    images: List[Path] = field(default_factory=list)
    #: Original (unnormalized) tags per image, for display.
    display_tags: List[Tuple[str, ...]] = field(default_factory=list)
    #: normalized full tag -> ids carrying it.
    postings: Dict[str, Set[int]] = field(default_factory=dict)
    #: normalized full tag -> its leaf, precomputed for pattern expansion.
    leaves: Dict[str, str] = field(default_factory=dict)
    #: leaf -> the full tags sharing it, so a bare pattern is a lookup, not a scan.
    by_leaf: Dict[str, List[str]] = field(default_factory=dict)
    #: Images whose sidecar was not written by the current model, by status.
    retag: Dict[str, int] = field(default_factory=lambda: {"legacy": 0, "outdated": 0})
    #: Which rebuild produced this index. Every rebuild reassigns ids, so anything
    #: cached by id — thumbnails, on the server and in the browser — is keyed by it too.
    generation: int = 0

    def __post_init__(self) -> None:
        self._expansion_cache: Dict[str, Set[int]] = {}

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def tag_count(self) -> int:
        return len(self.postings)

    def tags_of(self, image_id: int) -> Tuple[str, ...]:
        return self.display_tags[image_id]

    def match(self, image_id: int) -> Match:
        image = self.images[image_id]
        return Match(
            image=image,
            sidecar=image.with_suffix(image.suffix + ".xmp"),
            tags=list(self.display_tags[image_id]),
        )

    def expand(self, pattern: str) -> Set[int]:
        """Every id carrying a tag that `pattern` matches.

        A plain tag is one dict lookup. A glob is resolved against the vocabulary —
        thousands of tags, not hundreds of thousands of images — and then cached.
        """
        cached = self._expansion_cache.get(pattern)
        if cached is not None:
            return cached

        if not vocab.is_glob(pattern):
            if vocab.split_tag(pattern)[0]:
                ids = self.postings.get(pattern, set())
            else:
                # A bare pattern addresses the leaf, which several prefixes may share.
                ids = self._union(self.by_leaf.get(pattern, ()))
        else:
            ids = self._union(
                full for full, leaf in self.leaves.items()
                if filterer.pattern_matches(pattern, full, leaf)
            )

        self._expansion_cache[pattern] = ids
        return ids

    def _union(self, full_tags) -> Set[int]:
        """Postings of several full tags. Callers never mutate what expand() returns,
        so a single-tag union can hand back the posting set itself."""
        buckets = [self.postings[tag] for tag in full_tags if tag in self.postings]
        if not buckets:
            return set()
        if len(buckets) == 1:
            return buckets[0]
        return set().union(*buckets)

    def select(
        self,
        filters: Sequence[TagFilter],
        match_all: bool = False,
        exclude: TagFilter | None = None,
    ) -> List[int]:
        """Ids passing the filters and surviving `exclude`, in scan order.

        No filters means everything, so a bare exclusion is a valid selection on its own.
        """
        everything = range(len(self.images))
        if not filters:
            return self._veto(set(everything), exclude)

        groups: List[Set[int]] = []
        for tag_filter in filters:
            if tag_filter.required:
                ids: Set[int] | None = None
                for pattern in tag_filter.required:
                    matched = self.expand(pattern)
                    ids = set(matched) if ids is None else (ids & matched)
                    if not ids:
                        break
                ids = ids or set()
            else:
                ids = set(everything)
            for pattern in tag_filter.excluded:
                if ids:
                    ids -= self.expand(pattern)
            groups.append(ids)

        result = groups[0]
        for group in groups[1:]:
            result = (result & group) if match_all else (result | group)
        return self._veto(result, exclude)

    def _veto(self, ids: Set[int], exclude: TagFilter | None) -> List[int]:
        """Drop the globally excluded tags, then put the survivors back in scan order."""
        if exclude is not None:
            ids = set(ids)
            for pattern in exclude.excluded:
                if not ids:
                    break
                ids -= self.expand(pattern)
        return sorted(ids)

    def order(self, ids: List[int], how: str = "scan", seed: int | None = None) -> List[int]:
        if how == "random":
            shuffled = list(ids)
            random.Random(seed).shuffle(shuffled)
            return shuffled
        if how in ("newest", "oldest"):
            def mtime(image_id: int) -> float:
                try:
                    return self.images[image_id].stat().st_mtime
                except OSError:
                    return 0.0
            return sorted(ids, key=mtime, reverse=how == "newest")
        return ids

    def retag_counts(self) -> Dict[str, int]:
        return dict(self.retag)

    def warnings_for(self, filters: Sequence[TagFilter], exclude: TagFilter | None = None,
                     model=None) -> List[dict]:
        """Warnings for plain patterns the current model never writes, or that match
        nothing — the "you typed `bangs`" case. `row` is the filter's index, or -1 for
        the global exclusions."""
        model = vocab.load_model_vocabulary() if model is None else model
        rows = [(i, list(f.required) + list(f.excluded)) for i, f in enumerate(filters)]
        if exclude is not None:
            rows.append((-1, list(exclude.excluded)))
        warnings: List[dict] = []
        seen = set()
        for row, patterns in rows:
            for pattern in patterns:
                if vocab.is_glob(pattern) or (row, pattern) in seen:
                    continue
                seen.add((row, pattern))
                warning = vocab.classify_pattern(pattern, len(self.expand(pattern)), model,
                                                 library_names=self.by_leaf.keys())
                if warning:
                    warnings.append({**warning, "row": row})
        return warnings

    def tag_counts(self) -> List[Tuple[str, int]]:
        """(display tag, count), most common first. The autocomplete's source."""
        return sorted(
            ((tag, len(ids)) for tag, ids in self.postings.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )


def build(root: Path, progress: Callable[[int], None] | None = None) -> LibraryIndex:
    """Read every sidecar under `root`. Reports progress every 2000 images."""
    index = LibraryIndex(root=root)
    postings = index.postings
    leaves = index.leaves
    # A 100k-image library holds ~1.5M tag strings drawn from a vocabulary of ~7k.
    # Sharing one object per distinct spelling is most of this index's memory.
    pool: Dict[str, str] = {}

    for sidecar_path in filterer.sidecar_files(root):
        image = filterer.image_for_sidecar(sidecar_path)
        if image is None:
            continue
        data = sidecar.read_sidecar(sidecar_path)
        tags = data.tags
        status = library.status_of(data, vocab.MODEL_REPO, vocab.MODEL_REVISION)
        if status.value in index.retag:
            index.retag[status.value] += 1

        image_id = len(index.images)
        index.images.append(image)
        index.display_tags.append(tuple(pool.setdefault(tag, tag) for tag in tags))

        for tag in tags:
            normalized = filterer.normalize(tag)
            bucket = postings.get(normalized)
            if bucket is None:
                bucket = postings[normalized] = set()
                leaf = leaves[normalized] = vocab.leaf_of(normalized)
                index.by_leaf.setdefault(leaf, []).append(normalized)
            bucket.add(image_id)

        if progress and image_id % 2000 == 0:
            progress(image_id)

    if progress:
        progress(len(index.images))
    return index


class IndexHolder:
    """Owns the index and the thread that builds it, so the UI can load immediately.

    The server starts answering requests at once; anything needing the index waits on
    `ready` or reports `state` so the page can show a progress line instead of hanging.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.index: LibraryIndex | None = None
        self.scanned = 0
        self.state = "idle"  # idle | building | ready | error
        self.error: str | None = None
        self.generation = 0
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, root: Path | None = None) -> None:
        with self._lock:
            if self.state == "building":
                return
            if root is not None:
                self.root = root
            self.index = None
            self.scanned = 0
            self.error = None
            self.state = "building"
            self.generation += 1
            self.ready.clear()
            self._thread = threading.Thread(target=self._run, args=(self.generation,), daemon=True)
            self._thread.start()

    def _run(self, generation: int) -> None:
        try:
            index = build(self.root, progress=self._on_progress)
        except Exception as exc:  # a bad --dir shouldn't take the server down
            self.error = str(exc)
            self.state = "error"
        else:
            index.generation = generation
            self.index = index
            self.scanned = index.image_count
            self.state = "ready"
        finally:
            self.ready.set()

    def _on_progress(self, scanned: int) -> None:
        self.scanned = scanned

    def require(self, timeout: float = 30.0) -> LibraryIndex:
        self.ready.wait(timeout)
        if self.index is None:
            raise RuntimeError(self.error or "Index is still building")
        return self.index
