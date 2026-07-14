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
