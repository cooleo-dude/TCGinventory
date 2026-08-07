"""The jobs that build and maintain the recognition models.

Each one is ``job(conn) -> sentence``: it does the work, and returns the line the
Models window shows and writes to the run log. They live here rather than in a
view because none of them needs Qt — which is also what makes them testable
headless, and what lets the detector's trainer run in a separate interpreter
without dragging the UI along.

The heavier jobs (training the detector, indexing the catalog) are long enough
that the window runs them on a worker thread; nothing here assumes that, so a
script or a scheduled task can call them just as well.
"""

from __future__ import annotations

import json


def train_detector(_conn) -> str:
    """Train the card detector: export the corpus, drive the worker, record it."""
    import subprocess

    from tcginventory.data.vision import (
        ModelRunRepository,
        VisionRepository,
        open_vision,
    )
    from tcginventory.services import detector, training

    env = training.probe_training_env()
    conn = open_vision()
    try:
        repo = VisionRepository(conn)
        runs = ModelRunRepository(conn)
        job = training.prepare_detector_training(
            repo,
            runs,
            env=env,
            dataset_dir=trainingenv_dataset_dir(),
            out_path=detector.trained_model_destination(),
            source="ui",
        )
        proc = subprocess.run(
            job.command,
            capture_output=True,
            text=True,
            # utf-8 explicitly: the default decodes as the console codepage
            # (cp1252 here), and one byte outside it kills the reader thread
            # and takes the whole of the worker's output with it.
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-2:]
            training.fail_detector_training(job, runs, " ".join(tail)[:300])
            raise RuntimeError(f"Training failed: {' '.join(tail)[:200]}")
        outcome = training.complete_detector_training(
            job, runs, worker_output=proc.stdout or ""
        )
        return outcome.summary
    finally:
        conn.close()


def trainingenv_dataset_dir():
    """Where the exported YOLO dataset goes — beside the corpus, not in the repo."""
    from tcginventory import config

    return config.data_dir() / "vision" / "datasets" / "detector"


def train_condition(_conn) -> str:
    """Fit the condition grader from the graded corpus and record the run."""
    from tcginventory.data.vision import (
        ModelRunRepository,
        VisionRepository,
        open_vision,
    )
    from tcginventory.services.condition_train import train_condition_model

    conn = open_vision()
    try:
        repo = VisionRepository(conn)
        result = train_condition_model(repo, ModelRunRepository(conn), source="ui")
        if not result.ok:
            raise RuntimeError(result.reason)
        metrics = result.metrics
        return (
            f"{result.samples} cards · {metrics.accuracy:.0%} exact · "
            f"{metrics.mean_distance:.2f} grades off "
            f"(baseline {metrics.baseline_distance:.2f})"
        )
    finally:
        conn.close()


def build_training_env(_conn, flavour: str = "cpu") -> str:
    """Build (or rebuild) the training environment in the requested flavour.

    Runs in a worker thread: creating the virtualenv and downloading the stack take
    minutes. An existing environment is removed first, so switching flavours can't
    leave two torch builds fighting over the same site-packages.
    """
    import subprocess

    from tcginventory.services import training, trainingenv

    found = trainingenv.discover_interpreters()
    if not found:
        raise RuntimeError(trainingenv.NO_PYTHON_HINT)
    interpreter = found[0]
    if flavour == trainingenv.FLAVOUR_GPU and trainingenv.detect_gpu() is None:
        raise RuntimeError("No NVIDIA GPU is visible to nvidia-smi on this machine.")

    if trainingenv.is_bootstrapped():
        trainingenv.remove_env()

    for label, argv in trainingenv.bootstrap_steps(interpreter, flavour=flavour):
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            # utf-8 explicitly: the default decodes as the console codepage
            # (cp1252 here), and one byte outside it kills the reader thread
            # and takes the whole of the worker's output with it.
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise RuntimeError(f"{label} failed: {' '.join(tail)[:300]}")

    # Validate what was built, and record what torch can *actually* do — an install
    # can quietly fall back to CPU wheels.
    env = training.probe_training_env(str(trainingenv.env_python()))
    if not env.ready:
        raise RuntimeError(f"Environment built but not usable: {env.detail}")
    built, described = trainingenv.probe_flavour(trainingenv.env_python())
    training.remember_training_env(env)
    training.remember_training_flavour(built)
    if flavour == trainingenv.FLAVOUR_GPU and built != trainingenv.FLAVOUR_GPU:
        raise RuntimeError(
            f"Installed, but the GPU is not usable from it — {described}"
        )
    return f"{env.detail} · {described}"


