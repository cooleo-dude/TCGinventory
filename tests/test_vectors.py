"""How embeddings are stored, and how a file gets converted to float16.

The dtype marker exists so that a reader can never guess wrong. These tests are
mostly about the *mixed* state — a table holding both float32 and float16 rows —
because that is what an interrupted conversion leaves behind, and it has to keep
reading correctly rather than merely not crashing.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from tcginventory.data.vision import CardLabel, VisionRepository, open_vision
from tcginventory.services import vectors


@pytest.fixture()
def repo():
    conn = open_vision(":memory:")
    yield VisionRepository(conn)
    conn.close()


def _unit(seed: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=dim).astype(np.float32)
    return (vec / np.linalg.norm(vec)).astype(np.float32)


# -- the pack/unpack pair ---------------------------------------------------


@pytest.mark.parametrize("dtype", ["float32", "float16"])
def test_a_vector_survives_a_round_trip_in_either_dtype(repo, dtype):
    vec = _unit(1)
    blob, marker = vectors.pack(vec, dtype)
    repo.put_embedding("card-1", "v1", vec.size, blob, marker)

    stored = repo.get_embedding("card-1", "v1")
    assert stored is not None
    got = vectors.unpack(*stored, vec.size)

    assert got.dtype == np.float32  # always, whatever it was stored as
    assert got.shape == vec.shape
    np.testing.assert_allclose(got, vec, atol=1e-3 if dtype == "float16" else 0)


def test_float16_halves_the_bytes_without_moving_the_ranking():
    """The measurement the change rests on, in miniature."""
    index = np.vstack([_unit(i, 32) for i in range(200)])
    query = _unit(999, 32)

    wide = np.vstack([vectors.unpack(*vectors.pack(v, "float32"), 32) for v in index])
    narrow = np.vstack([vectors.unpack(*vectors.pack(v, "float16"), 32) for v in index])

    assert vectors.pack(index[0], "float16")[0].__len__() * 2 == len(
        vectors.pack(index[0], "float32")[0]
    )
    assert int(np.argmax(wide @ query)) == int(np.argmax(narrow @ query))


def test_unpack_rejects_a_blob_that_contradicts_its_own_dim():
    """The guard that turns a wrong dtype into an error instead of silence.

    Read float16 bytes as float32 and you get a half-length vector, which the
    scoring paths *skip* rather than reject — the whole failure would be a
    quietly empty candidate list.
    """
    blob, _ = vectors.pack(_unit(2, 8), "float16")
    with pytest.raises(ValueError, match="dim=8"):
        vectors.unpack(blob, "float32", 8)


def test_an_absent_marker_reads_as_float32():
    """The column is NOT NULL DEFAULT 'float32', so a stored NULL should be
    unreachable — but a marker is also what a caller passes by hand, and an
    absent one means "from before this existed", which is float32 by
    definition."""
    vec = _unit(3)
    np.testing.assert_allclose(vectors.unpack(vec.tobytes(), None, vec.size), vec)


# -- the mixed table --------------------------------------------------------


def test_a_half_converted_table_still_reads_correctly(repo):
    """Every row decodes by its own marker, not the table's majority."""
    originals = {}
    for i in range(6):
        vec = _unit(i)
        originals[f"card-{i}"] = vec
        dtype = "float32" if i % 2 else "float16"
        repo.put_embedding(f"card-{i}", "v1", vec.size, *vectors.pack(vec, dtype))

    stored = {
        r[0]: r[1]
        for r in repo.conn.execute(
            "SELECT dtype, COUNT(*) FROM card_embedding GROUP BY 1"
        )
    }
    assert stored == {"float32": 3, "float16": 3}  # genuinely mixed

    for card_id, vec in originals.items():
        got = vectors.unpack(*repo.get_embedding(card_id, "v1"), vec.size)
        np.testing.assert_allclose(got, vec, atol=1e-3)


def test_the_pool_loads_every_row_of_a_mixed_table(repo):
    """`load_pool` is the reader that fails *silently* if it guesses the dtype:
    a wrong-length vector is skipped, so the symptom is an index that quietly
    holds fewer cards than the table does."""
    from tcginventory.services import identity_eval as ie

    for i in range(6):
        vec = _unit(i)
        dtype = "float32" if i % 2 else "float16"
        repo.put_embedding(f"card-{i}", "v1", vec.size, *vectors.pack(vec, dtype))

    ids, matrix = ie.load_pool(repo, "v1")

    assert ids == [f"card-{i}" for i in range(6)]  # nothing silently dropped
    assert matrix.shape == (6, 8)
    assert matrix.dtype == np.float32


def test_exemplars_of_a_mixed_table_all_reach_the_recognizer(repo):
    """The same hazard on the region side, through `region_vectors`."""
    cap = repo.add_capture()
    img = repo.add_image(cap, "sha", "/x.png", width=64, height=64)
    for i in range(4):
        rid = repo.add_region(img, i + 1, (0, 0, 1, 1), None, CardLabel(card_id="A"))
        dtype = "float32" if i % 2 else "float16"
        repo.put_region_embedding(rid, "v1", "A", 8, *vectors.pack(_unit(i), dtype))

    rows = repo.region_vectors("v1")
    assert len(rows) == 4
    for _card_id, blob, dtype, dim in rows:
        assert vectors.unpack(blob, dtype, dim).size == 8


