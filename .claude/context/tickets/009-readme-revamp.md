# 009 — Rewrite the README to describe the app that actually exists

```
status:   open
kind:     feature
source:   /task-planning 2026-08-07
issue:    #22
axis:     maintainability
found:    2026-08-07
saving:   -
risk:     low — documentation and a dev-only capture script; no runtime code, no migration
```

## The finding

`README.md` is 27 lines and has not been touched since before M0. It states:

- `README.md:7` — *"**Status:** Planning. The implementation hasn't started yet —
  the baseline plan is being established first."*
- `README.md:26` — *"then begin **M0 — project scaffolding**."*
- `README.md:3` — *"tracking a **personal** inventory"* — the app is explicitly
  personal **and** business; `intent` is per-lot (`personal | resale | ripping`).
- `README.md:13` — *"**V1:** items, purchases/sales/trades, FIFO cost basis &
  P/L, search/filter, CSV import/export."* — that is M0–M6 and stops there.

What actually exists, per `.claude/context/app-map.json` (validated: *map ok — 11
windows, every path resolves, nav order matches*) and `CHANGELOG.md`:

- **v0.7.0, 319 commits**, V1 feature-complete (M0–M6), plus live post-V1 work.
- **Eleven nav destinations** in `main_window.NAV_ITEMS`: Dashboard, Transaction,
  Inventory, History, Calendar, Price Lookup, eBay Auctions, Recognition, Market,
  Models, Operations.
- **Four SQLite files**, only one of which is the ledger: `inventory.db`
  (migrations `0001..0019`), `vision.db`, `auctions.db`, `pricehistory.db`.
- Subsystems the README does not mention at all: the TCGdex catalog + daily
  price-history sweep, card-show scraping and a calendar, an eBay max-bid
  planner, market signals (liquidity / markup / volatility), and the
  recognition/vision engine of `specs/06-recognition.md`.

The README also omits the two rules that define the product: **cost basis is
inferred** by a market-value-weighted split of `net_consideration_in`, and
**disposals draw down FIFO**. Someone landing on the repo cold cannot learn from
the README what the app is, and the one factual claim it leads with is false.

There are **no screenshots anywhere in the repo** — `grep -ril screenshot` hits
only `CLAUDE.md`, `specs/06-recognition.md` and three view modules (all about
pasting images *into* the app, not capturing it).

## Area of impact

| File | Line | What changes |
|---|---|---|
| `README.md` | 1–27 | full rewrite |
| `docs/screenshots/*.png` | — | new: one capture per nav destination (~2–4 MB total) |
| `tools/capture_screenshots.py` | — | new: offscreen capture script so the shots are regenerable |
| `.claude/context/app-map.json` | `docs` | optional: add `README.md` so future dossiers know it exists |
| `.gitignore` | — | no change needed — `*.db` already ignored, PNGs are not |
| `docs/README.md` | — | no change — it already indexes `/docs` correctly |
| tests | — | none, except `tests/test_app_map.py` must still pass if the map gains an entry |

**No migration.** No service, repository, view, Qt model or delegate is touched.

## Approach

**Chosen (as revised during the work):** rewrite the README, with **four**
representative images under `docs/screenshots/` — a cropped portfolio chart as
the lead, then Transaction, Inventory and History. The eleven screens are
described in a prose table instead of one screenshot each.

The original plan was a screenshot per screen, produced by a committed capture
script. That was cut on the user's instruction mid-task: *"The only imagery in
the README.md should be simple and minimalistic in nature."* The capture harness
went with it — it had grown to ~300 lines (staging a copy of the databases,
junction-linking the 2.1 GB image cache, driving views into populated states) for
a one-off documentation task. The images were taken with it before it was
removed; regenerating them is a manual re-run, which is the right trade for how
rarely a README screenshot changes.

The capture script copies the four DBs out of `%APPDATA%\TCGinventory` into a
temp dir and points `TCGINVENTORY_DATA_DIR` at the copy, so the real ledger is
never opened for writing. It iterates `NAV_ITEMS` (never literal indexes, per the
`main_window` contract) and `grab()`s each page.

Rationale for the real-DB copy: the repo is **private** and single-user, so the
figures on screen are the owner's own. `data/seed.py` posts a handful of
transactions through the service layer, which covers Dashboard, Inventory,
History and Transaction — but it carries no catalog, no `pricehistory.db`, no
scraped shows, no watchlist and no vision corpus, so **six of the eleven screens
would render as empty states**. Screenshots of empty states are the opposite of
showing what the app is.

