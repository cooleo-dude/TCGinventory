"""SKU generation (Qt-free).

A SKU identifies an acquired **copy** of a card for quick lookup while selling or
trading. Format::

    {NAME3}-{SET}-{NUMBER}-{COND|GRADE}-{LANG}-{NNNN}

e.g. ``CHA-CPA-074-NM-EN-0001``. The trailing sequence counts prior copies of the
*same identity* (name + set + number + condition/grade + language), so a NM copy
sequences independently from an LP copy. SKUs live on the **lot** and are
generated when a card is acquired.
"""

from __future__ import annotations

import re
import sqlite3

from tcginventory.data.repositories import ItemRepository, LotRepository
from tcginventory.domain.models import Item


def _seg(text: str | None, limit: int | None = None) -> str:
    """Uppercased alphanumeric slug of ``text`` (optionally truncated)."""
    slug = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    return slug[:limit] if limit else slug


def make_base(item: Item) -> str:
    """The identity-derived SKU prefix (everything before the sequence)."""
    name = _seg(item.name, 3) or "ITM"
    if item.set_code:
        set_seg = _seg(item.set_code)
    elif item.set_name:
        set_seg = _seg(item.set_name, 3)
    else:
        set_seg = "NA"
    number = _seg((item.card_number or "").split("/")[0]) or "NA"
    if item.is_graded:
        company = item.grading_company.value if item.grading_company else "GR"
        cond_grade = _seg(f"{company}{item.grade or ''}") or "GR"
    elif item.condition:
        cond_grade = _seg(item.condition.value)
    else:
        cond_grade = "NA"
    lang = item.language.value
    return f"{name}-{set_seg}-{number}-{cond_grade}-{lang}"


def generate_sku(conn: sqlite3.Connection, item: Item) -> str:
    """Return the next unique SKU for a new copy of ``item``.

    Sequence starts at the item's existing copy count + 1; if the resulting SKU
    already exists (rare cross-identity collision after name truncation), it is
    bumped until free so the value is globally unique.
    """
    lots = LotRepository(conn)
    base = make_base(item)
    seq = lots.distinct_sku_count(item.id) + 1
    while lots.exists_sku(f"{base}-{seq:04d}"):
        seq += 1
    return f"{base}-{seq:04d}"


def backfill_skus(conn: sqlite3.Connection) -> int:
    """Assign SKUs to any lots missing one, in acquisition order. Returns count."""
    lots = LotRepository(conn)
    items = ItemRepository(conn)
    pending = conn.execute(
        "SELECT id FROM lot WHERE sku IS NULL ORDER BY acquired_at, id"
    ).fetchall()
    if not pending:
        return 0
    for row in pending:
        lot = lots.get(row["id"])
        item = items.get(lot.item_id)
        lot.sku = generate_sku(conn, item)
        lots.update(lot)
    conn.commit()
    return len(pending)
