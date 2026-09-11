"""Card art for the published snapshot, as one small zip (ticket 73, phase 7).

A phone at a card show has no usable connectivity, so art has to travel *with*
the snapshot rather than stream on arrival. Measured on the real ledger: the 969
in-stock rows resolve to 770 distinct ``card_id``s totalling **77 MB** of
full-size PNG at 103 KB each. Re-encoded to 200 px JPEG that is **14.7 MB**
(measured, 768 cards) — worth carrying, where 77 MB is not.

**One zip, not 770 objects.** 770 uploads at ~200 ms each is over two minutes of
round trips; one object is a single PUT. The zip is stored, not deflated: JPEG
does not compress again, and asking it to only burns CPU.

**Uploaded only when it changed.** The snapshot's content hash moves with every
price, and the daily task refreshes prices, so gating art on that hash would
re-upload ~15 MB nightly for nothing. :func:`art_fingerprint` hashes the set of
cached owned ids instead — the exact contents of the zip — and an unchanged
fingerprint carries the previous object forward.

**Ids are resolved once per distinct card, not once per lot.** Duplicate copies
share a lookup. It is still a lookup per distinct card rather than one bulk
query; see :func:`owned_card_ids` for why that stays a separate change.

This is the only module in the snapshot path that imports PIL, deliberately:
:mod:`tcginventory.services.snapshot` stays free of the vision stack's
dependencies so the ledger projection can never acquire them by accident.

Qt-free.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Callable

from tcginventory.services import cardimages
from tcginventory.services.inventory import InventoryService

log = logging.getLogger(__name__)

THUMB_WIDTH = 200
JPEG_QUALITY = 82
MANIFEST_NAME = "manifest.json"


def owned_card_ids(conn, resolve: Callable | None = None) -> list[str]:
    """Distinct ``card_id``s for the lots currently on hand.

    Resolution goes through ``cardimages.art_for`` **once per distinct printed
    identity** — ``(name, set, number)`` — not once per lot, so duplicate copies
    of a card cost one lookup between them. It is still one catalog query per
    distinct card rather than one query for all of them: ``art_for`` carries
    fuzzy matching rules (set containment, number-or-name agreement, trusting a
    sole same-name printing) that a bulk query would have to reproduce exactly,
    and that is a change to make on its own, with its own tests, not inside the
    publish job. Measured at ~0.7 s for 969 rows on the real ledger, on a worker
    thread.

    Sold and missing lots are skipped: their art is dead weight in a bundle whose
    whole reason for existing is to be small enough to carry.
    """
    resolve = resolve or cardimages.art_for
    identities: dict[tuple, None] = {}
    for row in InventoryService(conn).inventory_rows():
        if row.in_stock:
            item = row.item
            identities.setdefault((item.name, item.set_name, item.card_number), None)
    seen: dict[str, None] = {}
    for name, set_name, number in identities:
        hit = resolve(conn, name, set_name, number)
        if hit:
            seen.setdefault(hit[0], None)
    return list(seen)


def art_fingerprint(conn, resolve: Callable | None = None) -> str:
    """Identity of what the bundle *would* contain, without building it.

    The snapshot's content hash moves whenever a price does, and the daily task
    refreshes prices — so gating art on that hash re-encodes and re-uploads ~15 MB
    every night for a bundle that has not changed. This hashes the sorted set of
    owned ids that are actually cached, which is exactly the set of images that
    would land in the zip: unchanged fingerprint, unchanged bundle, and the
    publish carries the previous object forward instead of uploading it again.

    Cached-ness is part of it on purpose. A card whose art arrived in the cache
    since the last publish should get into the next bundle even though the set
    of owned cards did not move.
    """
    import hashlib

    ids = sorted(
        i for i in owned_card_ids(conn, resolve) if cardimages.cache_path(i).exists()
    )
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


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
