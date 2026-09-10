"""Sealing and opening published snapshots (headless, Qt-free).

The happy path is the least interesting thing here. AES-GCM is chosen for
*authentication*, so the tests that matter are the ones proving a wrong key, a
flipped byte or a truncated file fail to open rather than yielding plausible
nonsense.
"""

from __future__ import annotations

import pytest

from tcginventory.services import snapcrypto
from tcginventory.services.exceptions import SnapshotDecryptError

PLAIN = b'{"schema": 1, "rows": [{"purchase_price_cents": 1234}]}'


def test_round_trip_returns_the_original_bytes():
    key = snapcrypto.new_key()
    assert snapcrypto.unseal(key, snapcrypto.seal(key, PLAIN)) == PLAIN


def test_empty_payload_round_trips():
    """The smallest legal blob is still a blob — no special-casing on the way in."""
    key = snapcrypto.new_key()
    assert snapcrypto.unseal(key, snapcrypto.seal(key, b"")) == b""


def test_the_same_plaintext_seals_differently_every_time():
    """A fresh nonce per seal is what makes one key safe across many publishes.
    Equal output would mean a reused nonce, which is the way to lose the key."""
    key = snapcrypto.new_key()
    assert snapcrypto.seal(key, PLAIN) != snapcrypto.seal(key, PLAIN)


def test_a_wrong_key_does_not_open_it():
    blob = snapcrypto.seal(snapcrypto.new_key(), PLAIN)
    with pytest.raises(SnapshotDecryptError):
        snapcrypto.unseal(snapcrypto.new_key(), blob)


def test_a_flipped_ciphertext_byte_is_rejected():
    """The point of GCM: altered data fails loudly instead of decoding to a
    ledger with different numbers in it."""
    key = snapcrypto.new_key()
    blob = bytearray(snapcrypto.seal(key, PLAIN))
    blob[-20] ^= 0x01  # inside the ciphertext, clear of magic and nonce
    with pytest.raises(SnapshotDecryptError):
        snapcrypto.unseal(key, bytes(blob))


def test_a_flipped_tag_byte_is_rejected():
    key = snapcrypto.new_key()
    blob = bytearray(snapcrypto.seal(key, PLAIN))
    blob[-1] ^= 0x01  # the GCM tag itself
    with pytest.raises(SnapshotDecryptError):
        snapcrypto.unseal(key, bytes(blob))


def test_a_flipped_nonce_byte_is_rejected():
    key = snapcrypto.new_key()
    blob = bytearray(snapcrypto.seal(key, PLAIN))
    blob[len(snapcrypto.MAGIC)] ^= 0x01
    with pytest.raises(SnapshotDecryptError):
        snapcrypto.unseal(key, bytes(blob))


def test_a_truncated_blob_is_rejected_rather_than_indexed_into():
    key = snapcrypto.new_key()
    blob = snapcrypto.seal(key, PLAIN)
    with pytest.raises(SnapshotDecryptError):
        snapcrypto.unseal(key, blob[:10])


def test_a_foreign_format_is_rejected_on_the_magic():
    """A future format must fail here rather than have its header handed to AES
    as though it were a nonce."""
    key = snapcrypto.new_key()
    blob = b"TCGI2" + snapcrypto.seal(key, PLAIN)[len(snapcrypto.MAGIC) :]
    with pytest.raises(SnapshotDecryptError):
        snapcrypto.unseal(key, blob)


def test_plaintext_does_not_survive_in_the_sealed_bytes():
    """Cheap belt-and-braces against someone 'optimising' seal into a no-op."""
    key = snapcrypto.new_key()
    assert b"purchase_price_cents" not in snapcrypto.seal(key, PLAIN)


@pytest.mark.parametrize("bad", [b"", b"x" * 16, b"x" * 31, b"x" * 33])
def test_a_wrong_sized_key_is_refused_on_both_paths(bad):
    good = snapcrypto.new_key()
    with pytest.raises(ValueError):
        snapcrypto.seal(bad, PLAIN)
    with pytest.raises(ValueError):
        snapcrypto.unseal(bad, snapcrypto.seal(good, PLAIN))


def test_new_key_is_the_advertised_width():
    assert len(snapcrypto.new_key()) == snapcrypto.KEY_BYTES
    assert snapcrypto.new_key() != snapcrypto.new_key()
