"""Card art for the published snapshot, as one small zip (ticket 73, phase 7).

A phone at a card show has no usable connectivity, so art has to travel *with*
the snapshot rather than stream on arrival. Measured on the real ledger: the 969
in-stock rows resolve to 770 distinct ``card_id``s totalling **77 MB** of
full-size PNG at 103 KB each. Re-encoded to a ~200 px JPEG that is roughly 10 MB
— worth carrying, where 77 MB is not.

**One zip, not 770 objects.** 770 uploads at ~200 ms each is over two minutes of
round trips; one object is a single PUT. The zip is stored, not deflated: JPEG
does not compress again, and asking it to only burns CPU.

**Ids are resolved in one pass.** ``ui/views/inventory.py`` calls
``cardimages.art_for()`` per row, which costs 711 ms across 969 rows. That shape
is not copied here — the ids are collected once, deduplicated, and only then
turned into files.

This is the only module in the snapshot path that imports PIL, deliberately:
:mod:`tcginventory.services.snapshot` stays free of the vision stack's
dependencies so the ledger projection can never acquire them by accident.

Qt-free.
"""

from __future__ import annotations

import io
import logging
import zipfile

from tcginventory.services import cardimages
from tcginventory.services.inventory import InventoryService

log = logging.getLogger(__name__)

THUMB_WIDTH = 200
JPEG_QUALITY = 82
MANIFEST_NAME = "manifest.json"


def owned_card_ids(conn) -> list[str]:
    """Distinct ``card_id``s for the lots currently on hand, resolved once.

    Sold and missing lots are skipped: their art is dead weight in a bundle whose
    whole reason for existing is to be small enough to carry.
    """
    seen: dict[str, None] = {}
    for row in InventoryService(conn).inventory_rows():
        if not row.in_stock:
            continue
        item = row.item
        hit = cardimages.art_for(conn, item.name, item.set_name, item.card_number)
        if hit:
            seen.setdefault(hit[0], None)
    return list(seen)


def _thumbnail(path, width: int = THUMB_WIDTH) -> bytes | None:
    """Re-encode one cached image to a small JPEG, or ``None`` if unreadable.

    Best-effort by design: one corrupt file in the cache must not cost the whole
    bundle, and a missing thumbnail costs the phone a placeholder rather than a
    wrong row.
    """
    from PIL import Image

    try:
        with Image.open(path) as img:
            # Palette images carrying transparency warn (and lose the alpha) on a
            # direct RGB convert; going via RGBA composites it away quietly.
            if img.mode in ("P", "PA") and "transparency" in img.info:
                img = img.convert("RGBA")
            img = img.convert("RGB")
            if img.width > width:
                height = round(img.height * width / img.width)
                img = img.resize((width, height), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except Exception:
        log.debug("could not re-encode %s for the art bundle", path, exc_info=True)
        return None


def build_bundle(conn, *, width: int = THUMB_WIDTH) -> bytes | None:
    """A zip of thumbnails for everything on hand, or ``None`` if there is none.

    Only images already in the local cache are used — the bundle never reaches
    out to the network. A card whose art has not been fetched yet is simply
    absent, and appears once the desktop has had reason to fetch it.
    """
    import json

    ids = owned_card_ids(conn)
    if not ids:
        return None

    buffer = io.BytesIO()
    included: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for card_id in sorted(ids):
            path = cardimages.cache_path(card_id)
            if not path.exists():
                continue
            thumb = _thumbnail(path, width)
            if thumb is None:
                continue
            # ZIP_STORED: JPEG will not compress again, so deflating only costs
            # CPU. A fixed date keeps the archive byte-identical run to run.
            info = zipfile.ZipInfo(f"{card_id}.jpg", date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, thumb)
            included.append(card_id)
        if not included:
            return None
        manifest = zipfile.ZipInfo(MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
        archive.writestr(
            manifest,
            json.dumps(
                {"width": width, "count": len(included), "card_ids": included},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    return buffer.getvalue()


def bundle_card_ids(blob: bytes) -> list[str]:
    """The ids inside a bundle — the inverse used by tests and verification."""
    import json

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return json.loads(archive.read(MANIFEST_NAME))["card_ids"]
