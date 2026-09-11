"""Publishing a snapshot to the object store (ticket 74).

Build → seal → upload payloads → upload the pointer → prune. The ordering is the
design, and it comes out of what actually happened while this was being built.

**The pointer is written last.** ``latest.json`` is what makes a payload *live*;
a payload with nothing pointing at it is invisible. So a publish that dies
half-way leaves the previous snapshot readable rather than a torn one, and the
worst case is a wasted object rather than a phone showing half a ledger.

**Retention sweeps orphans, not just age.** On 2026-09-10 a client-side upload
error left a complete object sitting in the bucket: a transport failure does not
prove the write failed. Pruning only the oldest N would let those accumulate
forever, so anything under our prefix that the pointer does not name and that is
not among the newest N is swept.

**A publish that would change nothing does nothing.** The content hash ignores
``generated_at``, so an unchanged ledger produces the same hash; the previous
hash is read back from ``latest.json`` rather than cached locally, which means
the decision is self-correcting — wipe the bucket and the next run republishes.

Qt-free. PIL is reached only through :mod:`tcginventory.services.artbundle`, and
only when art is enabled.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from tcginventory.data.db import now_iso
from tcginventory.services import s3, snapcrypto, snapsecrets, snapshot
from tcginventory.services.exceptions import SnapshotNotConfiguredError

log = logging.getLogger(__name__)

POINTER_SCHEMA = 1
KEEP = 10  # matches the startup backup's keep-10 (data/backup.py)


def stamp_for_key() -> str:
    """UTC stamp whose lexicographic order is chronological order.

    Same format and same reason as ``backup.timestamp_for_filename`` — sorting
    keys as strings is how retention picks the oldest, so the coupling between
    the format and the sort is load-bearing.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def pointer_key(ledger_id: str) -> str:
    return f"{ledger_id}/inventory/latest.json"


def payload_key(ledger_id: str, stamp: str) -> str:
    return f"{ledger_id}/inventory/{stamp}.json.gz.enc"


def art_key(ledger_id: str, stamp: str) -> str:
    return f"{ledger_id}/art/{stamp}.zip.enc"


@dataclass(frozen=True)
class PublishResult:
    """What a run did, in the terms the Operations tile reports."""

    published: bool  # False when the ledger had not changed
    rows: int
    in_stock: int
    payload_bytes: int
    art_bytes: int  # bytes uploaded this run; 0 when the bundle was carried forward
    pruned: int
    summary: str
    art_reused: bool = False  # the previous bundle was unchanged and kept


def _read_pointer(store: s3.ObjectStore, ledger_id: str) -> dict | None:
    """The live pointer, or ``None`` if there is not one yet.

    A missing or unreadable pointer is not an error: it is what a fresh bucket
    looks like, and it simply means the next publish is the first.
    """
    try:
        return json.loads(store.get(pointer_key(ledger_id)).decode("utf-8"))
    except s3.S3Error as exc:
        if exc.status in (403, 404):
            return None
        raise
    except (ValueError, UnicodeDecodeError):
        log.warning("pointer at %s is not readable JSON; republishing", ledger_id)
        return None


