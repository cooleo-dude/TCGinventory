"""The pairing payload a phone scans once (ticket 74).

Everything a reader needs to find and open this ledger's snapshots: where the
bucket is, a credential to read it with, the key to decrypt payloads, and which
ledger it is. Rendered as a QR because the alternative is typing a 40-character
secret and a 44-character key into a phone keyboard, which nobody does twice.

**This payload is a secret in full.** Anyone holding it can read every snapshot
this ledger publishes. It is shown on screen, never written to disk, never
logged, and the QR is generated in memory — which is why :func:`payload` returns
it rather than saving it anywhere.

The read credential is deliberately a *separate, read-only* R2 token from the one
the desktop publishes with. Nothing here enforces that — R2 does, when the token
is minted — but a publish key in a pairing QR would let anyone who scanned it
overwrite the ledger's snapshots, so it is worth the sentence.

Qt-free: rendering to a widget is the view's business, and :func:`qr_svg` returns
markup rather than a Qt object.
"""

from __future__ import annotations

import json

from tcginventory.data.repositories import AppMetaRepository
from tcginventory.services import snapsecrets, snapshot

SCHEMA = 1


def payload(conn, *, read_access_key_id: str = "", read_secret: str = "") -> dict:
    """Build the pairing payload for this ledger.

    The read credential is passed in rather than read from storage: the desktop
    stores its *publish* key, and handing that to a phone would be handing out
    write access.
    """
    creds = snapsecrets.load_credentials()
    if creds is None:
        raise LookupError("no object store is connected yet")
    return {
        "v": SCHEMA,
        "payload_schema": snapshot.SCHEMA,
        "ledger_id": AppMetaRepository(conn).ledger_id(),
        "endpoint": creds.base_url(),
        "bucket": creds.bucket,
        "region": creds.region,
        "access_key_id": read_access_key_id or creds.access_key_id,
        "secret_access_key": read_secret or creds.secret_access_key,
        "encryption_key": snapsecrets.encryption_key_b64(),
    }


def encode(pairing: dict) -> str:
    """Compact JSON — a QR's capacity is finite and this is near its limit."""
    return json.dumps(pairing, separators=(",", ":"), sort_keys=True)


def qr_svg(pairing: dict, *, scale: int = 4) -> str:
    """The pairing payload as an SVG QR code, as a string.

    Colours are set explicitly — **dark modules on a white quiet zone** — and not
    left to the default. segno draws black modules on a transparent background,
    which on this app's dark theme renders black-on-near-black and does not scan
    at all. The QR ignores the theme on purpose: it is a thing a camera reads,
    not a thing that should match the window around it.

    Error correction stays at ``L``: this is scanned once, held steady, from a
    screen rather than a crumpled receipt, and every level above L costs capacity
    the keys need.
    """
    import io

    import segno

    # segno writes encoded bytes even for SVG, so buffer bytes and decode once.
    out = io.BytesIO()
    segno.make(encode(pairing), error="l").save(
        out,
        kind="svg",
        scale=scale,
        border=3,
        dark="#000000",
        light="#ffffff",
    )
    return out.getvalue().decode("utf-8")
