# User Guide

How to use TCGinventory. This is a living document — sections fill in as
features land (see the [roadmap](../specs/05-roadmap.md)). Until then it doubles
as the intended workflow.

## Getting started (planned)

1. Launch the app. On first run it creates a local database in your app-data
   folder (shown in the status bar).
2. Add your first item (Items → New), or record a purchase, which can create
   items inline.

## Everyday workflows (planned)

All buying, selling, and trading happen on the **one Transaction window**: what
you give on the left, what you receive on the right. The app figures out whether
it's a purchase, sale, or trade from what's on each side.

### Log a purchase
Open **Transaction** → left side: enter your total cash paid → right side: add a
row per card (name, set, links, **market value**, condition, grade, notes,
intent: **Personal / Resale / Ripping**) → Save. Each card's cost is inferred by
splitting your total across the cards by market value; a lot is created per card.
Cards tagged **Ripping** (sealed product you'll open, never sell) show in
inventory as a loss equal to their cost.

### Log a sale
Open **Transaction** → left side: pick the cards you're selling (quantities
capped at what you hold) → right side: enter the cash received → review the
previewed realized P/L → Save. Selling more than you hold is blocked.

### Log a trade
Open **Transaction** → left: cards you give (+ any cash you add); right: cards
you receive (+ any cash they add) → confirm the balance → Save.

### Check your position
- **Inventory** — everything on hand with cost basis, market value, unrealized
  P/L.
- **Item detail** — open lots and full history for one item.
- **Dashboard** — portfolio totals and realized P/L.

### Keep market values current
Open an item → Set market value (with an as-of date). This drives unrealized
P/L. (Automatic price feeds are a future feature.)

## Backups (planned)

Use **Backup database** to copy your data file somewhere safe, and/or export to
CSV. Everything can be reconstructed from a CSV export.

## FAQ

- **How is each card's cost decided?** You enter one total price for the lot
  and a market value per card; the app splits the total across cards by their
  relative market value. Fold shipping/fees into the total so profit is measured
  against true landed cost. See [cost-basis examples](cost-basis-examples.md).
- **Which lot gets sold first?** The oldest (FIFO). Specific-lot selection is a
  planned enhancement.
