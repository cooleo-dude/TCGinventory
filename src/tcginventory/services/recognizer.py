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
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
FINETUNED_FILENAME = "identifier_finetuned.onnx"


@contextmanager
def _open_image(src) -> Iterator[Image.Image]:
    """Yield a PIL image for ``src``, closing it only if we opened it here."""
    if isinstance(src, Image.Image):
        yield src
    else:
        img = Image.open(src)
        try:
            yield img
        finally:
            img.close()


def _resolve_model(filename: str) -> Path | None:
    """Find ``filename``: the bundled package copy first (dev tree or PyInstaller
    bundle), then a user-dropped file under the data dir."""
    try:
        res = resources.files("tcginventory") / "models" / filename
        bundled = Path(str(res))
        if bundled.is_file():
            return bundled
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    fallback = config.data_dir() / "vision" / "models" / filename
    return fallback if fallback.is_file() else None


def model_path() -> Path | None:
    """Locate the identifier ONNX model. A **fine-tuned** model (M-V2) supersedes
    the frozen base DINOv2 when present; ``None`` if neither exists."""
    return _resolve_model(FINETUNED_FILENAME) or _resolve_model(MODEL_FILENAME)


class PixelEmbedder:
    """Down-scaled grayscale features — a weak but real, dependency-free baseline."""

    def __init__(self, size: int = 48) -> None:
        self.size = size
        self.dim = size * size
        self.version = f"pixel-{size}"

    def embed(self, image) -> np.ndarray:
        with _open_image(image) as img:
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

    def embed(self, image) -> np.ndarray:
        with _open_image(image) as img:
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


_shared_embedder = None
_shared_embedder_lock = threading.Lock()


def get_shared_embedder():
    """The process-wide embedder, built once and reused across recognizers.

    The ONNX session is loaded lazily and cached; it is thread-safe to run from
    the main thread after loading (unlike a SQLite connection), so ``prewarm``
    may load it on a background thread."""
    global _shared_embedder
    with _shared_embedder_lock:
        if _shared_embedder is None:
            _shared_embedder = build_embedder()
        return _shared_embedder


def prewarm() -> None:
    """Load the shared embedder now — call on a background thread when a vision
    window opens so the first suggestion/autofill doesn't pay the model-load
    freeze. A no-op once loaded."""
    get_shared_embedder()


# Sentinel for a lazily-loaded artifact that may legitimately be absent, so a
# missing one is looked for once rather than on every box.
_UNSET: object = object()


@dataclass(frozen=True)
class Suggestion:
    card_id: str
    score: float  # cosine similarity in [-1, 1]
    # Agreement between the query's bottom strip and this printing's, when the
    # band re-ranker ran. It reorders suggestions but deliberately does not fold
    # into `score`, which stays a plain cosine so the low-confidence threshold in
    # the UI keeps meaning one thing.
    band: float = 0.0


