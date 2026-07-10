# 04 — UI / UX

PySide6 (Qt Widgets). The app is organized around **three primary windows**,
reachable from a persistent navigation rail / toolbar. Data-heavy views use Qt's
Model/View so sorting, filtering, and large datasets stay fast.

## Window map

| # | Window | Purpose | Status |
|---|--------|---------|--------|
| 1 | **Dashboard** (main) | At-a-glance charts of money movement & collection value | V1 (charts); calendar deferred |
| 2 | **Transaction** | One bidirectional screen for every purchase / sale / trade: cards/cash out ⇄ cards/cash in | V1 |
| 3 | **Inventory** | Full collection with filters | V1 |

Plus a full **Transaction log / History** view (V1, `F-VW-3`) reachable from the
nav, and an **Item detail** view reached by drilling into a card.

```
┌───────────────────────────────────────────────────────────────┐
│  TCGinventory                                    [🔍 search…]   │
├──────────────┬────────────────────────────────────────────────┤
│  ▸ Dashboard │                                                  │
│  ▸ Transaction│  (active window)                                │
│  ▸ Inventory │                                                  │
│  ▸ History   │                                                  │
├──────────────┴────────────────────────────────────────────────┤
│  status bar: DB path · item count · last backup                 │
└───────────────────────────────────────────────────────────────┘
```

## Window 1 — Dashboard (main)

The landing window. V1 content:
- **Line graph — profits / money movement over time.** Cumulative realized P/L
  and net cash flow across purchases, sales, and trades.
- **Line graph — raw collection value over time.** Total market value of
  holdings (cost basis shown as a comparison series).
- Both charts should be filterable by intent (all / personal / resale /
  ripping) so business vs. collection vs. ripping performance can be read
  separately.

**Deferred (post-V1) on this window:**
- Calendar-style widget for upcoming **shows & auctions**.
- Additional summary tiles/widgets (units on hand, unrealized P/L, etc.).

## Window 2 — Transaction (unified) (`F-PU-*`, `F-SA-*`, `F-TR-*`)

**One** bidirectional screen records every purchase, sale, and trade — because
each is the same thing: value/cards out on the left, value/cards in on the
right. The user never chooses a type; it is inferred from which sides carry
cards (see Spec 02).

