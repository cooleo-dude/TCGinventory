"""Push-to-talk microphone button (ticket 76).

One click starts capturing from the default microphone at 16 kHz mono; capture
stops the moment an end word ("finish") is spoken, after 1.5 s of quiet once
something has been said, or at a hard cap, and the clip is recognised off the UI
thread by
:func:`tcginventory.services.voice.recognize` against the words the owning view
knows about — card names, set names, collector numbers — and the result is
emitted as ``recognized``.

The button is the only place QtMultimedia is touched; the service layer sees PCM
bytes. It is disabled with an explanatory tooltip when the speech model or the
runtime is missing, so a build without them degrades to "no mic" rather than to
a click that fails.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QMutex, Qt, QThread, QTimer, QWaitCondition, Signal
from PySide6.QtWidgets import QToolButton

from tcginventory.services import voice, voice_kws
from tcginventory.ui import icons, theme
from tcginventory.ui.views._worker import retire

#: Silence this long after speech ends the capture. 1.5 s rather than 1 s since
#: ticket 79: saying "finish" ends capture at once, so the timer is the
#: fallback and can afford to let a speaker pause before "VMAX".
SILENCE_MS = 1500
#: A capture never runs longer than this, spoken or not.
MAX_CAPTURE_MS = 8000
#: With nothing said at all, give up after this long.
NO_SPEECH_MS = 4000
#: RMS of int16 samples above which a chunk counts as speech (~-36 dBFS).
SPEECH_RMS = 400.0

#: ``() -> (names, sets, numbers)`` — the words a clip may resolve to.
Vocabulary = Callable[[], tuple[list[str], list[str], list[str]]]


class _TranscribeWorker(QThread):
    """Recognise one clip against the vocabulary, off the UI thread."""

    done = Signal(str)

    def __init__(
        self,
        pcm16: bytes,
        rate: int,
        names: list[str],
        sets: list[str],
        numbers: list[str],
    ):
        super().__init__()
        self._pcm = pcm16
        self._rate = rate
        self._names = names
        self._sets = sets
        self._numbers = numbers

    def run(self) -> None:  # pragma: no cover - exercised via the button
        import logging

        try:
            query = voice.recognize(
                self._pcm, self._rate, self._names, self._sets, self._numbers
            )
        except Exception:
            logging.getLogger(__name__).exception("voice recognition failed")
            query = ""
        self.done.emit(query)


class _KeywordWorker(QThread):
    """Hears microphone chunks as they arrive and fires the moment an end word
    is spoken (ticket 79). Owns its own spotter; the button pushes chunks in
    under a mutex and retires the worker when capture ends."""

    fired = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[bytes] = []
        self._mutex = QMutex()
        self._wake = QWaitCondition()

    def push(self, chunk: bytes) -> None:
        self._mutex.lock()
        self._chunks.append(chunk)
        self._mutex.unlock()
        self._wake.wakeAll()

    def run(self) -> None:  # pragma: no cover - exercised via the button
        import logging

        try:
            spotter = voice_kws.EndWordSpotter()
        except Exception:
            logging.getLogger(__name__).exception("end-word spotter failed to load")
            return
        while not self.isInterruptionRequested():
            self._mutex.lock()
            if not self._chunks:
                self._wake.wait(self._mutex, 50)
            pending, self._chunks = self._chunks, []
            self._mutex.unlock()
            for chunk in pending:
                word = spotter.feed(chunk)
                if word:
                    self.fired.emit(word)
                    return


class VoiceInputButton(QToolButton):
    """A mic button that turns a spoken phrase into search text.

    ``vocabulary`` is called at capture time and returns ``(names, sets,
    numbers)`` — the grammar the clip is decoded against. The owning view
    supplies it from the rows it already holds, so nothing here touches the
    database.
    """

    recognized = Signal(str)  # the query text to place in a search field
    listening = Signal(bool)  # capture started / stopped

    def __init__(self, vocabulary: Vocabulary | None = None, parent=None) -> None:
        super().__init__(parent)
        self._vocabulary = vocabulary or (lambda: ([], [], []))
        self._source = None  # QAudioSource while capturing
        self._io = None  # its QIODevice
        self._chunks: list[bytes] = []
        self._heard_speech = False
        self._silence_ms = 0
        self._elapsed_ms = 0
        self._worker: _TranscribeWorker | None = None
        self._kws_worker: _KeywordWorker | None = None
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
            tip = "Voice search — click and say a card, set or number"
            if voice_kws.available():
                tip += '; say "finish" to end at once'
            self.setToolTip(tip)
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
        if voice_kws.available():
            # Retire (never overwrite) a spotter still winding down from the
            # previous capture, then start a fresh one for this clip.
            retire(self._kws_worker, self._parked)
            self._kws_worker = _KeywordWorker()
            self._kws_worker.fired.connect(self._on_end_word)
            self._kws_worker.start()
        self.setIcon(icons.icon("mic", theme.active().accent, 16))
        self.setChecked(True)
        self.listening.emit(True)

    def _on_ready_read(self) -> None:
        if self._io is None:
            return
        data = bytes(self._io.readAll().data())
        if data:
            self._chunks.append(data)
            if self._kws_worker is not None and self._kws_worker.isRunning():
                self._kws_worker.push(data)

    def _on_end_word(self, word: str) -> None:
        """The spotter heard "finish": stop now, and drop the word from the
        clip so recognition never sees it."""
        if self._source is None:
            return  # capture already ended (silence beat the spotter)
        self._heard_speech = True  # an end word is speech, whatever the RMS said
        self._stop_capture(end_word=True)

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

    def _stop_capture(self, end_word: bool = False) -> None:
        self._tick.stop()
        if self._source is None:
            self.setChecked(False)
            return
        self._source.stop()
        self._on_ready_read()  # drain what the device still held
        self._io = None
        self._source.deleteLater()
        self._source = None
        retire(self._kws_worker, self._parked)  # its clip is over
        pcm = b"".join(self._chunks)
        if end_word:
            pcm = voice_kws.trim_end_word(pcm)  # drop the spoken "finish"
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
        names, sets, numbers = self._vocabulary()
        worker = _TranscribeWorker(pcm16, rate, list(names), list(sets), list(numbers))
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
