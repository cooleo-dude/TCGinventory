"""One background job, run off the UI thread with its own connection.

Both maintenance screens need the same thing: call ``task(conn)``, get a
sentence back, never block the window and never let an exception escape into
Qt's event loop. Sharing it keeps the two screens behaving identically when a
job fails.

:func:`retire` is the other half: how a view *stops* a worker it is about to
replace, without killing the process.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal


def retire(worker: QThread | None, parked: list[QThread]) -> None:
    """Ask a running worker to stop, and hold it until it actually has.

    ``requestInterruption()`` only raises a flag — the thread runs on until its
    next checkpoint. A view that interrupts and then immediately overwrites its
    ``self._worker`` drops the last Python reference while the thread is still
    going, so the C++ ``QThread`` is destroyed underneath a live thread. Qt
    answers that with a hard abort, not an exception: the process dies at
    ``0xC0000409`` with no traceback, seconds into startup.

    So park the retiring worker in ``parked`` (one list per view) and let its
    ``finished`` signal release it. Views that never replace a running worker —
    they return early instead — don't need this.
    """
    if worker is None or not worker.isRunning():
        return
    worker.requestInterruption()
    parked.append(worker)
    worker.finished.connect(lambda: _release(worker, parked))
    if worker.isFinished():  # finished in the gap between the check and connect
        _release(worker, parked)


def _release(worker: QThread, parked: list[QThread]) -> None:
    if worker in parked:
        parked.remove(worker)


class TaskWorker(QThread):
    """Runs ``task(conn) -> summary`` off the UI thread with its own connection.

    The seam for any job that is "call a service and report a sentence" — the
    maintenance and card-show operations all arrive this way, so adding one costs
    a function rather than another QThread subclass.
    """

    done = Signal(object)  # (ok, summary)

    def __init__(self, db_path, task) -> None:
        super().__init__()
        self._db_path = db_path
        self._task = task

    def run(self) -> None:  # pragma: no cover - exercised via the UI
        import logging

        from tcginventory.data.db import open_database

        try:
            conn = open_database(self._db_path)
            try:
                summary = self._task(conn)
            finally:
                conn.close()
            self.done.emit((True, summary))
        except Exception as exc:
            logging.getLogger(__name__).exception("operation failed")
            self.done.emit((False, str(exc)[:200]))
