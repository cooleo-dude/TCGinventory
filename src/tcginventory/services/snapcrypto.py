"""Authenticated encryption for published snapshots (ticket 74).

A snapshot carries what you paid for every card, what it is worth now, where it
is stored and the cert numbers of anything graded. Publishing that to an object
store means it sits on someone else's disk, so it is sealed here first and the
provider holds ciphertext it cannot read.

**AES-256-GCM**, via ``cryptography``. There is no AES in the standard library,
and GCM is chosen over a bare cipher because the interesting failure is not
someone reading a snapshot — it is someone *changing* one. GCM authenticates, so
a flipped byte fails to open rather than decoding into a plausible ledger with
the wrong numbers in it.

Wire format, deliberately boring::

    TCGI1  ‖  nonce (12 bytes)  ‖  ciphertext ‖ tag

The 5-byte magic is a version marker. It is checked on open so that a future
format change fails loudly on old readers instead of being handed to AES as if
it were a nonce. 12 bytes is the GCM-native nonce width; it is random per seal,
and a fresh one every time is what makes reusing one key across thousands of
publishes safe.

Losing the key is survivable, which is worth stating because it is unusual: a
snapshot is a *derived* projection of the ledger, so a lost key costs a
republish, not data. The key is a convenience to protect, not a secret to fear
losing.

Qt-free.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tcginventory.services.exceptions import SnapshotDecryptError

MAGIC = b"TCGI1"
NONCE_BYTES = 12
KEY_BYTES = 32  # AES-256

# Smallest possible sealed blob: magic + nonce + the 16-byte GCM tag over an
# empty plaintext. Anything shorter cannot be one, and saying so here keeps the
# check in ``unseal`` from looking like a magic number.
_MIN_SEALED = len(MAGIC) + NONCE_BYTES + 16


def new_key() -> bytes:
    """Mint a fresh 256-bit key from the OS CSPRNG."""
    return AESGCM.generate_key(bit_length=KEY_BYTES * 8)


def seal(key: bytes, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext``, returning ``MAGIC ‖ nonce ‖ ciphertext‖tag``."""
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    nonce = os.urandom(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def unseal(key: bytes, blob: bytes) -> bytes:
    """Decrypt a blob written by :func:`seal`.

    Raises :class:`SnapshotDecryptError` for a wrong key, a truncated blob, a
    tampered one, or an unrecognised format marker — one error for all of them,
    because distinguishing them only helps someone guessing.
    """
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    if len(blob) < _MIN_SEALED or not blob.startswith(MAGIC):
        raise SnapshotDecryptError("not a snapshot payload, or it is truncated")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    try:
        return AESGCM(key).decrypt(nonce, blob[len(MAGIC) + NONCE_BYTES :], None)
    except InvalidTag as exc:
        raise SnapshotDecryptError(
            "could not decrypt the snapshot — wrong key, or the data was altered"
        ) from exc
