"""Microphone button and its listening-mode toggle (tickets 76, 79, 80, 81).

Two modes, one capture machine:

* **Push-to-talk** (default): a click opens the microphone at 16 kHz mono; the
  utterance ends the moment an end word ("finish") is spoken, after 1.5 s of
  quiet once something has been said, or at a hard cap; the microphone closes
  and the clip is recognised off the UI thread.
* **Always listening** (the toggle beside the button): the microphone stays
  open while the owning view is visible. Speech above the floor starts an
  utterance, keeping the 300 ms before it; it ends the same three ways; the
  clip is recognised **strictly** — only an answer the vocabulary accounts for
  reaches the search field, never a free-form guess, because nobody is watching
  the box while their hands are in a binder — and listening resumes at once.

Everything is accounted in **time, not chunks** (ticket 81): the device hands
audio over every 10 ms in 320-byte pieces, two hundred times a second, so a
window counted in chunks would be a tenth of what it claims and a floor judged
on the latest chunk alone would be a coin flip on any consonant.

Recognition is :func:`tcginventory.services.voice.recognize` against the words
the owning view knows about — card names, set names, collector numbers — and
the result is emitted as ``recognized``. Clips that arrive while one is being
recognised queue up rather than vanish. The button is the only place
QtMultimedia is touched; the service layer sees PCM bytes. It is disabled with
an explanatory tooltip when the speech model or the runtime is missing, so a
build without them degrades to "no mic" rather than to a click that fails.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QMutex, Qt, QThread, QTimer, QWaitCondition, Signal
from PySide6.QtWidgets import QToolButton

from tcginventory import config
from tcginventory.services import voice, voice_kws
from tcginventory.ui import icons, theme
from tcginventory.ui.views._worker import retire

log = logging.getLogger(__name__)

#: Silence this long after speech ends the utterance. 1.5 s rather than 1 s since
#: ticket 79: saying "finish" ends it at once, so the timer is the fallback and
#: can afford to let a speaker pause before "VMAX".
SILENCE_MS = 1500
#: An utterance never runs longer than this, spoken or not.
MAX_CAPTURE_MS = 8000
#: Push-to-talk only: with nothing said at all, give up after this long.
NO_SPEECH_MS = 4000
#: RMS of int16 samples above which a slice counts as speech (~-36 dBFS).
SPEECH_RMS = 400.0
#: Always listening: audio kept before the onset so the first syllable is not
#: clipped. Measured in bytes of 16 kHz int16 mono, whatever the chunk size.
PRE_ROLL_MS = 300
#: Clips waiting for the recogniser while one is running; the oldest is dropped
#: beyond this, since a speaker five cards ahead of the grid is not being served.
MAX_PENDING = 3
#: The preference key the toggle persists under.
ALWAYS_ON_PREF = "voice_always_on"

#: ``() -> (names, sets, numbers)`` — the words a clip may resolve to.
Vocabulary = Callable[[], tuple[list[str], list[str], list[str]]]


def _bytes_for_ms(ms: int) -> int:
    return voice.SAMPLE_RATE * 2 * ms // 1000


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
        strict: bool = False,
    ):
        super().__init__()
        self._pcm = pcm16
        self._rate = rate
        self._names = names
        self._sets = sets
        self._numbers = numbers
        self._strict = strict

    def run(self) -> None:  # pragma: no cover - exercised via the button
        try:
            query = voice.recognize(
                self._pcm,
                self._rate,
                self._names,
                self._sets,
                self._numbers,
                strict=self._strict,
            )
        except Exception:
            log.exception("voice recognition failed")
            query = ""
        self.done.emit(query)


class _KeywordWorker(QThread):
    """Hears microphone chunks as they arrive and fires the moment an end word
    is spoken (ticket 79). Owns its own spotter; the button pushes chunks in
    under a mutex and retires the worker when the utterance ends."""

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
        try:
            spotter = voice_kws.EndWordSpotter()
        except Exception:
            log.exception("end-word spotter failed to load")
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

    ``vocabulary`` is called at recognition time and returns ``(names, sets,
    numbers)`` — the grammar the clip is decoded against. The owning view
    supplies it from the rows it already holds, so nothing here touches the
    database.
    """

    recognized = Signal(str)  # the query text to place in a search field
    listening = Signal(bool)  # the microphone is open / closed
    capturing = Signal(bool)  # an utterance is being recorded

    def __init__(self, vocabulary: Vocabulary | None = None, parent=None) -> None:
        super().__init__(parent)
        self._vocabulary = vocabulary or (lambda: ([], [], []))
        self._always_on = False
        self._active = True  # the owning view is visible
        self._source = None  # QAudioSource while the microphone is open
        self._io = None  # its QIODevice
        self._chunks: list[bytes] = []  # the utterance so far (or the pre-roll)
        self._buffered = 0  # bytes in _chunks
        self._since_tick: list[bytes] = []  # audio since the last tick
        self._in_utterance = False
        self._heard_speech = False
        self._silence_ms = 0.0
        self._elapsed_ms = 0.0
        self._clock = time.monotonic  # injectable for tests
        self._last_tick = 0.0
        self._worker: _TranscribeWorker | None = None
        self._pending: list[tuple[bytes, int, bool]] = []
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

    # -- modes ------------------------------------------------------------

    @property
    def always_on(self) -> bool:
        return self._always_on

    def set_always_on(self, on: bool) -> None:
        """Switch between push-to-talk and always listening. Listening starts
        (or stops) immediately when the owning view is visible."""
        on = bool(on)
        if on == self._always_on:
            return
        self._always_on = on
        if on:
            if self._active and self._source is None:
                self._open_microphone()
        elif self._source is not None:
            if self._in_utterance:
                self._finish_utterance()
            self._close_microphone()

    def set_active(self, visible: bool) -> None:
        """The owning view came into (or went out of) view. Always listening
        only listens while the view is on screen."""
        self._active = visible
        if not self._always_on:
            return
        if visible and self._source is None:
            self._open_microphone()
        elif not visible and self._source is not None:
            if self._in_utterance:
                self._finish_utterance()
            self._close_microphone()

    # -- capture ----------------------------------------------------------

    def _on_clicked(self, checked: bool) -> None:
        if self._always_on:
            # In always-listening mode the click ends the current utterance
            # early (or is a no-op); the toggle owns the microphone.
            self.setChecked(self._in_utterance)
            if self._in_utterance:
                self._finish_utterance()
            return
        if checked:
            self._start_capture()
        else:
            self._stop_capture()

    def _open_microphone(self) -> bool:
        """Open the default microphone at 16 kHz mono. False if none can."""
        from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

        fmt = QAudioFormat()
        fmt.setSampleRate(voice.SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if device.isNull() or not device.isFormatSupported(fmt):
            self.setToolTip("No microphone that can record 16 kHz mono was found")
            return False
        self._reset_buffers()
        self._source = QAudioSource(device, fmt, self)
        self._io = self._source.start()
        if self._io is None:
            self._source = None
            return False
        self._io.readyRead.connect(self._on_ready_read)
        self._last_tick = self._clock()
        self._tick.start()
        self.listening.emit(True)
        return True

    def _reset_buffers(self) -> None:
        self._chunks = []
        self._buffered = 0
        self._since_tick = []
        self._in_utterance = False
        self._heard_speech = False
        self._silence_ms = 0.0
        self._elapsed_ms = 0.0

    def _close_microphone(self) -> None:
        self._tick.stop()
        retire(self._kws_worker, self._parked)
        if self._source is not None:
            self._source.stop()
            self._source.deleteLater()
        self._source = None
        self._io = None
        self._reset_buffers()
        self.setChecked(False)
        self.setIcon(icons.icon("mic", theme.active().ink2, 16))
        self.listening.emit(False)

    def _start_capture(self) -> None:
        """Push-to-talk: open the microphone and begin the utterance at once."""
        if self._source is not None:
            self.setChecked(True)
            return
        if not self._open_microphone():
            self.setChecked(False)
            return
        self._begin_utterance()

    def _begin_utterance(self) -> None:
        """Whatever is buffered (nothing, or the pre-roll) opens the clip."""
        self._in_utterance = True
        self._heard_speech = False
        self._silence_ms = 0.0
        self._elapsed_ms = 0.0
        if voice_kws.available():
            # Retire (never overwrite) a spotter still winding down from the
            # previous utterance, then start a fresh one for this clip and let
            # it hear the pre-roll too.
            retire(self._kws_worker, self._parked)
            self._kws_worker = _KeywordWorker()
            self._kws_worker.fired.connect(self._on_end_word)
            self._kws_worker.start()
            for chunk in self._chunks:
                self._kws_worker.push(chunk)
        self.setIcon(icons.icon("mic", theme.active().accent, 16))
        self.setChecked(True)
        self.capturing.emit(True)

    def _on_ready_read(self) -> None:
        if self._io is None:
            return
        data = bytes(self._io.readAll().data())
        if data:
            self._ingest(data)

    def _ingest(self, data: bytes) -> None:
        """One chunk of microphone audio has arrived — 10 ms of it, typically."""
        self._chunks.append(data)
        self._buffered += len(data)
        self._since_tick.append(data)
        if not self._in_utterance:
            # Idle: keep only the pre-roll, by bytes, whatever the chunk size.
            limit = _bytes_for_ms(PRE_ROLL_MS)
            while self._chunks and self._buffered - len(self._chunks[0]) >= limit:
                self._buffered -= len(self._chunks.pop(0))
        elif self._kws_worker is not None and self._kws_worker.isRunning():
            self._kws_worker.push(data)

    def _on_tick(self) -> None:
        now = self._clock()
        dt_ms = (now - self._last_tick) * 1000.0
        self._last_tick = now
        # Speech since the last tick, judged over every slice that arrived —
        # not the latest one, which on a 10 ms cadence is often a consonant gap.
        heard = any(_rms(c) >= SPEECH_RMS for c in self._since_tick)
        self._since_tick = []
        if not self._in_utterance:
            if heard:  # always listening, between utterances: speech began
                self._begin_utterance()
                self._heard_speech = True
            return
        self._elapsed_ms += dt_ms
        if heard:
            self._heard_speech = True
            self._silence_ms = 0.0
        else:
            self._silence_ms += dt_ms
        if self._elapsed_ms >= MAX_CAPTURE_MS:
            self._end_utterance(reason="cap")
        elif self._heard_speech and self._silence_ms >= SILENCE_MS:
            self._end_utterance(reason="silence")
        elif not self._heard_speech and self._elapsed_ms >= NO_SPEECH_MS:
            self._end_utterance(reason="nothing said")

    def _on_end_word(self, word: str) -> None:
        """The spotter heard "finish": end the utterance now. The word stays in
        the clip; recognition absorbs it."""
        if self._source is None or not self._in_utterance:
            return  # the utterance already ended (silence beat the spotter)
        self._heard_speech = True  # an end word is speech, whatever the RMS said
        self._end_utterance(end_word=True, reason=word)

    def _end_utterance(self, end_word: bool = False, reason: str = "") -> None:
        """An utterance is over: hand it to recognition, then either keep
        listening (always on) or close the microphone (push-to-talk)."""
        log.debug(
            "voice: utterance ended (%s) after %.1fs, %d bytes, speech=%s",
            reason,
            self._elapsed_ms / 1000,
            self._buffered,
            self._heard_speech,
        )
        if self._always_on:
            self._finish_utterance(end_word)
        else:
            self._stop_capture(end_word)

    def _take_clip(self, end_word: bool) -> bytes | None:
        """Drain the device and lift the utterance out of the buffers; ``None``
        when nothing was said."""
        self._on_ready_read()
        retire(self._kws_worker, self._parked)
        pcm = b"".join(self._chunks)
        if end_word:
            pcm = voice_kws.trim_end_word(pcm)
        heard = self._heard_speech
        self._reset_buffers()
        return pcm if heard and pcm else None

    def _finish_utterance(self, end_word: bool = False) -> None:
        """Always listening: recognise the utterance and resume listening."""
        if not self._in_utterance:
            return
        clip = self._take_clip(end_word)
        self.setChecked(False)
        self.setIcon(icons.icon("mic", theme.active().ink2, 16))
        self.capturing.emit(False)
        if clip:
            self.transcribe(clip, strict=True)

    def _stop_capture(self, end_word: bool = False) -> None:
        """Push-to-talk: the utterance is over and so is the microphone."""
        if self._source is None:
            self.setChecked(False)
            return
        clip = self._take_clip(end_word) if self._in_utterance else None
        self._close_microphone()
        self.capturing.emit(False)
        if clip:
            self.transcribe(clip)

    # -- transcription ----------------------------------------------------

    def transcribe(
        self, pcm16: bytes, rate: int = voice.SAMPLE_RATE, strict: bool = False
    ) -> None:
        """Recognise ``pcm16`` off the UI thread and emit ``recognized`` with
        the query. Public so a caller with audio from elsewhere (or a test with
        a canned clip) can drive the same path the mic does. ``strict`` refuses
        a free-form guess — always-listening mode uses it, since nobody is
        watching the search box to correct one.

        A clip that lands while a decode is still running waits its turn
        rather than replacing the worker (interruption is only a flag, and
        dropping the reference to a live QThread aborts the process) or being
        thrown away (ticket 81: that was every second card in a fast session).
        """
        job = (pcm16, rate, strict)
        if self._worker is not None and self._worker.isRunning():
            self._pending.append(job)
            del self._pending[:-MAX_PENDING]
            return
        self._run_job(job)

    def _run_job(self, job: tuple[bytes, int, bool]) -> None:
        pcm16, rate, strict = job
        retire(self._worker, self._parked)
        names, sets, numbers = self._vocabulary()
        worker = _TranscribeWorker(
            pcm16, rate, list(names), list(sets), list(numbers), strict
        )
        worker.done.connect(self._on_done)
        self._worker = worker
        worker.start()

    def _on_done(self, query: str) -> None:
        if query:
            self.recognized.emit(query)
        if self._pending:
            self._run_job(self._pending.pop(0))


class ListeningModeToggle(QToolButton):
    """The chip beside the mic: off = push-to-talk, on = always listening.

    Drives :meth:`VoiceInputButton.set_always_on` and remembers the choice in
    the preferences file, so the app comes back the way it was left."""

    def __init__(self, mic: VoiceInputButton, parent=None) -> None:
        super().__init__(parent)
        self._mic = mic
        t = theme.active()
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("listenmode")
        self.setText("Always on")
        self.setIcon(icons.icon("ear", t.ink2, 15))
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setStyleSheet(
            f"QToolButton#listenmode{{background:{t.surface};border:1px solid "
            f"{t.line2};border-radius:999px;color:{t.ink2};padding:5px 11px;"
            f"font-weight:600;font-size:12px;}}"
            f"QToolButton#listenmode:hover{{color:{t.ink};}}"
            f"QToolButton#listenmode:checked{{background:{t.accent_soft};"
            f"border-color:{t.accent};color:{t.accent_ink};}}"
        )
        self.setToolTip(
            "Always listening: the microphone stays open while Inventory is "
            "showing; speak a card and the grid follows, no click needed. Only "
            "answers the vocabulary accounts for reach the search field."
        )
        self.setEnabled(mic.isEnabled())
        self.toggled.connect(self._on_toggled)
        if mic.isEnabled() and bool(config.get_pref(ALWAYS_ON_PREF, False)):
            self.setChecked(True)  # fires _on_toggled → mic listens

    def _on_toggled(self, on: bool) -> None:
        t = theme.active()
        self.setIcon(icons.icon("ear", t.accent_ink if on else t.ink2, 15))
        self._mic.set_always_on(on)
        config.set_pref(ALWAYS_ON_PREF, bool(on))


def _rms(chunk: bytes) -> float:
    if len(chunk) < 2:
        return 0.0
    samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype="<i2")
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
