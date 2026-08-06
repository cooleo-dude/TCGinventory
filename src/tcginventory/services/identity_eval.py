"""Measure how well card identification actually works (identity Layer 0.5).

Every layer of the identifier will claim to have improved recognition. This is the
thing that makes the claim checkable — and it is deliberately built before the
layers rather than after, so no improvement is ever asserted from vibes.

**Queries** are the labelled crops in the corpus: real photographs, with a known
answer. **The pool** is whatever the catalog index currently holds, which is the
same pool the app searches, so the number means what it says.

Two properties make this worth logging rather than printing:

* the headline is **top-1 and top-5**, because a ranked shortlist that reliably
  contains the answer is a usable product even when the first guess is wrong;
* failures are split into **wrong printing** (the right card, wrong version — a
  reprint) and **wrong card**, because those have different fixes. Patch
  verification attacks the first; a better encoder attacks the second. Without the
  split, a layer can look useless when it fixed exactly what it aimed at.

Runs are recorded in ``model_run`` with ``kind='identity'`` so accuracy is tracked
over time beside the detector and the condition grader. Qt-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from tcginventory.data.vision import (
    KIND_IDENTITY,
    ModelRunRepository,
    VisionRepository,
)
from tcginventory.services.exemplars import crop_region

TOP_K = 5


@dataclass
class Query:
    region_id: int
    card_id: str
    capture_id: int
    vector: np.ndarray
    # The crop itself, kept so a re-ranking layer can look at the picture rather
    # than only its embedding. Small (a few hundred pixels square) and there are
    # as many as there are labelled cards.
    crop_image: object | None = None


@dataclass
class EvalResult:
    queries: int
    pool: int
    top1: float
    top5: float
    median_rank: float
    mrr: float
    wrong_printing: int  # ranked a different printing of the right card first
    wrong_card: int
    model_version: str
    misses: list[dict] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{self.queries} queries · pool {self.pool:,} · "
            f"top1 {self.top1:.0%} · top5 {self.top5:.0%} · "
            f"{self.wrong_printing} wrong printing / {self.wrong_card} wrong card"
        )

    def as_json(self) -> str:
        payload = {k: v for k, v in self.__dict__.items() if k not in {"misses"}}
        payload["misses"] = self.misses[:25]
        return json.dumps(payload, indent=2)


def collect_queries(repo: VisionRepository, embedder) -> list[Query]:
    """Every labelled front crop, embedded — the questions we know the answer to.

    Backs are excluded: they carry the same card's identity but none of its
    picture, so scoring them would measure nothing about identification.
    """
    queries: list[Query] = []
    rows = repo.conn.execute(
        "SELECT r.id, r.card_id, i.capture_id, i.path, r.x, r.y, r.w, r.h "
        "FROM region r JOIN image i ON i.id = r.image_id "
        "WHERE r.card_id IS NOT NULL AND r.card_id != '' "
        "AND COALESCE(i.side, 'front') != 'back' ORDER BY r.id"
    ).fetchall()
    for row in rows:
        try:
            crop = crop_region(row["path"], (row["x"], row["y"], row["w"], row["h"]))
        except (FileNotFoundError, OSError):
            continue
        queries.append(
            Query(
                region_id=int(row["id"]),
                card_id=row["card_id"],
                capture_id=int(row["capture_id"]),
                vector=np.asarray(embedder.embed(crop), dtype=np.float32),
                crop_image=crop,
            )
        )
    return queries


def load_pool(
    repo: VisionRepository, model_version: str
) -> tuple[list[str], np.ndarray]:
    """The catalog index as (card_ids, matrix). One matmul searches all of it."""
    rows = repo.conn.execute(
        "SELECT card_id, dim, vector FROM card_embedding WHERE model_version = ? "
        "ORDER BY card_id",
        (model_version,),
    ).fetchall()
    ids: list[str] = []
    vectors: list[np.ndarray] = []
    for row in rows:
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        if vec.size != int(row["dim"]):  # pragma: no cover - corrupt row
            continue
        ids.append(row["card_id"])
        vectors.append(vec)
    if not vectors:
        return [], np.zeros((0, 0), dtype=np.float32)
    return ids, np.vstack(vectors)


def evaluate(
    conn,
    repo: VisionRepository,
    *,
    embedder=None,
    rerank=None,
    top_k: int = TOP_K,
    project: bool = True,
) -> EvalResult:
    """Score the identifier against the corpus it is meant to recognise.

    ``rerank`` is the seam the later layers plug into: given the query crop and the
    ranked shortlist, it returns a reordered shortlist. Passing None measures the
    embedding alone, which is what makes each layer's contribution attributable.

    ``project`` applies the installed photo-to-render map, because the app applies
    it — a benchmark that skipped it would be scoring a configuration nobody runs.
    Pass False to measure the raw encoder.
    """
    from tcginventory.services import identity_projection as ip
    from tcginventory.services.recognizer import get_shared_embedder

    embedder = embedder or get_shared_embedder()
    rotation = ip.load() if project else None
    version = embedder.version
    queries = collect_queries(repo, embedder)
    ids, bank = load_pool(repo, version)
    if not queries or not ids:
        return EvalResult(len(queries), len(ids), 0.0, 0.0, 0.0, 0.0, 0, 0, version)

    index = {cid: i for i, cid in enumerate(ids)}
    names = _names_by_card(conn, ids)

    ranks: list[int] = []
    wrong_printing = wrong_card = 0
    misses: list[dict] = []
    for query in queries:
        if query.card_id not in index:
            continue  # the truth isn't in the index; scoring it would be noise
        scores = bank @ ip.apply(query.vector, rotation)
        order = np.argsort(-scores)
        if rerank is not None:
            order = rerank(query, order, ids, scores)
        ranked = [ids[i] for i in order[: max(top_k, 50)]]
        rank = ranked.index(query.card_id) + 1 if query.card_id in ranked else 10**6
        ranks.append(rank)
        if rank != 1:
            top = ranked[0]
            same_name = names.get(top) and names.get(top) == names.get(query.card_id)
            if same_name:
                wrong_printing += 1
            else:
                wrong_card += 1
            misses.append(
                {
                    "region_id": query.region_id,
                    "truth": query.card_id,
                    "predicted": top,
                    "rank": rank,
                    "same_name": bool(same_name),
                }
            )
    if not ranks:
        return EvalResult(0, len(ids), 0.0, 0.0, 0.0, 0.0, 0, 0, version)

    arr = np.array(ranks, dtype=float)
    return EvalResult(
        queries=len(ranks),
        pool=len(ids),
        top1=float((arr == 1).mean()),
        top5=float((arr <= top_k).mean()),
        median_rank=float(np.median(arr)),
        mrr=float(np.mean(1.0 / arr)),
        wrong_printing=wrong_printing,
        wrong_card=wrong_card,
        model_version=version,
        misses=misses,
    )


def _names_by_card(conn, ids: list[str]) -> dict[str, str]:
    """card_id -> name, for telling a reprint apart from a different card."""
    names: dict[str, str] = {}
    wanted = set(ids)
    for row in conn.execute("SELECT card_id, name FROM card_catalog"):
        if row["card_id"] in wanted:
            names[row["card_id"]] = (row["name"] or "").strip().casefold()
    return names


def record(
    repo: VisionRepository, result: EvalResult, runs: ModelRunRepository | None = None
) -> int:
    """Log a benchmark beside the trained models, so accuracy has a history."""
    runs = runs or ModelRunRepository(repo.conn)
    run_id = runs.start(
        kind=KIND_IDENTITY,
        source="benchmark",
        corpus_images=result.pool,
        corpus_boxes=result.queries,
        corpus_cards=result.pool,
        base_weights=result.model_version,
    )
    runs.finish(
        run_id,
        status="ok",
        map50=result.top1,  # the shared columns, read as identity metrics
        map50_95=result.top5,
        precision=result.mrr,
        recall=1.0 - (result.wrong_printing / max(1, result.queries)),
        metrics_json=result.as_json(),
        notes=result.summary,
    )
    return run_id
