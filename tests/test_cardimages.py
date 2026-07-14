"""Lazy card-image cache + catalog image-by-identity lookup (offline)."""

from __future__ import annotations

from tcginventory.data.repositories import CardCatalogRepository
from tcginventory.services import cardimages
from tcginventory.services.catalog import update_catalog

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-bytes"

_SETS = [{"id": "sv9", "name": "Journey Together", "ptcgoCode": "JTG"}]
_CARDS = {
    "sv9": [
        {
            "id": "sv9-163",
            "name": "Iono's Kilowattrel",
            "number": "163",
            "supertype": "Pokémon",
            "images": {
                "small": "https://images.pokemontcg.io/sv9/163.png",
                "large": "https://images.pokemontcg.io/sv9/163_hires.png",
            },
        },
        # A card with no image (defensive: dataset occasionally omits it).
        {"id": "sv9-1", "name": "Pikachu", "number": "1", "supertype": "Pokémon"},
    ],
}


def _fetch(url: str):
    if url.endswith("/sets/en.json"):
        return _SETS
    if url.endswith("/cards/en/sv9.json"):
        return _CARDS["sv9"]
    raise AssertionError(f"unexpected url: {url}")


def test_catalog_stores_small_image_url(conn):
    update_catalog(conn, fetch_json=_fetch)
    (kilo,) = CardCatalogRepository(conn).find_by_name("Iono's Kilowattrel")
    assert kilo.image_small == "https://images.pokemontcg.io/sv9/163.png"


def test_image_for_matches_by_identity(conn):
    update_catalog(conn, fetch_json=_fetch)
    repo = CardCatalogRepository(conn)

    hit = repo.image_for("Iono's Kilowattrel", "Journey Together", "163/159")
    assert hit == ("sv9-163", "https://images.pokemontcg.io/sv9/163.png")

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
