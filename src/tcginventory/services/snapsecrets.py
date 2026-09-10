"""Where the snapshot's credentials live (ticket 74).

Two homes, split by whether the value is a secret:

* **Secrets** — the R2 access key, its secret, and the payload encryption key —
  go to the OS keychain via ``keyring``. On Windows that is Credential Manager,
  which the daily Scheduled Task can also read as the same user, so a background
  publish needs no separate arrangement.
* **Everything else** — account id, bucket, endpoint, whether art is published —
  goes to ``preferences.json`` through :mod:`tcginventory.config`, like every
  other setting.

This is the first secret the app has ever stored, so it is worth saying what the
split is *for*: ``preferences.json`` is plain text in the app data directory, and
it is the file people copy around when they move machines or paste into a bug
report. A key in there is a key in a backup, an email and a screenshot.

The keychain is reached through an injectable backend. Tests use
:class:`MemoryBackend` and never touch the real Credential Manager — which also
means the suite still runs on a machine with no keychain at all.

Qt-free.
"""

from __future__ import annotations

import base64
from typing import Protocol

from tcginventory import config
from tcginventory.services import s3, snapcrypto

SERVICE = "TCGinventory"

KEY_ACCESS_ID = "r2_access_key_id"
KEY_SECRET = "r2_secret_access_key"
KEY_ENCRYPTION = "snapshot_encryption_key"

PREF_ACCOUNT = "snapshot_r2_account_id"
PREF_BUCKET = "snapshot_r2_bucket"
PREF_ENDPOINT = "snapshot_r2_endpoint"
PREF_PUBLISH_ART = "snapshot_publish_art"


class Backend(Protocol):
    """The slice of ``keyring`` this module uses."""

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class MemoryBackend:
    """An in-process stand-in for the keychain, for tests."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


_backend: Backend | None = None


def set_backend(backend: Backend | None) -> None:
    """Swap the keychain (tests pass a :class:`MemoryBackend`; ``None`` restores)."""
    global _backend
    _backend = backend


def _keychain() -> Backend:
    if _backend is not None:
        return _backend
    import keyring  # imported lazily: it touches the OS keychain on import

    return keyring


def _get(name: str) -> str | None:
    try:
        return _keychain().get_password(SERVICE, name)
    except Exception:  # pragma: no cover - a locked or absent keychain
        return None


def _set(name: str, value: str) -> None:
    _keychain().set_password(SERVICE, name, value)


def save_credentials(
    *,
    account_id: str,
    bucket: str,
    access_key_id: str,
    secret_access_key: str,
    endpoint: str = "",
) -> None:
    """Persist the connection: secrets to the keychain, the rest to prefs."""
    _set(KEY_ACCESS_ID, access_key_id)
    _set(KEY_SECRET, secret_access_key)
    config.set_pref(PREF_ACCOUNT, account_id)
    config.set_pref(PREF_BUCKET, bucket)
    config.set_pref(PREF_ENDPOINT, endpoint)


def load_credentials() -> s3.Credentials | None:
    """The stored connection, or ``None`` if it is not fully configured.

    Partial configuration counts as unconfigured — a half-set connection should
    read as "not connected yet" rather than fail deep inside a publish with a
    signature error.
    """
    account = config.get_pref(PREF_ACCOUNT)
    bucket = config.get_pref(PREF_BUCKET)
    access = _get(KEY_ACCESS_ID)
    secret = _get(KEY_SECRET)
    if not (account and bucket and access and secret):
        return None
    return s3.Credentials(
        account_id=account,
        bucket=bucket,
        access_key_id=access,
        secret_access_key=secret,
        endpoint=config.get_pref(PREF_ENDPOINT) or "",
    )


def is_configured() -> bool:
    return load_credentials() is not None


def encryption_key(*, create: bool = True) -> bytes | None:
    """The payload key, minting one on first use.

    Stored base64 because a keychain holds text. Losing it costs a republish,
    not data — the snapshot is derived — so there is no recovery ceremony here.
    """
    raw = _get(KEY_ENCRYPTION)
    if raw:
        return base64.b64decode(raw)
    if not create:
        return None
    key = snapcrypto.new_key()
    _set(KEY_ENCRYPTION, base64.b64encode(key).decode())
    return key


def encryption_key_b64() -> str:
    """The key as base64 — the form that travels in the pairing payload."""
    return base64.b64encode(encryption_key()).decode()


def publish_art() -> bool:
    """Whether the art bundle is published alongside the rows (default on)."""
    value = config.get_pref(PREF_PUBLISH_ART)
    return True if value is None else bool(value)


def set_publish_art(enabled: bool) -> None:
    config.set_pref(PREF_PUBLISH_ART, bool(enabled))


def forget() -> None:
    """Drop the stored connection. Used when re-connecting to somewhere else."""
    for name in (KEY_ACCESS_ID, KEY_SECRET, KEY_ENCRYPTION):
        try:
            _keychain().delete_password(SERVICE, name)
        except Exception:  # pragma: no cover - absent entry or locked keychain
            pass
    for pref in (PREF_ACCOUNT, PREF_BUCKET, PREF_ENDPOINT):
        config.set_pref(pref, None)
