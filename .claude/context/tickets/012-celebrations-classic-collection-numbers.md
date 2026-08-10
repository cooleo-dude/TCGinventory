# 012 — Celebrations Classic Collection autofill has no price, image or link

```
status:   open
kind:     fix
source:   /task-planning 2026-08-10
issue:    #36
axis:     correctness
found:    2026-08-10
saving:   -
risk:     low — catalog cache is rebuilt wholesale by replace_for_language; no
          schema change, no money path, and every consumer already splits the
          collector number on "/"
```

## The finding

Autofilling **Dark Gyarados, Celebrations Classic Collection** in the Transaction
window fills a nonsense collector number, leaves the preview pane blank, never
fills Market, and leaves the TCGplayer field empty (so the saved item later
resolves to `search_url` junk rather than a product link).

One data-side root cause, not three UI bugs. The catalog row is:

```
cel25cc-CC005 | Dark Gyarados | number 'CC005' | printed_total 25 | image_small NULL
```

**TCGdex numbers this set with a synthetic `CC001…CC025` index.** Every other
source uses the printed original-set number — tcgcsv/TCGplayer say `8/82`,
pokemontcg.io says `8`. `number` is the join key for pricing, for the image
fallback, and for what the row displays, so all three symptoms fall out of it:

| symptom | mechanism |
|---|---|
| no price, no product URL | `variants_for_card(…, "CC005", …)` → `_norm_number` = `"cc005"`; nothing in tcgcsv group 2931 matches → `[]` → `TransactionView._on_price_ready` returns early and both fields stay empty |
| no image | TCGdex carries no `image` for any of the 25 cards; `_fill_missing_images` keys on `(name, set_name, number)` and pokemontcg.io's key is `(…, "8")` — it misses on the number alone |
| wrong number shown | `CardCatalogRepository.display_number` sees prefix `CC` and derives a subset denominator → **`CC005/CC25`** |

### Evidence

Reproduced live on 2026-08-10 (Windows, Python 3.12.10):

```
CardCatalogRepository.display_number(cel25cc-CC005) -> 'CC005/CC25'
CardCatalogRepository.image_for('Dark Gyarados', …, 'CC005/CC25') -> None
cardimages.art_for(…) -> None

TcgcsvProvider().price_for_card('Celebrations Classic Collection', 'CC005/CC25',
    'Dark Gyarados', set_code='CEL:CC') -> None
TcgcsvProvider().price_for_card('Celebrations Classic Collection', '8/82',
    'Dark Gyarados', set_code='CEL:CC')
    -> (465, 'https://www.tcgplayer.com/product/250294/…-dark-gyarados')
```

tcgcsv group 2931 lists all 25 products under their printed numbers (`8/82`,
`2/102`, `4/102`, `66/64`, `17/17`, …), so resolution works the moment the number
is right. The group itself resolves fine — this is purely the number.

**Scope is one set.** A sweep of every English set in `card_catalog` against the
pokemontcg.io dump (matching sets by normalized name, then comparing the numbers
behind shared card names) flags exactly one: `cel25cc`, 24 of 24 shared names
mismatched. Every other set agrees.

**It is a regression from the TCGdex migration.** Three Classic Collection items
already in the ledger were entered under the old pokemontcg.io catalog and priced
correctly then, because the numerator was the printed one:

| item | card_number | note |
|---|---|---|
| 52 — `______'s Pikachu` | `24/53` | correct |
| 917 — `Cleffa` | `20/25` | numerator right, denominator from `printed_total` |
| 918 — `Xerneas-EX` | `97/25` | numerator right, denominator from `printed_total` |

**It also costs price history.** `marketprices.py:147` joins the catalog-wide
daily sweep on the same `_norm_number(card["number"])`, so `pricehistory.db` holds
**0 of 25** Classic Collection cards against 24/25 for the parent Celebrations
set.

## Area of impact

| File | Line | What changes |
|---|---|---|
| `src/tcginventory/domain/models.py` | 195 | `full_number` returns `number` verbatim when it already carries a `/`, so `"8/82"` + `printed_total 25` does not become `"8/82/25"` |
| `src/tcginventory/services/catalog.py` | 83 | new `_CELEBRATIONS_CLASSIC_NUMBERS` table beside `_tcgdex_number`, keyed by TCGdex `localId` |
| `src/tcginventory/services/catalog.py` | 237 | `update_catalog` applies the override when `set_id == "cel25cc"` |
| `src/tcginventory/services/catalog.py` | 152 | `_fill_missing_images` keys on the number **head** (`"8/82"` → `"8"`) on both sides, so the pokemontcg.io art matches |
| `data: item 917, item 918` | — | one-off correction of the two stale ledger numbers to their printed form (`20/111`, `97/146`) |
| `tests/test_catalog.py` | — | ingest applies the override; a slashed-number card picks up its pokemontcg.io image |
| `tests/test_models.py` | — | `full_number` short-circuit |

**No migration.** `card_catalog` is a cache replaced wholesale by
`CardCatalogRepository.replace_for_language`; the schema does not change.

Verified *not* to need changes: `CardCatalogRepository.display_number` (prefix
loop breaks on the leading digit) and `image_for` (its `norm_num` splits on `/`),
`services/pricing.py` and `services/marketprices.py` (`_norm_number` splits on
`/`), `ui/views/_card_entry.py` and `ui/views/transaction.py`
(`_request_price` passes `card.number` raw), and `services/catalog_index.py`
(does not reference `number`, and `card_id` is unchanged — the recognition corpus
keeps its labels).

