"""Publishing, retention and pairing (headless, Qt-free, no network).

The ordering guarantees are what these tests are really about: the pointer is
written last, retention never unpublishes what the pointer names, and a run that
would change nothing writes nothing.
"""

from __future__ import annotations

import gzip
import json

import pytest

from conftest import buy
from tcginventory.services import (
    pairing,
    s3,
    snapcrypto,
    snappublish,
    snapsecrets,
    snapshot,
)
from tcginventory.services.exceptions import SnapshotNotConfiguredError

DAY = "2026-03-01T12:00:00+00:00"


class FakeStore:
    """An in-memory object store with the ObjectStore surface.

    ``fail_on`` makes one key's PUT raise, which is how the "pointer is written
    last" guarantee is tested.
    """

    def __init__(self, fail_on: str | None = None):
        self.objects: dict[str, bytes] = {}
        self.fail_on = fail_on
        self.puts: list[str] = []
        self.deletes: list[str] = []

    def put(self, key, body, content_type="application/octet-stream"):
        if self.fail_on and self.fail_on in key:
            raise s3.S3Error(500, b"induced failure")
        self.puts.append(key)
        self.objects[key] = body

    def get(self, key):
        if key not in self.objects:
            raise s3.S3Error(404, b"<Error><Code>NoSuchKey</Code></Error>")
        return self.objects[key]

    def delete(self, key):
        self.deletes.append(key)
        self.objects.pop(key, None)

    def list(self, prefix=""):
        return sorted(k for k in self.objects if k.startswith(prefix))


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path, monkeypatch):
    """Never touch the real Credential Manager or the real prefs file."""
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    snapsecrets.set_backend(snapsecrets.MemoryBackend())
    snapsecrets.save_credentials(
        account_id="acct123",
        bucket="tcginventory-snapshots",
        access_key_id="AKIA_TEST",
        secret_access_key="secret_test",
    )
    yield
    snapsecrets.set_backend(None)


@pytest.fixture
def stocked(conn, svc):
    buy(svc, "Charizard", 12345, 15000, DAY, set_name="Base Set", card_number="4/102")
    buy(svc, "Blastoise", 5000, 6000, DAY, quantity=2, set_name="Base Set")
    return conn


def test_a_first_publish_writes_payload_then_pointer(stocked):
    store = FakeStore()
    result = snappublish.publish(stocked, store=store, include_art=False)

    assert result.published is True
    assert result.rows == 2
    # Ordering is the guarantee: the pointer is what makes a payload live.
    assert store.puts[-1].endswith("latest.json")
    assert len(store.puts) == 2


def test_the_published_payload_decrypts_to_the_ledger(stocked):
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)

    ledger_id = snapshot.build_snapshot(stocked)["ledger_id"]
    payload = snappublish.fetch_and_verify(store, ledger_id)
    assert payload["counts"]["rows"] == 2
    assert {r["name"] for r in payload["rows"]} == {"Charizard", "Blastoise"}


def test_the_payload_in_the_bucket_is_not_readable_without_the_key(stocked):
    """The whole point of sealing it: purchase prices must not sit in the
    provider's storage in the clear."""
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)
    blob = next(v for k, v in store.objects.items() if k.endswith(".json.gz.enc"))

    assert b"Charizard" not in blob
    assert blob[:2] != b"\x1f\x8b"  # not raw gzip either
    with pytest.raises(gzip.BadGzipFile):
        snapshot.decode(blob)
    # ...but it opens with the real key.
    plain = snapcrypto.unseal(snapsecrets.encryption_key(), blob)
    assert snapshot.decode(plain)["counts"]["rows"] == 2


def test_the_pointer_is_plaintext_and_carries_no_ledger_data(stocked):
    """It is readable on purpose, so a client can decide before decrypting — so
    it must not leak anything worth encrypting."""
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)
    pointer = json.loads(next(v for k, v in store.objects.items() if "latest" in k))

    assert pointer["counts"]["rows"] == 2
    assert "Charizard" not in json.dumps(pointer)
    assert pointer["payload"]["key"].endswith(".json.gz.enc")


def test_republishing_an_unchanged_ledger_writes_nothing(stocked):
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)
    before = dict(store.objects)

    again = snappublish.publish(stocked, store=store, include_art=False)
    assert again.published is False
    assert "no change" in again.summary
    assert store.objects == before


def test_force_republishes_an_unchanged_ledger(stocked):
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)
    forced = snappublish.publish(stocked, store=store, force=True, include_art=False)
    assert forced.published is True


def test_a_changed_ledger_publishes_again(stocked, svc):
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)
    buy(svc, "Venusaur", 3000, 3500, DAY, set_name="Base Set")

    again = snappublish.publish(stocked, store=store, include_art=False)
    assert again.published is True
    assert again.rows == 3


