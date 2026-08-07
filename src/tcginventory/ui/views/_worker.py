"""One background job, run off the UI thread with its own connection.

Both maintenance screens need the same thing: call ``task(conn)``, get a
sentence back, never block the window and never let an exception escape into
Qt's event loop. Sharing it keeps the two screens behaving identically when a
job fails.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal


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
