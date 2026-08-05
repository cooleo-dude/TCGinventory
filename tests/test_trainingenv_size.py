"""Measuring the training environment on disk — the walk, and its cache.

Operations asks for this size on every refresh. Finding it means walking the
whole environment (~37k files), which was the single slowest thing in the app:
four stat calls per path added up to ~4s and made the screen feel broken.
"""

from __future__ import annotations

from tcginventory.services import trainingenv


def _env(tmp_path, files: dict[str, int]):
    """A directory laid out like a virtualenv, with files of known sizes."""
    base = tmp_path / trainingenv.ENV_DIRNAME
    for rel, size in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return base


def test_size_is_the_sum_of_every_file_underneath(tmp_path):
    base = _env(
        tmp_path,
        {
            "Scripts/python.exe": 100,
            "Lib/site-packages/torch/__init__.py": 250,
            "Lib/site-packages/torch/lib/big.dll": 1000,
            "pyvenv.cfg": 25,
        },
    )
    assert trainingenv.env_size(base) == 1375


def test_no_environment_is_zero_not_an_error(tmp_path):
    assert trainingenv.env_size(tmp_path / "training-env") == 0


def test_the_walk_happens_once_and_then_the_answer_is_cached(tmp_path, monkeypatch):
    base = _env(tmp_path, {"Lib/site-packages/a.py": 10})
    trainingenv.forget_env_size()

    walks = []
    real = trainingenv._walk_size
    monkeypatch.setattr(
        trainingenv,
        "_walk_size",
        lambda b: (walks.append(str(b)), real(b))[1],
    )

    assert trainingenv.env_size(base) == 10
    for _ in range(5):  # what a few Operations refreshes look like
        assert trainingenv.env_size(base) == 10
    assert len(walks) == 1


def test_installing_into_the_environment_invalidates_the_cache(tmp_path):
    """A cached size must not outlive the thing it measured: switching the
    training stack (CPU ↔ GPU, say) rewrites site-packages, and the screen has
    to report the new size without anyone remembering to invalidate."""
    base = _env(tmp_path, {"Lib/site-packages/a.py": 10})
    assert trainingenv.env_size(base) == 10

    (base / "Lib" / "site-packages" / "torch").mkdir(parents=True)
    (base / "Lib" / "site-packages" / "torch" / "big.dll").write_bytes(b"x" * 500)
    assert trainingenv.env_size(base) == 510


def test_a_removed_environment_reports_zero_even_though_it_was_cached(tmp_path):
    import shutil

    base = _env(tmp_path, {"Lib/site-packages/a.py": 10})
    assert trainingenv.env_size(base) == 10
    shutil.rmtree(base)
    assert trainingenv.env_size(base) == 0


def test_recount_forces_a_fresh_walk(tmp_path, monkeypatch):
    base = _env(tmp_path, {"Lib/site-packages/a.py": 10})
    trainingenv.env_size(base)

    walks = []
    monkeypatch.setattr(trainingenv, "_walk_size", lambda b: walks.append(b) or 42)
    assert trainingenv.env_size(base, recount=True) == 42
    assert len(walks) == 1


def test_remove_env_reports_what_it_reclaimed_and_spares_the_models(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TCGINVENTORY_DATA_DIR", str(tmp_path))
    base = _env(tmp_path, {"Scripts/python.exe": 100, "Lib/site-packages/a.py": 900})
    models = tmp_path / "vision" / "models"
    models.mkdir(parents=True)
    (models / "regions.pt").write_bytes(b"x" * 64)

    assert trainingenv.remove_env(base) == 1000
    assert not base.exists()
    assert (models / "regions.pt").read_bytes() == b"x" * 64  # never touched


def test_remove_env_refuses_a_directory_that_is_not_the_environment(tmp_path):
    import pytest

    stray = tmp_path / "Documents"
    stray.mkdir()
    with pytest.raises(RuntimeError):
        trainingenv.remove_env(stray)
    assert stray.exists()
