# 00 — Overview & Goals

## Vision

A local, single-user desktop application that lets a collector/reseller track
their trading card inventory the way they actually acquire and move it: as
individual cards **and** as lots/bulk, through **purchases, sales, and trades**,
with an accurate running picture of what they own, what it cost, and what
they've made.

The platform serves **both personal collecting and business/resale at once**.
The goal is to account for **every card brought into the collection, regardless
of whether there's any intention to sell it again**. Each acquisition is
classified by **intent — personal (keeper), business (for resale), or ripping
(sealed product bought to be opened, never sold)** — so the
owner can see total collection value across everything *and* isolate business
activity (cost basis, P/L, and eventually taxes) when needed. Intent can change
over time (a keeper later listed for sale, or the reverse).

## Who it's for

- **Primary user:** the owner — a Pokémon collector/reseller managing a personal
  collection and side-selling activity.
- **Single user, single machine.** No multi-user accounts, no server, no cloud
  sync in V1. Data lives in one local SQLite file that the user can back up.

## Problems it solves

1. **"What do I own right now?"** — a live inventory with quantities and
   locations.
2. **"What did it cost me?"** — accurate cost basis across mixed acquisition
   paths (bought, traded-for, part of a lot that was later split).
3. **"Am I making money?"** — realized P/L on sales/trades and unrealized P/L
   vs. a manually entered market value.
4. **"Where did this come from / go?"** — a full transaction history per item
   and overall.

## In scope (V1)

- Catalog of **items** (a specific card in a specific condition/grade, or a
  sealed product).
- **Intent classification** (personal vs. business) on holdings, so collection
  and resale activity can be viewed together or separately.
- **Acquisition lots** — every unit entered the inventory via some event at some
  cost; lots preserve per-acquisition cost basis.
- **Transactions:** purchases, sales, trades — each on a single-item or
  multi-item (lot) basis.
- **Cost basis & P/L:** realized on disposal; unrealized vs. manual market value.
- **Views:** inventory list, item detail with history, transaction log, simple
  summary dashboard.
- **Search & filter** across items and transactions.
- **CSV import/export** for bulk entry and backup/portability.
- **Local persistence** (SQLite) with a schema-migration path.

## Out of scope (V1 — see [roadmap](05-roadmap.md))

- External/automated **price feeds** (TCGplayer, eBay, PriceCharting, etc.).
- **Image/scan** capture or OCR card recognition.
- **Barcode/QR** scanning.
- **Cloud sync, multi-device, multi-user,** authentication.
- **Tax reporting** exports (Schedule C / capital-gains forms).
- **Mobile** app.
- Non-Pokémon game-specific optimizations (schema stays generic enough to add
  them later, but no dedicated UI).

## Success criteria for the baseline

- User can enter a purchase (single or lot) and see it in inventory in < 30s.
- Selling part of a lot correctly reduces quantity and reports realized P/L.
- A trade updates both the disposed and acquired sides with correct basis.
- Inventory totals (quantity, cost basis) always reconcile with the
  transaction log.
- All data is recoverable from a plain CSV export.

## Guiding principles

- **Correctness of money math over features.** Cost basis and P/L must always
  reconcile; prefer explicit, auditable calculations.
- **Local-first & portable.** The user owns their data as a file; nothing is
  locked in.
- **Fast data entry.** The common path (log a buy/sell) must be quick and
  keyboard-friendly.
- **Additive schema.** Design tables so new games/fields/pricing can be added
  without destructive migrations.
