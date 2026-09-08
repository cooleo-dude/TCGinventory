# 02 — Data Model

This is the heart of the app. The model must let a single item be acquired
through multiple paths (bought, traded-for, split from a lot) while preserving
**per-acquisition cost basis**, and must keep inventory reconciled with the
transaction history at all times.

## Core concepts

- **Item** — a *distinct sellable thing*: a specific card in a specific
  condition/grade, or a specific sealed product. "Charizard VMAX #074 Champion's
  Path, PSA 10" is one item; the same card raw NM is a different item. Items are
  **auto match-or-create**: during acquisition the app matches an existing item
  by its **identity key** (name + set + card number + language + condition + holo
  pattern + edition + grade + grading company/number when graded) or creates a
  new one — there is no separate "add to catalog" step. (Holo pattern and edition
  were added to the identity key + schema in migration `0011` — a reverse-holo or
  1st-edition copy is a distinct item.) Item-level attributes (TCGplayer/eBay links,
  current market value) live here; per-acquisition data lives on the lot.
- **Lot (acquisition lot)** — a batch of units of one item that entered the
  inventory via one acquisition event, at one unit cost. Cost basis lives on the
  lot. Selling draws down lots. Each lot also carries its **intent**
  (`personal` | `resale` | `ripping`), set per card at acquisition and editable
  later, and a **market-value snapshot** captured at acquisition (used to
  allocate cost).
- **Intent** — every lot is **personal** (keeper), **resale** (business), or
  **ripping** (sealed product bought to be opened, never sold). Set per card when
  acquired; changeable via a plain editable toggle (no change history in V1).
  Reporting can include everything or filter to one intent.
  - **Ripping** lots are assumed to be opened no matter what, so their
    realizable value is **$0**: they stay visible in inventory but are valued as
    an **unrealized loss equal to their cost basis**, and are never offered as
    sellable. (Any cards pulled from opening are entered separately as an
    ordinary acquisition in the Transaction window — no linkage to the ripped
    lot.)
- **Transaction** — a single **bidirectional exchange**: value/cards you give
  (out) and value/cards you receive (in). Purchases, sales, and trades are all
  the *same* structure — the `type` is **inferred** from which sides carry cards
  vs. cash (see below). `ADJUSTMENT` remains a separate kind for corrections.
- **Transaction line** — the per-item detail of a transaction, each with a
  `direction` (`in` | `out`). A 3-card purchase = 1 transaction, 3 `in` lines.
  This is what supports "single card or lot basis."

## Entity overview

```
Item 1───∞ Lot 1───∞ LotConsumption ∞───1 TransactionLine ∞───1 Transaction
  │                                              │
  └──────────────── referenced by ──────────────┘
```