def remove_training_env(_conn) -> str:
    """Delete the training virtualenv; models and corpus are left alone."""
    from tcginventory.services import trainingenv
    from tcginventory.services.operations import human_bytes

    target = trainingenv.env_dir()
    reclaimed = trainingenv.remove_env()
    return f"removed {target} — {human_bytes(reclaimed)} reclaimed, models kept"


def index_catalog(conn, scope: str) -> str:
    """Embed a scope of the catalog, then hand back unneeded art automatically."""
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services import catalog_index as ci

    vision = open_vision()
    try:
        repo = VisionRepository(vision)
        summary = ci.index_catalog(conn, repo, scope)
        # No reclaim here. It used to run automatically, back when the art was kept
        # as a separate archive and the downloads were transient. With one store,
        # reclaiming straight after a sweep would delete exactly what the sweep
        # just fetched — and the reason for keeping it is that re-embedding for a
        # future model reads from disk. Reclaim is the tile beside this one.
        return summary.summary
    finally:
        vision.close()


def reclaim_catalog_art(conn) -> str:
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services import catalog_index as ci

    vision = open_vision()
    try:
        return ci.reclaim(conn, VisionRepository(vision)).summary
    finally:
        vision.close()


def reindex_corpus(_conn) -> str:
    """Re-embed every labelled crop against the current model."""
    from tcginventory.data.vision import VisionRepository, open_vision
    from tcginventory.services import exemplars
    from tcginventory.services.recognizer import get_shared_embedder

    vision = open_vision()
    try:
        repo = VisionRepository(vision)
        indexed = exemplars.reindex_all(repo, get_shared_embedder(), skip_existing=True)
        total = repo.region_embedding_count()
        return f"{indexed:,} newly indexed · {total:,} exemplars in the corpus"
    finally:
        vision.close()


# The fit converges well before the catalog runs out: measured, 3,000 cards
# (6,000 pairs) reaches the same accuracy as 10,000, while a thinner fit costs
# more top-5 than it returns. 4,000 leaves margin without a long wait.
_PROJECTION_CARDS = 4000


