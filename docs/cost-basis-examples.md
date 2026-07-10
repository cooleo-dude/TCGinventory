# Cost-Basis Worked Examples

Concrete walk-throughs of how the app infers cost basis and computes P/L. All
money shown in dollars; the app stores integer cents. Cost basis is **inferred
by market-value-weighted allocation** at acquisition; disposals draw down lots
**FIFO**.

Core formula (see [Spec 02](../specs/02-data-model.md#cost-allocation-how-incoming-basis-is-set)):

```
net_consideration_in = cash_out + Σ(agreed value of cards out) − cash_in
cost_basis(incoming card) = net_consideration_in × (market_value(card) / Σ market_values in)
```

---

## 1. Multi-card acquisition (market-value split)

Buy a lot of **3 different cards for $80 total**. Entered market values:

| Card | Market value | Share | Inferred cost |
|------|-------------:|------:|--------------:|
| A    | $60          | 60%   | **$48.00**    |
| B    | $30          | 30%   | **$24.00**    |
| C    | $10          | 10%   | **$8.00**     |
| **Σ**| **$100**     | 100%  | **$80.00**    |

`net_consideration_in = $80` (pure purchase: no cards out, no cash in). Each
card's basis is its market-value share of the $80 paid — i.e. you got the lot at
80% of market, spread proportionally. Three lots created (one per card), each
with its own intent; `cash_delta = −$80`. **No profit is recorded** — you own
the cards at cost.

---

## 2. Same card, two costs, then a FIFO sale

- Buy `Charizard X` for **$90** → lot A (basis $90).
- Later buy another `Charizard X` for **$75** → lot B (basis $75).

Both lots point to **one item**; cost lives on the **lot**, so the two prices
coexist. Now sell one copy for **$120**:

- FIFO consumes **lot A** first (basis $90).
- **Realized P/L = $120 − $90 = $30.**
- Remaining on hand: lot B, basis $75.

This is why item and lot are separate — a single "item row" could never hold two
different costs.

---

## 3. Rounding residual

Acquire **3 cards for $100 total**; market values **$12 / $11 / $10** (Σ $33).

| Card | Raw split | Floored | Final |
|------|----------:|--------:|------:|
| $12  | 3636.36¢  | 3636¢   | **3637¢** |
| $11  | 3333.33¢  | 3333¢   | 3333¢ |
| $10  | 3030.30¢  | 3030¢   | 3030¢ |
| **Σ**| 10000¢    | 9999¢   | **10000¢** |

Flooring leaves a **1¢ residual**, assigned to the **highest-value card** → the
allocated costs sum to **exactly $100**. Totals always reconcile.

---

## 4. Trade with cash back

Give **Card A** (basis **$20**, agreed value **$50**); receive **Card B**
(market **$40**) **+ $10 cash**.

- `net_consideration_in = cash_out($0) + cards_out($50) − cash_in($10) = $40`
- **Card B basis = $40** (you effectively paid $40 for it — gave $50 of card,
  got $10 back).
- **Realized P/L on A = $50 − $20 = $30.**
- `cash_delta = +$10`; **type inferred = trade** (cards on both sides).

Balance check: value given ($50 card) = value received ($40 card + $10 cash). ✓

> If you had instead *paid* $10 boot (cash out, no cash in),
> `net_consideration_in = $50 + $10 = $60` and Card B's basis would be $60.

---

## 5. Oversell guard

Position: 3 units on hand. Attempt to sell 5.

- Service raises `InsufficientQuantityError`; **nothing is written** (the whole
  post is atomic). The UI blocks submit and shows the on-hand quantity inline.

---

## Invariants these examples preserve

1. Sum of lot `remaining_qty` == units on hand.
2. Each `out` line's consumed quantity == the line quantity.
3. `Σ (incoming lot cost)` == `net_consideration_in` (rounding residual on the
   largest line).
4. Realized P/L reconciles with basis drawn from lots.
5. Cash deltas across transactions reconcile with actual cash movement.
