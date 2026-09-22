"""The card-art bundle that travels with a snapshot (headless, Qt-free).

Never touches the network: only images already in the local cache are used, and
these tests write that cache themselves.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from conftest import buy, sell
from tcginventory.services import artbundle, cardimages

DAY = "2026-03-01T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))


def _cache_png(card_id: str, size=(600, 830)) -> None:
    """Write a real PNG into the image cache for ``card_id``."""
    from PIL import Image

    path = cardimages.cache_path(card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 60, 120)).save(path, format="PNG")


def _fake_resolver(mapping):
    """Stand in for cardimages.art_for, which needs catalog rows we don't have."""

    def art_for(conn, name, set_name, number):
        hit = mapping.get(name)
        return (hit, f"https://example.test/{hit}.png") if hit else None

    return art_for


# --------------------------------------------------------------------------
# Thumbnailing
# --------------------------------------------------------------------------


def test_a_thumbnail_is_jpeg_and_scaled_down():
    """77 MB of full-size PNG is not carryable; ~200px JPEG is the whole point."""
    from PIL import Image

    _cache_png("sv9-163")
    thumb = artbundle._thumbnail(cardimages.cache_path("sv9-163"))

    assert thumb is not None
    with Image.open(io.BytesIO(thumb)) as img:
        assert img.format == "JPEG"
        assert img.width == artbundle.THUMB_WIDTH
        assert img.height == round(830 * artbundle.THUMB_WIDTH / 600)
    assert len(thumb) < cardimages.cache_path("sv9-163").stat().st_size


def test_an_image_smaller_than_the_target_is_not_upscaled():
    from PIL import Image

    _cache_png("tiny-1", size=(80, 110))
    with Image.open(
        io.BytesIO(artbundle._thumbnail(cardimages.cache_path("tiny-1")))
    ) as img:
        assert img.width == 80


def test_a_corrupt_cached_file_is_skipped_not_fatal():
    """One bad file in a 770-image cache must not cost the whole bundle."""
    path = cardimages.cache_path("broken-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a png")
    assert artbundle._thumbnail(path) is None


# --------------------------------------------------------------------------
# Bundling
# --------------------------------------------------------------------------


def test_a_bundle_contains_a_thumbnail_and_a_manifest_per_owned_card(
    conn, svc, monkeypatch
):
    buy(svc, "Charizard", 100, 100, DAY)
    buy(svc, "Blastoise", 100, 100, DAY)
    monkeypatch.setattr(
        cardimages,
        "art_for",
        _fake_resolver({"Charizard": "base1-4", "Blastoise": "base1-2"}),
    )
    _cache_png("base1-4")
    _cache_png("base1-2")

    blob = artbundle.build_bundle(conn)
    assert blob is not None
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        assert names == {"base1-4.jpg", "base1-2.jpg", artbundle.MANIFEST_NAME}
        manifest = json.loads(archive.read(artbundle.MANIFEST_NAME))
    assert manifest["count"] == 2
    assert sorted(manifest["card_ids"]) == ["base1-2", "base1-4"]
    assert manifest["width"] == artbundle.THUMB_WIDTH
    assert artbundle.bundle_card_ids(blob) == manifest["card_ids"]


def test_a_card_with_no_cached_image_is_simply_absent(conn, svc, monkeypatch):
    """The bundle never reaches out to the network; art appears once the desktop
    has had reason to fetch it."""
    buy(svc, "Charizard", 100, 100, DAY)
    buy(svc, "Blastoise", 100, 100, DAY)
    monkeypatch.setattr(
        cardimages,
        "art_for",
        _fake_resolver({"Charizard": "base1-4", "Blastoise": "base1-2"}),
    )
    _cache_png("base1-4")  # only one of the two is cached

    assert artbundle.bundle_card_ids(artbundle.build_bundle(conn)) == ["base1-4"]


def test_sold_lots_do_not_carry_their_art(conn, svc, monkeypatch):
    """Dead weight in a bundle whose reason for existing is to be small."""
    result = buy(svc, "Charizard", 100, 120, DAY)
    sell(svc, result.lots[0].item_id, 1, 200, "2026-03-05T12:00:00+00:00")
    monkeypatch.setattr(cardimages, "art_for", _fake_resolver({"Charizard": "base1-4"}))
    _cache_png("base1-4")

    assert artbundle.owned_card_ids(conn) == []
    assert artbundle.build_bundle(conn) is None