def test_a_failed_payload_upload_leaves_the_previous_pointer_intact(stocked, svc):
    """A publish that dies half-way must leave the last good snapshot readable
    rather than a torn one."""
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=False)
    ledger_id = snapshot.build_snapshot(stocked)["ledger_id"]
    good = json.loads(store.objects[snappublish.pointer_key(ledger_id)])

    buy(svc, "Venusaur", 3000, 3500, DAY, set_name="Base Set")
    store.fail_on = ".json.gz.enc"
    with pytest.raises(s3.S3Error):
        snappublish.publish(stocked, store=store, include_art=False)

    still = json.loads(store.objects[snappublish.pointer_key(ledger_id)])
    assert still == good
    assert snappublish.fetch_and_verify(store, ledger_id)["counts"]["rows"] == 2


def test_publish_without_a_connection_says_so(stocked):
    snapsecrets.forget()
    with pytest.raises(SnapshotNotConfiguredError):
        snappublish.publish(stocked)


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def _pointer_for(ledger_id, key):
    return {"payload": {"key": key}, "art": None}


def test_prune_keeps_the_newest_and_deletes_the_oldest():
    store = FakeStore()
    ledger = "abc"
    keys = [f"{ledger}/inventory/2026090{i}-000000.json.gz.enc" for i in range(1, 9)]
    for k in keys:
        store.objects[k] = b"x"
    live = keys[-1]
    store.objects[snappublish.pointer_key(ledger)] = b"{}"

    deleted = snappublish.prune(store, ledger, _pointer_for(ledger, live), keep=3)

    assert deleted == 4  # 8 objects, the live one is excluded, keep 3 of the other 7
    remaining = sorted(k for k in store.objects if k.endswith(".enc"))
    assert live in remaining
    assert remaining == sorted(keys[-4:])


def test_prune_never_deletes_what_the_pointer_names():
    """Retention must not be able to unpublish the snapshot people are reading,
    however old it happens to be."""
    store = FakeStore()
    ledger = "abc"
    live = f"{ledger}/inventory/20260101-000000.json.gz.enc"  # the oldest by far
    store.objects[live] = b"x"
    for i in range(2, 9):
        store.objects[f"{ledger}/inventory/2026090{i}-000000.json.gz.enc"] = b"x"

    snappublish.prune(store, ledger, _pointer_for(ledger, live), keep=2)
    assert live in store.objects


def test_prune_sweeps_an_orphan_no_pointer_ever_named():
    """A transport error does not prove the write failed -- observed on live R2,
    where a crashed upload left a complete object behind."""
    store = FakeStore()
    ledger = "abc"
    live = f"{ledger}/inventory/20260908-000000.json.gz.enc"
    store.objects[live] = b"x"
    for i in range(1, 8):
        store.objects[f"{ledger}/inventory/2026080{i}-000000.json.gz.enc"] = b"orphan"

    deleted = snappublish.prune(store, ledger, _pointer_for(ledger, live), keep=2)
    assert deleted == 5
    assert live in store.objects


def test_prune_leaves_the_pointer_object_alone():
    store = FakeStore()
    ledger = "abc"
    store.objects[snappublish.pointer_key(ledger)] = b"{}"
    for i in range(1, 9):
        store.objects[f"{ledger}/inventory/2026090{i}-000000.json.gz.enc"] = b"x"

    snappublish.prune(store, ledger, _pointer_for(ledger, "none"), keep=1)
    assert snappublish.pointer_key(ledger) in store.objects


def test_prune_does_nothing_when_under_the_limit():
    store = FakeStore()
    ledger = "abc"
    store.objects[f"{ledger}/inventory/20260901-000000.json.gz.enc"] = b"x"
    assert snappublish.prune(store, ledger, _pointer_for(ledger, "none"), keep=10) == 0


def test_stamps_sort_chronologically():
    """Retention picks the oldest by sorting strings, so the format and the sort
    are coupled -- the same coupling data/backup.py relies on."""
    stamp = snappublish.stamp_for_key()
    assert len(stamp) == 15 and stamp[8] == "-"
    assert sorted(["20260910-000000", "20250101-235959"])[0] == "20250101-235959"


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


def test_pairing_payload_carries_everything_a_reader_needs(stocked):
    data = pairing.payload(stocked)
    assert set(data) == {
        "v",
        "payload_schema",
        "ledger_id",
        "endpoint",
        "bucket",
        "region",
        "access_key_id",
        "secret_access_key",
        "encryption_key",
    }
    assert data["bucket"] == "tcginventory-snapshots"
    assert data["ledger_id"] == snapshot.build_snapshot(stocked)["ledger_id"]


def test_pairing_prefers_an_explicit_read_only_credential(stocked):
    """The desktop stores a publish key; handing that to a phone would hand out
    write access to the ledger's snapshots."""
    data = pairing.payload(stocked, read_access_key_id="READONLY", read_secret="ro")
    assert data["access_key_id"] == "READONLY"
    assert data["secret_access_key"] == "ro"


def test_pairing_encodes_to_a_scannable_qr(stocked):
    svg = pairing.qr_svg(pairing.payload(stocked))
    assert svg.lstrip().startswith("<?xml") or svg.lstrip().startswith("<svg")
    assert "<path" in svg or "<rect" in svg


