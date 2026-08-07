"""Inventory links panel: TCGplayer / eBay buttons over the shared proxy."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.domain.enums import Category, Intent  # noqa: E402
from tcginventory.domain.models import Item  # noqa: E402
from tcginventory.services.transactions import (  # noqa: E402
    IncomingCard,
    TransactionRequest,
    TransactionService,
)


def _seed(conn, *, tcg="https://tcgplayer.com/x", ebay="https://ebay.com/y"):
    item = Item(
        name="Charizard",
        category=Category.SINGLE,
        tcgplayer_url=tcg,
        ebay_url=ebay,
    )
    TransactionService(conn).post(
        TransactionRequest(
            occurred_at="2026-07-16",
            cash_out_cents=1000,
            incoming=[
                IncomingCard(
                    item=item,
                    quantity=1,
                    market_value_cents=1000,
                    intent=Intent.RESALE,
                )
            ],
        )
    )


def _view(app, conn):
    from tcginventory.ui.views.inventory import InventoryView

    return InventoryView(conn)


def test_link_columns_shown_inline_in_grid(app):
    # The redesigned Inventory grid surfaces the TCGplayer / eBay link buttons
    # inline (the separate links panel was removed).
    from tcginventory.ui.models.inventory_model import EBAY_COL, TCGPLAYER_COL

    conn = connect(":memory:")
    migrate(conn)
    _seed(conn)
    view = _view(app, conn)

    assert not view._table.isColumnHidden(TCGPLAYER_COL)
    assert not view._table.isColumnHidden(EBAY_COL)
    assert not hasattr(view, "_links")  # the companion panel is gone
    view.deleteLater()


def test_link_role_exposes_urls_through_proxy(app):
    from tcginventory.ui.models.inventory_model import (
        EBAY_COL,
        LINK_URL_ROLE,
        TCGPLAYER_COL,
    )

    conn = connect(":memory:")
    migrate(conn)
    _seed(conn)
    view = _view(app, conn)

    proxy = view._proxy
    # The TCGplayer link now carries the card's fields (here just the default
    # language) so it opens the most specific listing.
    assert proxy.index(0, TCGPLAYER_COL).data(LINK_URL_ROLE) == (
        "https://tcgplayer.com/x?page=1&Language=English"
    )
    assert proxy.index(0, EBAY_COL).data(LINK_URL_ROLE) == "https://ebay.com/y"
    view.deleteLater()


def test_clicking_a_link_button_opens_the_url(app, monkeypatch):
    from tcginventory.ui.models.inventory_model import TCGPLAYER_COL

    conn = connect(":memory:")
    migrate(conn)
    _seed(conn)
    view = _view(app, conn)

    opened: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.inventory.QDesktopServices",
        type("D", (), {"openUrl": staticmethod(lambda u: opened.append(u.toString()))}),
    )
    view._open_link(view._proxy.index(0, TCGPLAYER_COL))
    assert opened == ["https://tcgplayer.com/x?page=1&Language=English"]
    view.deleteLater()


def test_missing_link_does_not_open(app, monkeypatch):
    # TCGplayer has no fallback, so a missing link opens nothing.
    from tcginventory.ui.models.inventory_model import TCGPLAYER_COL

    conn = connect(":memory:")
    migrate(conn)
    _seed(conn, tcg=None)  # no TCGplayer link on this card
    view = _view(app, conn)

    opened: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.inventory.QDesktopServices",
        type("D", (), {"openUrl": staticmethod(lambda u: opened.append(u.toString()))}),
    )
    view._open_link(view._proxy.index(0, TCGPLAYER_COL))
    assert opened == []  # nothing to open
    view.deleteLater()


def test_ebay_missing_url_opens_derived_sold_search(app, monkeypatch):
    # eBay always resolves: with no stored URL it opens a derived sold-listings
    # search built from the card's identity.
    from tcginventory.ui.models.inventory_model import EBAY_COL

    conn = connect(":memory:")
    migrate(conn)
    _seed(conn, ebay=None)
    view = _view(app, conn)

    opened: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.inventory.QDesktopServices",
        type("D", (), {"openUrl": staticmethod(lambda u: opened.append(u.toString()))}),
    )
    view._open_link(view._proxy.index(0, EBAY_COL))
    assert len(opened) == 1
    assert "ebay.com/sch" in opened[0] and "LH_Sold=1" in opened[0]
    view.deleteLater()