class Recognizer:
    """Ranks candidate cards for a query image by embedding similarity."""

    def __init__(
        self,
        repo: VisionRepository,
        embedder=None,
        reference_image: Callable[[str], str | Path | None] | None = None,
    ) -> None:
        self._repo = repo
        self._embedder = embedder or get_shared_embedder()
        # card_id -> reference image path (defaults set by the caller/UI).
        self._reference_image = reference_image
        # Parsed exemplar index, cached across a scene's boxes; rebuilt when the
        # model version or the exemplar count changes (e.g. after a save).
        self._exemplar_index: dict[str, list[np.ndarray]] | None = None
        self._exemplar_key: tuple[str, int] | None = None
        # Bottom-strip descriptors keyed by card_id, reused across a scene's boxes
        # so the same candidate's art is read from disk at most once per session.
        self._band_cache: dict[str, np.ndarray] = {}
        # Photo-to-render map, read from disk once. _UNSET distinguishes "not
        # looked for yet" from "looked for and absent", so a missing artifact
        # is not re-checked on every box.
        self._projection = _UNSET  # np.ndarray | None once resolved

    @property
    def model_version(self) -> str:
        return self._embedder.version

    @property
    def embedder(self):
        return self._embedder

    def suggest(
        self, image, candidates, top: int = 5, *, rerank: bool = True
    ) -> list[Suggestion]:
        """Top ``top`` candidate card_ids ranked by similarity to the query.

        ``image`` is a file path or an already-loaded PIL image (e.g. a crop of a
        larger scene), so callers need not spill crops to disk.

        Each card is scored by its **best** matching vector: any accumulated crop
        **exemplars** for that card (same visual domain as the query — accurate
        and improving with ingestion), falling back to the catalog reference
        render only for cards not yet labeled.

        The leaders are then re-ranked on the card's bottom strip
        (:mod:`identity_patches`), which is what separates two printings of the
        same artwork — the embedding cannot, and reprints are the single largest
        source of wrong answers. Pass ``rerank=False`` to measure the embedding
        alone.
        """
        query = self._embedder.embed(image)
        # Two comparisons live here in two different domains. Crop exemplars are
        # photographs like the query, so they are compared as they are; catalog
        # renders are not, so the query is carried into render domain first. One
        # projected query for everything would score the exemplars in a space
        # they were never in.
        as_render = self._projected(query)
        exemplars = self._exemplars_by_card(set(candidates))
        scored: list[Suggestion] = []
        for card_id in candidates:
            vectors = exemplars.get(card_id)
            probe = query
            if not vectors:
                ref = self._reference_vector(card_id)
                vectors = [ref] if ref is not None else []
                probe = as_render
            best: float | None = None
            for vec in vectors:
                if vec.shape == probe.shape:
                    score = float(np.dot(probe, vec))
                    if best is None or score > best:
                        best = score
            if best is not None:
                scored.append(Suggestion(card_id, best))
        scored.sort(key=lambda s: s.score, reverse=True)
        if rerank and len(scored) > 1:
            scored = self._rerank_on_band(image, scored)
        return scored[:top]

    def _projected(self, query: np.ndarray) -> np.ndarray:
        """The query in render domain, if a photo-to-render map is installed."""
        from tcginventory.services import identity_projection as ip

        if self._projection is _UNSET:
            self._projection = ip.load()
        return ip.apply(query, self._projection)

    def _rerank_on_band(self, image, scored: list[Suggestion]) -> list[Suggestion]:
        """Reorder the leaders by how well their bottom strip matches the query.

        Only the shortlist is touched: reading and normalising a candidate's art
        costs milliseconds each, and the answer is almost never further down.
        """
        from tcginventory.services import identity_patches as ip

        head, tail = scored[: ip.SHORTLIST], scored[ip.SHORTLIST :]
        ranked = ip.rescore(
            image, [(s.card_id, s.score) for s in head], cache=self._band_cache
        )
        return [Suggestion(r.card_id, r.embed, r.band) for r in ranked] + tail

    def _exemplars_by_card(self, candidates: set[str]) -> dict[str, list[np.ndarray]]:
        index = self._exemplar_index_for_version()
        return {cid: index[cid] for cid in candidates if cid in index}

    def _exemplar_index_for_version(self) -> dict[str, list[np.ndarray]]:
        """All exemplar vectors for the current model version, parsed once and
        cached until the version or exemplar count changes (so a scene's boxes
        don't each reload + re-parse the whole table)."""
        version = self._embedder.version
        key = (version, self._repo.region_embedding_count())
        if self._exemplar_index is None or self._exemplar_key != key:
            index: dict[str, list[np.ndarray]] = {}
            for card_id, blob in self._repo.region_vectors(version):
                index.setdefault(card_id, []).append(
                    np.frombuffer(blob, dtype=np.float32)
                )
            self._exemplar_index = index
            self._exemplar_key = key
        return self._exemplar_index

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