# -- the conversion ---------------------------------------------------------


def test_convert_repacks_every_row_and_leaves_the_vectors_intact(repo):
    originals = {}
    for i in range(5):
        vec = _unit(i)
        originals[f"card-{i}"] = vec
        repo.put_embedding(f"card-{i}", "v1", vec.size, *vectors.pack(vec, "float32"))

    result = vectors.convert_stored(repo.conn)

    assert result.converted == 5
    dtypes = {
        r[0] for r in repo.conn.execute("SELECT DISTINCT dtype FROM card_embedding")
    }
    assert dtypes == {"float16"}
    for card_id, vec in originals.items():
        got = vectors.unpack(*repo.get_embedding(card_id, "v1"), vec.size)
        np.testing.assert_allclose(got, vec, atol=1e-3)


def test_convert_is_idempotent_and_resumable(repo):
    for i in range(4):
        vec = _unit(i)
        dtype = "float32" if i % 2 else "float16"  # a half-finished earlier run
        repo.put_embedding(f"card-{i}", "v1", vec.size, *vectors.pack(vec, dtype))

    first = vectors.convert_stored(repo.conn)
    assert (first.converted, first.already) == (2, 2)  # only the unconverted ones

    second = vectors.convert_stored(repo.conn)
    assert second.converted == 0  # a second run has nothing to do


def test_convert_covers_the_region_table_too(repo):
    cap = repo.add_capture()
    img = repo.add_image(cap, "sha", "/x.png", width=64, height=64)
    rid = repo.add_region(img, 1, (0, 0, 1, 1), None, CardLabel(card_id="A"))
    repo.put_region_embedding(rid, "v1", "A", 8, *vectors.pack(_unit(0), "float32"))

    vectors.convert_stored(repo.conn)

    assert [r[2] for r in repo.region_vectors("v1")] == ["float16"]


def test_convert_reclaims_the_file_rather_than_leaving_free_pages(tmp_path):
    """VACUUM is the half that returns bytes to the filesystem — without it the
    freed pages stay in the file and the saving is invisible."""
    path = tmp_path / "vision.db"
    conn = open_vision(path)
    repo = VisionRepository(conn)
    for i in range(400):
        vec = _unit(i, 384)
        repo.put_embedding(f"card-{i}", "v1", vec.size, *vectors.pack(vec, "float32"))
    conn.commit()

    result = vectors.convert_stored(conn, path=path)
    conn.close()

    assert result.converted == 400
    assert result.bytes_after < result.bytes_before
    assert result.reclaimed > 400 * 384  # ~2 bytes/element actually returned
    assert path.stat().st_size == result.bytes_after


def test_convert_refuses_a_row_that_contradicts_its_own_marker(repo):
    """A blob whose length disagrees with `dim` is corrupt. Converting it would
    bake the corruption in under a fresh marker, so the pass stops instead."""
    vec = _unit(0)
    repo.put_embedding("bad", "v1", vec.size, vec.tobytes(), "float32")
    repo.conn.execute("UPDATE card_embedding SET dim = 99")

    with pytest.raises(ValueError, match="does not match its own marker"):
        vectors.convert_stored(repo.conn)


# -- the rule that stops this regressing ------------------------------------


def test_no_module_decodes_an_embedding_with_a_hardcoded_dtype():
    """`services/vectors.py` is the only place a stored dtype is named.

    Every other reader once did ``np.frombuffer(blob, dtype=np.float32)``, which
    is exactly the bug this change removes: after conversion such a reader
    yields half-length vectors that the scoring paths skip in silence. A new one
    would reintroduce it, so the boundary is asserted rather than documented.
    """
    from pathlib import Path

    import tcginventory

    root = Path(tcginventory.__file__).parent
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "frombuffer" in path.read_text(encoding="utf-8")
        and path.name != "vectors.py"
        and path.name != "vision.py"  # comment only, asserted below
    ]
    assert offenders == []

    text = (root / "data" / "vision.py").read_text(encoding="utf-8")
    assert "np.frombuffer" not in text.replace("`np.frombuffer(..., np.float32)`", "")


# -- the schema upgrade -----------------------------------------------------


def test_an_existing_file_gains_the_column_and_keeps_reading(tmp_path):
    """A vision.db written before the marker existed opens, gains the column
    defaulted to float32, and its vectors still decode."""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE card_embedding (
            card_id TEXT NOT NULL, model_version TEXT NOT NULL, dim INTEGER NOT NULL,
            vector BLOB NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (card_id, model_version)
        );
        """
    )
    vec = _unit(7)
    raw.execute(
        "INSERT INTO card_embedding VALUES (?,?,?,?,?)",
        ("old-card", "v1", vec.size, vec.tobytes(), "2026-08-09T00:00:00+00:00"),
    )
    raw.commit()
    raw.close()

    conn = open_vision(path)
    try:
        repo = VisionRepository(conn)
        blob, dtype = repo.get_embedding("old-card", "v1")
        assert dtype == "float32"  # the default is what makes the old bytes readable
        np.testing.assert_allclose(vectors.unpack(blob, dtype, vec.size), vec)
    finally:
        conn.close()