### Rejected alternatives

- **`TCGINVENTORY_SEED=1` demo data** — reproducible by anyone, but leaves Price
  Lookup, Calendar, eBay Auctions, Recognition, Market and Models blank. Making
  the seed rich enough would mean fabricating catalog and price-history rows in
  `data/seed.py`; that is a real chunk of work and a separate ticket, not part of
  a README rewrite.
- **Screenshots hosted externally / linked from a release** — the README should
  render offline and from a fresh clone. A private repo cannot serve images to an
  unauthenticated CDN anyway.
- **Manual screenshots taken by hand** — quicker once, unrepeatable forever. The
  shots will drift as the theme and views change; a committed script means
  regenerating them is one command.
- **Restating the milestone table and the invariants in the README** — that is
  exactly how the current README went stale. `CLAUDE.md` owns milestone status,
  `specs/02-data-model.md` owns the invariants; the README links to them.
- **All eleven screenshots at full width** — makes a README nobody scrolls to the
  end of. Four featured (Dashboard, Transaction, Inventory, History), the rest
  grouped smaller.

## Plan

1. Write `tools/capture_screenshots.py` — copy the four DBs to a temp dir, set
   `TCGINVENTORY_DATA_DIR` + `QT_QPA_PLATFORM=offscreen`, build `QApplication` +
   `MainWindow(conn)`, iterate `NAV_ITEMS`, `grab()` each to
   `docs/screenshots/<key>.png`. Must never write to the real data dir and must
   set no styling of its own — the shots have to show `theme.active()` as the app
   resolves it (Ember Duo dark).
2. Run it; inspect all eleven. QtCharts needs a paint cycle and card art arrives
   via `thumbcache`, so any view that renders blank or half-drawn gets a targeted
   settle rather than shipping broken.
3. Rewrite `README.md`: what it is → screenshot tour (4 featured + the rest
   grouped) → *How the money works* (inferred basis, FIFO, per-lot intent
   including ripping-at-$0, never-hard-delete + `audit_log`) → *Under the hood*
   (Qt-free service layer, integer cents, the four databases and why only
   `inventory.db` is the ledger) → *Install & run* (venv, `python -m
   tcginventory`, env overrides, pytest/ruff, PyInstaller) → *Where to read more*.
4. Status line only — "v0.7.0 · V1 feature-complete (M0–M6)" pointing at
   `CHANGELOG.md` and `specs/05-roadmap.md`. No milestone table.
5. Optionally add `README.md` to the map's `docs` section; re-run
   `plan.py validate` and `tests/test_app_map.py`.
6. `ruff format .` / `ruff check .` (the capture script is Python), full suite
   offscreen, branch + PR with `Closes #N`.

## Verification

- `python tools/capture_screenshots.py` regenerates all eleven PNGs from a clean
  checkout without touching `%APPDATA%\TCGinventory` (verify mtimes on the real
  DBs are unchanged).
- Every screenshot referenced by the README resolves — no broken image links.
- Every factual claim in the README traces to the app map, `CHANGELOG.md` or
  `specs/`; specifically the cost-basis and FIFO wording matches
  `specs/02-data-model.md`.
- `ruff check .` clean; `pytest -q` green with `QT_QPA_PLATFORM=offscreen`.
- `plan.py validate` still reports *map ok*.

## Notes

**Dashboard hero renders blank — needs its own ticket.** While capturing the
dashboard, the hero "Collection Value" card painted only its delta chips and
sparkline: no title, no figure. A probe confirmed the widget is `isVisible()`,
sits at `QRect(19, 34, 821, 56)`, and holds the right text (`'$15,031.36'`) — it
simply does not paint. Reproduced at three window sizes, maximized, under both
the offscreen platform and a real composited screen capture. The owner reports it
renders correctly on their machine, so it may be specific to constructing
`MainWindow` directly rather than through `app.main()`. That is why the dashboard
image here is cropped to the portfolio chart.

Found while writing this ticket, out of scope here: `.claude/context/app-map.json`
`workflow.branch` still reads *"Commit directly to main. No PR / feature branch
by default"*, which contradicts the 2026-08-07 move to branch + PR. The validator
does not check that field. Worth fixing deliberately.
