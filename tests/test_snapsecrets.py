"""Where the snapshot's credentials live (headless, Qt-free, no keychain).

Every test swaps in :class:`MemoryBackend`: nothing here may touch Windows
Credential Manager, and the suite has to pass on a machine that has no keychain
at all. The prefs file is real, under ``tmp_path`` — that is the file whose
contents matter, because it is the one people copy between machines.
"""

from __future__ import annotations

import base64
import json

import pytest

from tcginventory import config
from tcginventory.services import snapcrypto, snapsecrets

FULL = dict(
    account_id="acct123",
    bucket="tcginventory-snapshots",
    access_key_id="AKIA_TEST",
    secret_access_key="secret_test",
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    backend = snapsecrets.MemoryBackend()
    snapsecrets.set_backend(backend)
    yield backend
    snapsecrets.set_backend(None)


def test_nothing_is_configured_on_a_fresh_install():
    assert snapsecrets.load_credentials() is None
    assert snapsecrets.is_configured() is False


def test_save_puts_secrets_in_the_keychain_and_settings_in_prefs(_isolated):
    snapsecrets.save_credentials(**FULL)

    # Secrets went to the keychain, under the service name.
    store = _isolated._store
    assert store[(snapsecrets.SERVICE, snapsecrets.KEY_ACCESS_ID)] == "AKIA_TEST"
    assert store[(snapsecrets.SERVICE, snapsecrets.KEY_SECRET)] == "secret_test"

    # Settings went to the prefs file...
    prefs = json.loads(config.prefs_path().read_text(encoding="utf-8"))
    assert prefs[snapsecrets.PREF_ACCOUNT] == "acct123"
    assert prefs[snapsecrets.PREF_BUCKET] == "tcginventory-snapshots"

    # ...and no secret did. This is the file that ends up in backups and
    # screenshots; a key in it is a leak.
    text = config.prefs_path().read_text(encoding="utf-8")
    assert "AKIA_TEST" not in text
    assert "secret_test" not in text


def test_load_round_trips_a_full_configuration():
    snapsecrets.save_credentials(**FULL, endpoint="https://example.test/")
    creds = snapsecrets.load_credentials()
    assert creds is not None
    assert creds.account_id == "acct123"
    assert creds.bucket == "tcginventory-snapshots"
    assert creds.access_key_id == "AKIA_TEST"
    assert creds.secret_access_key == "secret_test"
    assert creds.endpoint == "https://example.test/"
    assert snapsecrets.is_configured() is True


@pytest.mark.parametrize(
    "missing",
    ["account_id", "bucket", "access_key_id", "secret_access_key"],
)
def test_a_partial_configuration_reads_as_not_connected(_isolated, missing):
    """Half a connection must not look like a connection -- it would fail deep
    inside a publish with a signature error, far from where it was typed."""
    snapsecrets.save_credentials(**FULL)
    # Knock one field out after the fact, the way a half-cleared prefs file or
    # a keychain entry removed by hand would.
    if missing in ("account_id", "bucket"):
        pref = (
            snapsecrets.PREF_ACCOUNT
            if missing == "account_id"
            else snapsecrets.PREF_BUCKET
        )
        config.set_pref(pref, None)
    else:
        key = (
            snapsecrets.KEY_ACCESS_ID
            if missing == "access_key_id"
            else snapsecrets.KEY_SECRET
        )
        _isolated.delete_password(snapsecrets.SERVICE, key)

    assert snapsecrets.load_credentials() is None
    assert snapsecrets.is_configured() is False


def test_encryption_key_is_absent_until_asked_to_create():
    assert snapsecrets.encryption_key(create=False) is None


def test_encryption_key_is_minted_once_and_read_back_from_base64(_isolated):
    first = snapsecrets.encryption_key()
    assert len(first) == snapcrypto.KEY_BYTES

    # Stored as text, because a keychain holds text.
    stored = _isolated._store[(snapsecrets.SERVICE, snapsecrets.KEY_ENCRYPTION)]
    assert base64.b64decode(stored) == first

    # The second call decodes what was stored rather than minting again.
    assert snapsecrets.encryption_key() == first
    assert snapsecrets.encryption_key(create=False) == first
    assert base64.b64decode(snapsecrets.encryption_key_b64()) == first


def test_publish_art_defaults_on_and_can_be_switched_off():
    assert snapsecrets.publish_art() is True
    snapsecrets.set_publish_art(False)
    assert snapsecrets.publish_art() is False
    snapsecrets.set_publish_art(True)
    assert snapsecrets.publish_art() is True


def test_forget_clears_secrets_settings_and_the_key(_isolated):
    snapsecrets.save_credentials(**FULL)
    snapsecrets.encryption_key()
    assert snapsecrets.is_configured()

    snapsecrets.forget()

    assert snapsecrets.load_credentials() is None
    assert snapsecrets.encryption_key(create=False) is None
    assert _isolated._store == {}
    assert config.get_pref(snapsecrets.PREF_BUCKET) is None


def test_an_unreadable_keychain_reads_as_not_connected():
    """A locked or absent keychain has the same next step as never having
    connected, and a status provider must never be able to raise through it."""

    class Exploding:
        def get_password(self, service, username):
            raise RuntimeError("keychain is locked")

        def set_password(self, service, username, password):
            raise RuntimeError("keychain is locked")

        def delete_password(self, service, username):
            raise RuntimeError("keychain is locked")

    snapsecrets.set_backend(Exploding())
    config.set_pref(snapsecrets.PREF_ACCOUNT, "acct123")
    config.set_pref(snapsecrets.PREF_BUCKET, "b")
    assert snapsecrets.load_credentials() is None
    assert snapsecrets.encryption_key(create=False) is None
