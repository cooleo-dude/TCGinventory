"""The Personal Collection window's templates and slot computation (ticket 60).

A collection asks the opposite question to Inventory. Inventory is one row per
*lot* and can only draw what is held; a collection has to draw what is **not**
held, so membership comes from ``card_catalog`` and ownership is painted on top.

Three states per slot, and ownership *is* membership for a template collection
(migration 0025 explains why there are no membership rows):

    filled      owned, and the lot's intent is ``personal``
    importable  owned, but the lot is ``resale``/``ripping`` — matches a slot,
                not yet brought in
    missing     not owned

Qt-free and issues its own SQL through the repositories only, so it is unit
testable headless. No money math lives here: slot values come from the item's
recorded market value, formatted by the UI.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from tcginventory.data.repositories import (
    CardCatalogRepository,
    CardTreatmentRepository,
    CollectionRepository,
    number_forms,
)
from tcginventory.domain.enums import Intent
from tcginventory.domain.models import Collection

# ---------------------------------------------------------------------------
# the template registry
# ---------------------------------------------------------------------------

# Described as data, mirroring services/operations.py: the view renders itself
# from this, so adding a template is one entry and cannot drift out of line with
# the others. `selector_kind` tells the UI what to ask for when creating one.
TEMPLATE_SET = "set"
TEMPLATE_SUBSET = "subset"
TEMPLATE_ARTIST = "artist"
TEMPLATE_RARITY = "rarity"
TEMPLATE_SUPERTYPE = "supertype"
TEMPLATE_CUSTOM = "custom"

SLOT_BY_NUMBER = "number"
SLOT_MASTER = "master"


@dataclass(frozen=True)
class Template:
    """One way a collection can decide what belongs in it."""

    id: str
    label: str
    blurb: str  # what it does, in the user's terms
    icon: str  # bundled Lucide name (ui/assets/icons)
    selector_kind: (
        str  # what to prompt for: set | subset | artist | rarity | supertype | none
    )
    english_only: bool = False


TEMPLATES: tuple[Template, ...] = (
    Template(
        TEMPLATE_SET,
        "Set",
        "Every card in one set, numbered as printed.",
        "layers",
        "set",
        english_only=True,  # printed_total is EN-only (0 of 29,845 JP rows)
    ),
    Template(
        TEMPLATE_SUBSET,
        "Subset",
        "A lettered run inside a set — Trainer Gallery, Radiant Collection.",
        "image",
        "subset",
    ),
    Template(
        TEMPLATE_ARTIST,
        "Artist",
        "Every card one illustrator drew.",
        "pencil",
        "artist",
        english_only=True,  # artist is EN-only; JP ids do not join to TCGdex
    ),
    Template(
        TEMPLATE_RARITY,
        "Rarity",
        "Every card of one rarity — Illustration Rares, say.",
        "sparkles",
        "rarity",
    ),
    Template(
        TEMPLATE_SUPERTYPE,
        "Card type",
        "Every Pokémon, Trainer or Energy card.",
        "shapes",
        "supertype",
    ),
    Template(
        TEMPLATE_CUSTOM,
        "Custom",
        "Cards you pick by hand.",
        "folder-open",
        "none",
    ),
)

TEMPLATES_BY_ID = {t.id: t for t in TEMPLATES}

# Slot states
FILLED = "filled"
IMPORTABLE = "importable"
MISSING = "missing"


@dataclass
class Slot:
    """One position in a collection's checklist."""

    card_id: str
    name: str
    number: str | None
    set_name: str | None
    image_url: str | None
    # Shown as list-view columns, so they come from the same query rather than a
    # second lookup per row.
    artist: str | None = None
    rarity: str | None = None
    treatment: str = ""  # the printing, in master mode; "" in by-number mode
    # The foil pattern of the copy actually held, when one is. In master mode the
    # printing is already known from `treatment`; by number it is not, so this is
    # the only way the checklist can say "you have the reverse holo".
    holo: str | None = None
    # The card's sole printing, when the catalog knows of exactly one. Not an
    # identity — `treatment` is — just enough to say "this one is a holo" on a
    # by-number checklist, where every slot is a collector number.
    only_printing: str | None = None
    state: str = MISSING
    # Populated when owned. `lot_ids` is every matching lot, so the UI can offer
    # an import without re-querying; `qty` is units on hand across them.
    item_id: int | None = None
    lot_ids: list[int] = field(default_factory=list)
    qty: int = 0
    market_value_cents: int | None = None
    # True when master mode could not learn this card's printings, so the slot
    # count for it is unknown rather than one. Never conflate the two.
    treatments_unknown: bool = False

    @property
    def owned(self) -> bool:
        return self.state in (FILLED, IMPORTABLE)


