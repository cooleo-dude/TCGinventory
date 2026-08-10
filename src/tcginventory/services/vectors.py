"""How an embedding becomes bytes, and how it comes back.

Every embedding in ``vision.db`` is an L2-normalised float vector stored as a
raw blob. Which float it is stored as is **written down next to it**, in the
row's ``dtype`` column — it is never inferred from the blob's length, because a
model with a different ``dim`` would make that inference silently wrong.

Vectors are stored as **float16** and read back as **float32**.

*Stored* as float16 because half precision costs nothing here: measured over all
49,551 catalog vectors, 300 random queries against the full index picked the
identical best match **every time**, and the file is 38 MB smaller for it. These
are cosine-ranked unit vectors, not accumulators — they never needed float32's
range.

*Read back* as float32 because that is what the comparisons want. numpy has no
native float16 arithmetic on most hardware and emulates it, so a float16 matmul
is slower than the float32 one it replaces: dequantising per query measured 111
ms against 74 ms, a regression that would have wiped out the point. Cast once
where the pool is materialised, keep the working matrix float32, and the search
runs at exactly the speed it did before.

Qt-free, and the only module that names a storage dtype. Readers call
:func:`unpack` and get float32; writers call :func:`pack` and get a
``(bytes, dtype)`` pair to hand the repository.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# What new writes are packed as. Readers honour whatever a row actually says.
STORAGE_DTYPE = "float16"

# What every caller computes in, whatever the row was stored as.
WORKING_DTYPE = "float32"

# The dtype a row with no marker predates. `_ensure_columns` defaults the column
# to this, so an unconverted file keeps reading correctly.
LEGACY_DTYPE = "float32"


def pack(vector, dtype: str = STORAGE_DTYPE) -> tuple[bytes, str]:
    """An embedding as ``(blob, dtype)``, ready for the repository.

    The dtype travels with the bytes so no caller has to remember it, and so a
    row can never be stored under a marker that does not describe it.
    """
    arr = np.asarray(vector, dtype=np.dtype(dtype))
    return arr.tobytes(), dtype


def unpack(blob: bytes, dtype: str | None, dim: int | None = None) -> np.ndarray:
    """A stored blob as a **float32** array, whatever it was written as.

    ``dtype`` is the row's marker; ``None`` means a row from before the marker
    existed, which is float32 by definition. Passing ``dim`` checks the blob
    against the element count the row claims — cheap insurance against a reader
    that has been pointed at the wrong dtype, which otherwise produces a
    half-length vector that later code silently *skips* rather than rejects.
    """
    arr = np.frombuffer(blob, dtype=np.dtype(dtype or LEGACY_DTYPE))
    if dim is not None and arr.size != int(dim):
        raise ValueError(
            f"embedding is {arr.size} elements read as {dtype or LEGACY_DTYPE}, "
            f"but the row claims dim={dim} ({len(blob)} bytes)"
        )
    return arr.astype(np.dtype(WORKING_DTYPE), copy=True)


def itemsize(dtype: str | None) -> int:
    """Bytes per element for a stored dtype."""
    return np.dtype(dtype or LEGACY_DTYPE).itemsize


# -- converting a file that predates the marker ----------------------------

EMBEDDING_TABLES = ("card_embedding", "region_embedding")

# Rows re-packed per transaction. The whole table would fit in memory, but a
# bounded chunk keeps the peak flat on a 50k-row index and lets an interrupted
# run leave a *mixed* table, which every reader handles.
CHUNK = 4096


@dataclass
class Conversion:
    """What a conversion did, in the units the tile reports."""

    converted: int = 0
    already: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def reclaimed(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)


def convert_stored(
    conn,
    *,
    dtype: str = STORAGE_DTYPE,
    path: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Conversion:
    """Re-pack every stored embedding as ``dtype``, then reclaim the file.

    A pure cast of bytes already on disk — no model is loaded and nothing is
    re-embedded, so this is seconds rather than the ~68 minutes a re-embed
    costs. Safe to interrupt and safe to re-run: rows carry their own dtype, so
    a partial pass leaves a mixed table that still reads correctly, and a second
    run picks up exactly what the first did not finish.

    ``VACUUM`` is the half that actually returns bytes to the filesystem —
    without it the freed pages stay in the file as free space.
    """
    result = Conversion()
    if path is not None and path.exists():
        result.bytes_before = path.stat().st_size

    total = 0
    for table in EMBEDDING_TABLES:
        total += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE dtype IS NOT ?",  # noqa: S608
            (dtype,),
        ).fetchone()[0]
        result.already += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE dtype IS ?",  # noqa: S608
            (dtype,),
        ).fetchone()[0]

    target = np.dtype(dtype)
    for table in EMBEDDING_TABLES:
        while True:
            rows = conn.execute(
                f"SELECT rowid, vector, dim, dtype FROM {table} "  # noqa: S608
                "WHERE dtype IS NOT ? LIMIT ?",
                (dtype, CHUNK),
            ).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                vec = np.frombuffer(
                    row["vector"], dtype=np.dtype(row["dtype"] or LEGACY_DTYPE)
                )
                if vec.size != int(row["dim"]):
                    raise ValueError(
                        f"{table} rowid={row['rowid']}: {vec.size} elements but "
                        f"dim={row['dim']} — refusing to convert a row that does "
                        "not match its own marker"
                    )
                updates.append((vec.astype(target).tobytes(), dtype, row["rowid"]))
            with conn:
                conn.executemany(
                    f"UPDATE {table} SET vector = ?, dtype = ? WHERE rowid = ?",  # noqa: S608
                    updates,
                )
            result.converted += len(updates)
            if on_progress is not None:
                on_progress(result.converted, total)

    conn.commit()
    conn.execute("VACUUM")
    conn.commit()
    # VACUUM rebuilds the database, but in WAL mode (which `open_vision` sets)
    # the rebuilt pages land in the -wal file and the main file is not truncated
    # until a checkpoint. Without this the file on disk is exactly the size it
    # was and the whole saving is invisible.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if path is not None and path.exists():
        result.bytes_after = path.stat().st_size
    return result
