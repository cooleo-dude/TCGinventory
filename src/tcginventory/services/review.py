"""Mark-for-review logic shared by card entry and the Inventory detail/editor.

A reviewed item's shown market value is user-maintained and poll-protected (see
``PricingService.refresh``); marking one also attaches an explanatory note so
it's identifiable in the Notes column. Qt-free.
"""

from __future__ import annotations

import sqlite3

from tcginventory.data.repositories import ItemRepository
from tcginventory.domain.models import Item

# Note auto-attached to a reviewed item so the reason its price is
# user-maintained is visible in the Notes column.
REVIEW_NOTE = "Manual price changed detected."


def with_review_note(notes: str | None) -> str:
    """Ensure ``notes`` carries the review marker (idempotent)."""
    existing = (notes or "").strip()
    if not existing:
        return REVIEW_NOTE
    if REVIEW_NOTE in existing:
        return existing
    return f"{existing} {REVIEW_NOTE}"


def without_review_note(notes: str | None) -> str | None:
    """Remove the review marker, preserving any user-authored text.

    Line structure survives. This used to be a flat ``" ".join(notes.split())``,
    which was harmless while every notes editor was a single-line QLineEdit — but
    the item editor's notes field is multi-line, and toggling the review flag on
    a card would otherwise silently flatten the user's paragraphs into one line.
    Within a line, runs of whitespace still collapse, so single-line input comes
    back exactly as it did before.
    """
    if not notes:
        return None
    lines = [" ".join(ln.split()) for ln in notes.replace(REVIEW_NOTE, "").splitlines()]
    while lines and not lines[-1]:  # trim blank lines the marker left behind
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines) or None


def set_item_review(
    conn: sqlite3.Connection, item_id: int, review: bool
) -> Item | None:
    """Mark/unmark an existing item for review, applying the same logic as entry.

    Marking sets the flag (freezing the current shown price against polling) and
    attaches the review note; unmarking clears the flag and strips the note.
    Returns the updated item, or None if it no longer exists.
    """
    items = ItemRepository(conn)
    item = items.get(item_id)
    if item is None:
        return None
    item.review = review
    item.notes = (
        with_review_note(item.notes) if review else without_review_note(item.notes)
    )
    items.update(item)
    conn.commit()
    return item
