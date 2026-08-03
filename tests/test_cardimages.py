"""Lazy card-image cache + catalog image-by-identity lookup (offline)."""

from __future__ import annotations

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.services import cardimages
from tcginventory.services.catalog import update_catalog

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-bytes"

# TCGdex shape: /sets index + /sets/{id} detail. ``image`` is an asset base URL;
# the catalog appends "/low.png". Some cards omit the image entirely.
_SETS = [{"id": "sv9", "name": "Journey Together"}]
_SET_DETAIL = {
    "sv9": {
        "id": "sv9", "name": "Journey Together",
        "abbreviation": {"official": "JTG"},
        "cardCount": {"official": 159, "total": 159},
        "cards": [
            {"id": "sv9-163", "localId": "163", "name": "Iono's Kilowattrel",
             "image": "https://assets.tcgdex.net/en/sv/sv9/163"},
            # A card with no image (dataset occasionally omits it).
            {"id": "sv9-1", "localId": "001", "name": "Pikachu", "image": None},
        ],
    },
}

_KILO_IMG = "https://assets.tcgdex.net/en/sv/sv9/163/low.png"


def _fetch(url: str):
    if url.endswith("/sets"):
        return _SETS
    if url.endswith("/sets/sv9"):
        return _SET_DETAIL["sv9"]
    raise AssertionError(f"unexpected url: {url}")


def test_catalog_stores_small_image_url(conn):
    update_catalog(conn, fetch_json=_fetch)
    (kilo,) = CardCatalogRepository(conn).find_by_name("Iono's Kilowattrel")
    assert kilo.image_small == _KILO_IMG


def test_image_for_matches_by_identity(conn):
    update_catalog(conn, fetch_json=_fetch)
    repo = CardCatalogRepository(conn)

    hit = repo.image_for("Iono's Kilowattrel", "Journey Together", "163/159")
    assert hit == ("sv9-163", _KILO_IMG)

    # A card whose only printing has no image yields nothing to preview.
    assert repo.image_for("Pikachu", "Journey Together", "1") is None
    assert repo.image_for("Nonexistent Card") is None


