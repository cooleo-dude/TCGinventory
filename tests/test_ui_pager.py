"""The shared page selector (ticket 64).

Extracted from the eBay Auctions watchlist, which drew one button per page. A
2,072-slot collection is ~116 pages, so the row has to be windowed.
"""

from __future__ import annotations

import pytest

from tcginventory.ui.pager import Pager, page_numbers

# ---------------------------------------------------------------------------
# windowing (pure)
# ---------------------------------------------------------------------------


def test_a_short_run_shows_every_page():
    assert page_numbers(0, 1) == [0]
    assert page_numbers(0, 3) == [0, 1, 2]
    assert page_numbers(2, 5) == [0, 1, 2, 3, 4]


def test_a_long_run_is_windowed_not_enumerated():
    """The reason this exists: 116 buttons is not a control."""
    out = page_numbers(60, 116)
    assert out == [0, None, 58, 59, 60, 61, 62, None, 115]
    assert len([p for p in out if p is not None]) <= 9


def test_the_first_and_last_page_are_always_reachable():
    for current in (0, 1, 57, 114, 115):
        out = page_numbers(current, 116)
        assert out[0] == 0
        assert out[-1] == 115


def test_the_current_page_is_always_present():
    for current in range(0, 116, 7):
        assert current in page_numbers(current, 116)


def test_a_one_page_gap_shows_the_page_rather_than_an_ellipsis():
    """ "… 4 …" is longer than "4"; an ellipsis must earn its place."""
    out = page_numbers(3, 8)
    assert None not in out
    assert out == [0, 1, 2, 3, 4, 5, 6, 7]


def test_no_pages_yields_nothing():
    assert page_numbers(0, 0) == []


# ---------------------------------------------------------------------------
# the widget
# ---------------------------------------------------------------------------


def test_a_single_page_hides_the_control(app):
    pager = Pager()
    pager.set_pages(1)
    assert not pager.isVisibleTo(pager)
    pager.deleteLater()


def test_paging_emits_the_new_page(app):
    pager = Pager()
    pager.set_pages(10)
    seen: list[int] = []
    pager.page_changed.connect(seen.append)
    pager.set_page(4)
    assert seen == [4]
    assert pager.page == 4
    pager.deleteLater()


def test_setting_the_same_page_emits_nothing(app):
    pager = Pager()
    pager.set_pages(10)
    pager.set_page(3)
    seen: list[int] = []
    pager.page_changed.connect(seen.append)
    pager.set_page(3)
    assert seen == []
    pager.deleteLater()


def test_a_page_beyond_the_end_clamps(app):
    pager = Pager()
    pager.set_pages(5)
    pager.set_page(99)
    assert pager.page == 4
    pager.set_page(-3)
    assert pager.page == 0
    pager.deleteLater()


def test_shrinking_the_page_count_pulls_the_current_page_back(app):
    """A filter can drop 116 pages to 3 while sitting on page 90; the grid must
    not be left showing nothing."""
    pager = Pager()
    pager.set_pages(116)
    pager.set_page(90)
    pager.set_pages(3)
    assert pager.page == 2
    pager.deleteLater()


def test_a_long_run_renders_a_windowed_row_not_one_button_each(app):
    """The widget-level version of the windowing guarantee."""
    from PySide6.QtWidgets import QPushButton

    pager = Pager()
    pager.set_pages(116)
    pager.set_page(60)
    buttons = pager.findChildren(QPushButton)
    # 9 page buttons at most, plus the two arrows.
    assert len(buttons) <= 11, f"{len(buttons)} buttons for 116 pages"
    pager.deleteLater()


@pytest.mark.parametrize("pages", [2, 7, 40, 116, 500])
def test_the_row_stays_a_bounded_width_at_any_scale(app, pages):
    from PySide6.QtWidgets import QPushButton

    pager = Pager()
    pager.set_pages(pages)
    pager.set_page(pages // 2)
    assert len(pager.findChildren(QPushButton)) <= 11
    pager.deleteLater()


def test_the_page_digits_are_actually_painted(app):
    """The bug this guards: the app-level stylesheet gives every QPushButton
    `padding: 6px 14px`, and these buttons are fixed at 26px wide. Without a
    padding reset the content box goes negative, Qt elides the digit to nothing,
    and the pager renders as a row of empty boxes.

    Asserting the text is *set* passes even when nothing is drawn, and so does
    ``contentsRect()`` (stylesheet padding does not affect it). So this renders
    the widget and looks for ink inside the button, which is the only thing that
    actually distinguishes a digit from an empty box.
    """
    from PySide6.QtWidgets import QPushButton

    from tcginventory.ui import theme

    theme.apply_theme(app)  # the app stylesheet is what breaks it
    pager = Pager()
    pager.set_pages(12)
    pager.set_page(5)
    # Lay out before sampling: until the layout runs every button reports
    # geometry (0, 0, 26, 26), so all the samples would read the same spot.
    pager.resize(pager.sizeHint())
    pager.show()
    app.processEvents()
    image = pager.grab().toImage()

    checked = 0
    for button in pager.findChildren(QPushButton):
        if not button.text().isdigit():
            continue
        rect = button.geometry()
        # Inset past the rounded border so only the glyph area is sampled.
        inset = 5
        colours = {
            image.pixel(x, y)
            for x in range(rect.left() + inset, rect.right() - inset)
            for y in range(rect.top() + inset, rect.bottom() - inset)
        }
        assert len(colours) > 1, (
            f"button {button.text()!r} is a flat block of colour — the digit was "
            "not painted"
        )
        checked += 1
    assert checked >= 5, "expected several page buttons to sample"
    pager.deleteLater()
