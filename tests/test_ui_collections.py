"""The Personal Collection window (ticket 60).

Offscreen per TESTS-HEADLESS. The import path is the money path, so it is
exercised through the real view rather than only through the service.
"""

from __future__ import annotations

import pytest

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import (
    AuditLogRepository,
    CardCatalogRepository,
    CollectionRepository,
    ItemRepository,
    LotRepository,
)
from tcginventory.domain.enums import Category, Condition, Intent
from tcginventory.domain.models import CatalogCard, Collection, Item, Lot
from tcginventory.services.collections import FILLED, IMPORTABLE, MISSING
from tcginventory.services.reporting import ReportingService


def _seed(conn, intent=Intent.RESALE, qty=1):
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(
                card_id="s1-1",
                name="Alpha",
                set_id="s1",
                set_name="Set One",
                number="1",
                printed_total=2,
            ),
            CatalogCard(
                card_id="s1-2",
                name="Beta",
                set_id="s1",
                set_name="Set One",
                number="2",
                printed_total=2,
            ),
        ],
        "EN",
        now_iso(),
    )
    item = ItemRepository(conn).add(
        Item(
            name="Alpha",
            set_name="Set One",
            card_number="1",
            category=Category.SINGLE,
            condition=Condition.NM,
            market_value_cents=2500,
        )
    )
    LotRepository(conn).add(
        Lot(
            item_id=item.id,
            acquired_at="2026-08-01",
            quantity=qty,
            remaining_qty=qty,
            unit_cost_cents=1000,
            acquired_market_value_cents=2500,
            intent=intent,
        )
    )
    coll = CollectionRepository(conn).add(
        Collection(name="Set One", template="set", selector="Set One")
    )
    conn.commit()
    return item, coll


@pytest.fixture()
def view(app, conn):
    from tcginventory.ui.views.collections import CollectionsView

    v = CollectionsView(conn)
    yield v
    v.deleteLater()


def _slots(view):
    """The collection's slots as the model holds them (unfiltered, source order)."""
    return [view._model.slot_at(r) for r in range(view._model.rowCount())]


def _first(view, state):
    """The first slot in a given state — what a click would act on."""
    return next(s for s in _slots(view) if s.state == state)


# ---------------------------------------------------------------------------
# the window builds and shows both states
# ---------------------------------------------------------------------------


def test_the_window_builds_with_no_collections(app, conn):
    from tcginventory.ui.views.collections import CollectionsView

    v = CollectionsView(conn)
    v.refresh()
    assert v._empty.isVisibleTo(v), "an empty screen must explain itself"
    v.deleteLater()


def test_opening_a_block_shows_the_checklist(view, conn):
    _seed(conn)
    view.refresh()
    assert view._pages.currentIndex() == 0
    view._open(view._nav_order[0])
    assert view._pages.currentIndex() == 1
    assert "Set One" in view._detail_title.text()


def test_the_checklist_has_a_row_per_slot_including_unowned(view, conn):
    """The property Inventory cannot express."""
    _seed(conn)
    view.refresh()
    view._open(view._nav_order[0])
    assert view._model.rowCount() == 2
    assert {s.name: s.state for s in _slots(view)} == {
        "Alpha": IMPORTABLE,
        "Beta": MISSING,
    }


def test_nav_lists_every_collection(view, conn):
    _seed(conn)
    view.refresh()
    assert view._nav.count() == 1
    assert "Set One" in view._nav.item(0).text()


# ---------------------------------------------------------------------------
# the import
# ---------------------------------------------------------------------------


def test_importing_a_single_copy_flips_intent_and_fills_the_slot(view, conn):
    item, _coll = _seed(conn, intent=Intent.RESALE, qty=1)
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, IMPORTABLE)
    view._import_slot(slot)

    lots = LotRepository(conn).list_for_item(item.id)
    assert len(lots) == 1
    assert lots[0].intent is Intent.PERSONAL
    assert len([s for s in _slots(view) if s.state == FILLED]) == 1


def test_importing_from_a_multi_copy_lot_splits_it(view, conn):
    """The behaviour the user chose: keep one, the rest stay stock."""
    item, _coll = _seed(conn, intent=Intent.RESALE, qty=3)
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, IMPORTABLE)
    view._import_slot(slot)

    lots = sorted(LotRepository(conn).list_for_item(item.id), key=lambda x: x.quantity)
    assert [(lot.quantity, lot.intent) for lot in lots] == [
        (1, Intent.PERSONAL),
        (2, Intent.RESALE),
    ]


def test_an_import_does_not_move_any_total(view, conn):
    """Intent is a filter dimension. If an import changed a total, the
    Dashboard's Collection Value tile would drift on a membership action."""
    _seed(conn, intent=Intent.RESALE, qty=3)
    rep = ReportingService(conn)
    before_basis = rep.cost_basis_cents()
    before_value = rep.collection_value_cents()
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, IMPORTABLE)
    view._import_slot(slot)
    assert rep.cost_basis_cents() == before_basis
    assert rep.collection_value_cents() == before_value
    # only the slices moved
    assert rep.cost_basis_cents(Intent.PERSONAL) == 1000


def test_an_import_is_audited(view, conn):
    item, _coll = _seed(conn, intent=Intent.RESALE, qty=2)
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, IMPORTABLE)
    lot_id = LotRepository(conn).list_for_item(item.id)[0].id
    view._import_slot(slot)
    assert AuditLogRepository(conn).list_for_entity("lot", lot_id)