def test_image_for_keys_on_set_and_number_not_exact_name(conn):
    """Regression: a human-typed name ("Clefable EX") must resolve to the right
    printing by set + collector number, never fall through to a wrong-set card
    that happens to match the name literally."""
    from tcginventory.domain.models import CatalogCard

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="me3-119", name="Mega Clefable ex", set_id="me3",
                set_name="Perfect Order", number="119",
                image_small="https://img/me3-119",
            ),
            CatalogCard(
                card_id="ex6-106", name="Clefable ex", set_id="ex6",
                set_name="FireRed & LeafGreen", number="106",
                image_small="https://img/ex6-106",
            ),
            CatalogCard(
                card_id="sv3-82", name="Clefable ex", set_id="sv3",
                set_name="Obsidian Flames", number="82",
                image_small="https://img/sv3-82",
            ),
            # Mewtwo #14 in a WoTC promo set (set total 117) — a decoy for a
            # card whose real set ("Trading Card Game Classic", /034) the dataset
            # does not carry.
            CatalogCard(
                card_id="basep-14", name="Mewtwo", set_id="basep",
                set_name="Wizards Black Star Promos", number="14",
                printed_total=117, image_small="https://img/basep-14",
            ),
            # Alt-art Jirachi-GX filed under its base set (Unified Minds, /236);
            # hyphen vs space in the name, unique number "79a".
            CatalogCard(
                card_id="sm11-79a", name="Jirachi-GX", set_id="sm11",
                set_name="Unified Minds", number="79a",
                printed_total=236, image_small="https://img/sm11-79a",
            ),
            # Frosmoth #43 in the base set "Mega Evolution" — a decoy for a
            # "Mega Evolution Promo" query (the base set name is a substring).
            CatalogCard(
                card_id="me1-43", name="Frosmoth", set_id="me1",
                set_name="Mega Evolution", number="43",
                printed_total=132, image_small="https://img/me1-43",
            ),
        ],
        "t",
    )
    repo = CardCatalogRepository(conn)

    # set + number pins the exact printing regardless of the typed name form.
    for nm in ("Mega Clefable ex", "Clefable EX", "Clefable ex"):
        assert repo.image_for(nm, "Perfect Order", "119/088")[0] == "me3-119"
    # a slightly different set spelling still matches (containment).
    assert repo.image_for("Clefable ex", "Order", "119")[0] == "me3-119"
    # number + fuzzy name works even with no set given.
    assert repo.image_for("Clefable ex", None, "119")[0] == "me3-119"
    # a bare number with an unrelated name is not trusted → no wrong card.
    assert repo.image_for("Totally Unrelated", None, "106") is None

    # Alt-art under a non-catalog set name still resolves: the "/236"
    # denominator matches Unified Minds' total, and name ignores the hyphen.
    assert repo.image_for("Jirachi GX", "Alternate Art Promos", "79a/236")[0] == (
        "sm11-79a"
    )
    assert repo.image_for("Jirachi-GX", "Unified Minds", "79a")[0] == "sm11-79a"

    # The real set is absent AND the "/034" denominator matches no candidate's
    # total → show NOTHING, never the WoTC promo Mewtwo #14 (whose set is /117).
    assert repo.image_for("Mewtwo", "Trading Card Game Classic", "014/034") is None
    # …but when the set genuinely is the promo set, it resolves.
    assert repo.image_for("Mewtwo", "Wizards Black Star Promos", "14")[0] == "basep-14"

    # A containment set match ("Mega Evolution" ⊂ "Mega Evolution Promo") must
    # NOT borrow a different card by number alone — Rowlet is not Frosmoth.
    assert repo.image_for("Rowlet", "Mega Evolution Promo", "043") is None
    # The exact base set still resolves its own card.
    assert repo.image_for("Frosmoth", "Mega Evolution", "43")[0] == "me1-43"


def test_ensure_cached_fetches_once_then_serves_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    calls = {"n": 0}

    def fake_download(url: str) -> bytes:
        calls["n"] += 1
        return _PNG

    path = cardimages.ensure_cached("sv9-163", "http://x/y.png", fake_download)
    assert path is not None and path.exists()
    assert path.read_bytes() == _PNG
    assert cardimages.is_cached("sv9-163")

    # Second request is a cache hit — no second download.
    again = cardimages.ensure_cached("sv9-163", "http://x/y.png", fake_download)
    assert again == path
    assert calls["n"] == 1


def test_ensure_cached_swallows_fetch_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))

    def boom(url: str) -> bytes:
        raise RuntimeError("offline")

    assert cardimages.ensure_cached("x-1", "http://x/y.png", boom) is None
    assert not cardimages.is_cached("x-1")


def test_image_for_reconciles_legacy_set_name(conn):
    """An item saved under the old promo name still resolves after the catalog
    renamed the set (Scarlet & Violet Black Star Promos -> SVP Black Star Promos)."""
    from tcginventory.data.setnames import canonical_set_name
    from tcginventory.domain.models import CatalogCard

    assert canonical_set_name("Scarlet & Violet Black Star Promos") == (
        "SVP Black Star Promos"
    )
    assert canonical_set_name("SVP Black Star Promos") is None  # already canonical

    CardCatalogRepository(conn).replace_all(
        [
            CatalogCard(
                card_id="svp-056", name="Charizard ex", set_id="svp",
                set_name="SVP Black Star Promos", number="56",
                image_small="https://img/svp-056",
            ),
        ],
        "t",
    )
    repo = CardCatalogRepository(conn)
    # Old name resolves via the alias; new name resolves directly.
    assert repo.image_for("Charizard ex", "Scarlet & Violet Black Star Promos",
                          "56/102") == ("svp-056", "https://img/svp-056")
    assert repo.image_for("Charizard ex", "SVP Black Star Promos", "56") == (
        "svp-056", "https://img/svp-056",
    )
