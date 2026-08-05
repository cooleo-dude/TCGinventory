"""Index the card catalog for recognition (spec 06, M-V1c → identity Layer 0).

Until this runs, "Suggest labels" can only propose cards you have already
labelled: the candidate pool is the labelled corpus, so the matcher is a
re-identifier rather than an identifier. This sweep embeds the catalog itself, so
a card can be recognised the first time it is seen.

**Two costs, and only one of them is large.** The index — a 384-float vector per
card — is ~82 MB for 53k cards, and a brute-force search over all of it takes about
a millisecond, so no index structure is needed. The images are the weight: ~6.9 GB
as downloaded. They are kept as **lossless WebP** (~5.6 GB) rather than discarded,
because a future identifier model invalidates every vector and re-embedding from
disk is a 30-minute job where re-downloading is not. Lossless rather than q95
because ~5% of cards sit within 0.004 cosine of a near-identical reprint, and
lossy compression moves a vector by about that much — noise the same size as the
signal, precisely where the signal matters.

The sweep is resumable (it skips what is already embedded for the current model
version), polite (bounded concurrency, and it never re-downloads what it has), and
reversible: :func:`reclaim` deletes archived art that nothing depends on, keeping
the vectors and every card you have actually used.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
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

ARCHIVE_SUFFIX = ".webp"


def archive_dir() -> Path:
    """Where the lossless art copies live — beside the corpus, not the previews."""
    return config.data_dir() / "vision" / "catalog"


def archive_path(card_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in card_id)
    return archive_dir() / f"{safe}{ARCHIVE_SUFFIX}"


def source_for(card_id: str) -> Path | None:
    """The best local copy of a card's art: the archive first, then the preview
    cache. This is what a re-embed reads, so it must not need the network."""
    archived = archive_path(card_id)
    if archived.is_file():
        return archived
    preview = cardimages.cache_path(card_id)
    return preview if preview.is_file() else None


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


def index_catalog(
    conn: sqlite3.Connection,
    repo: VisionRepository,
    scope: str = SCOPE_ALL,
    *,
    embedder=None,
    on_progress: Callable[[IndexProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    allow_download: bool = True,
) -> IndexSummary:
    """Embed a scope of the catalog, archiving the art it had to fetch.

    Skips anything already embedded for this model version, so a second run is
    cheap and an interrupted one resumes where it stopped.
    """
    from tcginventory.services.recognizer import get_shared_embedder

    embedder = embedder or get_shared_embedder()
    version = embedder.version
    summary = IndexSummary(scope=scope)
    pairs = scope_card_ids(conn, scope)
    summary.considered = len(pairs)
    archive_dir().mkdir(parents=True, exist_ok=True)

    for done, (card_id, url) in enumerate(pairs, start=1):
        if should_stop is not None and should_stop():
            summary.stopped = True
            break
        if repo.get_embedding(card_id, version) is not None:
            summary.already_indexed += 1
        else:
            try:
                source = source_for(card_id)
                if source is None and allow_download and url:
                    if cardimages.ensure_cached(card_id, url):
                        summary.downloaded += 1
                        source = source_for(card_id)
                if source is None:
                    summary.failed += 1
                else:
                    vector = embedder.embed(source)
                    repo.put_embedding(
                        card_id,
                        version,
                        int(vector.shape[0]),
                        np.asarray(vector, dtype=np.float32).tobytes(),
                    )
                    summary.embedded += 1
                    if _archive(card_id, source):
                        summary.archived += 1
            except Exception as exc:  # one bad image must not end the sweep
                summary.failed += 1
                if len(summary.errors) < 10:
                    summary.errors.append(f"{card_id}: {exc}")
        if on_progress is not None:
            on_progress(
                IndexProgress(
                    done=done,
                    total=len(pairs),
                    card_id=card_id,
                    downloaded=summary.downloaded,
                    skipped=summary.already_indexed,
                )
            )
    return summary


def _archive(card_id: str, source: Path) -> bool:
    """Keep a lossless copy so a future model can re-embed without the network."""
    target = archive_path(card_id)
    if target.is_file():
        return False
    from PIL import Image

    try:
        with Image.open(source) as im:
            im.convert("RGB").save(target, "WEBP", lossless=True)
    except (OSError, ValueError):
        return False
    return True


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
    try:
        rows = conn.execute(
            "SELECT DISTINCT c.card_id FROM card_catalog c JOIN item i "
            "ON i.name = c.name AND (i.set_name IS NULL OR i.set_name = c.set_name)"
        ).fetchall()
        protected.update(r["card_id"] for r in rows)
    except sqlite3.Error:  # pragma: no cover - defensive
        pass
    protected.update(p.stem for p in cardimages.cache_dir().glob("*.png"))
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
    for path in archive_dir().glob(f"*{ARCHIVE_SUFFIX}"):
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
    conn: sqlite3.Connection, repo: VisionRepository, *, embedder=None
) -> IndexStatus:
    """What the Operations tile reports: how much of the catalog is searchable."""
    from tcginventory.services.recognizer import get_shared_embedder

    version = (embedder or get_shared_embedder()).version
    catalog = conn.execute("SELECT COUNT(*) FROM card_catalog").fetchone()[0]
    indexed = repo.conn.execute(
        "SELECT COUNT(*) FROM card_embedding WHERE model_version = ?", (version,)
    ).fetchone()[0]
    archived = 0
    total_bytes = 0
    if archive_dir().is_dir():
        for path in archive_dir().glob(f"*{ARCHIVE_SUFFIX}"):
            archived += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:  # pragma: no cover
                continue
    return IndexStatus(catalog, indexed, archived, total_bytes, version)