def test_an_import_emits_changed_so_other_screens_refresh(view, conn):
    _seed(conn, intent=Intent.RESALE)
    seen: list[int] = []
    view.changed.connect(lambda: seen.append(1))
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, IMPORTABLE)
    view._import_slot(slot)
    assert seen, "intent moved; the Dashboard filter and Inventory must be told"


def test_importing_an_already_personal_copy_is_harmless(view, conn):
    item, _coll = _seed(conn, intent=Intent.PERSONAL, qty=1)
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, FILLED)
    view._import_slot(slot)  # no-op path
    lots = LotRepository(conn).list_for_item(item.id)
    assert len(lots) == 1 and lots[0].intent is Intent.PERSONAL


def test_a_refused_split_shows_a_message_and_does_not_write(view, conn, monkeypatch):
    """A partially sold lot cannot be split. The view must report it, not crash
    and not half-commit."""
    item, _coll = _seed(conn, intent=Intent.RESALE, qty=3)
    lot = LotRepository(conn).list_for_item(item.id)[0]
    lot.remaining_qty = 2  # one unit sold
    LotRepository(conn).update(lot)
    conn.commit()

    warned: list[str] = []
    monkeypatch.setattr(
        "tcginventory.ui.views.collections.QMessageBox.warning",
        lambda *a, **k: warned.append(a[2] if len(a) > 2 else ""),
    )
    view.refresh()
    view._open(view._nav_order[0])
    slot = _first(view, IMPORTABLE)
    view._import_slot(slot)
    assert warned, "a refusal must be reported"
    assert len(LotRepository(conn).list_for_item(item.id)) == 1


# ---------------------------------------------------------------------------
# nav registration
# ---------------------------------------------------------------------------


def test_the_window_is_a_nav_destination_in_inventory_order(app):
    from tcginventory.ui.main_window import NAV_ICONS, NAV_ITEMS

    assert "Personal Collection" in NAV_ITEMS
    assert NAV_ITEMS.index("Personal Collection") == NAV_ITEMS.index("Inventory") + 1
    assert NAV_ICONS["Personal Collection"] == "library"


def test_every_nav_icon_actually_exists(app):
    """icons.pixmap returns a BLANK pixmap for an unknown name rather than
    raising, so a typo would be an invisible glyph no other test catches."""
    from tcginventory.ui import icons
    from tcginventory.ui.main_window import NAV_ICONS

    for name in NAV_ICONS.values():
        assert not icons.pixmap(name, "#ffffff", 16).isNull()
        assert icons._svg_text(name) is not None, f"no bundled icon {name!r}"


def test_every_template_icon_actually_exists(app):
    from tcginventory.services.collections import TEMPLATES
    from tcginventory.ui import icons

    for template in TEMPLATES:
        assert icons._svg_text(template.icon) is not None, (
            f"template {template.id!r} names a missing icon {template.icon!r}"
        )


def test_the_view_hardcodes_no_colours():
    """THEME-TOKENS: every colour comes from theme.active()."""
    import re
    from pathlib import Path

    import tcginventory.ui.views.collections as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    # Strip the token interpolations, then look for surviving hex literals.
    stripped = re.sub(r"\{t\.[a-z_0-9]+\}", "", source)
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b", stripped)


# ---------------------------------------------------------------------------
# the navigator must agree with the page
# ---------------------------------------------------------------------------


def _three(conn):
    CardCatalogRepository(conn).replace_for_language(
        [
            CatalogCard(
                card_id="s1-1",
                name="Alpha",
                set_id="s1",
                set_name="Set One",
                number="1",
                printed_total=1,
            ),
        ],
        "EN",
        now_iso(),
    )
    repo = CollectionRepository(conn)
    for name, template, selector in [
        ("Chase cards", "custom", None),
        ("Set One", "set", "Set One"),
        ("Another", "set", "Set One"),
    ]:
        repo.add(Collection(name=name, template=template, selector=selector))
    conn.commit()


def test_opening_a_block_shows_that_collection_not_the_first_one(view, conn):
    """Regression: blockSignals does NOT stop currentRowChanged, which is driven
    by the list's selection model. Moving currentRow off its initial -1 emitted a
    change for row 0, so the page rendered the first collection while the
    navigator highlighted the one clicked."""
    _three(conn)
    view.refresh()
    wanted = next(c for c in view._nav_order if c.name == "Set One")
    view._open(wanted)
    assert view._detail_title.text() == "Set One"
    assert view._nav.currentRow() == view._nav_order.index(wanted)


def test_the_navigator_still_works_as_a_navigator(view, conn):
    """The guard must not disable real clicks."""
    _three(conn)
    view.refresh()
    view._open(view._nav_order[0])
    target = len(view._nav_order) - 1
    view._nav.setCurrentRow(target)  # as a user click would
    assert view._detail_title.text() == view._nav_order[target].name


def test_rebuilding_the_navigator_does_not_render_a_stale_collection(view, conn):
    _three(conn)
    view.refresh()
    wanted = next(c for c in view._nav_order if c.name == "Set One")
    view._open(wanted)
    view.refresh()  # rebuilds the nav under the open page
    assert view._detail_title.text() == "Set One"