def fit_photo_projection(conn) -> str:
    """Fit the map from photo-domain embeddings onto catalog-render ones.

    Two sources of truth: catalog art degraded to look photographed (broad, but
    an imitation) and the crops you have actually labelled (few, but the real
    thing). Logged as a model run like the trained models, with **held-out**
    accuracy — the corpus pairs are also the benchmark's queries, so an in-sample
    score would flatter itself.
    """
    import numpy as np

    from tcginventory.data.vision import (
        KIND_IDENTITY,
        ModelRunRepository,
        VisionRepository,
        open_vision,
    )
    from tcginventory.services import identity_eval as ie
    from tcginventory.services import identity_projection as ip
    from tcginventory.services.recognizer import get_shared_embedder

    vision = open_vision()
    try:
        repo = VisionRepository(vision)
        runs = ModelRunRepository(vision)
        embedder = get_shared_embedder()
        ids, bank = ie.load_pool(repo, embedder.version)
        if not ids:
            return "nothing indexed yet — index the card catalog first"
        targets = {cid: bank[i] for i, cid in enumerate(ids)}
        sample = list(ids)
        np.random.default_rng(7).shuffle(sample)

        run_id = runs.start(
            kind=KIND_IDENTITY,
            source="projection",
            corpus_cards=len(ids),
            base_weights=embedder.version,
        )
        try:
            pairs_x, pairs_y, cards = ip.synthetic_pairs(
                repo, embedder, sample[:_PROJECTION_CARDS], targets
            )
            if pairs_x is None:
                runs.finish(run_id, status="skipped", notes="not enough usable art")
                return "not enough usable art to fit — index more of the catalog first"
            corpus = ip.corpus_pairs(repo, embedder.version, targets)
            report = ip.cross_validate(conn, repo, embedder, (pairs_x, pairs_y))
            rotation, result = ip.fit_rotation(
                (pairs_x, pairs_y), (corpus[0], corpus[1]), cards=cards
            )
            ip.save(rotation)
        except Exception as exc:  # the run log should not lose a failure
            runs.finish(run_id, status="failed", notes=str(exc)[:400])
            raise

        note = result.summary
        if report is not None:
            runs.finish(
                run_id,
                status="ok",
                map50=report.fitted_top1,
                map50_95=report.fitted_top5,
                precision=report.baseline_top1,
                recall=report.baseline_top5,
                metrics_json=json.dumps(report.__dict__),
                notes=f"{result.summary} · {report.summary}",
            )
            return f"photo matching fitted · {report.summary}"
        runs.finish(run_id, status="ok", notes=note)
        return f"photo matching fitted · {note}"
    finally:
        vision.close()


def forget_photo_projection(_conn) -> str:
    """Remove the fitted map, putting identification back on the raw encoder."""
    from tcginventory.services import identity_projection as ip

    path = ip.matrix_path()
    if not path.is_file():
        return "no photo matching was fitted"
    path.unlink()
    return "photo matching removed — suggestions use the encoder as-is"