def publish(
    conn,
    *,
    store: s3.ObjectStore | None = None,
    force: bool = False,
    include_art: bool | None = None,
) -> PublishResult:
    """Publish the current inventory. Returns what happened.

    ``store`` is injectable so the whole path can be tested without a network.
    ``force`` republishes even when the content hash is unchanged.
    """
    creds = snapsecrets.load_credentials()
    if store is None:
        if creds is None:
            raise SnapshotNotConfiguredError(
                "no object store is connected yet — run Connect first"
            )
        store = s3.ObjectStore(creds)

    payload = snapshot.build_snapshot(conn)
    ledger_id = payload["ledger_id"]
    digest = snapshot.content_hash(payload)

    previous = _read_pointer(store, ledger_id)
    if not force and previous and previous.get("content_hash") == digest:
        rows = payload["counts"]["rows"]
        return PublishResult(
            published=False,
            rows=rows,
            in_stock=payload["counts"]["in_stock"],
            payload_bytes=0,
            art_bytes=0,
            pruned=0,
            summary=f"no change — {rows:,} rows already published",
        )

    key = snapsecrets.encryption_key()
    stamp = stamp_for_key()
    blob = snapcrypto.seal(key, snapshot.encode(payload))
    p_key = payload_key(ledger_id, stamp)
    store.put(p_key, blob, "application/octet-stream")

    art_bytes = 0
    art_entry = None
    art_reused = False
    want_art = snapsecrets.publish_art() if include_art is None else include_art
    if want_art:
        from tcginventory.services import artbundle

        fingerprint = artbundle.art_fingerprint(conn)
        prior = (previous or {}).get("art") or {}
        if prior.get("key") and prior.get("fingerprint") == fingerprint:
            # Same set of cached, owned images as last time: the bundle would be
            # byte-identical, so keep the object we already have rather than
            # re-encoding ~15 MB and uploading it again. The pointer keeps naming
            # it, which is what protects it from retention.
            art_entry = dict(prior)
            art_reused = True
        else:
            bundle = artbundle.build_bundle(conn)
            if bundle:
                sealed = snapcrypto.seal(key, bundle)
                a_key = art_key(ledger_id, stamp)
                store.put(a_key, sealed, "application/octet-stream")
                art_bytes = len(sealed)
                art_entry = {
                    "key": a_key,
                    "bytes": art_bytes,
                    "fingerprint": fingerprint,
                }

    # Only now does any of it become live.
    pointer = {
        "schema": POINTER_SCHEMA,
        "payload_schema": snapshot.SCHEMA,
        "ledger_id": ledger_id,
        "generated_at": payload["generated_at"],
        "published_at": now_iso(),
        "content_hash": digest,
        "counts": payload["counts"],
        "payload": {"key": p_key, "bytes": len(blob)},
        "art": art_entry,
    }
    store.put(
        pointer_key(ledger_id),
        json.dumps(pointer, indent=2, sort_keys=True).encode("utf-8"),
        "application/json",
    )

    pruned = prune(store, ledger_id, pointer, keep=KEEP)
    rows = payload["counts"]["rows"]
    summary = f"{rows:,} rows · {len(blob) / 1024:.0f} KB"
    if art_bytes:
        summary += f" · art {art_bytes / 1048576:.1f} MB"
    elif art_reused:
        summary += " · art unchanged"
    if pruned:
        summary += f" · pruned {pruned}"
    return PublishResult(
        published=True,
        rows=rows,
        in_stock=payload["counts"]["in_stock"],
        payload_bytes=len(blob),
        art_bytes=art_bytes,
        pruned=pruned,
        summary=summary,
        art_reused=art_reused,
    )


def prune(
    store: s3.ObjectStore, ledger_id: str, pointer: dict, *, keep: int = KEEP
) -> int:
    """Keep the newest ``keep`` of each payload family; delete the rest.

    Whatever the live pointer names is never deleted, however old it is —
    retention must not be able to unpublish the snapshot people are reading.
    """
    live = {
        (pointer.get("payload") or {}).get("key"),
        (pointer.get("art") or {}).get("key"),
        pointer_key(ledger_id),
    }
    deleted = 0
    for prefix in (f"{ledger_id}/inventory/", f"{ledger_id}/art/"):
        keys = [k for k in store.list(prefix) if k not in live]
        keys = [k for k in keys if not k.endswith("latest.json")]
        # Stamped names sort chronologically, so the tail is the newest.
        for old in sorted(keys)[:-keep] if len(keys) > keep else []:
            store.delete(old)
            deleted += 1
    return deleted


def fetch_and_verify(store: s3.ObjectStore, ledger_id: str) -> dict:
    """Read back what a client would read. Used by the verification path.

    Deliberately goes the long way round — pointer, then payload, then decrypt,
    then decode — because that is the sequence a phone performs, and a check that
    skips it is not checking the thing that matters.
    """
    pointer = _read_pointer(store, ledger_id)
    if pointer is None:
        raise LookupError(f"no snapshot published for ledger {ledger_id}")
    blob = store.get(pointer["payload"]["key"])
    payload = snapshot.decode(snapcrypto.unseal(snapsecrets.encryption_key(), blob))
    if payload["ledger_id"] != ledger_id:
        raise ValueError("published payload belongs to a different ledger")
    return payload
