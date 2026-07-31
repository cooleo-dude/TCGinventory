"""The Movers columns show 5 rows and expand by 5 (repeatably) / collapse."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tcginventory.data.db import connect  # noqa: E402
from tcginventory.data.migrator import migrate  # noqa: E402
from tcginventory.services.reporting import MoverRow  # noqa: E402


@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _rows(n: int) -> list[MoverRow]:
    return [
        MoverRow(
            item_id=i,
            name=f"Card {i}",
            set_name="Set",
            card_number=str(i),
            qty=1,
            price_cents=1000 + i,
            change_cents=100 + i,
            change_pct=10.0,
        )
        for i in range(n)
    ]


def test_movers_column_expands_and_collapses(app):
    from tcginventory.ui.views.dashboard import _PAGE, MoversColumn, _ImageLoader

    conn = connect(":memory:")
    migrate(conn)
    col = MoversColumn("Top Gainers", "#ffffff", _ImageLoader(conn))

    col.set_movers(_rows(12), gain=True)  # 12 available
    assert col._rows.count() == _PAGE  # shows 5

    col._expand()
    assert col._rows.count() == 2 * _PAGE  # 10

    col._expand()
    assert col._rows.count() == 12  # clamped to what's available

    col._collapse()
    assert col._rows.count() == _PAGE  # back to 5
    col.deleteLater()


def test_movers_column_no_expand_when_five_or_fewer(app):
    from tcginventory.ui.views.dashboard import MoversColumn, _ImageLoader

    conn = connect(":memory:")
    migrate(conn)
    col = MoversColumn("Top Losers", "#ffffff", _ImageLoader(conn))
    col.set_movers(_rows(3), gain=False)
    assert col._rows.count() == 3
    assert col._footer.count() == 0  # nothing to expand/collapse
    col.deleteLater()
