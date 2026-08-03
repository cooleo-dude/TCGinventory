"""The shared image-fetch queue bounds concurrency so a cold cache + rapid
hovering can't spawn a QThread storm (which crashed the app after the catalog
image URLs all changed)."""

from __future__ import annotations


class _FakeSignal:
    def connect(self, *_a, **_k) -> None:
        pass


class _FakeWorker:
    """Stands in for _FetchWorker: it "starts" but never finishes, so retirement
    never frees a slot — letting us observe the concurrency cap deterministically
    without real threads or network."""

    def __init__(self, card_id: str, url: str) -> None:
        self.card_id = card_id
        self.done = _FakeSignal()
        self.finished = _FakeSignal()
        self.started = False

    def start(self) -> None:
        self.started = True


def test_fetch_queue_caps_active_and_pending(monkeypatch):
    from tcginventory.ui import card_preview as cp

    monkeypatch.setattr(cp, "_FetchWorker", _FakeWorker)
    q = cp._FetchQueue()

    for i in range(20):
        q.submit(f"card-{i}", f"http://img/{i}", lambda *_a: None)
    # At most _MAX_INFLIGHT run; the rest wait in the pending queue.
    assert len(q._active) == cp._MAX_INFLIGHT
    assert len(q._pending) == 20 - cp._MAX_INFLIGHT
    assert all(w.started for w in q._active)

    # A flood past the pending cap drops the stalest requests, never unbounded.
    for i in range(200):
        q.submit(f"flood-{i}", f"http://img/{i}", lambda *_a: None)
    assert len(q._active) == cp._MAX_INFLIGHT
    assert len(q._pending) <= cp._MAX_PENDING


def test_fetch_queue_ignores_empty_requests(monkeypatch):
    from tcginventory.ui import card_preview as cp

    monkeypatch.setattr(cp, "_FetchWorker", _FakeWorker)
    q = cp._FetchQueue()
    q.submit(None, "http://img", lambda *_a: None)
    q.submit("card", None, lambda *_a: None)
    assert len(q._active) == 0 and len(q._pending) == 0