@dataclass
class Progress:
    """A collection's headline numbers."""

    total: int = 0
    filled: int = 0
    importable: int = 0
    missing: int = 0
    # Market value of the slots actually **in** the collection — filled only.
    # Counting importable copies here made the grid read "Slots filled 0" beside
    # "In collection $1,113.02", which is a figure contradicting its own label.
    value_cents: int = 0
    # Value of the owned-but-not-brought-in copies, kept separate for the same
    # reason: it answers "what could I add", not "what is in here".
    importable_value_cents: int = 0
    unknown_treatments: int = 0

    @property
    def percent(self) -> float:
        return (self.filled / self.total * 100) if self.total else 0.0


def _no_edition(value: str | None) -> bool:
    """Whether an edition value means "this card has no edition distinction".

    ``Edition.NA.value`` is the *string* ``"N/A"``, and an unset column is
    ``None`` — semantically the same thing, and both are what the overwhelming
    majority of cards are. ``TREATMENT_IDENTITY`` yields ``None`` for the
    plain printings (Normal, Holofoil, Reverse Holofoil), so a master-mode slot
    comparing raw values against an item rejected every one of the 1,055 of 1,115
    real items that store ``"N/A"`` — master mode found nothing ownable at all.
    """
    return value in (None, "", "N/A")


def _same_edition(item_edition: str | None, wanted: str | None) -> bool:
    """Compare an item's edition with a treatment's, treating "no edition" as one
    value however it is spelled."""
    if _no_edition(wanted):
        return _no_edition(item_edition)
    return item_edition == wanted


def collector_key(number: str | None) -> tuple[str, int, str]:
    """Natural sort key for a collector number.

    ``ORDER BY number`` is lexicographic, which runs a 102-card set as
    1, 10, 100, 101, 102, 11, 12 … — useless as a checklist. Collector numbers
    are a letter head, a number, and sometimes a letter tail ("TG01", "006",
    "195a"), so sort on those three parts.
    """
    text = (number or "").split("/", 1)[0].strip()
    head = ""
    i = 0
    while i < len(text) and not text[i].isdigit():
        head += text[i]
        i += 1
    digits = ""
    while i < len(text) and text[i].isdigit():
        digits += text[i]
        i += 1
    return (head.upper(), int(digits) if digits else 1 << 30, text[i:].lower())


class CollectionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.collections = CollectionRepository(conn)
        self.catalog = CardCatalogRepository(conn)
        self.treatments = CardTreatmentRepository(conn)

    # -- what the templates can offer --------------------------------------

    def selector_options(self, template: str, language: str = "EN") -> list[str]:
        """The values a template can be pointed at, for the creation dialog.

        Empty means the catalog cannot support that template yet — an artist run
        before the catalog has been re-pulled, for instance. The UI must say so
        rather than offering an empty picker.
        """
        if template == TEMPLATE_SET:
            rows = self.conn.execute(
                "SELECT DISTINCT set_name FROM card_catalog WHERE language = ? "
                "AND set_name IS NOT NULL AND printed_total IS NOT NULL "
                "ORDER BY set_name",
                (language,),
            ).fetchall()
            return [r["set_name"] for r in rows]
        if template == TEMPLATE_ARTIST:
            return self.catalog.distinct_artists(language)
        if template == TEMPLATE_RARITY:
            rows = self.conn.execute(
                "SELECT DISTINCT rarity FROM card_catalog WHERE language = ? "
                "AND rarity IS NOT NULL AND rarity <> '' AND rarity <> 'None' "
                "ORDER BY rarity",
                (language,),
            ).fetchall()
            return [r["rarity"] for r in rows]
        if template == TEMPLATE_SUPERTYPE:
            rows = self.conn.execute(
                "SELECT DISTINCT supertype FROM card_catalog WHERE language = ? "
                "AND supertype IS NOT NULL AND supertype <> '' ORDER BY supertype",
                (language,),
            ).fetchall()
            return [r["supertype"] for r in rows]
        if template == TEMPLATE_SUBSET:
            # Lettered collector-number heads ("TG", "GG", "RC") — the runs that
            # live inside a parent set. Derived rather than listed, so a new
            # subset era needs no code change.
            rows = self.conn.execute(
                "SELECT DISTINCT number FROM card_catalog WHERE language = ? "
                "AND number IS NOT NULL AND number GLOB '[A-Za-z]*'",
                (language,),
            ).fetchall()
            prefixes = set()
            for r in rows:
                head = ""
                for ch in r["number"]:
                    if ch.isdigit():
                        break
                    head += ch
                if head:
                    prefixes.add(head.upper())
            return sorted(prefixes)
        return []

    # -- slots --------------------------------------------------------------

    def _catalog_rows(self, collection: Collection) -> list[sqlite3.Row]:
        """The catalog cards a collection's template matches."""
        lang = collection.language
        sel = collection.selector
        base = (
            "SELECT card_id, name, number, set_name, set_id, image_small, artist, "
            "rarity FROM card_catalog WHERE language = ? "
        )
        if collection.template == TEMPLATE_CUSTOM:
            picks = self.collections.cards_for(collection.id)
            if not picks:
                return []
            ids = [c for c, _t in picks]
            out: list[sqlite3.Row] = []
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                out.extend(
                    self.conn.execute(
                        base + f"AND card_id IN ({marks})", (lang, *chunk)
                    ).fetchall()
                )
            return out
        if sel is None:
            return []
        if collection.template == TEMPLATE_SET:
            sql, params = base + "AND set_name = ? ORDER BY set_id, number", (lang, sel)
        elif collection.template == TEMPLATE_ARTIST:
            sql, params = base + "AND artist = ? ORDER BY set_name, number", (lang, sel)
        elif collection.template == TEMPLATE_RARITY:
            sql, params = base + "AND rarity = ? ORDER BY set_name, number", (lang, sel)
        elif collection.template == TEMPLATE_SUPERTYPE:
            sql, params = (
                base + "AND supertype = ? ORDER BY set_name, number",
                (lang, sel),
            )
        elif collection.template == TEMPLATE_SUBSET:
            sql, params = (
                base + "AND number LIKE ? ORDER BY set_name, number",
                (lang, f"{sel}%"),
            )
        else:
            return []
        return self.conn.execute(sql, params).fetchall()

    def _ownership(self, language: str) -> dict[tuple, list[sqlite3.Row]]:
        """``(name, set_name, number-form) -> lot rows`` for what is held.

        ``item`` has no ``card_id`` — identity is twelve NULL-safe columns — so a
        catalog slot is matched to holdings by printed identity. About 88% of real
        items resolve; the rest are reported by :meth:`unmatched_items` rather
        than silently dropped.
        """
        rows = self.conn.execute(
            "SELECT i.id AS item_id, i.name, i.set_name, i.card_number, "
            "i.market_value_cents, i.holo_pattern, i.edition, "
            "l.id AS lot_id, l.intent, l.remaining_qty "
            "FROM item i JOIN lot l ON l.item_id = i.id "
            "WHERE i.language = ? AND l.missing = 0 AND l.remaining_qty > 0",
            (language,),
        ).fetchall()
        index: dict[tuple, list[sqlite3.Row]] = {}
        for row in rows:
            name = (row["name"] or "").strip().lower()
            set_name = (row["set_name"] or "").strip().lower()
            for form in number_forms((row["card_number"] or "").split("/", 1)[0]):
                index.setdefault((name, set_name, form), []).append(row)
        return index

    def slots_for(self, collection: Collection) -> list[Slot]:
        """Every position in a collection, with its ownership state."""
        cards = self._catalog_rows(collection)
        owned = self._ownership(collection.language)
        master = collection.slot_mode == SLOT_MASTER
        # Loaded in both modes. Master mode needs them to make a slot per
        # printing; by number they still say *which* printing a card is when it
        # only comes one way, which is how a Base Set holo can read as a holo
        # without anyone owning it. One query either way.
        treatments = self.treatments.treatments_for_many([c["card_id"] for c in cards])

        slots: list[Slot] = []
        # Collector order, not lexicographic. The catalog query cannot do this:
        # SQL sorts "10" before "2" and a checklist has to read 1, 2, 3.
        cards = sorted(
            cards,
            key=lambda c: (
                (c["set_name"] or "") if collection.template != TEMPLATE_SET else "",
                collector_key(c["number"]),
            ),
        )
        for card in cards:
            # What the card *is* (every printing the catalog knows of), kept
            # apart from what produces slots: by number a card is one slot
            # whatever its printings, and conflating the two made a by-number
            # slot claim a printing of "".
            known = treatments.get(card["card_id"], [])
            unknown = master and not known
            printings = known if master and known else [""]
            # Master mode with no treatment data: one slot, flagged. Never
            # silently one printing — that under-counts a master set and makes it
            # read complete when it is not.
            for treatment in printings:
                slot = Slot(
                    card_id=card["card_id"],
                    name=card["name"],
                    number=card["number"],
                    set_name=card["set_name"],
                    image_url=card["image_small"],
                    artist=card["artist"],
                    rarity=card["rarity"],
                    treatment=treatment,
                    only_printing=(known[0] if len(known) == 1 else None),
                    treatments_unknown=unknown,
                )
                self._apply_ownership(slot, owned, master)
                slots.append(slot)
        return slots

    def _apply_ownership(
        self, slot: Slot, owned: dict[tuple, list[sqlite3.Row]], master: bool
    ) -> None:
        """Paint a slot's state from what is held."""
        from tcginventory.services.pricing import TREATMENT_IDENTITY

        key = (
            (slot.name or "").strip().lower(),
            (slot.set_name or "").strip().lower(),
            (slot.number or "").split("/", 1)[0].lstrip("0") or (slot.number or ""),
        )
        lots = owned.get(key)
        if not lots and slot.number:
            for form in number_forms((slot.number or "").split("/", 1)[0]):
                lots = owned.get((key[0], key[1], form))
                if lots:
                    break
        if not lots:
            return

        if master and slot.treatment:
            # In master mode a slot is one printing, so only a copy that *is*
            # that printing fills it. An item whose foil is unrecorded cannot be
            # claimed for a specific printing.
            want = TREATMENT_IDENTITY.get(slot.treatment)
            if want is not None:
                lots = [
                    r
                    for r in lots
                    if r["holo_pattern"] == want[0]
                    and _same_edition(r["edition"], want[1])
                ]
            if not lots:
                return

        slot.item_id = lots[0]["item_id"]
        slot.holo = lots[0]["holo_pattern"]
        slot.lot_ids = [r["lot_id"] for r in lots]
        slot.qty = sum(r["remaining_qty"] for r in lots)
        slot.market_value_cents = lots[0]["market_value_cents"]
        slot.state = (
            FILLED
            if any(r["intent"] == Intent.PERSONAL.value for r in lots)
            else IMPORTABLE
        )

    def progress_for(self, collection: Collection) -> Progress:
        return summarise(self.slots_for(collection))

    def unmatched_items(self, language: str = "EN") -> int:
        """Owned items that resolve to no catalog card at all.

        Reported so the window can say so. ``item`` carries no ``card_id``, so
        this is a real limit of identity matching, not a bug to hide.

        Counted as a **set difference in two passes**, not as a correlated
        subquery. The SQL form was::

            SELECT COUNT(*) FROM item i WHERE NOT EXISTS (
              SELECT 1 FROM card_catalog c
              WHERE LOWER(TRIM(c.name)) = LOWER(TRIM(i.name)) AND …)

        which plans as ``SCAN i`` + a correlated ``SCAN c``: 1,115 items x 53,289
        catalog rows is **59.4 million comparisons**, and wrapping both sides in
        ``LOWER(TRIM(...))`` means no index can serve any of them. Measured at
        **11.3 seconds** on the real ledger — and the Personal Collection window
        called it on every click into a collection, which was the entire reported
        lag. Building the catalog's key set once and probing it is O(n+m):
        **27 ms for the identical answer.**

        The result is portfolio-wide, not per-collection, so the caller computes
        it once per refresh rather than once per collection. (Ticket 64.)
        """
        catalog = {
            self._identity_key(row["name"], row["set_name"])
            for row in self.conn.execute(
                "SELECT name, set_name FROM card_catalog WHERE language = ?",
                (language,),
            )
        }
        return sum(
            1
            for row in self.conn.execute(
                "SELECT name, set_name FROM item WHERE language = ?", (language,)
            )
            if self._identity_key(row["name"], row["set_name"]) not in catalog
        )

    @staticmethod
    def _identity_key(name: str | None, set_name: str | None) -> tuple[str, str]:
        """The loose (name, set) key both sides of the match normalise to.

        Mirrors what the old SQL did with ``LOWER(TRIM(COALESCE(...)))`` on each
        column, so the count is unchanged — only the algorithm is.
        """
        return (
            (name or "").strip().lower(),
            (set_name or "").strip().lower(),
        )