```
┌───────────── YOU GIVE (out) ──────────────┬───────────── YOU RECEIVE (in) ──────────────┐
│  Pick cards from your collection:         │  Card rows (`+ add row`):                   │
│   • item picker (in-stock only)           │   • Name, Set, Card # (60/107, GG09/GG12),  │
│   • qty (capped at on-hand)               │     Language (EN/JP/CN/…),                  │
│   • agreed value each                     │     TCGplayer / eBay links,                 │
│  + your cash out:  $[____]                │     Market Value (required),                │
│                                           │     Condition, Grade (+company/#/cert),     │
│                                           │     Notes, intent (Pers/Resale/Rip)         │
│                                           │  + their cash in:  $[____]                  │
├───────────────────────────────────────────┴─────────────────────────────────────────────┤
│  Balance footer: consideration given · value in · net cash · est. realized P/L (FIFO)   │
│  [ Submit ]                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Header:** date, counterparty (optional).
- **Left (out):** cards selected from the collection (qty capped at on-hand,
  oversell blocked inline) each with an agreed value, plus optional cash you add.
  **Ripping-intent lots are excluded** from the picker — they are never sold.
- **Right (in):** incoming card rows with the full field set — Name, Set,
  **Card #** (e.g. `60/107`, `GG09/GG12`), **Language** (EN/JP/CN/…), links,
  Market Value, Condition, Grade, Notes — including a per-row **intent (Personal
  / Resale / Ripping)**, plus optional cash the other party provides. Card # and
  Language are part of item identity, so they steer match-or-create. Market value
  **required** on every incoming row.
- **Corner cases the one screen covers:**
  - *Purchase* — left side is cash only (your cash out), right side has cards.
  - *Sale* — left side has cards, right side is cash only (their cash in).
  - *Trade* — cards on both sides, optional cash boot either way.
- **Trade summary (live overview):** a summary panel updates as the transaction
  is entered, showing **value given · value received · net cash · cost basis
  drawn (FIFO) · margin**. Margin is the realized P/L on cards given up, shown as
  **profit in dollars and a gross-margin %** (`margin ÷ proceeds`), colored
  green/red (with a non-color cue). A pure purchase shows no margin (nothing
  disposed). This is the at-a-glance "did this deal make money" readout.
- **Submit → success screen** showing the **inferred cost of each incoming card**
  (consideration-given split by market value; residual cent to the largest line)
  and the final **margin** (profit $ + gross-margin %) on cards given up.
- **On submit** (one atomic DB transaction): consumes outgoing lots FIFO
  (realizing P/L against agreed values), match-or-creates incoming items, writes
  one lot per incoming card, records net cash, and logs an audit entry.

## Window 3 — Inventory (`F-VW-1`, `F-VW-5`, `F-VW-9`)

Full collection view. Columns: Name · Set · # · Lang · Cond/Grade · Intent · Qty ·
Avg cost · Cost basis · Market · Unrealized P/L · Sold status · Location.
Features:
- **Every column is both sortable and filterable** (`F-VW-9`) — including sold
  status (unsold / partially / fully), intent, and value ranges (cost, market,
  P/L). Filter bar exposes the common ones (intent, game, set, category,
  condition, graded, location, in-stock/sold); any column header also sorts.
- Quick text search filters live.
- Double-click a row → **Item detail**.
- Right-click → Sell/Trade this card · Edit item.
- Positive P/L green, negative red.

## Supporting view — Item detail

Header with item attributes + current position (qty, basis, market, P/L). Tabs:
- **Lots** — open lots with acquired date, qty, remaining, unit cost, intent,
  market-value-at-acquisition, source. Intent is **editable** here
  (Personal / Resale / Ripping).
- **History** — every transaction line touching this item, with running qty.
- Actions: Edit item, Sell/Trade, Set market value.

## Transaction log / History (`F-VW-3`) — full V1 view

Chronological table: date, type (color-coded), counterparty, items summary, cash
delta, realized P/L, status (posted/voided). Filters: type, date range,
counterparty text. Click → **transaction detail** (all `in`/`out` lines + the
lot consumption behind each disposal). Actions: **Void** and **Edit**
(reverse + re-post, per Spec 02). Voided rows remain visible, greyed. This is
the surface from which misentries are corrected, so it ships in V1.

## Interaction principles

- **Keyboard-first data entry:** Tab across a row's fields, **Enter adds a new
  row**, Ctrl+S submits. The Transaction window and its rows are fully operable
  without the mouse.
- **Live type inference feedback:** the Transaction window shows a badge
  reflecting what it will record — **PURCHASE / SALE / TRADE** — updating as
  cards/cash appear on each side, so the user always knows the outcome before
  submitting.
- **Live totals footer:** consideration given, value received, net cash, and an
  **estimated realized P/L** (FIFO preview) recompute as rows change.
- **Validation before commit** (screens never write partial state; the service
  call is atomic):
  - **Market value required** on every incoming row — submit disabled until all
    are valued.
  - **Oversell blocked inline** — outgoing quantity is capped at units on hand
    with a live remaining indicator; over-quantity can't be submitted.
  - Errors shown inline next to the offending field, not in a modal after the
    fact.
- **Success screen** after submit shows the **inferred per-card cost** for
  incoming cards and any **realized P/L** on cards given up, then returns to a
  fresh Transaction (a toast confirms and links to the created entry in History).
- **Non-destructive corrections:** Void asks for confirmation; Edit reverses and
  re-posts atomically and **warns when it changes realized P/L**. Voided rows
  stay visible (greyed) in History.
- **Money formatting:** always 2-dp currency from integer cents; never show raw
  cents. Positive P/L green, negative red (with a non-color cue for
  accessibility).
- **Empty states:** each view guides the first action (e.g. Inventory: "Nothing
  here yet — record your first purchase").

## Theming & polish (light for V1)

- Follow the OS light/dark theme via Qt defaults; no custom skin in V1.
- Consistent iconography and color for the inferred transaction type
  (purchase/sale/trade) and for intent (personal/resale/ripping).
- Non-blocking toasts / status-bar messages for successful saves and backups.

## Accessibility & platform

- Full keyboard navigation and visible focus; color is never the only signal
  (icons/labels accompany P/L and status colors).
- Native menus and file dialogs; consistent look on macOS and Windows via Qt.

## Deferred UI (LATER)

- Dashboard **calendar for shows & auctions**; extra dashboard tiles.
- Image thumbnails per item, barcode scan entry, price-feed refresh button,
  bulk edit, saved filter presets, printable reports.