## Approach

**Chosen:** correct the data at ingest, storing the **full printed number**
(`"8/82"`) in `card_catalog.number`, from a static 25-entry table.

The app's `number` column means *the number printed on the card*. `CC005` is a
TCGdex internal index that appears on no card and in no other source, so it is
simply wrong data, and the fix belongs in one place rather than in three
consumers that each work around it.

Storing the full `NNN/TTT` form is already this catalog's convention: all 27,169
Japanese rows do it (they come from tcgcsv). Every consumer normalizes by
splitting on `/`.

### Rejected alternatives

- **Store the numerator only (`"8"`), leaving `printed_total` at 25.** Restores
  pricing and the image — this is what the old pokemontcg.io catalog effectively
  did — but the row then reads `8/25`, a denominator that appears on no card and
  that the user reads back against the physical card. Items 917/918 are exactly
  this failure preserved in the ledger.
- **A per-card denominator column.** General and correct, but a migration plus
  model plus repo change to serve one 25-card set frozen since 2021.
- **Derive the mapping from tcgcsv group 2931 at ingest.** Exactly correct and
  self-maintaining, but adds a tcgcsv round-trip and a fuzzy name join to the
  English catalog path for one historical special case, and fails offline. The
  set will never change; the join is done once, at authoring time.
- **Name-only fallback in `pricing.py` when a number matches no product in a
  resolved group.** Would have masked this generically — which is the argument
  against it. It turns a loud miss into a quiet guess, and `price_for_card`
  already commits to "better no link than a wrong one" in its docstring.
- **Special-case the row in `_card_entry.py`.** Violates `CARD-ENTRY-STANDARD`
  and fixes one screen out of the several that read this column.

## Plan

1. `domain/models.py` — `full_number` short-circuits on a number that already
   contains `/`. Test alongside.
2. `services/catalog.py` — add `_CELEBRATIONS_CLASSIC_NUMBERS`, keyed by TCGdex
   `localId`, with a comment recording why it exists. Author the 25 rows by
   joining tcgcsv group 2931's product numbers to the catalog names once, now —
   not at runtime.
3. `services/catalog.py` — `update_catalog` applies it for `set_id == "cel25cc"`.
4. `services/catalog.py` — `_fill_missing_images` joins on the number head.
5. `tests/test_catalog.py` — two tests against a fake fetch: a `cel25cc` payload
   ingests as `8/82`, and an image-less card with a slashed number picks up its
   pokemontcg.io image. Both must go red on revert.
6. Correct ledger items 917 and 918 to `20/111` and `97/146`.
7. Re-run *Update card catalog* from Operations against the real API.

## Verification

- `ruff format .` and `ruff check .` clean; full suite green under
  `QT_QPA_PLATFORM=offscreen`.
- After a real catalog update, `cel25cc-CC005` reads `number = '8/82'` with a
  non-null `image_small`.
- In the Transaction window, picking *Dark Gyarados* fills number `8/82`, shows
  art in the preview pane, fills Market at ~$4.65, and fills TCGplayer with
  product `250294`.
- `SELECT COUNT(*) FROM catalog_price_latest WHERE card_id LIKE 'cel25cc-%'`
  moves off 0 after the next daily sweep.
- Items 917 and 918 read `20/111` and `97/146`.

## Measured after

Live pull against the TCGdex API on 2026-08-10 — 218 sets, 23,444 English cards,
0 errors. All 25 Classic Collection rows now carry their printed number, and
**24 of 25 gained art** (up from 0):

```
cel25cc-CC005    8/82      art     Dark Gyarados
  number          8/82
  display_number  8/82
  image_small     https://images.pokemontcg.io/cel25c/8_A.png
  art_for         ('cel25cc-CC005', 'https://images.pokemontcg.io/cel25c/8_A.png')
  variants        [CardVariant(label='Base', price_cents=465,
                    tcgplayer_url='…/product/250294/…-dark-gyarados', is_base=True)]
```

English cards with no art: **781, down from 805** — the 24 recovered are this set.

Ledger corrected: item 917 `20/25 → 20/111`, item 918 `97/25 → 97/146`.

Each of the three code changes was reverted individually and the suite re-run, to
confirm the new tests go red rather than merely pass:

| reverted | fails |
|---|---|
| `_printed_number` override | `…stores_the_printed_collector_number`, `…art_joins_on_the_printed_numerator` |
| `_norm_join_number(card.number)` lookup key | `…art_joins_on_the_printed_numerator` |
| `full_number` short-circuit | `test_full_number_combines_number_with_set_total`, `…stores_the_printed_collector_number` |

Note: the matching `_norm_join_number` on the *index* side of the join is inert
today (pokemontcg.io numbers are bare, never denominated) and no test pins it. It
is kept because a join key computed two different ways on the two sides is a
latent bug, not because it currently does work.

## Known remainder (not this ticket)

`Gardevoir ex` (CC014) is the one card still art-less: pokemontcg.io names it
`Gardevoir ex δ`, so the join fails on the **name**, not the number. Different
root cause, affects a handful of cards catalog-wide (9 art-less English cards
have a non-ASCII name), and the card gets its price and product link from this
fix regardless — `cardimages.art_for` will supply its art from the TCGplayer
product photo once a ledger item exists.
