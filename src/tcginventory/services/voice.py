"""Voice search (ticket 76) — offline speech to a search-bar query. Qt-free.

Two halves, deliberately separate so each tests on its own:

* :func:`transcribe` turns 16-bit PCM into text with Whisper ``base.en`` running
  on sherpa-onnx. The model ships as package data beside the DINOv2 weights and
  is located by :func:`voice_model`; :func:`available` reports whether it is
  installed so the UI can grey the microphone out instead of failing on click.
* :func:`parse_query` turns what Whisper heard into what the Inventory filter
  bar wants: spoken number forms become the catalog's ``25/102`` shape, and card
  and set names snap to values that actually exist in the grid, so a slightly
  misheard "Pikachoo" still filters to Pikachu.

Audio *capture* is QtMultimedia and therefore lives in ``ui/views/_voice_input.py``;
this module only ever sees bytes and a sample rate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher, get_close_matches
from importlib import resources
from pathlib import Path

import numpy as np

from tcginventory import config

try:
    import sherpa_onnx as _sherpa
except Exception:  # pragma: no cover - sherpa-onnx missing (e.g. minimal env)
    _sherpa = None

ENCODER_FILENAME = "whisper-base.en-encoder.int8.onnx"
DECODER_FILENAME = "whisper-base.en-decoder.int8.onnx"
TOKENS_FILENAME = "whisper-base.en-tokens.txt"
_FILENAMES = (ENCODER_FILENAME, DECODER_FILENAME, TOKENS_FILENAME)

#: Whisper is trained on 16 kHz audio; sherpa-onnx resamples anything else, but
#: capturing at the native rate skips that work.
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class VoiceModel:
    encoder: Path
    decoder: Path
    tokens: Path


def _complete(directory: Path) -> VoiceModel | None:
    paths = [directory / name for name in _FILENAMES]
    if all(p.is_file() for p in paths):
        return VoiceModel(*paths)
    return None


def voice_model(data_dir: Path | None = None) -> VoiceModel | None:
    """Locate the speech model: the bundled package copy first (dev tree or
    PyInstaller bundle), then a user-dropped set under the data dir. ``None`` if
    neither directory holds all three files — a partial drop is not a model."""
    try:
        bundled = Path(str(resources.files("tcginventory") / "models"))
        found = _complete(bundled)
        if found is not None:
            return found
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    root = data_dir if data_dir is not None else config.data_dir()
    return _complete(root / "voice" / "models")


def available() -> bool:
    """Whether voice search can work here: the runtime is importable and a
    complete model is installed."""
    return _sherpa is not None and voice_model() is not None


_recognizer = None


def _load_recognizer():
    global _recognizer
    if _recognizer is None:
        if _sherpa is None:
            raise RuntimeError("sherpa-onnx is not installed")
        model = voice_model()
        if model is None:
            raise RuntimeError("speech model is not installed")
        _recognizer = _sherpa.OfflineRecognizer.from_whisper(
            encoder=str(model.encoder),
            decoder=str(model.decoder),
            tokens=str(model.tokens),
            language="en",
            task="transcribe",
            num_threads=2,
        )
    return _recognizer


def transcribe(pcm16: bytes, rate: int = SAMPLE_RATE) -> str:
    """Transcribe mono 16-bit little-endian PCM at ``rate`` Hz. Blocking — call
    from a worker thread. The first call also loads the model (~0.5 s)."""
    if not pcm16:
        return ""
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    recognizer = _load_recognizer()
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, samples)
    recognizer.decode_stream(stream)
    return stream.result.text.strip()


# --------------------------------------------------------------------------
# Transcript -> query
# --------------------------------------------------------------------------

_UNITS = {
    "zero": 0,
    "oh": 0,
    "o": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEENS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_WORDS = set(_UNITS) | set(_TEENS) | set(_TENS) | {"hundred", "and"}

#: Spoken separators between a collector number and its denominator.
_SLASH_PHRASES = (("out", "of"), ("of",), ("over",), ("slash",), ("on",))

#: Words Whisper hears that carry no search meaning. Anything else that fails to
#: snap is kept as spoken — a card the user does not own should filter to an
#: honest empty grid, not to everything.
_FILLER = frozenset(
    "um uh er ah the a an card cards show me search find look for please "
    "up filter".split()
)

#: Below this ratio a spoken span is not "the same name, misheard".
SNAP_THRESHOLD = 0.8


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _words(transcript: str) -> list[str]:
    """Lowercase tokens; punctuation dropped except a ``/`` that Whisper wrote
    itself, which is kept as its own token."""
    text = transcript.lower().replace("/", " / ")
    return [w for w in re.split(r"[^a-z0-9/]+", text) if w]


def _digits_run(words: list[str], start: int) -> tuple[str, int] | None:
    """Read one spoken number starting at ``start``. Returns its digit string and
    the index after it, or ``None`` if ``words[start]`` does not begin a number.

    Handles *digit by digit* ("one oh two" → 102) and *place value* ("twenty
    five", "one hundred and two", and the collector's habit of "one sixty five"
    for 165). A number ends where the next one would have to begin: "twenty five
    six" is 25 then 6.
    """
    if start >= len(words):
        return None
    first = words[start]
    if first.isdigit():
        return first, start + 1
    if first not in _NUMBER_WORDS or first == "and":
        return None
    i = start
    digits = ""  # digit-by-digit reading
    value = 0  # place-value reading
    place_value = False
    last: str | None = None
    while i < len(words):
        w = words[i]
        if w in _UNITS:
            if last == "tens":  # twenty *five*
                value += _UNITS[w]
                i += 1
                break
            if last in ("units_done", "teens"):
                break
            digits += str(_UNITS[w])
            value += _UNITS[w]
            last = "units"
        elif w in _TEENS or w in _TENS:
            if last in ("tens", "teens", "units_done"):
                break
            part = _TEENS.get(w) or _TENS[w]
            if last == "units":
                if len(digits) != 1:
                    break
                value = value * 100 + part  # "one sixty" → 160
            else:
                value += part
            place_value = True
            last = "teens" if w in _TEENS else "tens"
        elif w == "hundred":
            if last not in (None, "units"):
                break
            value = (value or 1) * 100
            place_value = True
            last = "hundred"
        elif w == "and":
            following = words[i + 1] if i + 1 < len(words) else ""
            if (
                last != "hundred"
                or following not in _NUMBER_WORDS
                or following == "and"
            ):
                break
        else:
            break
        i += 1
    if i == start:
        return None
    return (str(value) if place_value else digits), i


def _match_slash(words: list[str], i: int) -> int | None:
    for phrase in _SLASH_PHRASES:
        if tuple(words[i : i + len(phrase)]) == phrase:
            return i + len(phrase)
    if i < len(words) and words[i] == "/":
        return i + 1
    return None


def _normalise_numbers(words: list[str]) -> list[str]:
    """Collapse spoken numbers to digits and ``N <sep> M`` to ``N/M``."""
    out: list[str] = []
    i = 0
    while i < len(words):
        run = _digits_run(words, i)
        if run is None:
            out.append(words[i])
            i += 1
            continue
        number, j = run
        after = _match_slash(words, j)
        if after is not None:
            denominator = _digits_run(words, after)
            if denominator is not None:
                number = f"{number}/{denominator[0]}"
                j = denominator[1]
        out.append(number)
        i = j
    return out


def _candidates(names: Iterable[str], sets: Iterable[str]) -> dict[str, str]:
    """Normalised span → the text to search for.

    Every full name and set is a candidate, and so is each leading-word prefix
    of a multi-word one, mapping to *that prefix*: saying "Umbreon" against a
    grid holding "Umbreon VMAX" and "Umbreon V" yields the token ``umbreon``,
    which the substring filter matches on both. Sets are added first so a full
    name wins a collision with a set prefix.
    """
    out: dict[str, str] = {}
    for group in (sets, names):
        for value in group:
            parts = value.split()
            for n in range(len(parts), 0, -1):
                prefix = " ".join(parts[:n])
                key = _norm(prefix)
                if key:
                    out.setdefault(key, prefix)
    return out


_MAX_SPAN = 4


def _snap_span(
    words: list[str], candidates: dict[str, str]
) -> tuple[str, float] | None:
    """The candidate ``words`` most resemble and how closely, or ``None`` when
    nothing clears ``SNAP_THRESHOLD``."""
    span = _norm(" ".join(words))
    if not span:
        return None
    match = get_close_matches(span, candidates, n=1, cutoff=SNAP_THRESHOLD)
    if not match:
        return None
    return candidates[match[0]], SequenceMatcher(None, span, match[0]).ratio()


def _snap_all(
    words: list[str], skip: list[bool], candidates: dict[str, str]
) -> dict[int, tuple[int, str]]:
    """Choose the segmentation of ``words`` into snapped spans that explains the
    most of the transcript: ``{start: (end, text)}``.

    Neither longest-first nor best-first is right on its own. "charizard base
    set" must be *charizard* + *base set*, although "charizard base" resembles
    "charizard" well enough to clear the bar; "charizard vmao" must be the one
    card *Charizard VMAX*, although "charizard" alone is an exact match. Scoring
    a segmentation by ``sum(ratio × words covered)`` and taking the best over
    all of them gets both. Words flagged in ``skip`` (numbers) are never part
    of a span.
    """
    n = len(words)
    best = [0.0] * (n + 1)
    choice: list[tuple[int, str] | None] = [None] * (n + 1)  # (start, text) ending here
    for end in range(1, n + 1):
        best[end], choice[end] = best[end - 1], None  # words[end-1] left as is
        for start in range(max(0, end - _MAX_SPAN), end):
            if any(skip[start:end]):
                continue
            snapped = _snap_span(words[start:end], candidates)
            if snapped is None:
                continue
            score = best[start] + snapped[1] * (end - start)
            if score > best[end]:
                best[end], choice[end] = score, (start, snapped[0])
    spans: dict[int, tuple[int, str]] = {}
    end = n
    while end > 0:
        picked = choice[end]
        if picked is None:
            end -= 1
            continue
        start, text = picked
        spans[start] = (end, text)
        end = start
    return spans


def parse_query(
    transcript: str, names: Iterable[str] = (), sets: Iterable[str] = ()
) -> str:
    """Turn a transcript into the text to place in the Inventory search field.

    Numbers are normalised first; then the remaining words are snapped to a
    known set name, card name or leading part of one when they resemble it
    closely enough (``SNAP_THRESHOLD``), choosing the segmentation that explains
    the most of the transcript. Filler words are dropped; anything else that
    snaps to nothing is kept as spoken.
    """
    words = _normalise_numbers(_words(transcript))
    if not words:
        return ""
    candidates = _candidates(names, sets)
    is_number = [bool(re.fullmatch(r"\d+(/\d+)?", w)) for w in words]
    spans = _snap_all(words, is_number, candidates) if candidates else {}

    parts: list[str] = []
    i = 0
    while i < len(words):
        if i in spans:
            end, text = spans[i]
            parts.append(text.lower())
            i = end
            continue
        if is_number[i] or words[i] not in _FILLER:
            parts.append(words[i])
        i += 1
    return " ".join(parts)
