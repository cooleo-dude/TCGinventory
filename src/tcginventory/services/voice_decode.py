"""Vocabulary-constrained Whisper decoding (ticket 77). Qt-free.

Free-form Whisper hears "Celebi" and writes "Celiby", "Sell-a" or "syllabi":
the sounds are right and the spelling is English. Snapping the text afterwards
recovers some of that (ticket 77's phonetic channel), but the transcript has
already thrown information away. This module keeps it: it drives the bundled
Whisper encoder/decoder pair directly on onnxruntime and, at every decoding
step, masks the token logits so the decoder can only continue a phrase from the
vocabulary — every card name, every set name. The output is then a real name by
construction, chosen by acoustic likelihood among real names.

The grammar is a byte-level trie over ``" " + phrase`` for each phrase (Whisper
tokens carry their leading space). After a complete phrase the decoder may emit
end-of-text or a space that starts another phrase, up to ``max_phrases``. Case
is folded on ASCII so "Celebi" and " celebi" walk the same path. Because one
phrase can be a prefix of another ("Celebi", "Celebi V"), a walk keeps every
reading alive — it is a small NFA, not a single pointer.

The numpy log-mel front end reproduces ``whisper.audio.log_mel_spectrogram``;
run on sherpa-onnx's own test clip, the unconstrained greedy decode of this
module matches sherpa's transcript byte for byte.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from math import log
from pathlib import Path

import numpy as np

from tcginventory.services import voice

try:
    import onnxruntime as _ort
except Exception:  # pragma: no cover - onnxruntime missing (e.g. minimal env)
    _ort = None

SR = 16_000
N_FFT = 400
HOP = 160
N_MELS = 80
N_FRAMES = 3000  # 30 s
SOT, EOT, NO_TIMESTAMPS = 50257, 50256, 50362
N_LAYER, N_CTX, N_STATE = 6, 448, 512


# --------------------------------------------------------------------------
# Front end
# --------------------------------------------------------------------------


def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(
        f >= min_log_hz,
        min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
        f / f_sp,
    )


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(
        m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m
    )


@lru_cache(maxsize=1)
def mel_filters() -> np.ndarray:
    """The Slaney-normalised 80-band filterbank Whisper was trained with
    (``librosa.filters.mel(sr=16000, n_fft=400, n_mels=80)``)."""
    fftfreqs = np.linspace(0, SR / 2, 1 + N_FFT // 2)
    edges = _mel_to_hz(
        np.linspace(_hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(SR / 2)), N_MELS + 2)
    )
    fdiff = np.diff(edges)
    ramps = edges[:, None] - fftfreqs[None, :]
    weights = np.zeros((N_MELS, len(fftfreqs)))
    for i in range(N_MELS):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (edges[2 : N_MELS + 2] - edges[:N_MELS]))[:, None]
    return weights.astype(np.float32)


def log_mel(audio: np.ndarray) -> np.ndarray:
    """``whisper.audio.log_mel_spectrogram`` over a clip padded to 30 s:
    ``(1, 80, 3000)`` float32."""
    audio = np.pad(audio.astype(np.float32), (0, max(0, 30 * SR - len(audio))))
    audio = audio[: 30 * SR]
    window = np.hanning(N_FFT + 1)[:-1].astype(np.float32)
    padded = np.pad(audio, (N_FFT // 2, N_FFT // 2), mode="reflect")
    n_frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    spec = np.fft.rfft(padded[idx] * window, axis=1)
    power = (np.abs(spec) ** 2)[:-1].T
    mel = mel_filters() @ power
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)[None]


def resample(audio: np.ndarray, rate: int) -> np.ndarray:
    """Linear resampling to 16 kHz — adequate for speech from a microphone or a
    synthetic voice; the app captures at 16 kHz and skips it."""
    if rate == SR:
        return audio
    n = int(round(len(audio) * SR / rate))
    return np.interp(
        np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio
    ).astype(np.float32)


# --------------------------------------------------------------------------
# Grammar
# --------------------------------------------------------------------------


@dataclass
class _Node:
    children: dict[int, _Node] = field(default_factory=dict)
    end: str | None = None  # the phrase completed at this node


#: One reading of the bytes so far: where in the trie, and the phrases already
#: completed on the way.
State = tuple[_Node, tuple[str, ...]]

#: Log-probability charged for starting another phrase. Trailing breath or room
#: noise otherwise gets narrated as a second card ("Arceus" → "Arceus Bisharp"):
#: a second phrase has to be clearly heard to pay for itself.
PHRASE_PENALTY = -4.0


def number_phrases(card_number: str) -> list[tuple[str, str]]:
    """Spoken forms of a collector number, each completing the search token
    that finds it: "025/102" is said "25 out of 102", "25 of 102", "25 slash
    102" or "25 102", and the token ``25/102`` is a substring of the stored
    ``025/102``. A number with no denominator is spoken as is."""
    text = card_number.strip()
    if not text:
        return []
    numerator, _, denominator = text.partition("/")
    numerator = numerator.lstrip("0") or "0" if numerator.isdigit() else numerator
    if not denominator:
        return [(numerator, numerator)]
    canonical = f"{numerator}/{denominator}"
    spoken = (
        f"{numerator} out of {denominator}",
        f"{numerator} of {denominator}",
        f"{numerator} over {denominator}",
        f"{numerator} slash {denominator}",
        f"{numerator}/{denominator}",
        f"{numerator} {denominator}",
    )
    return [(s, canonical) for s in spoken]


def build_grammar(
    names: Iterable[str],
    sets: Iterable[str] = (),
    numbers: Iterable[str] = (),
    max_phrases: int = 3,
) -> PhraseTrie:
    """The grammar the Inventory filter bar wants: every card name and set name
    in each of its spoken forms, plus every collector number present."""
    entries: list[tuple[str, str]] = []
    for group in (names, sets):
        for phrase in group:
            for spoken in spoken_variants(phrase):
                entries.append((spoken, phrase))
    for set_name in sets:
        entries.extend(voice.set_shorthands(set_name))  # "promo", "black star"
    for number in numbers:
        entries.extend(number_phrases(number))
    return PhraseTrie(entries, max_phrases=max_phrases)


#: How the catalog writes a suffix → how people say it. Each spoken form is
#: added to the grammar as an alias that completes the *catalog* phrase, so the
#: decoder may hear "Charizard V Max" and still answer ``Charizard VMAX``.
_SPOKEN: dict[str, tuple[str, ...]] = {
    "vmax": ("V Max", "VMAX"),
    "vstar": ("V Star", "VSTAR"),
    "gx": ("GX", "G X"),
    "ex": ("EX", "X", "E X"),
    "lv.x": ("Level X", "LV X"),
    "δ": ("Delta",),
    "&": ("and",),
    "☆": ("Star",),
}


def spoken_variants(phrase: str) -> list[str]:
    """The ways ``phrase`` is said aloud, the catalog spelling first. Hyphens
    between a name and its suffix ("Charizard-EX") are spoken as a gap; a
    suffix word is replaced by each of its spoken forms."""
    base = " ".join(phrase.replace("-", " ").split())
    words = base.split()
    variants: list[list[str]] = [[]]
    for word in words:
        forms = _SPOKEN.get(word.lower(), (word,))
        variants = [v + [f] for v in variants for f in forms]
    out: list[str] = []
    for candidate in [phrase, base] + [" ".join(v) for v in variants]:
        text = " ".join(candidate.split())
        if text and text not in out:
            out.append(text)
    return out


class PhraseTrie:
    """Byte trie over ``" " + phrase`` with ASCII case folded, walked as an NFA
    so a phrase that is a prefix of another keeps both readings alive.

    Entries are canonical phrases, or ``(spoken, canonical)`` pairs when the
    grammar should hear one spelling and answer another; :func:`build_grammar`
    adds every spoken variant of a name that way.
    """

    def __init__(
        self, phrases: Iterable[str | tuple[str, str]], max_phrases: int = 3
    ) -> None:
        self.root = _Node()
        self.max_phrases = max_phrases
        for entry in phrases:
            spoken, canonical = entry if isinstance(entry, tuple) else (entry, entry)
            text = " " + " ".join(spoken.split())
            node = self.root
            for byte in text.encode("utf-8").lower():
                node = node.children.setdefault(byte, _Node())
            if node.end is None:
                node.end = canonical

    def start(self) -> list[State]:
        return [(self.root, ())]

    def walk(self, states: list[State], data: bytes) -> list[State]:
        """Consume ``data`` from every reading in ``states``; the readings that
        survive (empty when the bytes leave the grammar). A completed phrase
        may be followed by a space that starts the next one."""
        for byte in data.lower():
            nxt: list[State] = []
            seen: set[tuple[int, tuple[str, ...]]] = set()
            for node, done in states:
                child = node.children.get(byte)
                if child is not None and (id(child), done) not in seen:
                    seen.add((id(child), done))
                    nxt.append((child, done))
                if node.end is not None and len(done) + 1 < self.max_phrases:
                    restart = self.root.children.get(byte)
                    key = (id(restart), done + (node.end,))
                    if restart is not None and key not in seen:
                        seen.add(key)
                        nxt.append((restart, done + (node.end,)))
            states = nxt
            if not states:
                return []
        return states

    @staticmethod
    def finished(states: list[State]) -> list[tuple[str, ...]]:
        """The phrase sequences that may end here — what EOT would mean."""
        return [done + (node.end,) for node, done in states if node.end is not None]

    def first_bytes(self, states: list[State]) -> set[int]:
        """Every byte some reading can take next — the only tokens worth
        scoring are those beginning with one of them."""
        out: set[int] = set()
        for node, done in states:
            out.update(node.children)
            if node.end is not None and len(done) + 1 < self.max_phrases:
                out.update(self.root.children)
        return out


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------


@dataclass
class _Beam:
    states: list[State]
    score: float
    self_k: np.ndarray
    self_v: np.ndarray
    offset: int


class ConstrainedWhisper:
    """The bundled Whisper pair, decoded under a phrase grammar."""

    def __init__(self, model: voice.VoiceModel | None = None, threads: int = 2):
        if _ort is None:
            raise RuntimeError("onnxruntime is not installed")
        model = model or voice.voice_model()
        if model is None:
            raise RuntimeError("speech model is not installed")
        opts = _ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self._enc = _ort.InferenceSession(str(model.encoder), opts)
        self._dec = _ort.InferenceSession(str(model.decoder), opts)
        self._tokens = self._load_tokens(model.tokens)
        # Text tokens grouped by their (case-folded) first byte, so a decoding
        # step scores only the tokens that can continue the grammar instead of
        # scanning all 51k.
        by_first: dict[int, list[int]] = {}
        for tok, piece in enumerate(self._tokens):
            if piece and tok < SOT:
                by_first.setdefault(piece.lower()[0], []).append(tok)
        self._by_first = {
            b: np.asarray(ids, dtype=np.int64) for b, ids in by_first.items()
        }

    @staticmethod
    def _load_tokens(path: Path) -> list[bytes]:
        table: dict[int, bytes] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            b64, _, idx = line.rpartition(" ")
            table[int(idx)] = base64.b64decode(b64)
        return [table.get(i, b"") for i in range(max(table) + 1)]

    def _step(self, tokens, self_k, self_v, cross_k, cross_v, offset):
        logits, k, v = self._dec.run(
            None,
            {
                "tokens": np.asarray([tokens], dtype=np.int64),
                "in_n_layer_self_k_cache": self_k,
                "in_n_layer_self_v_cache": self_v,
                "n_layer_cross_k": cross_k,
                "n_layer_cross_v": cross_v,
                "offset": np.asarray([offset], dtype=np.int64),
            },
        )
        return logits[0, -1], k, v

    def _expand(self, beam: _Beam, logits: np.ndarray, trie: PhraseTrie, width: int):
        """The ``width`` best legal continuations of ``beam``: a list of
        ``(score, token, states)`` where ``states`` is the readings after the
        token, or ``None`` for end-of-text."""
        logp = logits - _logsumexp(logits)
        firsts = trie.first_bytes(beam.states)
        pools = [self._by_first[b] for b in firsts if b in self._by_first]
        can_end = bool(PhraseTrie.finished(beam.states))
        if can_end:
            pools.append(np.asarray([EOT], dtype=np.int64))
        if not pools:
            return []
        ids = np.concatenate(pools)
        order = ids[np.argsort(-logp[ids])]
        done_before = min(len(done) for _, done in beam.states)
        out: list[tuple[float, int, list[State] | None]] = []
        for tok in order:
            tok = int(tok)
            if tok == EOT:
                out.append((beam.score + float(logp[tok]), tok, None))
            else:
                states = trie.walk(beam.states, self._tokens[tok])
                if not states:
                    continue
                started = min(len(done) for _, done in states) - done_before
                penalty = PHRASE_PENALTY * max(0, started)
                out.append((beam.score + float(logp[tok]) + penalty, tok, states))
            if len(out) >= width:
                break
        return out

    def decode(
        self,
        audio: np.ndarray,
        trie: PhraseTrie,
        *,
        beam_width: int = 4,
        max_tokens: int = 24,
    ) -> list[str]:
        """The phrases the clip most likely says, in order. ``audio`` is mono
        float32 at 16 kHz. Empty when nothing in the grammar fits at all."""
        return self.decode_scored(
            audio, trie, beam_width=beam_width, max_tokens=max_tokens, with_free=False
        ).phrases

    def decode_scored(
        self,
        audio: np.ndarray,
        trie: PhraseTrie,
        *,
        beam_width: int = 4,
        max_tokens: int = 24,
        with_free: bool = True,
    ) -> Decoded:
        """:meth:`decode` plus the evidence behind it: the constrained path's
        log-probability and, for comparison, that of the unconstrained greedy
        transcript of the same clip. A constrained answer far less likely than
        what Whisper would freely have written is a clip the vocabulary does
        not contain — the caller can refuse it rather than name a wrong card."""
        cross_k, cross_v = self._enc.run(None, {"mel": log_mel(audio)})
        phrases, score = self._beam(cross_k, cross_v, trie, beam_width, max_tokens)
        free_score = self._free(cross_k, cross_v, max_tokens) if with_free else 0.0
        return Decoded(phrases, score, free_score)

    def _free(self, cross_k, cross_v, max_tokens: int) -> float:
        """Log-probability of the greedy unconstrained transcript."""
        zeros = np.zeros((N_LAYER, 1, N_CTX, N_STATE), dtype=np.float32)
        logits, k, v = self._step(
            [SOT, NO_TIMESTAMPS], zeros, zeros, cross_k, cross_v, 0
        )
        offset, total = 2, 0.0
        for _ in range(max_tokens):
            logp = logits - _logsumexp(logits)
            tok = int(np.argmax(logp))
            total += float(logp[tok])
            if tok == EOT:
                break
            logits, k, v = self._step([tok], k, v, cross_k, cross_v, offset)
            offset += 1
        return total

    def _beam(
        self, cross_k, cross_v, trie: PhraseTrie, beam_width: int, max_tokens: int
    ) -> tuple[list[str], float]:
        zeros = np.zeros((N_LAYER, 1, N_CTX, N_STATE), dtype=np.float32)
        logits, k, v = self._step(
            [SOT, NO_TIMESTAMPS], zeros, zeros, cross_k, cross_v, 0
        )
        live: list[tuple[_Beam, np.ndarray]] = [
            (_Beam(trie.start(), 0.0, k, v, 2), logits)
        ]
        finished: list[tuple[float, list[str]]] = []
        for _ in range(max_tokens):
            candidates: list[tuple[float, _Beam, int, list[State] | None]] = []
            for beam, logit in live:
                for score, tok, states in self._expand(beam, logit, trie, beam_width):
                    candidates.append((score, beam, tok, states))
            if not candidates:
                break
            candidates.sort(key=lambda c: -c[0])
            live = []
            for score, parent, tok, states in candidates[:beam_width]:
                if states is None:
                    readings = PhraseTrie.finished(parent.states)
                    finished.append((score, list(min(readings, key=len))))
                    continue
                logit, k, v = self._step(
                    [tok], parent.self_k, parent.self_v, cross_k, cross_v, parent.offset
                )
                live.append((_Beam(states, score, k, v, parent.offset + 1), logit))
            if finished and (
                not live or max(f[0] for f in finished) > max(b.score for b, _ in live)
            ):
                break
        if finished:
            score, phrases = max(finished, key=lambda f: f[0])
            return phrases, score
        if live:  # out of steps mid-phrase: the best live reading, if any ends here
            best = max(live, key=lambda p: p[0].score)[0]
            readings = PhraseTrie.finished(best.states)
            if readings:
                return list(min(readings, key=len)), best.score
        return [], float("-inf")


@dataclass(frozen=True)
class Decoded:
    phrases: list[str]
    score: float  # log-probability of the constrained path
    free_score: float  # log-probability of the unconstrained greedy transcript

    @property
    def gap(self) -> float:
        """How much likelihood the grammar cost: ``free_score - score``. Small
        when the clip really says a vocabulary phrase, large when the decoder
        was forced into one."""
        return self.free_score - self.score


def _logsumexp(x: np.ndarray) -> float:
    m = float(np.max(x))
    return m + log(float(np.sum(np.exp(x - m))))
