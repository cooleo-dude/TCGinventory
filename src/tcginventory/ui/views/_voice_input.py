"""Push-to-talk microphone button (ticket 76).

One click starts capturing from the default microphone at 16 kHz mono; capture
stops on its own after about a second of quiet once something has been said, or
at a hard cap, and the clip is transcribed off the UI thread. What comes back is
run through :func:`tcginventory.services.voice.parse_query` against the words the
owning view knows about (card names, set names) and emitted as ``recognized``.

The button is the only place QtMultimedia is touched; the service layer sees PCM
bytes. It is disabled with an explanatory tooltip when the speech model or the
runtime is missing, so a build without them degrades to "no mic" rather than to
a click that fails.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QToolButton

from tcginventory.services import voice
from tcginventory.ui import icons, theme
from tcginventory.ui.views._worker import retire

#: Silence this long after speech ends the capture.
SILENCE_MS = 1000
#: A capture never runs longer than this, spoken or not.
MAX_CAPTURE_MS = 8000
#: With nothing said at all, give up after this long.
NO_SPEECH_MS = 4000
#: RMS of int16 samples above which a chunk counts as speech (~-36 dBFS).
SPEECH_RMS = 400.0

Vocabulary = Callable[[], tuple[list[str], list[str]]]


class _TranscribeWorker(QThread):
    """Transcribe one clip and snap it to the vocabulary, off the UI thread."""

    done = Signal(str)

    def __init__(self, pcm16: bytes, rate: int, names: list[str], sets: list[str]):
        super().__init__()
        self._pcm = pcm16
        self._rate = rate
        self._names = names
        self._sets = sets

    def run(self) -> None:  # pragma: no cover - exercised via the button
        import logging

        try:
            text = voice.transcribe(self._pcm, self._rate)
            query = voice.parse_query(text, self._names, self._sets)
        except Exception:
            logging.getLogger(__name__).exception("voice transcription failed")
            query = ""
        self.done.emit(query)


class VoiceInputButton(QToolButton):
    """A mic button that turns a spoken phrase into search text.

    ``vocabulary`` is called at capture time and returns ``(names, sets)`` — the
    values the transcript may snap to. The owning view supplies it from the rows
    it already holds, so nothing here touches the database.
    """

    recognized = Signal(str)  # the query text to place in a search field
    listening = Signal(bool)  # capture started / stopped

    def __init__(self, vocabulary: Vocabulary | None = None, parent=None) -> None:
        super().__init__(parent)
        self._vocabulary = vocabulary or (lambda: ([], []))
        self._source = None  # QAudioSource while capturing
        self._io = None  # its QIODevice
        self._chunks: list[bytes] = []
        self._heard_speech = False
        self._silence_ms = 0
        self._elapsed_ms = 0
        self._worker: _TranscribeWorker | None = None
        self._parked: list[QThread] = []
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._on_tick)

        t = theme.active()
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)
        self.setObjectName("mic")
        self.setIcon(icons.icon("mic", t.ink2, 16))
        self.setStyleSheet(
            f"QToolButton#mic{{background:{t.surface};border:1px solid {t.line2};"
            f"border-radius:999px;padding:5px 9px;}}"
            f"QToolButton#mic:hover{{border-color:{t.accent};}}"
            f"QToolButton#mic:checked{{background:{t.accent_soft};"
            f"border-color:{t.accent};}}"
            f"QToolButton#mic:disabled{{color:{t.ink3};}}"
        )
        self.clicked.connect(self._on_clicked)
        self._apply_availability()

    # -- availability -----------------------------------------------------

    def _apply_availability(self) -> None:
        if voice.available():
            self.setEnabled(True)
            self.setToolTip("Voice search — click and say a card, set or number")
        else:
            self.setEnabled(False)
            self.setToolTip(
                "Voice search needs the speech model, which is not installed"
            )

    # -- capture ----------------------------------------------------------

    def _on_clicked(self, checked: bool) -> None:
        if checked:
            self._start_capture()
        else:
            self._stop_capture()

    def _start_capture(self) -> None:
        if self._source is not None or (self._worker and self._worker.isRunning()):
            self.setChecked(self._source is not None)
            return
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

        fmt = QAudioFormat()
        fmt.setSampleRate(voice.SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if device.isNull() or not device.isFormatSupported(fmt):
            self.setChecked(False)
            self.setToolTip("No microphone that can record 16 kHz mono was found")
            return
        self._chunks = []
        self._heard_speech = False
        self._silence_ms = 0
        self._elapsed_ms = 0
        self._source = QAudioSource(device, fmt, self)
        self._io = self._source.start()
        if self._io is None:
            self._source = None
            self.setChecked(False)
            return
        self._io.readyRead.connect(self._on_ready_read)
        self._tick.start()
        self.setIcon(icons.icon("mic", theme.active().accent, 16))
        self.setChecked(True)
        self.listening.emit(True)

    def _on_ready_read(self) -> None:
        if self._io is None:
            return
        data = bytes(self._io.readAll().data())
        if data:
            self._chunks.append(data)

    def _on_tick(self) -> None:
        self._elapsed_ms += self._tick.interval()
        recent = self._chunks[-1] if self._chunks else b""
        if _rms(recent) >= SPEECH_RMS:
            self._heard_speech = True
            self._silence_ms = 0
        else:
            self._silence_ms += self._tick.interval()
        if self._elapsed_ms >= MAX_CAPTURE_MS:
            self._stop_capture()
        elif self._heard_speech and self._silence_ms >= SILENCE_MS:
            self._stop_capture()
        elif not self._heard_speech and self._elapsed_ms >= NO_SPEECH_MS:
            self._stop_capture()

    def _stop_capture(self) -> None:
        self._tick.stop()
        if self._source is None:
            self.setChecked(False)
            return
        self._source.stop()
        self._on_ready_read()  # drain what the device still held
        self._io = None
        self._source.deleteLater()
        self._source = None
        pcm = b"".join(self._chunks)
        self._chunks = []
        self.setChecked(False)
        self.setIcon(icons.icon("mic", theme.active().ink2, 16))
        self.listening.emit(False)
        if self._heard_speech and pcm:
            self.transcribe(pcm)

    # -- transcription ----------------------------------------------------

    def transcribe(self, pcm16: bytes, rate: int = voice.SAMPLE_RATE) -> None:
        """Transcribe ``pcm16`` off the UI thread and emit ``recognized`` with
        the snapped query. Public so a caller with audio from elsewhere (or a
        test with a canned clip) can drive the same path the mic does."""
        # A press that lands while a decode is still running returns early
        # rather than replacing the worker: interruption is only a flag, and
        # dropping the reference to a live QThread aborts the process.
        if self._worker is not None and self._worker.isRunning():
            return
        retire(self._worker, self._parked)
        names, sets = self._vocabulary()
        worker = _TranscribeWorker(pcm16, rate, list(names), list(sets))
        worker.done.connect(self._on_done)
        self._worker = worker
        worker.start()

    def _on_done(self, query: str) -> None:
        if query:
            self.recognized.emit(query)


def _rms(chunk: bytes) -> float:
    if len(chunk) < 2:
        return 0.0
    samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype="<i2")
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