@dataclass
class ArtGap:
    """What a collection's checklist is missing, picture-wise.

    Two very different things, kept apart because only one of them is work:
    ``fetchable`` is art the catalog has a URL for and the disk cache does not
    have yet, and ``no_source`` is cards the catalog has no image URL for at all.
    Measured on the real catalog, the second dominates — a 5,805-slot rarity run
    had 224 cards without art of which **220 had no URL**, leaving 4 to fetch.
    Reporting them as one number would make a permanent gap look like pending
    work and invite a warm that re-runs forever.
    """

    fetchable: list[tuple[str, str]] = field(default_factory=list)
    no_source: int = 0

    def __len__(self) -> int:
        return len(self.fetchable)


def art_gap(slots: list[Slot]) -> ArtGap:
    """The art a collection would need to fetch, deduplicated by card.

    Qt-free and does no I/O beyond stat-ing the cache, so it is cheap enough to
    call on every open: 2,072 slots resolve in a few milliseconds.
    """
    from tcginventory.services import cardimages

    gap = ArtGap()
    seen: set[str] = set()
    for slot in slots:
        if not slot.card_id or slot.card_id in seen:
            continue
        seen.add(slot.card_id)
        if cardimages.cache_path(slot.card_id).exists():
            continue
        if slot.image_url:
            gap.fetchable.append((slot.card_id, slot.image_url))
        else:
            gap.no_source += 1
    return gap


def summarise(slots: list[Slot]) -> Progress:
    """Roll slots up into headline numbers. Pure, so it is trivially testable."""
    p = Progress(total=len(slots))
    for slot in slots:
        if slot.state == FILLED:
            p.filled += 1
        elif slot.state == IMPORTABLE:
            p.importable += 1
        else:
            p.missing += 1
        if slot.treatments_unknown:
            p.unknown_treatments += 1
        if slot.market_value_cents:
            worth = slot.market_value_cents * max(slot.qty, 1)
            if slot.state == FILLED:
                p.value_cents += worth
            elif slot.state == IMPORTABLE:
                p.importable_value_cents += worth
    return p