def test_the_qr_paints_its_own_light_background(stocked):
    """segno defaults to dark modules on a *transparent* background, which on
    this app's dark theme renders black-on-near-black and does not scan at all.
    Reverting the explicit ``light`` removes this path entirely."""
    svg = pairing.qr_svg(pairing.payload(stocked)).lower()
    assert 'fill="#fff"' in svg or 'fill="#ffffff"' in svg
    assert 'stroke="#000"' in svg or 'stroke="#000000"' in svg


def test_pairing_without_a_connection_says_so(stocked):
    snapsecrets.forget()
    with pytest.raises(LookupError):
        pairing.payload(stocked)


# --------------------------------------------------------------------------
# Art rides along only when it changed
# --------------------------------------------------------------------------


def _cache_png(card_id: str) -> None:
    from PIL import Image

    from tcginventory.services import cardimages

    path = cardimages.cache_path(card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (300, 420), (200, 40, 40)).save(path, format="PNG")


def _resolver(mapping):
    def art_for(conn, name, set_name, number):
        hit = mapping.get(name)
        return (hit, f"https://example.test/{hit}.png") if hit else None

    return art_for


def _art_puts(store):
    return [k for k in store.puts if "/art/" in k]


def _pointer(store, ledger_id):
    return json.loads(store.objects[snappublish.pointer_key(ledger_id)])


def _bump_price(conn, name, cents):
    item_id = conn.execute("SELECT id FROM item WHERE name = ?", (name,)).fetchone()[0]
    conn.execute(
        "UPDATE item SET review = 1, market_value_cents = ? WHERE id = ?",
        (cents, item_id),
    )
    conn.commit()


def test_a_price_only_change_republishes_the_rows_but_not_the_art(stocked, monkeypatch):
    """The daily task refreshes prices, and prices are in the content hash.
    Without a separate fingerprint that re-encoded and re-uploaded ~15 MB every
    night for a bundle that had not changed."""
    from tcginventory.services import cardimages

    monkeypatch.setattr(
        cardimages,
        "art_for",
        _resolver({"Charizard": "base1-4", "Blastoise": "base1-2"}),
    )
    _cache_png("base1-4")
    _cache_png("base1-2")
    store = FakeStore()
    ledger_id = snapshot.build_snapshot(stocked)["ledger_id"]

    first = snappublish.publish(stocked, store=store, include_art=True)
    assert first.art_bytes > 0 and first.art_reused is False
    assert len(_art_puts(store)) == 1
    first_art_key = _pointer(store, ledger_id)["art"]["key"]

    _bump_price(stocked, "Charizard", 77777)  # a price, and nothing else

    second = snappublish.publish(stocked, store=store, include_art=True)
    assert second.published is True  # the rows did change
    assert second.art_reused is True and second.art_bytes == 0
    assert "art unchanged" in second.summary
    assert len(_art_puts(store)) == 1, "the bundle was uploaded again"

    pointer = _pointer(store, ledger_id)
    assert pointer["art"]["key"] == first_art_key  # carried forward, still live
    assert pointer["art"]["fingerprint"]
    assert first_art_key in store.objects  # and retention left it alone


def test_owning_a_new_card_does_republish_the_art(stocked, svc, monkeypatch):
    from tcginventory.services import cardimages

    monkeypatch.setattr(
        cardimages,
        "art_for",
        _resolver(
            {"Charizard": "base1-4", "Blastoise": "base1-2", "Venusaur": "base1-15"}
        ),
    )
    _cache_png("base1-4")
    _cache_png("base1-2")
    store = FakeStore()
    snappublish.publish(stocked, store=store, include_art=True)
    assert len(_art_puts(store)) == 1

    buy(svc, "Venusaur", 3000, 3500, DAY, set_name="Base Set")
    _cache_png("base1-15")
    third = snappublish.publish(stocked, store=store, include_art=True)
    assert third.art_reused is False and third.art_bytes > 0
    assert len(_art_puts(store)) == 2


def test_a_carried_forward_bundle_survives_retention(stocked, monkeypatch):
    """Ten-plus price-only publishes later the pointer still names the original
    art object, so prune must never have deleted it."""
    from tcginventory.services import cardimages

    monkeypatch.setattr(cardimages, "art_for", _resolver({"Charizard": "base1-4"}))
    _cache_png("base1-4")
    store = FakeStore()
    ledger_id = snapshot.build_snapshot(stocked)["ledger_id"]
    snappublish.publish(stocked, store=store, include_art=True)
    art_key = _pointer(store, ledger_id)["art"]["key"]

    for cents in range(1, snappublish.KEEP + 4):
        _bump_price(stocked, "Charizard", cents * 100)
        snappublish.publish(stocked, store=store, include_art=True)

    assert len(_art_puts(store)) == 1
    assert art_key in store.objects
    assert _pointer(store, ledger_id)["art"]["key"] == art_key
