"""Card identity matcher (spec 06, M-V1a) — embedding + nearest-neighbour.

`Recognizer.suggest(image, candidates)` embeds a query card image and ranks a
**candidate pool** by cosine similarity against each card's reference embedding
(cached in ``vision.db``, keyed by card + embedder version). Open-vocabulary: a
new card just adds a reference vector — no retraining.

Embedder backends (chosen at build time):

* **DINOv2-small via onnxruntime** — the intended identifier, active by default.
  Used when ``onnxruntime`` is installed **and** the model file is found: the
  bundled package copy (``tcginventory/models/dinov2_small.onnx``, shipped via
  Git LFS / PyInstaller) first, or a user-dropped file under the data dir.
* **PixelEmbedder** — a lightweight, dependency-free fallback (down-scaled
  grayscale) so the pipeline runs and is testable before the model is
  provisioned. Weak, but real.

Qt-free. Vectors are L2-normalised, so a dot product is the cosine similarity.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image

from tcginventory import config
from tcginventory.data.vision import VisionRepository

try:
    import onnxruntime as _ort
except Exception:  # pragma: no cover - onnxruntime missing (e.g. minimal env)
    _ort = None

MODEL_FILENAME = "dinov2_small.onnx"


def model_path() -> Path | None:
    """Locate the DINOv2 ONNX model: the bundled package copy first (dev tree or
    PyInstaller bundle), then a user-dropped file under the data dir. ``None`` if
    neither exists."""
    try:
        bundled = Path(str(resources.files("tcginventory") / "models" / MODEL_FILENAME))
        if bundled.is_file():
            return bundled
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    fallback = config.data_dir() / "vision" / "models" / MODEL_FILENAME
    return fallback if fallback.is_file() else None


class PixelEmbedder:
    """Down-scaled grayscale features — a weak but real, dependency-free baseline."""

    def __init__(self, size: int = 48) -> None:
        self.size = size
        self.dim = size * size
        self.version = f"pixel-{size}"

    def embed(self, image_path: str | Path) -> np.ndarray:
        with Image.open(image_path) as img:
            gray = img.convert("L").resize((self.size, self.size))
        vec = np.asarray(gray, dtype=np.float32).reshape(-1)
        vec -= float(vec.mean())
        return _normalise(vec)


class OnnxEmbedder:
    """DINOv2-small image encoder via onnxruntime (ImageNet-style preprocess)."""

    _MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    _STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    def __init__(self, path: str | Path, size: int = 224) -> None:
        if _ort is None:  # pragma: no cover
            raise RuntimeError("onnxruntime is not installed")
        self._session = _ort.InferenceSession(
            str(path), providers=["CPUExecutionProvider"]
        )
        self._input = self._session.get_inputs()[0].name
        self._size = size
        self.version = f"onnx:{Path(path).stem}"
        self.dim = 0  # set on first embed (depends on the model head)

    def embed(self, image_path: str | Path) -> np.ndarray:
        with Image.open(image_path) as img:
            rgb = img.convert("RGB").resize((self._size, self._size))
        arr = np.asarray(rgb, dtype=np.float32) / 255.0
        arr = (arr - self._MEAN) / self._STD
        tensor = arr.transpose(2, 0, 1)[None, ...].astype(np.float32)  # NCHW
        raw = self._session.run(None, {self._input: tensor})[0]
        out = np.asarray(raw, dtype=np.float32)
        # DINOv2 last_hidden_state is [batch, 1 + num_patches, dim]; token 0 is the
        # CLS token — the pooled image embedding. Fall back to a flat vector for
        # models that already emit a pooled output.
        if out.ndim == 3:
            vec = out[0, 0]
        elif out.ndim == 2:
            vec = out[0]
        else:
            vec = out.reshape(-1)
        self.dim = int(vec.shape[0])
        return _normalise(vec)


def _normalise(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return (vec / norm).astype(np.float32) if norm else vec.astype(np.float32)


def build_embedder():
    """DINOv2 if onnxruntime + the model file are available, else the fallback."""
    path = model_path()
    if _ort is not None and path is not None:
        try:
            return OnnxEmbedder(path)
        except Exception:  # pragma: no cover - corrupt/incompatible model
            pass
    return PixelEmbedder()


@dataclass(frozen=True)
class Suggestion:
    card_id: str
    score: float  # cosine similarity in [-1, 1]


class Recognizer:
    """Ranks candidate cards for a query image by embedding similarity."""

    def __init__(
        self,
        repo: VisionRepository,
        embedder=None,
        reference_image: Callable[[str], str | Path | None] | None = None,
    ) -> None:
        self._repo = repo
        self._embedder = embedder or build_embedder()
        # card_id -> reference image path (defaults set by the caller/UI).
        self._reference_image = reference_image

    @property
    def model_version(self) -> str:
        return self._embedder.version

    def suggest(
        self, image_path: str | Path, candidates, top: int = 5
    ) -> list[Suggestion]:
        """Top ``top`` candidate card_ids ranked by similarity to the query."""
        query = self._embedder.embed(image_path)
        scored: list[Suggestion] = []
        for card_id in candidates:
            ref = self._reference_vector(card_id)
            if ref is None or ref.shape != query.shape:
                continue
            scored.append(Suggestion(card_id, float(np.dot(query, ref))))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:top]

    def _reference_vector(self, card_id: str) -> np.ndarray | None:
        version = self._embedder.version
        cached = self._repo.get_embedding(card_id, version)
        if cached is not None:
            return np.frombuffer(cached, dtype=np.float32)
        path = self._reference_image(card_id) if self._reference_image else None
        if not path or not Path(path).exists():
            return None
        vec = self._embedder.embed(path)
        self._repo.put_embedding(
            card_id, version, int(vec.shape[0]), vec.astype(np.float32).tobytes()
        )
        return vec


def catalog_reference_resolver(conn: sqlite3.Connection):
    """A ``card_id -> reference image path`` resolver over the local catalog.

    Uses the catalog's reference image (``image_small``), lazily fetched + cached
    on first use. For real (UI) recognition; tests inject their own resolver.
    """
    from tcginventory.services import cardimages

    def resolve(card_id: str) -> str | None:
        row = conn.execute(
            "SELECT image_small FROM card_catalog WHERE card_id = ?", (card_id,)
        ).fetchone()
        url = row["image_small"] if row else None
        if not url:
            return None
        path = cardimages.ensure_cached(card_id, url)
        return str(path) if path else None

    return resolve
