"""End-word spotting for voice capture (ticket 79). Qt-free.

Capture used to end only on a second of silence. Saying "finish" now ends it at
once: a streaming keyword spotter (sherpa-onnx Zipformer transducer, GigaSpeech,
3.3 M parameters, ~5 MB int8) hears each microphone chunk as it arrives and
fires on the end words a few hundred milliseconds after they are spoken —
Whisper could not do this, since it only runs once capture is over and a pass
costs ~0.5 s.

The spotter's keyword list is pre-tokenised with the model's own BPE
(``kws-keywords.txt``: ``▁F IN ISH`` for "finish"), generated once at dev time;
sentencepiece is not a runtime dependency. The spotter, its token table and
the keyword list ship as package data beside the Whisper model and are located
the same way (bundled copy, then ``<data>/voice/models``).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import numpy as np

from tcginventory import config
from tcginventory.services.voice import END_WORDS

try:
    import sherpa_onnx as _sherpa
except Exception:  # pragma: no cover - sherpa-onnx missing (e.g. minimal env)
    _sherpa = None

ENCODER_FILENAME = "kws-encoder.int8.onnx"
DECODER_FILENAME = "kws-decoder.int8.onnx"
JOINER_FILENAME = "kws-joiner.int8.onnx"
TOKENS_FILENAME = "kws-tokens.txt"
KEYWORDS_FILENAME = "kws-keywords.txt"
_FILENAMES = (
    ENCODER_FILENAME,
    DECODER_FILENAME,
    JOINER_FILENAME,
    TOKENS_FILENAME,
    KEYWORDS_FILENAME,
)

__all__ = [
    "END_WORDS",
    "EndWordSpotter",
    "available",
    "kws_model",
    "trim_end_word",
    "trim_tail",
]

#: How much speech to cut back from where it stops, to remove the spoken end
#: word. **Zero**: the word stays in the clip and recognition absorbs it — the
#: grammar has "finish" as a phrase that adds no token, and the snap fallback
#: treats it as filler. Measured (ticket 79): on synthetic speech the word
#: lasts 250–460 ms with no gap before it, so any fixed cut either leaves part
#: of it or eats the end of the name ("AZ's Tranquility" → "astral qu" at
#: 600 ms). Only the trailing quiet after speech (the spotter's latency) goes.
END_WORD_MS = 0
#: Samples quieter than this (2% of full scale) count as silence.
_QUIET = 655
#: Trigger probability. Lower fires sooner and on less; tuned on the benchmark's
#: false-fire rate over every owned name.
THRESHOLD = 0.25
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class KwsModel:
    encoder: Path
    decoder: Path
    joiner: Path
    tokens: Path
    keywords: Path


def _complete(directory: Path) -> KwsModel | None:
    paths = [directory / name for name in _FILENAMES]
    if all(p.is_file() for p in paths):
        return KwsModel(*paths)
    return None


def kws_model(data_dir: Path | None = None) -> KwsModel | None:
    """Locate the spotter: the bundled package copy first, then a user-dropped
    set under the data dir. ``None`` unless all five files are present."""
    try:
        found = _complete(Path(str(resources.files("tcginventory") / "models")))
        if found is not None:
            return found
    except Exception:  # pragma: no cover - resource lookup edge cases
        pass
    root = data_dir if data_dir is not None else config.data_dir()
    return _complete(root / "voice" / "models")


def available() -> bool:
    return _sherpa is not None and kws_model() is not None


def trim_tail(pcm16: bytes, ms: int, rate: int = SAMPLE_RATE) -> bytes:
    """Drop the last ``ms`` of 16-bit mono PCM, never leaving less than nothing
    and never splitting a sample."""
    drop = int(rate * ms / 1000) * 2
    keep = max(0, len(pcm16) - drop)
    return pcm16[: keep - keep % 2]


def trim_end_word(
    pcm16: bytes, word_ms: int = END_WORD_MS, rate: int = SAMPLE_RATE
) -> bytes:
    """Tidy a clip the spotter stopped: drop everything after the last loud
    sample (the spotter's latency and any silence), then ``word_ms`` more —
    by default nothing, see ``END_WORD_MS``. A clip with no loud sample is
    returned empty — there was nothing said to recognise."""
    even = pcm16[: len(pcm16) - len(pcm16) % 2]
    if not even:
        return b""
    samples = np.frombuffer(even, dtype="<i2")
    loud = np.nonzero(np.abs(samples.astype(np.int32)) > _QUIET)[0]
    if not len(loud):
        return b""
    speech_end = int(loud[-1]) + 1
    keep = max(0, speech_end - int(rate * word_ms / 1000))
    return even[: keep * 2]


class EndWordSpotter:
    """Feeds microphone chunks to the streaming spotter and reports an end word
    the moment it fires. One instance per capture; ``reset`` for the next."""

    def __init__(self, model: KwsModel | None = None, threshold: float = THRESHOLD):
        if _sherpa is None:
            raise RuntimeError("sherpa-onnx is not installed")
        model = model or kws_model()
        if model is None:
            raise RuntimeError("keyword-spotting model is not installed")
        self._kws = _sherpa.KeywordSpotter(
            tokens=str(model.tokens),
            encoder=str(model.encoder),
            decoder=str(model.decoder),
            joiner=str(model.joiner),
            keywords_file=str(model.keywords),
            num_threads=1,
            keywords_threshold=threshold,
        )
        self._stream = self._kws.create_stream()
        self.samples_fed = 0

    def feed(self, pcm16: bytes, rate: int = SAMPLE_RATE) -> str | None:
        """Hear one chunk. Returns the end word (lowercase) when one has just
        been spoken, else ``None``."""
        if len(pcm16) < 2:
            return None
        samples = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype="<i2")
        self._stream.accept_waveform(rate, samples.astype(np.float32) / 32768.0)
        self.samples_fed += len(samples)
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
        result = self._kws.get_result(self._stream)
        if result:
            self._kws.reset_stream(self._stream)
            return result.strip().lower()
        return None

    def reset(self) -> None:
        self._stream = self._kws.create_stream()
        self.samples_fed = 0