- A **PURCHASE** line *creates* a `Lot` for its item.
- A **SALE**/**TRADE-away** line *consumes* quantity from one or more existing
  lots (recorded as `LotConsumption` rows), realizing P/L against those lots'
  cost bases.
- A **TRADE** is a single transaction with both outgoing lines (consume lots)
  and incoming lines (create lots), plus optional cash boot on either side.

### Why item and lot are separate (worked example)

The split is what lets the **same card carry different costs**. Buy a
`Charizard X` at 80% for **$90**, then buy another for **$75**:

- **One item** — `Charizard X` (identity, links, one current market value).
- **Two lots** — lot A `unit_cost = $90`, lot B `unit_cost = $75`.

Cost lives on the **lot**, so the disparity is preserved; a later sale consumes
lot A first (FIFO) and realizes P/L against *its* $90 basis. **Each acquired
card is its own lot row** — the item row exists only to avoid re-entering
identity per copy and to hold a single market value per card type for
collection-value reporting. (A fully flat "one row per physical card" model was
considered and rejected: it duplicates identity and has no single place to
update market value.)

## Cost allocation (how incoming basis is set)

Cost basis is **inferred**, not typed. Every transaction is a bidirectional
exchange, so the basis of **incoming** cards is derived from the **net
consideration** you paid for them — the value you gave up, minus any cash you
got back:

```
net_consideration_in = cash_out + Σ(current market value of cards given out) − cash_in
cost_basis(incoming card) = net_consideration_in × (market_value(card) / Σ market_values of incoming cards)
```

> **Note (ADR 2026-07-13):** the value of cards *given out* is their **current
> `item.market_value_cents`** (× qty), not a separately-typed "agreed value" —
> the per-card agreed-value field was dropped; the deal derives it from the
> ledger. "Agreed value" wording below is retained historically; read it as
> current market value.

A **pure purchase** is the corner case where `cards out = 0` and `cash_in = 0`,
so `net_consideration_in = total_purchase_price` and this reduces exactly to the
originally-ratified rule (total price split by market value). Netting out
`cash_in` is what makes a trade-with-cash-back assign the right basis (worked
example in [docs/cost-basis-examples](../docs/cost-basis-examples.md)).

Rules (ratified 2026-07-10):

- **Market value is required on every incoming row** — submit is blocked until
  all are valued, guaranteeing a clean weighted split.
- **Rounding:** allocated costs are computed in integer cents and **must sum
  exactly to `net_consideration_in`**; any leftover cent(s) go to the
  highest-value card (largest-remainder on the largest line).
- The entered market value is stored twice: as the lot's
  `acquired_market_value_cents` (immutable snapshot for the split) and as the
  item's current `market_value_cents` (most-recent entry wins; drives unrealized
  P/L).
- `net_consideration_in` is the full landed cost; any shipping/fees to
  capitalize are folded into the cash/values entered (V1 has no separate fee
  field).

### Transaction type inference

The `type` is derived from the two sides, not chosen by the user:

| Cards out | Cards in | Inferred type |
|-----------|----------|---------------|
| no        | yes      | `purchase`    |
| yes       | no       | `sale`        |
| yes       | yes      | `trade`       |

(Cash may appear on either side in any case; `cash_delta` is the net.)

## Cost-basis method (disposal)

**Default: FIFO** (first-in, first-out) per item, with the design allowing
other methods later.

- Each item has an ordered set of open lots (by acquisition date, then id).
- A disposal consumes from the oldest open lot first, then the next, etc.
- Realized P/L for a disposal line =
  `proceeds − Σ(consumed_qty × lot_unit_cost)`.
- **Specific-lot selection** — shipped in ticket 71, as this section anticipated.
  An outgoing card may name a **cost group**: the copies of that item that share
  one `unit_cost_cents`. The disposal is then restricted to that group's lots and
  still consumes them **FIFO**, so acquisition order remains the tiebreak and no
  part of the algorithm becomes non-deterministic. The *group*, not the lot, is
  the unit of choice because that is the distinction the picker can show — two
  lots bought at the same price are indistinguishable to the user, so offering
  them separately would be a false choice. An outgoing card naming no group keeps
  plain across-all-lots FIFO, unchanged.

> Alternative methods (average cost, LIFO) are out of scope for V1 but the
> `LotConsumption` join table keeps the door open without schema changes.

## Valuation & P/L

**Profit is recorded at disposal, never at purchase.** A purchase converts cash
into inventory: it records **cash out** (`cash_delta < 0`) and establishes
**cost basis** on the new lots. At that instant profit on those cards is exactly
**$0** — you own them at cost. Profit is realized only when cards leave the
collection (sale/trade), as the difference between what you get and their basis.

### Two market values — do not conflate

The word "market value" appears at two levels doing two different jobs:

- **`lot.acquired_market_value_cents`** — the card's market value **at that
  specific acquisition**, frozen forever on the lot. This records "what the
  market was when I bought *this* copy," so two lots of the same card keep, say,
  $10 and $15 independently. Its only job is to allocate the price paid into
  **cost basis** at acquisition. It never changes and is **not** used for margin.
- **`item.market_value_cents` / `market_value_asof`** — the card type's
  **current** estimate (most-recent entry wins) and the date it was set. Its only
  job is **unrealized** valuation on cards *still on hand* (collection value,
  unrealized P/L). It is never used to compute margin on a sale.

Margin on a sale is measured against **cost basis** (what you actually paid),
which lives on the lot — not against either market-value field.

- **Cost basis (unrealized):** sum of `remaining_qty × unit_cost` over open lots.
- **Market value (unrealized):** computed **per lot** so intent can override it —
  `remaining_qty × item.market_value_cents` for `personal`/`resale` lots, but
  **forced to $0 for `ripping` lots** (their realizable value is nil). Market
  value is **manually entered** in V1 (no price feed).
- **Unrealized P/L:** `market value − cost basis` per lot. A ripping lot
  therefore always shows a loss equal to its full cost basis while it remains in
  inventory.
- **Realized P/L:** `proceeds − consumed cost basis`, accumulated from
  `lot_consumption` at disposal time; stored on the disposal line. Ripping is
  **not** a disposal — it never touches the realized line; its loss lives in
  unrealized/collection value.
- **Margin (on a disposal):** the reseller-facing name for realized P/L on the
  cards given up — `proceeds − cost basis`, stored per `out` line in
  `realized_pl_cents` (no new column). The **gross-margin %** =
  `realized_pl_cents / proceeds_cents` is **derived** for display (not stored,
  per the integer-cents rule). A transaction's total margin is the sum of its
  `out`-line realized P/L; a pure purchase has no margin (nothing disposed).

### Missing / stolen lots

A lot can be flagged **missing** (stolen or lost) — a reversible write-down set
from the Inventory grid (right-click → *mark as stolen*), not a transaction. Its
accounting mirrors **ripping**, the existing "in inventory but valued at $0"
pattern:

- A missing lot is **valued at $0 market** while **retaining its cost basis**, so
  its unrealized P/L = `0 − cost basis` — the lot shows a **loss equal to its
  purchase price**, and the portfolio's collection value / unrealized total drop
  accordingly. (This is the impairment view; a stricter *realized* write-off is a
  possible later refinement.)
- It is **never sellable** and **never consumed** by a disposal (like ripping),
  and is **excluded from on-hand counts** — the units are physically gone.
- The Inventory **Sold** column reads **"Missing"** for these lots.
- **Reversible:** clearing the flag restores the lot to normal valuation exactly.
  Because it moves money, each toggle writes an `audit_log` `edit` entry on the
  lot (before/after), honouring the append-only audit rule without a transaction.

### Feeding the Dashboard (Window 1) line graphs

- **Money-movement series** = running cumulative **net cash** (`Σ cash_delta`
  ordered by `occurred_at`). Purchases pull it down; sales/positive-boot push it
  up.
- **Realized-profit series** = running cumulative **realized P/L** (moves only on
  sales/trades).
- **Collection-value series** = total `market_value` of holdings over time
  (with cost basis as an optional comparison series).
- All three are filterable by intent (all / personal / resale / ripping).

## Money & quantities

- All money stored as **integer cents** (`INTEGER`), signed. UI formats to
  currency. Currency is USD in V1 (single `currency` field reserved for later).
- Quantities are **integers** (no fractional cards). A `unit` field on items
  distinguishes singles vs. sealed for reporting, but quantity is always whole.

## Schema (SQLite, V1)

```sql
-- Distinct sellable things
CREATE TABLE item (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL,       -- "Charizard VMAX"
    game               TEXT    NOT NULL DEFAULT 'pokemon',
    set_name           TEXT,                   -- "Champion's Path"
    set_code           TEXT,                   -- "CPA"
    card_number        TEXT,                   -- "60/107", "GG09/GG12"
    language           TEXT    NOT NULL DEFAULT 'EN', -- enum: EN|JP|CN|KO|FR|DE|IT|ES|PT|TH|ID|RU (added in 0002)
    category           TEXT    NOT NULL,       -- enum: single | sealed | accessory | other
    condition          TEXT,                   -- enum: NM | LP | MP | HP | DMG | sealed; NULL when graded
    holo_pattern       TEXT,                   -- enum foiling; part of identity (added in 0011)
    edition            TEXT,                   -- enum edition (1st/unlimited/…); part of identity (added in 0011)
    is_graded          INTEGER NOT NULL DEFAULT 0,  -- graded cards are identified by grade, not condition
    grading_company    TEXT,                   -- enum: PSA | BGS | CGC | SGC | null
    grade              TEXT,                   -- "10", "9.5"
    cert_number        TEXT,                   -- slab cert #
    location           TEXT,                   -- "Binder A", "Safe", "Box 3"
    tcgplayer_url      TEXT,                   -- reference link
    ebay_url           TEXT,                   -- reference link
    market_value_cents INTEGER,                -- current value (most-recent entry wins)
    market_value_asof  TEXT,                   -- ISO date of that value
    notes              TEXT,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);
CREATE INDEX idx_item_name ON item(name);
CREATE INDEX idx_item_set  ON item(set_name, card_number);

-- A batch of one item acquired at one cost
CREATE TABLE lot (
    id                  INTEGER PRIMARY KEY,
    item_id             INTEGER NOT NULL REFERENCES item(id),
    acquired_txn_line_id INTEGER REFERENCES transaction_line(id),
    acquired_at         TEXT    NOT NULL,      -- ISO date (drives FIFO order)
    quantity            INTEGER NOT NULL,      -- units originally acquired
    remaining_qty       INTEGER NOT NULL,      -- units still on hand
    unit_cost_cents     INTEGER NOT NULL,      -- inferred basis / unit (market-value-weighted split)
    acquired_market_value_cents INTEGER NOT NULL, -- per-unit market value at acquisition (allocation snapshot)
    intent              TEXT    NOT NULL,      -- enum: personal | resale | ripping (editable, no history in V1)
    missing             INTEGER NOT NULL DEFAULT 0, -- 1 = stolen/lost write-down, reversible (added in 0014)
    sku                 TEXT,                  -- auto-generated per-copy lookup SKU (added in 0003)
    source              TEXT,                  -- vendor/seller/friend
    notes               TEXT,                  -- acquisition notes (per this lot)
    created_at          TEXT    NOT NULL
);
CREATE INDEX idx_lot_item_open ON lot(item_id, remaining_qty);
CREATE INDEX idx_lot_sku ON lot(sku) WHERE sku IS NOT NULL;  -- non-unique (see note; 0016)

-- SKUs identify an acquired copy for quick lookup while selling/trading. Format:
--   {NAME3}-{SET}-{NUMBER}-{COND|GRADE}-{LANG}-{NNNN}   e.g. CHA-CPA-074-NM-EN-0001
-- The NNNN sequence counts prior copies of the same identity, so a NM copy
-- sequences independently from an LP copy. Auto-generated on acquisition (see
-- services/sku.py); one SKU per acquisition line, **shared across the split
-- lots** of that line (an indivisible multi-qty purchase splits into two lots).
-- The index is therefore **non-unique** — the original UNIQUE index was dropped
-- in 0016 to allow shared SKUs. (Migrated from the old manual item.sku in 0003.)

-- An inventory/value-affecting event
CREATE TABLE "transaction" (
    id            INTEGER PRIMARY KEY,
    type          TEXT    NOT NULL,            -- enum: purchase | sale | trade | adjustment
    occurred_at   TEXT    NOT NULL,            -- ISO datetime
    counterparty  TEXT,                        -- who
    reference     TEXT,                        -- order #, platform
    cash_delta_cents INTEGER NOT NULL DEFAULT 0,-- net cash in(+)/out(-) incl. boot (fees folded into cash/values)
    status        TEXT    NOT NULL DEFAULT 'posted', -- enum: posted | voided
    voided_at     TEXT,                        -- set when voided (soft, never hard-deleted)
    notes         TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX idx_txn_date ON "transaction"(occurred_at);

-- Per-item detail of a transaction
CREATE TABLE transaction_line (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES "transaction"(id) ON DELETE CASCADE,
    item_id        INTEGER NOT NULL REFERENCES item(id),
    direction      TEXT    NOT NULL,           -- enum: in | out
    quantity       INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,         -- agreed per-unit value (proceeds or cost)
    realized_pl_cents INTEGER,                 -- set for 'out' lines at post time
    notes          TEXT
);
CREATE INDEX idx_line_txn ON transaction_line(transaction_id);
CREATE INDEX idx_line_item ON transaction_line(item_id);

-- Which lots an 'out' line drew down (FIFO), for auditable basis
CREATE TABLE lot_consumption (
    id                  INTEGER PRIMARY KEY,
    transaction_line_id INTEGER NOT NULL REFERENCES transaction_line(id) ON DELETE CASCADE,
    lot_id              INTEGER NOT NULL REFERENCES lot(id),
    quantity            INTEGER NOT NULL,
    unit_cost_cents     INTEGER NOT NULL       -- copied from lot at time of disposal
);
CREATE INDEX idx_consumption_line ON lot_consumption(transaction_line_id);

-- Append-only audit trail of every create / edit / void
CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY,
    entity         TEXT    NOT NULL,           -- e.g. 'transaction'
    entity_id      INTEGER NOT NULL,
    action         TEXT    NOT NULL,           -- enum: create | edit | void
    at             TEXT    NOT NULL,           -- ISO datetime
    before_json    TEXT,                       -- snapshot prior to change (null on create)
    after_json     TEXT,                       -- snapshot after change (null on void)
    note           TEXT
);
CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);

CREATE TABLE schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL
);
```

## Operation rules

### Posting a transaction (the one unified algorithm)

Purchases, sales, and trades are all posted by the **same** procedure. The user
never picks a type — it is inferred (see
[type inference](#transaction-type-inference)). All steps run in **one DB
transaction**.

1. Create the `transaction` (`status='posted'`), recording `occurred_at`,
   counterparty, and the two sides.
2. **Outgoing side** — for each card given out (`direction=out`):
   consume open lots **FIFO** into `lot_consumption`, decrementing
   `remaining_qty` — narrowed to the card's **cost group** when it names one
   (see [cost-basis method](#cost-basis-method-disposal)); reject if quantity
   exceeds the on-hand units *that side of the narrowing*
   (`InsufficientQuantityError`). Realized P/L for the line =
   `agreed_value − Σ(consumed unit_cost)`.
3. **Net consideration in** = `cash_out + Σ(agreed value of cards out) − cash_in`.
4. **Incoming side** — for each card received (`direction=in`):
   **match-or-create** the `item` by identity key; refresh its
   `market_value_cents`/`_asof` and links; allocate `net_consideration_in` across
   incoming cards by market value (rounding residual → largest line); create a
   `transaction_line` and a `lot` with `unit_cost_cents` = allocated basis,
   `acquired_market_value_cents` = entered value, and per-card `intent`.
5. `cash_delta = cash_in − cash_out` (net; a single signed figure).
6. Write an `audit_log` `create` entry.

Reduces to the familiar cases:

- **Purchase** — only an incoming side; `net_consideration_in = cash_out`; each
  card's basis = its market-value share of the total paid.
- **Sale** — only an outgoing side; `cash_in` = proceeds; realized P/L per line
  = `proceeds share − basis`.
- **Trade** — both sides; incoming basis comes from the value of what you gave
  (cards + any cash); outgoing lines realize P/L against FIFO basis.

### Adjustment
- Manual correction (found cards, damage write-off, shrinkage). Creates a line
  (`in` or `out`) with optional basis; used sparingly and always noted.

### Void / Edit (correcting a misentry)
Transactions are **never hard-deleted** — with one sanctioned exception:
`TransactionService.delete_inventory_entry` (ADR 2026-07-11) hard-deletes a
mistaken **inventory entry** (an unsold acquisition lot and, if it empties, its
line/transaction) rather than soft-voiding, writing an `audit_log` `delete` with
a before-snapshot. This is the only path that removes rows; all normal
corrections use soft-void below.
- **Void:** atomically reverses *all* of a transaction's effects — restores
  `remaining_qty` on lots it consumed, deletes lots it created (only valid if
  those lots are still fully intact/unsold; otherwise the dependent transaction
  must be voided first), backs out `cash_delta` — then sets `status='voided'`
  and `voided_at`. The row stays visible in history. Writes an `audit_log`
  `void` entry with a before-snapshot.
- **Edit:** performed as **void + re-post** inside one DB transaction, so lot
  draw-downs and realized P/L are recomputed from scratch rather than patched.
  Writes an `audit_log` `edit` entry with before/after snapshots. The user is
  warned when an edit changes realized P/L.
- Every posted transaction also writes an `audit_log` `create` entry.

## Derived views (SQL views or service methods)

- `v_inventory` — per item: `remaining_qty`, `total_cost_basis_cents`,
  `market_value_cents`, `unrealized_pl_cents`, location.
- `v_realized_pl` — per period/item: sum of `realized_pl_cents`.
- Portfolio summary — totals across the collection for the dashboard.

## Invariants (must always hold)

1. `Σ lot.remaining_qty` for an item == units currently on hand for that item.
2. For any `out` line: `Σ lot_consumption.quantity == transaction_line.quantity`.
3. A lot's `remaining_qty` never goes negative and never exceeds `quantity`.
4. Realized P/L across all lines reconciles with lot cost drawn down.
5. Deleting/editing a posted transaction reverses its lot effects atomically
   (edit = reverse + re-post inside one transaction).
6. For any transaction, `Σ (incoming lot.unit_cost_cents × quantity)` ==
   `net_consideration_in` (`cash_out + Σ current market value of cards out −
   cash_in`), with the rounding residual absorbed on the largest-value incoming
   line. For a pure purchase this is just the total price paid. The **outgoing**
   side reconciles symmetrically: `Σ (out line.unit_price_cents × quantity) ==
   net_consideration_out`, with any indivisible-per-unit residual absorbed by
   splitting the out line (mirrors the incoming two-lot split).
7. Every lot has a non-null `intent` (`personal`, `resale`, or `ripping`) and a
   non-null `acquired_market_value_cents`.
8. `ripping` lots are never consumed by a disposal (they cannot appear on an
   `out` line) and are always valued at $0 market → unrealized loss = cost basis.
9. A lot flagged `missing` is excluded from on-hand quantity and is never
   sellable/consumed; it is valued at $0 market while keeping its cost basis
   (unrealized loss = cost basis). Clearing the flag restores it exactly.