def test_duplicate_copies_contribute_one_image(conn, svc, monkeypatch):
    """Two lots of the same card share one picture -- resolving per row and
    zipping per row would carry it twice."""
    buy(svc, "Charizard", 100, 100, DAY)
    buy(svc, "Charizard", 120, 120, "2026-03-02T12:00:00+00:00")
    monkeypatch.setattr(cardimages, "art_for", _fake_resolver({"Charizard": "base1-4"}))
    _cache_png("base1-4")

    assert artbundle.owned_card_ids(conn) == ["base1-4"]
    assert artbundle.bundle_card_ids(artbundle.build_bundle(conn)) == ["base1-4"]


def test_an_empty_inventory_produces_no_bundle(conn):
    assert artbundle.build_bundle(conn) is None


def test_the_bundle_is_byte_identical_across_runs(conn, svc, monkeypatch):
    """Zip stamps the current time into each entry by default, which would make
    every publish differ and defeat the unchanged-ledger check."""
    buy(svc, "Charizard", 100, 100, DAY)
    monkeypatch.setattr(cardimages, "art_for", _fake_resolver({"Charizard": "base1-4"}))
    _cache_png("base1-4")

    assert artbundle.build_bundle(conn) == artbundle.build_bundle(conn)


def test_entries_are_stored_not_deflated(conn, svc, monkeypatch):
    """JPEG does not compress again; asking zip to try only burns CPU."""
    buy(svc, "Charizard", 100, 100, DAY)
    monkeypatch.setattr(cardimages, "art_for", _fake_resolver({"Charizard": "base1-4"}))
    _cache_png("base1-4")

    with zipfile.ZipFile(io.BytesIO(artbundle.build_bundle(conn))) as archive:
        for info in archive.infolist():
            assert info.compress_type == zipfile.ZIP_STORED


# --------------------------------------------------------------------------
# Resolution cost, and the fingerprint that gates re-uploading
# --------------------------------------------------------------------------


def test_each_distinct_card_is_resolved_once_not_once_per_copy(conn, svc):
    """Three lots, two distinct printed identities -> two lookups. The per-row
    shape from ui/views/inventory.py:267 would make three."""
    buy(svc, "Charizard", 100, 100, DAY, set_name="Base Set", card_number="4/102")
    buy(
        svc,
        "Charizard",
        120,
        120,
        "2026-03-02T12:00:00+00:00",
        set_name="Base Set",
        card_number="4/102",
    )
    buy(svc, "Blastoise", 100, 100, DAY, set_name="Base Set", card_number="2/102")

    calls = []

    def counting(conn_, name, set_name, number):
        calls.append((name, set_name, number))
        return (f"base1-{number.split('/')[0]}", "https://example.test/x.png")

    ids = artbundle.owned_card_ids(conn, resolve=counting)
    assert sorted(ids) == ["base1-2", "base1-4"]
    assert len(calls) == 2, f"expected one lookup per distinct card, got {len(calls)}"


def test_the_fingerprint_covers_only_cached_owned_art(conn, svc, monkeypatch):
    buy(svc, "Charizard", 100, 100, DAY)
    buy(svc, "Blastoise", 100, 100, DAY)
    monkeypatch.setattr(
        cardimages,
        "art_for",
        _fake_resolver({"Charizard": "base1-4", "Blastoise": "base1-2"}),
    )
    _cache_png("base1-4")
    only_one = artbundle.art_fingerprint(conn)

    # Owning the same cards but with a second image now cached is a different
    # bundle, so it must be a different fingerprint.
    _cache_png("base1-2")
    assert artbundle.art_fingerprint(conn) != only_one
    # ...and rebuilding with nothing changed is stable.
    assert artbundle.art_fingerprint(conn) == artbundle.art_fingerprint(conn)


def test_the_fingerprint_ignores_prices(conn, svc, monkeypatch):
    """The whole reason it exists: the nightly price refresh must not look like
    a change to the art."""
    result = buy(svc, "Charizard", 100, 100, DAY)
    monkeypatch.setattr(cardimages, "art_for", _fake_resolver({"Charizard": "base1-4"}))
    _cache_png("base1-4")
    before = artbundle.art_fingerprint(conn)

    conn.execute(
        "UPDATE item SET review = 1, market_value_cents = 99999 WHERE id = ?",
        (result.lots[0].item_id,),
    )
    conn.commit()
    assert artbundle.art_fingerprint(conn) == before