def benchmark_identity(conn) -> str:
    """Measure identification, one layer at a time.

    Each layer is scored on top of the ones before it, so the window can show
    what each is actually worth rather than one number for the whole stack. The
    breakdown goes into ``metrics_json``; the headline figures go into the shared
    columns, so an identity run reads like any other model run.

    The queries are the labelled crops and the pool is the live index, which is
    the same pool the app searches — so the number means what it says.
    """
    import json

    import numpy as np

    from tcginventory.data.vision import (
        KIND_IDENTITY,
        ModelRunRepository,
        VisionRepository,
        open_vision,
    )
    from tcginventory.services import catalog_index as ci
    from tcginventory.services import identity_eval as ie
    from tcginventory.services import identity_patches as ip
    from tcginventory.services.recognizer import get_shared_embedder

    vision = open_vision()
    try:
        repo = VisionRepository(vision)
        runs = ModelRunRepository(vision)
        embedder = get_shared_embedder()
        if not ie.load_pool(repo, embedder.version)[0]:
            return "nothing indexed yet — index the card catalog first"

        english = set(ci.candidate_pool(conn, repo, embedder.version, languages=["EN"]))

        def scoped(inner=None):
            """Rank the chosen languages first, exactly as a scoped box does."""

            def rerank(query, order, ids, scores):
                head = [i for i in order[:400] if ids[i] in english]
                tail = [i for i in order[:400] if ids[i] not in english]
                moved = np.array(head + tail + list(order[400:]), dtype=int)
                return inner(query, moved, ids, scores) if inner else moved

            return rerank

        stages = [
            ("encoder", dict(project=False)),
            ("scope", dict(project=False, rerank=scoped())),
            ("projection", dict(project=True, rerank=scoped())),
            ("band", dict(project=True, rerank=scoped(ip.reranker()))),
        ]
        pool_size = len(ie.load_pool(repo, embedder.version)[0])
        run_id = runs.start(
            kind=KIND_IDENTITY,
            source="benchmark",
            base_weights=embedder.version,
            corpus_cards=pool_size,
        )
        try:
            results = {name: ie.evaluate(conn, repo, **kw) for name, kw in stages}
        except Exception as exc:
            runs.finish(run_id, status="failed", notes=str(exc)[:400])
            raise
        final = results["band"]

        # The photo-matching map is fitted partly on the labelled crops, and those
        # crops are exactly these queries. Anything measured downstream of it is
        # therefore scoring data the map has already seen — flag it, because a
        # number that flatters itself is worse than no number. The honest figure
        # for that layer is the leave-one-capture-out one from the fit itself.
        in_sample = _projection_used_the_corpus(runs)

        from tcginventory.services import fitness

        # Identity has no single artifact to promote — it is an encoder plus three
        # corrections, any of which can change under it. What a version pins is
        # therefore the *measurement*: the benchmark this one has to be judged
        # against. Without it every identity run was promoted=0 and
        # current_model('identity') was always None, so a change that halved top-1
        # would have passed in silence (2026-08-07).
        score = fitness.identity_fitness(final.top1, final.top5)
        incumbent = runs.current_model(KIND_IDENTITY, source="benchmark")
        # A hundred-odd labelled crops put a wide error bar on any single figure:
        # at 58 queries the standard error of a 45% top-1 is about 6.5 points, so
        # two benchmarks inside that band are the same benchmark.
        floor = fitness.binomial_noise(final.top1, final.queries)
        was = {}
        if incumbent is not None:
            try:
                was = json.loads(incumbent["metrics_json"] or "{}")
            except (ValueError, TypeError):  # pragma: no cover - corrupt payload
                was = {}
        verdict = fitness.compare(
            score,
            incumbent["fitness"] if incumbent else None,
            floor=floor,
            candidate_data=final.queries,
            incumbent_data=int(was.get("queries") or 0),
        )
        payload = json.loads(final.as_json())
        payload["layers"] = {name: r.top1 for name, r in results.items()}
        payload["layers_top5"] = {name: r.top5 for name, r in results.items()}
        payload["in_sample_from"] = "projection" if in_sample else None
        payload["verdict"] = verdict.reason
        payload["margin"] = verdict.margin
        payload["noise_floor"] = floor
        # A regression is the thing this gate exists to catch: materially worse
        # than the benchmark in use, not merely unlucky.
        payload["regressed"] = bool(
            incumbent is not None and verdict.margin < -floor and not verdict.promote
        )
        runs.finish(
            run_id,
            status="ok",
            map50=final.top1,
            map50_95=final.top5,
            precision=final.mrr,
            recall=1.0 - (final.wrong_printing / max(1, final.queries)),
            fitness=score,
            metrics_json=json.dumps(payload),
            notes=final.summary,
        )
        if verdict.promote:
            runs.promote(run_id)

        gain = final.top1 - results["encoder"].top1
        line = f"{final.summary} · the stack is worth {gain:+.0%} over the encoder"
        if payload["regressed"]:
            line += f" · ⚠ down on the benchmark in use — {verdict.reason}"
        if in_sample:
            line += " (photo matching was fitted on these crops — in-sample)"
        return line
    finally:
        vision.close()


def _projection_used_the_corpus(runs) -> bool:
    """Did the installed photo-matching map learn from the labelled crops?

    If it did, every figure measured downstream of it on those same crops is
    in-sample. Read from the fit's own run rather than guessed.
    """
    import json

    from tcginventory.data.vision import KIND_IDENTITY

    for row in runs.recent(KIND_IDENTITY, limit=40):
        if row["source"] != "projection" or row["status"] != "ok":
            continue
        try:
            return (
                int(json.loads(row["metrics_json"] or "{}").get("corpus_pairs", 0)) > 0
            )
        except (ValueError, TypeError):  # pragma: no cover - corrupt payload
            return False
    return False
