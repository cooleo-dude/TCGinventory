"""Lazy on-disk cache for small card images, and the art-source decision.

Storing every card's image would be ~3.3 GB across the ~20k-card catalog, so
instead we fetch a card's image the first time it's needed (e.g. hovered) and
keep it on disk keyed by ``card_id``. Subsequent requests serve straight from
disk. Qt-free; the network fetch is injectable so it's testable offline.

:func:`art_for` picks *which* image a card gets: the reference catalog when it
can pin the exact printing, otherwise the TCGplayer product photo of the matching
ledger item — the catalog only carries mainline printings, so variants like the
alt-art "Dedenne-GX 195a/214" exist only as a priced product.
"""

from __future__ import annotations

import sqlite3
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


def _norm_num(value: str | None) -> str:
    head = (value or "").split("/", 1)[0].strip().lower()
    return head.lstrip("0") or head


def tcgplayer_art(
    conn: sqlite3.Connection,
    name: str | None,
    set_name: str | None = None,
    number: str | None = None,
) -> tuple[str, str] | None:
    """``(cache_key, url)`` from the TCGplayer product linked on the ledger item
    with this printed identity, or None if there's no such item / link.

    The product id is the exact printing the card is priced from, so its photo is
    the right art whenever the catalog can't identify the printing.
    """
    from tcginventory.services import tcgplayer

    if not name:
        return None
    where = ["name = ? COLLATE NOCASE", "tcgplayer_url IS NOT NULL"]
    params: list[str] = [name]
    if set_name:
        where.append("set_name = ? COLLATE NOCASE")
        params.append(set_name)
    if number:
        where.append("card_number = ? COLLATE NOCASE")
        params.append(number)
    rows = conn.execute(
        f"SELECT tcgplayer_url FROM item WHERE {' AND '.join(where)} "
        "ORDER BY id LIMIT 20",
        params,
    ).fetchall()
    ids = {pid for pid in (tcgplayer.product_id(r[0]) for r in rows) if pid}
    # Items sharing this identity must agree on the product; two different
    # printings behind one name/set/number is a guess, and a wrong art is worse
    # than a blank (the same rule the catalog matcher follows).
    if len(ids) != 1:
        return None
    pid = ids.pop()
    return f"tcgp-{pid}", tcgplayer.product_image_url(pid)


def art_for(
    conn: sqlite3.Connection,
    name: str | None,
    set_name: str | None = None,
    number: str | None = None,
) -> tuple[str, str] | None:
    """``(cache_key, url)`` for a card's art, by printed identity.

    The reference catalog wins when it **pins the printing** — the collector
    number agrees, or there was no number to contradict — because those scans are
    clean and consistent. Otherwise the card is something the catalog doesn't
    carry (an alt art such as "195a/214", or a row with no image), and the
    matching ledger item's TCGplayer product photo is used instead: stripping the
    variant suffix to borrow a sibling's art would show the wrong picture.
    """
    from tcginventory.data.repositories import CardCatalogRepository
    from tcginventory.services import tcgplayer

    catalog = CardCatalogRepository(conn)
    hit = catalog.image_for(name, set_name, number)
    if hit:
        card = catalog.get(hit[0])
        pinned = not number or (
            card is not None and _norm_num(card.number) == _norm_num(number)
        )
        if pinned:
            return hit
    product = tcgplayer_art(conn, name, set_name, number)
    if product and hit:
        # The Japanese catalog already stores TCGplayer product photos, so when
        # both routes name the same product keep the catalog's entry — no second
        # copy of one picture under a different cache key.
        if tcgplayer.product_id(hit[1]) == tcgplayer.product_id(product[1]):
            return hit
    return product or hit


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
