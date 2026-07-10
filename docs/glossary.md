# Glossary

Terms used across the app and specs.

- **Item** — a distinct sellable thing: a specific card in a specific
  condition/grade, or a specific sealed product. The same card raw vs. graded is
  two different items.
- **Lot (acquisition lot)** — a batch of units of one item that entered
  inventory in one event at one unit cost. Cost basis lives on the lot.
- **Cost basis** — what a unit cost you, inferred at acquisition by splitting
  the total you paid across cards by their relative market value (fees/shipping
  are folded into that total). Used to compute profit when you dispose of it.
- **Proceeds** — what you received for a unit when selling/trading it, net of
  selling fees.
- **Realized P/L** — profit/loss locked in when you dispose of an item:
  `proceeds − cost basis`.
- **Unrealized P/L** — paper profit/loss on items still on hand:
  `market value − cost basis`.
- **FIFO (first-in, first-out)** — default rule for choosing which lot a sale
  draws from: the oldest lot goes first.
- **Boot** — cash added to one side of a trade to balance unequal values.
- **Fees** — shipping and marketplace/processing costs. V1 has no separate fee
  field: fold them into the total you paid (raises cost basis) or the cash you
  received (lowers proceeds).
- **Net consideration in** — the value you effectively paid for incoming cards:
  `cash paid out + value of cards you gave − cash you got back`. This is what
  gets split across incoming cards (by market value) to set their cost basis.
- **Adjustment** — a manual inventory correction (found item, damage
  write-off, shrinkage) that isn't a purchase/sale/trade.
- **Grading** — third-party authentication/condition grade (PSA, BGS, CGC,
  SGC) with a numeric grade and a cert number identifying the slab.
- **Condition** — for ungraded cards: NM (near mint), LP (lightly played),
  MP (moderately played), HP (heavily played), DMG (damaged).
- **Card number** — the collector number printed on the card (e.g. `60/107`,
  `GG09/GG12`). Part of item identity.
- **SKU** — an auto-generated per-**copy** lookup code on the lot, formatted
  `{name}-{set}-{number}-{cond/grade}-{lang}-{seq}` (e.g. `CHA-CPA-074-NM-EN-0001`).
  The sequence counts prior copies of the same identity, so NM and LP copies
  sequence separately. Used to quickly find a card when selling/trading.
- **Language** — the card's printed language (EN, JP, CN, KO, FR, DE, IT, ES, PT,
  TH, ID, RU); defaults to EN. Part of item identity, so the same card in a
  different language is a distinct item.
- **Sealed product** — unopened product (booster boxes, ETBs, packs) tracked as
  items with `category = sealed`.
- **Intent** — why a card is held: **personal** (keeper), **resale** (business),
  or **ripping**. Set per acquisition, editable later.
- **Ripping** — sealed product bought to be opened, never sold. Assumed opened no
  matter what, so it's valued at $0 and shows in inventory as a loss equal to its
  cost basis. Cards pulled from opening are entered separately as an ordinary
  acquisition (no link to the ripped item).
- **Market value (current)** — `item.market_value_cents` + `market_value_asof`:
  a manually maintained estimate of what a card type is worth **now** (most-recent
  entry wins), with the date it was set. Used only for **unrealized** P/L and
  collection value on cards still on hand. No automatic price feed in V1.
- **Market value at acquisition** — `lot.acquired_market_value_cents`: the card's
  market value **when that copy was acquired**, frozen on the lot. Used only to
  split the price paid into cost basis; two lots of the same card can differ
  (e.g. $10 vs $15). Never used for margin.
- **Margin** — realized profit on cards you disposed of: `proceeds − cost basis`
  (stored as `realized_pl_cents`). Shown as dollars plus a **gross-margin %**
  (`margin ÷ proceeds`). Measured against what you *paid*, not market value.
