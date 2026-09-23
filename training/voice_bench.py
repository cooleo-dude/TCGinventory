"""Voice-search recovery benchmark (ticket 77). Dev-only, never bundled.

Speaks every distinct card name in a ledger with an offline text-to-speech voice,
runs the clip through the **production** voice-search path —
``voice.transcribe`` (Whisper) then ``voice.parse_query`` (the snap step, fed the
same vocabulary the Inventory grid supplies) — and scores whether the text that
would land in the search field actually finds the card.

Two scores per name:

* ``hit``   — every token of the produced query is a substring of
  ``"<name> <set>"``, i.e. the Inventory grid would show the row. This is the
  user-facing criterion.
* ``exact`` — the query equals the lowercased name.

Synthetic voices are not the user's voice, so the rate is a floor on *systematic*
mishearings (Whisper spelling a proper noun like English), not a promise about a
given microphone. Its value is the per-name CSV: the concrete list of names that
fail, before and after each change to the snap step.

Voices are Piper VITS models from the sherpa-onnx ``tts-models`` release, kept in a
dev cache outside the repo (default ``%LOCALAPPDATA%/tcginventory-bench``), e.g.
``vits-piper-en_US-lessac-medium`` and ``vits-piper-en_US-amy-medium``.

Example:
    python training/voice_bench.py --limit 50
    python training/voice_bench.py --voices lessac,amy --out build/voice_bench.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tcginventory import config  # noqa: E402
from tcginventory.services import voice  # noqa: E402

VOICE_DIRS = {
    "lessac": "vits-piper-en_US-lessac-medium",
    "amy": "vits-piper-en_US-amy-medium",
}


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--db",
        default=None,
        help="ledger to draw names from (read-only); default: the app's",
    )
    p.add_argument(
        "--tts-dir",
        default=str(
            Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
            / "tcginventory-bench"
        ),
        help="dev cache holding the Piper voice directories",
    )
    p.add_argument(
        "--voices", default="lessac", help="comma-separated keys from VOICE_DIRS"
    )
    p.add_argument(
        "--limit", type=int, default=0, help="only the first N names (0 = all)"
    )
    p.add_argument(
        "--with-set",
        action="store_true",
        help='speak "<name> <set>" instead of the name alone',
    )
    p.add_argument("--out", default=str(REPO_ROOT / "build" / "voice_bench.csv"))
    p.add_argument(
        "--no-near", action="store_true", help="disable the one-edit phonetic channel"
    )
    p.add_argument("--no-phonetic", action="store_true", help="spelling channel only")
    p.add_argument(
        "--mode",
        choices=("snap", "constrained", "production", "kws"),
        default="snap",
        help="snap: free-form Whisper + parse_query; constrained: grammar-masked, "
        "unfiltered (records the gap); production: voice.recognize(), i.e. the "
        "constrained decode behind the reject gate with snap as the fallback",
    )
    p.add_argument("--beam", type=int, default=4, help="constrained mode beam width")
    p.add_argument(
        "--unowned",
        type=int,
        default=0,
        help="also speak N catalog names the ledger does NOT hold: a correct answer "
        "for those is *no card*, so they measure false accepts",
    )
    return p.parse_args(argv)


def _unowned(conn: sqlite3.Connection, owned: set[str], n: int) -> list[str]:
    """``n`` English catalog names the ledger does not hold, spread over the
    alphabet deterministically (every k-th distinct name)."""
    rows = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT name FROM card_catalog "
            "WHERE upper(language) = 'EN' OR language IS NULL ORDER BY name"
        )
        if r[0] and r[0] not in owned
    ]
    if not rows or n <= 0:
        return []
    step = max(1, len(rows) // n)
    return rows[::step][:n]


def _vocabulary(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    pairs = [
        (name, set_name or "")
        for name, set_name in conn.execute(
            "SELECT DISTINCT name, set_name FROM item "
            "WHERE name IS NOT NULL ORDER BY name"
        )
    ]
    names = sorted({n for n, _ in pairs})
    sets = sorted({s for _, s in pairs if s})
    return pairs, names, sets


def _tts(tts_dir: Path, key: str):
    import sherpa_onnx

    folder = tts_dir / VOICE_DIRS[key]
    model = next(folder.glob("*.onnx"))
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(model),
                tokens=str(folder / "tokens.txt"),
                data_dir=str(folder / "espeak-ng-data"),
            ),
            num_threads=2,
        )
    )
    return sherpa_onnx.OfflineTts(cfg)


def _pcm16(samples, gain: float = 0.9) -> bytes:
    arr = np.asarray(samples, dtype=np.float32)
    peak = float(np.max(np.abs(arr))) or 1.0
    return (np.clip(arr / peak * gain, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _hit(query: str, name: str, set_name: str) -> bool:
    haystack = f"{name} {set_name}".lower()
    tokens = query.split()
    return bool(tokens) and all(t in haystack for t in tokens)


def _wrong(query: str, name: str, pairs: list[tuple[str, str]]) -> bool:
    """A miss that would still *show a card* — the wrong one. Worse than an
    empty grid, and invisible in the hit rate alone."""
    return bool(query) and any(
        other != name and _hit(query, other, other_set) for other, other_set in pairs
    )


def _kws_bench(args, targets, names, sets) -> int:
    """Ticket 79: for each owned name, speak it alone (does the end-word spotter
    fire falsely?) and followed by " finish" (does it fire, how long after the
    word ends, and does the trimmed clip still recognise the name?)."""
    from tcginventory.services import voice_kws

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    chunk_ms = 100
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["voice", "name", "false_fire", "fired", "delay_ms", "query", "hit"]
        )
        for key in args.voices.split(","):
            tts = _tts(Path(args.tts_dir), key.strip())
            rate = tts.sample_rate
            false = fired = hits = n = 0
            delays: list[float] = []
            t_start = time.perf_counter()
            for name, set_name, _owned in targets:
                n += 1
                # A microphone keeps streaming after the speaker stops, and the
                # spotter confirms a keyword on the blanks that follow it; the
                # synthetic clip ends abruptly, so give it the room a mic would.
                silence = b"\x00\x00" * (rate // 2)
                # 1. The bare name must not end capture.
                audio = tts.generate(name, sid=0, speed=1.0)
                pcm = _pcm16(audio.samples) + silence
                spotter = voice_kws.EndWordSpotter()
                false_fire = any(
                    spotter.feed(pcm[i : i + rate * chunk_ms // 1000 * 2], rate)
                    for i in range(0, len(pcm), rate * chunk_ms // 1000 * 2)
                )
                false += false_fire
                # 2. "<name> finish" must fire, soon, and leave the name intact.
                audio = tts.generate(f"{name} finish", sid=0, speed=1.0)
                pcm = _pcm16(audio.samples) + silence
                # Where speech ends: last sample above 2% of full scale.
                samples = np.frombuffer(pcm, dtype="<i2")
                loud = np.nonzero(np.abs(samples) > 655)[0]
                speech_end_ms = (
                    (int(loud[-1]) if len(loud) else len(samples)) * 1000 / rate
                )
                spotter = voice_kws.EndWordSpotter()
                step = rate * chunk_ms // 1000 * 2
                fire_at_ms = None
                for i in range(0, len(pcm), step):
                    if spotter.feed(pcm[i : i + step], rate):
                        fire_at_ms = (i + step) / 2 * 1000 / rate
                        break
                query = ""
                hit = False
                if fire_at_ms is not None:
                    fired += 1
                    delays.append(fire_at_ms - speech_end_ms)
                    heard = pcm[: int(fire_at_ms / 1000 * rate) * 2]
                    clip = voice_kws.trim_end_word(heard, rate=rate)
                    query = voice.recognize(clip, rate, names, sets)
                    hit = _hit(query, name, set_name)
                    hits += hit
                writer.writerow(
                    [
                        key,
                        name,
                        int(false_fire),
                        int(fire_at_ms is not None),
                        ""
                        if fire_at_ms is None
                        else f"{fire_at_ms - speech_end_ms:.0f}",
                        query,
                        int(hit),
                    ]
                )
                fh.flush()
                if n % 50 == 0:
                    elapsed = time.perf_counter() - t_start
                    print(f"  {key}: {n}/{len(targets)}  ({elapsed:.0f}s)")
            d = np.asarray(delays) if delays else np.zeros(1)
            print(
                f"{key:8s} n={n:4d}  false fire {false / n:6.1%}  "
                f"fired {fired / n:6.1%}  delay after word end: "
                f"median {np.median(d):.0f} ms  p90 {np.percentile(d, 90):.0f} ms  "
                f"max {d.max():.0f} ms  |  trimmed clip still hits "
                f"{hits / max(1, fired):6.1%}"
            )
    print(f"\nCSV: {out}")
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.no_near:
        voice.PHONETIC_NEAR = False
    if args.no_phonetic:
        voice.PHONETIC_SNAP = False
    db = Path(args.db) if args.db else config.data_dir() / "inventory.db"
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    pairs, names, sets = _vocabulary(conn)
    # One row per distinct name; the first set it appears with is the one spoken.
    seen: dict[str, str] = {}
    for name, set_name in pairs:
        seen.setdefault(name, set_name)
    targets = [(name, set_name, True) for name, set_name in seen.items()]
    if args.limit:
        targets = targets[: args.limit]
    targets += [(name, "", False) for name in _unowned(conn, set(names), args.unowned)]
    conn.close()
    print(
        f"{len(targets)} names, vocabulary {len(names)} names / {len(sets)} sets, "
        f"from {db}"
    )

    if args.mode == "kws":
        return _kws_bench(args, [t for t in targets if t[2]], names, sets)

    decoder = trie = None
    if args.mode == "constrained":
        from tcginventory.services import voice_decode

        t0 = time.perf_counter()
        decoder = voice_decode.ConstrainedWhisper()
        trie = voice_decode.build_grammar(names, sets)
        print(f"constrained decoder ready in {time.perf_counter() - t0:.1f}s")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    totals: dict[str, dict[str, int]] = {}
    failures: list[tuple[str, str, str, str]] = []
    t_start = time.perf_counter()
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "voice",
                "name",
                "set",
                "spoken",
                "transcript",
                "query",
                "hit",
                "exact",
                "wrong",
                "owned",
                "gap",
            ]
        )
        for key in args.voices.split(","):
            tts = _tts(Path(args.tts_dir), key.strip())
            rate = tts.sample_rate
            tally = totals.setdefault(
                key,
                {"n": 0, "hit": 0, "exact": 0, "wrong": 0, "unowned": 0, "false": 0},
            )
            for name, set_name, owned in targets:
                spoken = f"{name} {set_name}".strip() if args.with_set else name
                audio = tts.generate(spoken, sid=0, speed=1.0)
                gap = ""
                if args.mode == "production":
                    transcript = ""
                    query = voice.recognize(_pcm16(audio.samples), rate, names, sets)
                elif decoder is not None:
                    from tcginventory.services import voice_decode

                    samples = np.asarray(audio.samples, dtype=np.float32)
                    peak = float(np.max(np.abs(samples))) or 1.0
                    decoded = decoder.decode_scored(
                        voice_decode.resample(samples / peak * 0.9, rate),
                        trie,
                        beam_width=args.beam,
                    )
                    # Recorded unfiltered so the reject margin can be chosen
                    # from the CSV; the production gate is voice.REJECT_GAP.
                    phrases = decoded.phrases
                    gap = f"{decoded.gap:.2f}"
                    transcript = " | ".join(phrases)
                    query = " ".join(phrases).lower()
                else:
                    transcript = voice.transcribe(_pcm16(audio.samples), rate)
                    query = voice.parse_query(transcript, names, sets)
                if owned:
                    hit = _hit(query, name, set_name)
                    exact = query == name.lower() or query == spoken.lower()
                    wrong = not hit and _wrong(query, name, pairs)
                    tally["n"] += 1
                    tally["hit"] += hit
                    tally["exact"] += exact
                    tally["wrong"] += wrong
                else:
                    # The right answer is no owned card at all.
                    hit = exact = False
                    wrong = _wrong(query, name, pairs)
                    tally["unowned"] += 1
                    tally["false"] += wrong
                writer.writerow(
                    [
                        key,
                        name,
                        set_name,
                        spoken,
                        transcript,
                        query,
                        int(hit),
                        int(exact),
                        int(wrong),
                        int(owned),
                        gap,
                    ]
                )
                fh.flush()
                if owned and not hit:
                    failures.append((key, name, transcript, query))
                if (tally["n"] + tally["unowned"]) % 50 == 0:
                    print(
                        f"  {key}: {tally['n'] + tally['unowned']}/{len(targets)}  "
                        f"hit {tally['hit']}  ({time.perf_counter() - t_start:.0f}s)"
                    )
    print()
    for key, tally in totals.items():
        n = tally["n"] or 1
        line = (
            f"{key:8s} n={tally['n']:4d}  hit {tally['hit'] / n:6.1%}  "
            f"exact {tally['exact'] / n:6.1%}  wrong card {tally['wrong'] / n:6.1%}"
        )
        if tally["unowned"]:
            line += (
                f"  |  unowned n={tally['unowned']}  "
                f"false accept {tally['false'] / tally['unowned']:6.1%}"
            )
        print(line)
    if failures:
        print(f"\n{len(failures)} misses (voice, name -> heard -> query):")
        for key, name, transcript, query in failures[:80]:
            print(f"  {key:7s} {name!r} -> {transcript!r} -> {query!r}")
        if len(failures) > 80:
            print(f"  … {len(failures) - 80} more in {out}")
    print(f"\nCSV: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
