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
- **Sealed product** — unopened product (booster boxes, ETBs, packs) tracked as
  items with `category = sealed`.
- **Intent** — why a card is held: **personal** (keeper), **resale** (business),
  or **ripping**. Set per acquisition, editable later.
- **Ripping** — sealed product bought to be opened, never sold. Assumed opened no
  matter what, so it's valued at $0 and shows in inventory as a loss equal to its
  cost basis. Cards pulled from opening are entered separately as an ordinary
  acquisition (no link to the ripped item).
- **Market value** — a manually maintained estimate of what an item is worth
  now; used for unrealized P/L (no automatic price feed in V1).
