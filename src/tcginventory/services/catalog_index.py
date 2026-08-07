"""Index the card catalog for recognition (spec 06, M-V1c → identity Layer 0).

Until this runs, "Suggest labels" can only propose cards you have already
labelled: the candidate pool is the labelled corpus, so the matcher is a
re-identifier rather than an identifier. This sweep embeds the catalog itself, so
a card can be recognised the first time it is seen.

**Two costs, and only one of them is large.** The index — a 384-float vector per
card — is ~82 MB for 53k cards, and a brute-force search over all of it takes about
a millisecond, so no index structure is needed. The art is the weight: ~6.9 GB.

**The art is kept as the downloaded PNG, in one place.** Re-encoding to lossless
WebP saved only 18% (card art is photographic, so lossless compression has little
to work with) and would have meant storing every card twice — once as the preview
the app already caches, once as an archive — which costs more disk than the format
saves. Lossy was ruled out on measurement rather than taste: the median card sits
0.19 cosine from its nearest rival, but ~5% sit within 0.004, and q95
recompression moves a vector by about that much. That is noise the size of the
signal exactly where reprints have to be told apart.

Keeping the art at all is what makes a future identifier affordable: a new model
invalidates every vector, and re-embedding from disk is half an hour where
re-downloading is 7 GB.

The sweep is resumable (it skips what is already embedded for the current model
version), polite (a small download pool, and it never re-fetches what it has), and
reversible: :func:`reclaim` deletes art that nothing depends on, keeping the
vectors and every card you had before the sweep or have used since.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tcginventory import config
from tcginventory.data.vision import VisionRepository
from tcginventory.services import cardimages

SCOPE_CACHED = "cached"  # only art already on disk — no network at all
SCOPE_OWNED = "owned"  # every card in the sets you hold cards from
SCOPE_ALL = "all"  # the whole catalog

SCOPES = (SCOPE_CACHED, SCOPE_OWNED, SCOPE_ALL)
SCOPE_LABELS = {
    SCOPE_CACHED: "Art already downloaded",
    SCOPE_OWNED: "Sets you own cards from",
    SCOPE_ALL: "The whole catalog",
}

# Cards whose art was on disk before a sweep ran. Written once, at the start of
# the first sweep: afterwards every card looks "already downloaded", so there
# would be no way to tell what the sweep brought in and what you already had.
SNAPSHOT_NAME = "pre_sweep_cache.json"

# Small on purpose: the art comes from a free CDN, and 53k requests is already
# a lot to ask of it.
DOWNLOAD_WORKERS = 8


def snapshot_path() -> Path:
    return config.data_dir() / "vision" / SNAPSHOT_NAME


def art_path(card_id: str) -> Path:
    """Where a card's art lives. One store: the preview cache the app already
    uses, so a swept card and a hovered card are the same file."""
    return cardimages.cache_path(card_id)


def source_for(card_id: str) -> Path | None:
    """The local copy of a card's art, or None. This is what a re-embed reads, so
    it must never need the network."""
    path = art_path(card_id)
    return path if path.is_file() else None


def take_snapshot(*, force: bool = False) -> set[str]:
    """Record what was cached before any sweep, so reclaim can give back only what
    a sweep fetched. Idempotent: the first snapshot is the one that counts."""
    import json

    target = snapshot_path()
    if target.is_file() and not force:
        try:
            return set(json.loads(target.read_text(encoding="utf-8")))
        except (OSError, ValueError):  # pragma: no cover - unreadable snapshot
            return set()
    existing = sorted(p.stem for p in cardimages.cache_dir().glob("*.png"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing), encoding="utf-8")
    return set(existing)


def snapshot_ids() -> set[str]:
    """The recorded pre-sweep cache, or everything on disk if none was taken."""
    import json

    target = snapshot_path()
    if not target.is_file():
        return {p.stem for p in cardimages.cache_dir().glob("*.png")}
    try:
        return set(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError):  # pragma: no cover
        return set()


@dataclass
class IndexProgress:
    done: int
    total: int
    card_id: str
    downloaded: int
    skipped: int


@dataclass
class IndexSummary:
    scope: str
    considered: int = 0
    embedded: int = 0
    already_indexed: int = 0
    downloaded: int = 0
    archived: int = 0
    failed: int = 0
    stopped: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [f"{self.embedded:,} embedded"]
        if self.already_indexed:
            parts.append(f"{self.already_indexed:,} already indexed")
        if self.downloaded:
            parts.append(f"{self.downloaded:,} downloaded")
        if self.failed:
            parts.append(f"{self.failed:,} failed")
        if self.stopped:
            parts.append("stopped early")
        return " · ".join(parts)


def scope_card_ids(
    conn: sqlite3.Connection, scope: str
) -> list[tuple[str, str | None]]:
    """The (card_id, image_url) pairs a scope covers, in a stable order."""
    if scope == SCOPE_OWNED:
        rows = conn.execute(
            "SELECT card_id, image_small FROM card_catalog WHERE set_name IN ("
            "  SELECT DISTINCT set_name FROM item"
            "  WHERE set_name IS NOT NULL AND set_name != ''"
            ") ORDER BY card_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT card_id, image_small FROM card_catalog ORDER BY card_id"
        ).fetchall()
    pairs = [(r["card_id"], r["image_small"]) for r in rows]
    if scope == SCOPE_CACHED:
        # No network: only what is already on disk, from either location.
        pairs = [(cid, url) for cid, url in pairs if source_for(cid) is not None]
    return pairs


def candidate_pool(
    conn: sqlite3.Connection,
    repo: VisionRepository,
    model_version: str,
    *,
    languages: Sequence[str] | None = None,
    extra: Iterable[str] = (),
) -> list[str]:
    """Every indexed card the identifier should rank against.

    ``languages`` narrows the pool, and on a mixed catalog that matters more than
    any scoring change. Measured on the corpus: the same artwork is released in
    English and in Japanese, so an English card's nearest neighbour is routinely
    its Japanese twin — 42 of 46 wrong answers were cross-language. Restricting
    the pool to the languages actually being photographed took top-1 from 20.7% to
    67.2%, which is more than every scoring layer combined.

    Inferring the language from the picture was tried and rejected: a linear probe
    on the card embeddings separates the two at 99.8% on catalog renders but 51.7%
    on photographs — worse than a constant guess. The scope is a stated preference
    rather than a prediction because that is the part we can actually be right
    about.

    ``extra`` is always kept regardless of language: cards the user has already
    labelled are known-relevant, and dropping one because of a scope setting would
    make the identifier forget something it had been taught.
    """
    rows = repo.conn.execute(
        "SELECT card_id FROM card_embedding WHERE model_version = ?",
        (model_version,),
    ).fetchall()
    indexed = {r["card_id"] for r in rows}
    if not indexed:
        return sorted(set(extra))

    wanted = {code.strip().upper() for code in languages or () if code and code.strip()}
    if wanted:
        allowed = {
            r["card_id"]
            for r in conn.execute("SELECT card_id, language FROM card_catalog")
            if (r["language"] or "").strip().upper() in wanted
        }
        indexed &= allowed
    indexed.update(cid for cid in extra if cid)
    return sorted(indexed)


def index_catalog(
    conn: sqlite3.Connection,
    repo: VisionRepository,
    scope: str = SCOPE_ALL,
    *,
    embedder=None,
    on_progress: Callable[[IndexProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    allow_download: bool = True,
    workers: int = DOWNLOAD_WORKERS,
) -> IndexSummary:
    """Embed a scope of the catalog, fetching art it doesn't have.

    Skips anything already embedded for this model version, so a second run is
    cheap and an interrupted one resumes where it stopped.

    Downloads run in a small pool while embedding stays on this thread: the work
    is network-bound, and 53k sequential round trips would take hours where the
    embedding itself takes about half of one. The pool is deliberately small — the
    art comes from a free CDN.
    """
    from concurrent.futures import ThreadPoolExecutor

    from tcginventory.services.recognizer import get_shared_embedder

    embedder = embedder or get_shared_embedder()
    version = embedder.version
    summary = IndexSummary(scope=scope)
    pairs = scope_card_ids(conn, scope)
    summary.considered = len(pairs)

    # Record what was already here before anything is fetched, so a later reclaim
    # can tell the sweep's downloads from the art you already had.
    take_snapshot()

    todo: list[tuple[str, str | None]] = []
    for card_id, url in pairs:
        if repo.get_embedding(card_id, version) is not None:
            summary.already_indexed += 1
        else:
            todo.append((card_id, url))

    def fetch(item: tuple[str, str | None]) -> tuple[str, Path | None, bool]:
        card_id, url = item
        local = source_for(card_id)
        if local is not None:
            return card_id, local, False
        if not (allow_download and url):
            return card_id, None, False
        try:
            got = cardimages.ensure_cached(card_id, url)
        except Exception:  # pragma: no cover - network paths
            return card_id, None, False
        return card_id, (got if got and Path(got).is_file() else None), bool(got)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for card_id, source, downloaded in pool.map(fetch, todo):
            done += 1
            if should_stop is not None and should_stop():
                summary.stopped = True
                break
            if downloaded:
                summary.downloaded += 1
            if source is None:
                summary.failed += 1
            else:
                try:
                    vector = embedder.embed(source)
                    repo.put_embedding(
                        card_id,
                        version,
                        int(vector.shape[0]),
                        np.asarray(vector, dtype=np.float32).tobytes(),
                    )
                    summary.embedded += 1
                except Exception as exc:  # one bad image must not end the sweep
                    summary.failed += 1
                    if len(summary.errors) < 10:
                        summary.errors.append(f"{card_id}: {exc}")
            if on_progress is not None:
                on_progress(
                    IndexProgress(
                        done=done + summary.already_indexed,
                        total=len(pairs),
                        card_id=card_id,
                        downloaded=summary.downloaded,
                        skipped=summary.already_indexed,
                    )
                )
    return summary


# ---------------------------------------------------------------------------
# reclaim: give the disk back without losing anything that is in use
# ---------------------------------------------------------------------------


def protected_card_ids(conn: sqlite3.Connection, repo: VisionRepository) -> set[str]:
    """Cards whose art must survive a reclaim.

    Three sources, and each is something the user did rather than something the
    sweep did: cards they labelled, cards they own, and art that was on disk as a
    preview before any sweep ran.
    """
    protected = set(repo.labeled_card_ids())
    protected.update(snapshot_ids())  # what you had before any sweep
    try:
        rows = conn.execute(
            "SELECT DISTINCT c.card_id FROM card_catalog c JOIN item i "
            "ON i.name = c.name AND (i.set_name IS NULL OR i.set_name = c.set_name)"
        ).fetchall()
        protected.update(r["card_id"] for r in rows)
    except sqlite3.Error:  # pragma: no cover - defensive
        pass
    return protected


@dataclass
class ReclaimSummary:
    removed: int = 0
    kept: int = 0
    bytes_freed: int = 0

    @property
    def summary(self) -> str:
        return (
            f"{self.removed:,} removed · {self.kept:,} kept · "
            f"{self.bytes_freed / 1e9:.2f} GB reclaimed"
        )


def reclaim(
    conn: sqlite3.Connection,
    repo: VisionRepository,
    *,
    dry_run: bool = False,
    protected: Iterable[str] | None = None,
) -> ReclaimSummary:
    """Delete archived art that nothing depends on. Vectors are never touched —
    they are 82 MB and they are the part that makes recognition work."""
    keep = set(protected) if protected is not None else protected_card_ids(conn, repo)
    result = ReclaimSummary()
    for path in cardimages.cache_dir().glob("*.png"):
        if path.stem in keep:
            result.kept += 1
            continue
        try:
            size = path.stat().st_size
            if not dry_run:
                path.unlink()
            result.removed += 1
            result.bytes_freed += size
        except OSError:  # pragma: no cover - vanished or locked
            continue
    return result


@dataclass
class IndexStatus:
    catalog: int
    indexed: int
    archived: int
    archive_bytes: int
    model_version: str

    @property
    def complete(self) -> bool:
        return self.catalog > 0 and self.indexed >= self.catalog


def index_status(
    conn: sqlite3.Connection,
    repo: VisionRepository,
    *,
    embedder=None,
    with_archive: bool = False,
) -> IndexStatus:
    """What the Operations tile reports: how much of the catalog is searchable.

    ``with_archive`` counts the kept card art on disk, which means a ``stat`` per
    file — 50,546 of them here, and just over two seconds. Off by default because
    only the reclaim tile ever reads those two fields, while the Models window
    calls this twice per refresh for ``indexed`` alone. Leaving it on cost four
    seconds of frozen UI every time a training job started or finished
    (2026-08-07); the counts either side of it are single-digit milliseconds.

    The version is read without building an embedder
    (:func:`~tcginventory.services.recognizer.shared_embedder_version`) — it only
    keys the ``card_embedding`` count, and constructing an ONNX session for it
    cost 0.13s and ~110MB on the UI thread.
    """
    from tcginventory.services.recognizer import shared_embedder_version

    version = embedder.version if embedder is not None else shared_embedder_version()
    catalog = conn.execute("SELECT COUNT(*) FROM card_catalog").fetchone()[0]
    indexed = repo.conn.execute(
        "SELECT COUNT(*) FROM card_embedding WHERE model_version = ?", (version,)
    ).fetchone()[0]
    archived = 0
    total_bytes = 0
    if with_archive and cardimages.cache_dir().is_dir():
        for path in cardimages.cache_dir().glob("*.png"):
            archived += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:  # pragma: no cover
                continue
    return IndexStatus(catalog, indexed, archived, total_bytes, version)
