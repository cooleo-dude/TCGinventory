"""Voice search (tickets 76, 77) — offline speech to a search-bar query. Qt-free.

:func:`recognize` is the entry point the microphone button calls. Given the
grid's vocabulary it decodes the clip **under a grammar** of those names, sets
and numbers (:mod:`voice_decode`), refuses an answer the grammar had to force
(``REJECT_GAP``), and otherwise falls back to the two halves below, which are
deliberately separate so each tests on its own:

* :func:`transcribe` turns 16-bit PCM into free-form text with Whisper
  ``base.en`` running on sherpa-onnx. The model ships as package data beside the
  DINOv2 weights and is located by :func:`voice_model`; :func:`available`
  reports whether it is installed so the UI can grey the microphone out instead
  of failing on click.
* :func:`parse_query` turns what Whisper heard into what the Inventory filter
  bar wants: spoken number forms become the catalog's ``25/102`` shape, and card
  and set names snap to values that actually exist in the grid — by spelling,
  and by sound (:func:`phonetic_key`), so "Celiby" still filters to Celebi.

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
# Speech -> query
# --------------------------------------------------------------------------

_grammar_cache: tuple[int, object] | None = None
_decoder = None


def _grammar(names: list[str], sets: list[str], numbers: list[str]):
    """The constrained-decoding grammar for this vocabulary, rebuilt only when
    the vocabulary changes (the grid's rows between refreshes)."""
    global _grammar_cache
    from tcginventory.services import voice_decode

    key = hash((tuple(names), tuple(sets), tuple(numbers)))
    if _grammar_cache is None or _grammar_cache[0] != key:
        _grammar_cache = (key, voice_decode.build_grammar(names, sets, numbers))
    return _grammar_cache[1]


def recognize(
    pcm16: bytes,
    rate: int = SAMPLE_RATE,
    names: Iterable[str] = (),
    sets: Iterable[str] = (),
    numbers: Iterable[str] = (),
    strict: bool = False,
) -> str:
    """Speech to the text for the search field, the way the app does it.

    With a vocabulary, the clip is decoded **under the grammar** of those names,
    sets and collector numbers (ticket 77): Whisper may only spell out a real
    entry, so the answer is a catalog name chosen by sound, never an English
    word that resembles one. If constrained decoding is unavailable or hears
    nothing legal, fall back to a free-form transcript snapped by
    :func:`parse_query` — unless ``strict``, which returns ``""`` instead:
    always-listening mode (ticket 80) uses it, because a free-form guess
    landing in an unwatched search field is worse than nothing.
    Blocking — call from a worker thread.
    """
    if not pcm16:
        return ""
    names, sets, numbers = sorted(set(names)), sorted(set(sets)), sorted(set(numbers))
    if names or sets:
        try:
            phrases = _recognize_constrained(pcm16, rate, names, sets, numbers)
        except Exception:  # pragma: no cover - a missing runtime degrades, not fails
            phrases = []
        if phrases:
            return " ".join(p for p in phrases if p).lower()  # "" = an end word
        if strict:
            return ""
    elif strict:
        return ""
    return parse_query(transcribe(pcm16, rate), names, sets)


#: A constrained answer this many nats less likely than the free transcript is
#: refused: the clip said something outside the vocabulary, and naming the
#: nearest card would be a confident wrong answer. Chosen on the benchmark's
#: owned/unowned split (``training/voice_bench.py --unowned``, 2026-09-22):
#: at 10, 79.5% of owned names were still recovered while wrong cards fell
#: from 13.5% to 1.0% and false accepts on unowned names from 88.5% to 4.0%.
REJECT_GAP = 10.0


def _recognize_constrained(pcm16, rate, names, sets, numbers) -> list[str]:
    global _decoder
    from tcginventory.services import voice_decode

    if _decoder is None:
        _decoder = voice_decode.ConstrainedWhisper()
    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    audio = voice_decode.resample(samples, rate)
    decoded = _decoder.decode_scored(audio, _grammar(names, sets, numbers))
    if decoded.gap > REJECT_GAP:
        return []
    return decoded.phrases


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
#: Words that end capture (ticket 79). Recognition absorbs them too, so one
#: left at the tail of a clip never reaches the search field.
END_WORDS = ("finish", "done")

_FILLER = (
    frozenset(
        "um uh er ah the a an card cards show me search find look for please "
        "up filter and of to in at it is s".split()
    )
    | frozenset(END_WORDS)
    | frozenset({"finnish"})  # how Whisper likes to spell the end word
)

#: Spoken forms of the card-name suffixes, folded to how the catalog writes
#: them before any snapping: "Charizard X" is *Charizard ex*, not Charizard GX,
#: and "V Max" is one token. Keys are word tuples, values the catalog token.
_SUFFIXES: dict[tuple[str, ...], str] = {
    ("x",): "ex",
    ("e", "x"): "ex",
    ("ecks",): "ex",
    ("g", "x"): "gx",
    ("v", "max"): "vmax",
    ("v", "star"): "vstar",
    ("delta",): "δ",
}
_SUFFIX_MAX = max(len(k) for k in _SUFFIXES)


def _fold_suffixes(words: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(words):
        for n in range(_SUFFIX_MAX, 0, -1):
            key = tuple(words[i : i + n])
            if key in _SUFFIXES:
                out.append(_SUFFIXES[key])
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


#: A leading-word prefix of a name is only a candidate when it is a word in its
#: own right: "Mr." and "M" from "Mr. Mime" / "M Rayquaza EX" must not catch
#: every spoken "more" and "am".
MIN_PREFIX_LETTERS = 4

#: Below this ratio a spoken span is not "the same name, misheard" *by spelling*.
SNAP_THRESHOLD = 0.8
#: Whether spans may also snap by sound (ticket 77). Off only in tests that pin
#: the spelling channel on its own.
PHONETIC_SNAP = True
#: Whether a key one edit away may snap too (keys of four or more letters).
PHONETIC_NEAR = True
#: Scores a phonetic snap earns in the segmentation: below an exact spelling
#: match, above the spelling bar, so sound never outranks a name spelled right.
PHONETIC_EQUAL_SCORE = 0.78
PHONETIC_NEAR_SCORE = 0.7
#: A span shorter than this (letters) never snaps — "no" must not become "Neo".
MIN_SNAP_LETTERS = 3


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


_VOWELS = frozenset("AEIOU")


def phonetic_key(text: str) -> str:
    """A Metaphone-style sound key: what a word *sounds* like, spelling aside.

    Whisper's errors on proper nouns are phonetic — it hears "Celebi" and writes
    "Celiby", hears "Abomasnow" and writes "Obama's no" — so two spellings that
    say the same thing must key the same. Original Metaphone, with one domain
    change: every leading vowel keys as ``A``, because the vowel Whisper picks
    for an unstressed opening syllable is noise ("Obama" / "Aboma").

    Digits pass through, so "Base Set 2" keys apart from "Base Set".
    """
    s = "".join(ch for ch in text.upper() if ch.isalnum())
    if not s:
        return ""
    # Leading clusters whose first letter is silent.
    if s[:2] in ("AE", "GN", "KN", "PN", "WR"):
        s = s[1:]
    elif s[:1] == "X":
        s = "S" + s[1:]
    elif s[:2] == "WH":
        s = "W" + s[1:]
    out: list[str] = []
    n = len(s)
    for i, ch in enumerate(s):
        prev = s[i - 1] if i else ""
        nxt = s[i + 1] if i + 1 < n else ""
        nxt2 = s[i + 2] if i + 2 < n else ""
        if ch == prev and ch != "C":
            continue  # doubled letters say nothing extra
        if ch.isdigit():
            out.append(ch)
        elif ch in _VOWELS:
            if i == 0:
                out.append("A")
        elif ch == "B":
            if not (prev == "M" and i == n - 1):
                out.append("B")
        elif ch == "C":
            if nxt == "I" and nxt2 == "A":
                out.append("X")
            elif nxt == "H":
                out.append("K" if prev == "S" else "X")
            elif nxt in ("I", "E", "Y"):
                if prev != "S":
                    out.append("S")
            else:
                out.append("K")
        elif ch == "D":
            out.append("J" if nxt == "G" and nxt2 in ("E", "Y", "I") else "T")
        elif ch == "G":
            if nxt == "H" and not (nxt2 and nxt2 in _VOWELS):
                continue  # "GH" before a consonant or at the end is silent
            if nxt == "N":
                continue
            if nxt in ("I", "E", "Y") and prev != "G":
                out.append("J")
            else:
                out.append("K")
        elif ch == "H":
            if nxt and nxt in _VOWELS and prev not in ("C", "S", "P", "T", "G"):
                out.append("H")
        elif ch == "K":
            if prev != "C":
                out.append("K")
        elif ch == "P":
            if nxt == "H":
                out.append("F")
            else:
                out.append("P")
        elif ch == "Q":
            out.append("K")
        elif ch == "S":
            if nxt == "H" or (nxt == "I" and nxt2 in ("O", "A")):
                out.append("X")
            else:
                out.append("S")
        elif ch == "T":
            if nxt == "I" and nxt2 in ("O", "A"):
                out.append("X")
            elif nxt == "H":
                out.append("0")
            elif not (nxt == "C" and nxt2 == "H"):
                out.append("T")
        elif ch == "V":
            out.append("F")
        elif ch == "W":
            if nxt and nxt in _VOWELS:
                out.append("W")
        elif ch == "X":
            out.append("KS")
        elif ch == "Y":
            if nxt and nxt in _VOWELS:
                out.append("Y")
        elif ch == "Z":
            out.append("S")
        else:  # F J L M N R
            out.append(ch)
    key = "".join(out)
    # "PH" -> F consumed the H above via the P branch; drop the H that followed.
    return key.replace("FH", "F")


def _key_distance(a: str, b: str) -> int:
    """Levenshtein distance, for keys that are a few letters long."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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


def set_shorthands(set_name: str) -> list[tuple[str, str]]:
    """How people refer to a set in one or two words, and the search token
    each stands for (ticket 78).

    Nobody says "SVP Black Star Promos"; they say "promo". Thirteen of the
    ledger's sets are promo sets, and without this the word was outside the
    vocabulary — the grammar could not say it and the sound channel rewrote it
    as the nearest owned card ("Prime", from Prime Catcher). The token is a
    substring of every spelling the catalog uses (Promo, Promos, Promotional),
    so it filters to all of them at once.
    """
    low = set_name.lower()
    out: list[tuple[str, str]] = []
    if "promo" in low:
        out += [("promo", "promo"), ("promos", "promo")]
    if "black star" in low:
        out += [
            ("black star", "black star"),
            ("black star promo", "black star promo"),
            ("black star promos", "black star promo"),
        ]
    return out


@dataclass
class _Candidates:
    """What a spoken span may snap to, indexed two ways.

    ``spelling`` maps a normalised candidate to the text to search for;
    ``sound`` maps a :func:`phonetic_key` to the candidates that share it.
    Every full name and set is a candidate, and so is each leading-word prefix
    of a multi-word one, mapping to *that prefix*: saying "Umbreon" against a
    grid holding "Umbreon VMAX" and "Umbreon V" yields the token ``umbreon``,
    which the substring filter matches on both. Sets are added first so a full
    name wins a collision with a set prefix.
    """

    spelling: dict[str, str]
    sound: dict[str, list[str]]

    def __bool__(self) -> bool:
        return bool(self.spelling)


def _candidates(names: Iterable[str], sets: Iterable[str]) -> _Candidates:
    spelling: dict[str, str] = {}
    sound: dict[str, list[str]] = {}
    # Shorthands first, so "promo" is an exact spelling match before any
    # prefix of a longer name can claim it by sound.
    for set_name in sets:
        for spoken, token in set_shorthands(set_name):
            norm = _norm(spoken)
            if norm not in spelling:
                spelling[norm] = token
                sound.setdefault(phonetic_key(spoken), []).append(norm)
    for group in (sets, names):
        for value in group:
            # "&", "+" and the like are not words: "Black & White" must yield
            # the prefix "Black", never "Black &" (the baseline benchmark put
            # "black &" in the search field for "Black Kyurem").
            parts = [p for p in value.split() if _norm(p)]
            for n in range(len(parts), 0, -1):
                prefix = " ".join(parts[:n])
                norm = _norm(prefix)
                if n < len(parts) and len(norm) < MIN_PREFIX_LETTERS:
                    continue
                if norm and norm not in spelling:
                    spelling[norm] = prefix
                    sound.setdefault(phonetic_key(prefix), []).append(norm)
    return _Candidates(spelling, sound)


_MAX_SPAN = 4


def _snap_span(words: list[str], candidates: _Candidates) -> tuple[str, float] | None:
    """The candidate ``words`` most resemble and how closely, or ``None``.

    Two channels. **Spelling** first: the closest candidate by character ratio,
    if it clears ``SNAP_THRESHOLD``. Then **sound**: a candidate whose phonetic
    key equals the span's, or sits one edit away when the keys are long enough
    to make that meaningful. A sound match scores below any exact spelling
    match, so a name Whisper spelled right is never displaced by one that merely
    sounds like it. Spans under ``MIN_SNAP_LETTERS`` never snap.
    """
    span = _norm(" ".join(words))
    if sum(ch.isalpha() for ch in span) < MIN_SNAP_LETTERS and not span.isdigit():
        return None
    if not span or all(w in _FILLER for w in words):
        return None  # "and" must never become Entei, however alike they sound
    match = get_close_matches(span, candidates.spelling, n=1, cutoff=SNAP_THRESHOLD)
    if match:
        return candidates.spelling[match[0]], SequenceMatcher(
            None, span, match[0]
        ).ratio()
    if not PHONETIC_SNAP:
        return None
    key = phonetic_key(span)
    if len(key) < 3:
        return None  # "feel" and "Volo" both key FL; two consonants prove nothing
    same = candidates.sound.get(key)
    if same:
        # Several spellings share the sound: take the one closest in letters.
        best = max(same, key=lambda norm: SequenceMatcher(None, span, norm).ratio())
        return candidates.spelling[best], PHONETIC_EQUAL_SCORE
    if PHONETIC_NEAR and len(key) >= 4:
        near = [
            norm
            for other, norms in candidates.sound.items()
            if abs(len(other) - len(key)) <= 1 and _key_distance(key, other) == 1
            for norm in norms
        ]
        if near:
            best = max(near, key=lambda norm: SequenceMatcher(None, span, norm).ratio())
            return candidates.spelling[best], PHONETIC_NEAR_SCORE
    return None


def _snap_all(
    words: list[str], skip: list[bool], candidates: _Candidates
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
    words = _fold_suffixes(_normalise_numbers(_words(transcript)))
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
