# 03 — Features & Requirements (V1)

Requirements are labeled **MUST** (V1 blocker), **SHOULD** (V1 if time allows),
or **LATER** (deferred; see [roadmap](05-roadmap.md)). Each has an ID for
traceability.

## Items / catalog

Items are **auto match-or-created** during transactions — there is no manual
"add item" step. This section covers how identity is resolved and how items are
maintained afterward.

| ID | Req | Priority |
|----|-----|----------|
| F-IT-1 | Auto match-or-create an item by its **identity key** (name + set + card number + language + condition + grade + grading company/number) when a card is entered on a transaction row. | MUST |
| F-IT-2 | Capture grading (company, grade #, cert #); **Condition greys out when graded** (grade identifies the card). | MUST |
| F-IT-3 | Edit an item (fix identity, notes) and soft-delete it (block delete if lots/lines exist; require adjustment-out instead). | MUST |
| F-IT-4 | Auto-generate a unique per-copy **SKU** on the lot at acquisition (`{name}-{set}-{number}-{cond/grade}-{lang}-{seq}`, sequenced per identity); maintain location on the item. | MUST |
| F-IT-5 | Set/maintain a manual market value with an "as-of" date (most-recent entry wins; drives unrealized P/L). | MUST |
| F-IT-6 | Store reference links (TCGplayer, eBay) on an item. | MUST |
| F-IT-7 | Quick-lookup a card by SKU when selling/trading (resolve the entered SKU to its card). | MUST |

> **All three of the below are served by one unified Transaction window**
> (Spec 04, Window 2): cards/cash out ⇄ cards/cash in, with the `type` inferred
> from which sides carry cards. The requirements are grouped by type for clarity.

| ID | Req | Priority |
|----|-----|----------|
| F-TX-1 | A single bidirectional Transaction screen records purchases, sales, and trades. | MUST |
| F-TX-2 | Transaction `type` is inferred (no cards out → purchase; no cards in → sale; both → trade). | MUST |
| F-TX-3 | Post the whole transaction atomically (all lot/cash/P/L effects in one DB transaction). | MUST |
| F-TX-4 | Tag each incoming card with an intent per row: **personal / resale / ripping**. | MUST |
| F-TX-5 | Live transaction summary shows value given/received, net cash, cost basis drawn (FIFO), and **margin** on cards given up (profit $ + gross-margin % = profit ÷ proceeds); repeated on the success screen. | MUST |

## Purchases (acquisition side)

| ID | Req | Priority |
|----|-----|----------|
| F-PU-1 | Acquire a single card: enter total price + date; market value per card. | MUST |
| F-PU-2 | Acquire multiple cards in one transaction (a lot/order), one row each. | MUST |
| F-PU-3 | Infer each card's cost basis by market-value-weighted split of the consideration given (fees folded into the total). | MUST |
| F-PU-4 | Creating an acquisition match-or-creates items and writes one lot per card capturing its inferred basis. | MUST |
| F-PU-5 | Require a market value on every incoming row before submit. | MUST |

## Sales (disposal side)

| ID | Req | Priority |
|----|-----|----------|
| F-SA-1 | Record a sale of a single card; draw down lots FIFO. | MUST |
| F-SA-2 | Record a multi-card sale in one transaction. | MUST |
| F-SA-4 | Compute and store realized P/L per line at sale time. | MUST |
| F-SA-5 | Block a sale exceeding units on hand with a clear error. | MUST |
| F-SA-6 | Choose specific lot(s) to sell from (override FIFO). | LATER |

## Trades

| ID | Req | Priority |
|----|-----|----------|
| F-TR-1 | Record a trade with outgoing and incoming cards in one transaction. | MUST |
| F-TR-2 | Value each side at agreed fair value; realize P/L on outgoing side. | MUST |
| F-TR-3 | Establish incoming cost basis from `net_consideration_in` (cash out + cards out − cash in), split by market value. | MUST |
| F-TR-4 | Handle cash boot (cash added to balance a trade) on either side. | MUST |

## Inventory & views

| ID | Req | Priority |
|----|-----|----------|
| F-VW-1 | Inventory grid: item, intent, qty on hand, cost basis, market value, unrealized P/L, sold status, location. | MUST |
| F-VW-2 | Item detail: attributes, open lots (w/ intent toggle), full transaction history, P/L. | MUST |
| F-VW-3 | Full transaction log: chronological list; filter by type/date/counterparty; open a transaction detail (all lines + lot consumption); void and edit from here. | MUST |
| F-VW-4 | Dashboard: totals — units, cost basis, market value, realized & unrealized P/L. | SHOULD |
| F-VW-5 | Sort and multi-column filter on the inventory grid. | MUST |
| F-VW-6 | Dashboard line graph: **money movement** (cumulative net cash) & **realized profit** over time. | MUST |
| F-VW-7 | Dashboard line graph: **collection value** (market value, with cost basis comparison) over time. | MUST |
| F-VW-8 | Dashboard charts filterable by intent (all / personal / resale / ripping). | SHOULD |
| F-VW-9 | **Every** card/inventory attribute is filterable **and** sortable in the Inventory view (incl. sold status, intent, value ranges). | MUST |
| F-VW-10 | **Ripping** lots are valued at $0 (shown as an unrealized loss = cost basis), stay visible in inventory, and are never offered as sellable. | MUST |
| F-VW-11 | Dashboard **Highlights**: Top Gainers / Top Losers (by unrealized $), Top Sellers (units sold, trailing 90 days) and Upcoming Shows (attending/walking only). Movers reflect a **resale** business view. | SHOULD |

> **Movers — current exclusions (V1).** The Top Gainers / Losers / Sellers
> windows currently **exclude graded slabs and items marked for review**, in
> addition to personal and ripping lots. A graded card's value and a reviewed
> item's value aren't tracked consistently yet (user-maintained, with no reliable
> automatic feed), so they can't be ranked by market movement. Ways to include
> them (e.g. a graded-price source, or ranking reviewed items by their maintained
> value) will be explored in a future iteration.

## Search & filter

| ID | Req | Priority |
|----|-----|----------|
| F-SE-1 | Quick text search across item name/set/number/SKU. | MUST |
| F-SE-2 | Quick-filter presets in the inventory filter bar (intent, game, set, category, condition, graded, location, in-stock/sold) — a convenience layer over the universal filter/sort of `F-VW-9`. | MUST |
| F-SE-3 | Date-range and type filters on transactions. | SHOULD |

## Import / export

| ID | Req | Priority |
|----|-----|----------|
| F-IE-1 | Export items, lots, and transactions to CSV. | MUST |
| F-IE-2 | Import items from CSV (with column mapping + validation preview). | MUST |
| F-IE-3 | Import purchases/transactions from CSV. | SHOULD |
| F-IE-4 | Full data export suitable as a portable backup. | MUST |

## Data integrity & ops

| ID | Req | Priority |
|----|-----|----------|
| F-DA-1 | Editing/voiding a posted transaction atomically reverses & re-applies lot effects. | MUST |
| F-DA-2 | Transactions are soft-voided (never hard-deleted); voided rows stay visible in history. | MUST |
| F-DA-3 | Append-only audit log records every create/edit/void with timestamp and before/after snapshots. | MUST |
| F-DA-4 | Enforce the invariants in [data model](02-data-model.md#invariants). | MUST |
| F-DA-5 | "Backup database" action copies the SQLite file to a chosen location. | MUST |
| F-DA-6 | Automatic timestamped backup on startup. | SHOULD |
| F-DA-7 | Run pending schema migrations on startup inside a transaction. | MUST |

## Non-functional requirements

- **NFR-1 Correctness:** money in integer cents; all money ops transactional;
  invariants covered by automated tests.
- **NFR-2 Performance:** inventory grid responsive up to ~50k rows (virtualized
  Qt model/view).
- **NFR-3 Portability:** runs on macOS and Windows from the same codebase; DB is
  a single portable file.
- **NFR-4 Usability:** common data-entry paths keyboard-navigable; validation
  errors shown inline before commit.
- **NFR-5 Recoverability:** all data reconstructable from CSV export.
- **NFR-6 Testability:** domain/service layer runs headless with no Qt import.

## Acceptance scenarios (representative)

1. **Multi-card acquisition, market-value split.** Buy a lot of 3 different
   cards for **$80 total**; entered market values **$60 / $30 / $10** (Σ $100).
   → inferred cost basis **$48 / $24 / $8** (each = 80% of its market value),
   summing exactly to $80. One item match-or-created per card, one lot each,
   with per-row intent; `cash_delta = −$80`; no profit recorded.

2. **Same card, two costs, FIFO sale.** Buy `Charizard X` for **$90**, later
   another for **$75** → one item, two lots (basis $90 and $75). Sell one for
   **$120** → FIFO consumes the $90 lot; **realized P/L = $30**; one lot (basis
   $75) remains on hand. Confirms cost lives on the lot, not the item.

3. **Rounding residual.** Acquire 3 cards for **$100 total**; market values
   **$12 / $11 / $10** (Σ $33). Raw split = 3636.36 / 3333.33 / 3030.30¢ →
   floored to 3636 / 3333 / 3030 (Σ 9999¢); the leftover **1¢ goes to the
   highest-value card** → **$36.37 / $33.33 / $30.30**, summing to exactly $100.

4. **Trade with cash back.** Give Card A (basis $20, **agreed value $50**);
   receive Card B (market $40) **+ $10 cash**. → `net_consideration_in =
   50 − 10 = $40`, so **B's basis = $40**; A realizes **$30** gain
   (`50 − 20`); `cash_delta = +$10`; type inferred = **trade**.

5. **Oversell guard.** Attempt to sell 5 when 3 on hand → rejected with
   `InsufficientQuantityError`; nothing is written (atomic).
