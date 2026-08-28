"""Background pre-fetch of a collection's missing card art.

Opening a collection whose art is not cached would otherwise fill in over the
network as you page through it. This warms it once, when the collection is
created and on open if anything is still missing, so the grid is complete before
you look at it.

**One QThread, several fetches inside it.** The obvious route — hand the list to
``card_preview._fetch_queue()`` — is wrong here: that queue exists for hover
previews and deliberately *drops the oldest* request past 48 pending, because a
stale hover no longer matters. A bulk warm of 200 cards would silently lose most
of them. So this owns a single thread (``QTHREAD-LIFETIME``: one object to hold
and await) and runs a small pool inside it for throughput.

Interruptible between items, so leaving the screen does not leave a thread
downloading for another minute.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QThread, Signal

# Matches card_preview's process-wide ceiling. Six is enough to hide latency and
# few enough that a warm cannot monopolise the network or the thread pool.
_WORKERS = 6


class ArtWarmWorker(QThread):
    """Fetch ``[(card_id, url)]`` into the on-disk cache.

    ``progress`` reports ``(done, total)`` as each finishes; ``done`` reports
    ``(fetched, failed)`` once at the end. Neither the disk cache nor the
    thumbnail cache is touched from here beyond what ``ensure_cached`` writes —
    the view re-reads on completion.
    """

    progress = Signal(int, int)  # done, total
    finished_with = Signal(int, int)  # fetched, failed

    def __init__(self, targets: list[tuple[str, str]]) -> None:
        super().__init__()
        self._targets = list(targets)

    def run(self) -> None:  # pragma: no cover - thread body, exercised via fetch_all
        fetched, failed = self._fetch_all()
        self.finished_with.emit(fetched, failed)

    def _fetch_all(self) -> tuple[int, int]:
        """The body, split out so it is testable without starting a thread."""
        total = len(self._targets)
        if not total:
            return 0, 0
        fetched = failed = 0
        done = 0
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = {}
            for card_id, url in self._targets:
                if self.isInterruptionRequested():
                    break
                futures[pool.submit(self._fetch_one, card_id, url)] = card_id
            for future in futures:
                if self.isInterruptionRequested():
                    break
                try:
                    ok = future.result()
                except Exception:
                    ok = False
                fetched += 1 if ok else 0
                failed += 0 if ok else 1
                done += 1
                self.progress.emit(done, total)
        return fetched, failed

    @staticmethod
    def _fetch_one(card_id: str, url: str) -> bool:
        """One image. A failure is a miss, never an exception out of the pool —
        a card whose art 404s must not stop the other 200."""
        from tcginventory.services import cardimages

        try:
            return bool(cardimages.ensure_cached(card_id, url))
        except Exception:
            return False
