"""Lazy on-disk cache for small card images (pokemontcg.io CDN).

Storing every card's ``images.small`` would be ~3.3 GB across the ~20k-card
catalog, so instead we fetch a card's image the first time it's needed (e.g.
hovered) and keep the PNG on disk keyed by catalog ``card_id``. Subsequent
requests serve straight from disk. Qt-free; the network fetch is injectable so
it's testable offline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tcginventory import config

_SUBDIR = "card_images"


def cache_dir() -> Path:
    return config.data_dir() / _SUBDIR


def cache_path(card_id: str) -> Path:
    # card_ids are dataset slugs like "sv9-163" — filesystem-safe already, but
    # guard the odd separator just in case.
    safe = card_id.replace("/", "_").replace("\\", "_")
    return cache_dir() / f"{safe}.png"


def is_cached(card_id: str) -> bool:
    return cache_path(card_id).exists()


def _download(url: str) -> bytes:  # pragma: no cover - real network
    import httpx

    resp = httpx.get(
        url,
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "TCGinventory/1.0"},
    )
    resp.raise_for_status()
    return resp.content


def ensure_cached(
    card_id: str,
    url: str,
    fetch_bytes: Callable[[str], bytes] | None = None,
) -> Path | None:
    """Return the local path for ``card_id``'s image, fetching it if absent.

    Returns ``None`` on any failure (offline, 404, empty body) so callers can
    silently skip the preview rather than crash a hover.
    """
    if not card_id or not url:
        return None
    path = cache_path(card_id)
    if path.exists():
        return path
    fetch = fetch_bytes or _download
    try:
        data = fetch(url)
    except Exception:
        return None
    if not data:
        return None
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        return None
    return path
