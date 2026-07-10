# User Guide

How to use TCGinventory.

## Getting started

1. Launch the app. On first run it creates a local SQLite database in your
   app-data folder (the path is shown in the status bar) and takes a timestamped
   backup on every startup.
2. Navigate with the left rail: **Dashboard · Transaction · Inventory · History**.
3. You don't create items by hand — they're created automatically the first time
   you enter a card on a transaction, and matched afterward by their identity
   (name + set + number + language + condition/grade).

## Everyday workflows

All buying, selling, and trading happen on the **one Transaction window**: what
you give on the left, what you receive on the right. The badge shows whether the
app will record a **PURCHASE / SALE / TRADE**, inferred from which sides carry
cards. The footer previews consideration, net cash, and estimated margin. Press
**Ctrl+S** (or click **Submit**) to post.

### Log a purchase
**Transaction** → "Cash you pay" on the left → **Add card to receive…** for each
card (name, set, card #, language, market value, condition or grade, intent:
**Personal / Resale / Ripping**). Submit. Each card's cost is inferred by
splitting your total across the cards by market value; a lot is created per card.
Cards tagged **Ripping** (sealed product you'll open, never sell) show in
inventory as a loss equal to their cost.

### Log a sale
**Transaction** → **Add card to give…** to pick cards you're selling (quantity
capped at what you hold; ripping lots are excluded) → enter "Cash they pay" on
the right → check the previewed margin → Submit. Selling more than you hold is
blocked.

### Log a trade
**Transaction** → add cards on the left (+ any cash you add) and cards on the
right (+ any cash they add) → confirm the balance and margin → Submit.

### Check your position
- **Inventory** — everything, sortable and filterable (search, intent, sold
  status, in-stock). Double-click a row for **Item detail** (open lots + full
  history), where **Edit item…** lets you fix identity, set the market value and
  as-of date, SKU, location, and links.
- **Dashboard** — portfolio tiles (cost basis, market value, unrealized/realized
  P/L, net cash) and a money-movement / realized-profit chart, filterable by
  intent.
- **History** — every transaction; click one for its detail (lines + which lots
  were drawn down).

### Correct a mistake
In **History**, select a transaction:
- **Void** reverses its effects but keeps it visible (greyed) for the record.
- **Edit** re-opens it in the Transaction window; saving voids the original and
  re-posts the corrected version atomically, warning you if realized P/L changes.

Nothing is ever hard-deleted, and every create/edit/void is written to an
append-only audit log.

## Import, export & backup

From the **File** menu:
- **Import items from CSV** / **Import purchases from CSV** — column headers are
  matched automatically; you get a preview of valid rows and any errors before
  anything is written.
- **Export inventory to CSV** — a spreadsheet-friendly snapshot.
- **Export full backup (CSV folder)** — a complete dump of every table that can
  be restored exactly (all data is reconstructable from it).
- **Backup database** — copy the SQLite file somewhere safe.

## FAQ

- **How is each card's cost decided?** You enter one total price and a market
  value per card; the app splits the total across cards by relative market value.
  Fold shipping/fees into the total so profit is measured against true landed
  cost. See [cost-basis examples](cost-basis-examples.md).
- **Which lot gets sold first?** The oldest (FIFO). Specific-lot selection is a
  planned enhancement.
- **What's "margin"?** Realized profit on cards you sold or traded away
  (proceeds − cost basis), shown in dollars and as a gross-margin %.
