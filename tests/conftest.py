"""Shared pytest fixtures and helpers for the service/data tests."""

from __future__ import annotations

import gc
import os
import sys

import pytest

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.domain.enums import Category, Intent
from tcginventory.domain.models import Item
from tcginventory.services.transactions import (
    IncomingCard,
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)


@pytest.fixture(scope="module", autouse=True)
def _drain_deferred_deletions():
    """Carry out the ``deleteLater()`` calls a module's tests left pending.

    Views drop their old widgets with ``deleteLater()``, which only takes effect
    when an event loop delivers the deferred-delete event. The suite never spins
    one, so nothing was ever freed: a full run ended holding roughly 33,000 live
    ``QWidget`` objects and 1,800 top-level windows, and somewhere past thirty
    thousand Qt fell over with an access violation inside whichever widget
    happened to be under construction. It looked like a flaky crash in an
    unrelated file — the site moved between runs — because the cliff was a total
    rather than a line of code (2026-08-07).

    Deliberately **module**-scoped. Draining after every test tripped over
    widgets a still-live fixture held a Python reference to, and freeing the C++
    side under a live wrapper crashes far sooner than the leak does. By the time
    a module finishes, its function fixtures have been torn down, so the only
    objects collected are ones the code already asked to delete.
    """
    yield
    # Never import Qt for the headless service tests — most of the suite has no
    # business paying for it.
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    gc.collect()  # drop wrappers the module was holding, then honour the deletes
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Setup more than one file needs. It did not used to live here: files grew
# their own copies instead, and the suite ended up carrying 25 identical
# ``app`` fixtures, five identical ``conn`` fixtures *shadowing* the one above
# (so an edit here reached none of them), and the same ``repo`` / ``svc`` /
# ``inv`` / ``seeded_conn`` bodies over and over. Identical copies do not stay
# identical. Qt and vision imports stay inside the fixtures, so the
# pure-Python service tests never pay for PySide6.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    """The one QApplication for the process.

    Qt permits a single instance, so this was always shared in practice even
    while each file declared its own function-scoped copy; session scope just
    says so. Under xdist each worker is its own process and gets its own.
    """
    pytest.importorskip("PySide6")
    # Must be set before the first QApplication; harmless if already headless.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def seeded_conn():
    """A migrated database carrying the demo dataset."""
    from tcginventory.data.seed import seed_demo

    c = connect(":memory:")
    migrate(c)
    seed_demo(c)
    yield c
    c.close()


@pytest.fixture()
def repo():
    """A VisionRepository over an in-memory vision database."""
    from tcginventory.data.vision import VisionRepository, open_vision

    c = open_vision(":memory:")
    yield VisionRepository(c)
    c.close()


@pytest.fixture()
def svc(conn):
    return TransactionService(conn)


@pytest.fixture()
def inv(conn):
    from tcginventory.services.inventory import InventoryService

    return InventoryService(conn)


def type_catalog_search(field, text: str, owner=None) -> None:
    """Type ``text`` into a catalog search box and run the search it triggers.

    Two things this does that ``setText`` cannot. It emits ``textEdited``, which
    is what the search boxes listen to — a programmatic ``setText`` deliberately
    no longer starts a query (ticket 47), so a test using it would assert on a
    search that never ran. And it fires the 120 ms debounce immediately rather
    than waiting on the clock, because a suite that sleeps for its own timers is
    a suite that goes flaky under load (TESTS-HEADLESS).

    ``owner`` holds the timer when that is not the field itself — the Price
    Lookup view owns its search box's.
    """
    from PySide6.QtTest import QTest

    field.clear()
    QTest.keyClicks(field, text)
    timer = (owner if owner is not None else field)._search_timer
    if timer.isActive():
        timer.stop()
        timer.timeout.emit()


def make_item(name: str, **overrides) -> Item:
    """A minimal single-card item for building transactions."""
    base = dict(name=name, category=Category.SINGLE)
    base.update(overrides)
    return Item(**base)


def incoming(
    name: str,
    market_value_cents: int,
    *,
    quantity: int = 1,
    intent: Intent = Intent.RESALE,
    **item_overrides,
) -> IncomingCard:
    return IncomingCard(
        item=make_item(name, **item_overrides),
        quantity=quantity,
        market_value_cents=market_value_cents,
        intent=intent,
    )


def buy(
    svc: TransactionService,
    name: str,
    price_cents: int,
    market_value_cents: int,
    occurred_at: str,
    *,
    quantity: int = 1,
    intent: Intent = Intent.RESALE,
    **item_overrides,
):
    """Post a single-card purchase; returns the PostResult."""
    req = TransactionRequest(
        occurred_at=occurred_at,
        cash_out_cents=price_cents,
        incoming=[
            incoming(
                name,
                market_value_cents,
                quantity=quantity,
                intent=intent,
                **item_overrides,
            )
        ],
    )
    return svc.post(req)


def sell(
    svc: TransactionService,
    item_id: int,
    quantity: int,
    proceeds_cents: int,
    occurred_at: str,
):
    """Post a single-card sale (cash in); returns the PostResult.

    Proceeds are the cash received (the value rule derives per-card proceeds
    from the deal — for a pure sale that's the cash).
    """
    req = TransactionRequest(
        occurred_at=occurred_at,
        outgoing=[OutgoingCard(item_id=item_id, quantity=quantity)],
        cash_in_cents=proceeds_cents,
    )
    return svc.post(req)
