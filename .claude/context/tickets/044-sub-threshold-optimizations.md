# 44# — Two measured optimizations that are real and too small to ship

```
status:   deferred
kind:     optimization
source:   /optimization-sweep research pass, 2026-08-07
mr:       44
axis:     latency
found:    2026-08-07
saving:   ~2.5 ms total, in real use
risk:     low — both are local, mechanical and reversible
```

## The finding

The research pass measured both of these as **genuinely better mechanisms** and
both as **not worth shipping**. They are recorded here so the next sweep does not
spend its budget rediscovering them, and so the decision to skip them is on
record rather than implied.

### 1. `sqlite3.Row` → tuple unpacking in the collection series

`services/reporting.py:531-535` loads all 23,583 `price_snapshot` rows into two
dicts using `sqlite3.Row` indexing. Plain tuple unpacking over the same cursor:

| | |
|---|---:|
| as shipped (`Row` objects) | 17.32 ms |
| plain tuple unpacking | **14.80 ms** |
| **saving** | **2.5 ms** |

On a Dashboard refresh that now costs ~113 ms. Verified identical output.

Also tested and rejected: filtering the query to items that have lots saves
nothing — **all 903 items have lots**, so it drops zero rows and adds a subquery.

### 2. Module-level `re.compile` in pricing

`services/pricing.py:47` (`_norm`) and five other sites call `re.sub` with a
literal pattern inside loops over products and catalog keys. A module-level
compiled pattern:

| over 53,289 real catalog names | |
|---|---:|
| `re.sub` with a literal | 23.95 ms |
| module-level compiled | **14.21 ms** |
| **41% faster** | 0.183 µs per call |

Identical output. But `_norm`'s real call volume in a pricing lookup is
*hundreds*, not 53,289 — so the actual saving is around **0.02 ms**. The 41%
headline is true and irrelevant.

## Why deferred rather than open

Neither clears the bar the sweep sets for itself: *no edit without a measurable
user-visible improvement*. 2.5 ms on a 113 ms refresh is inside run-to-run
variance; 0.02 ms is not a number anyone can perceive. Shipping them would spend
review attention and diff surface to buy nothing.

They are not wrong, and they are not dead ends — that distinction is why they are
`deferred` here rather than in `declined.md` with the measured rejections.

**Reopen if:** either code path is being touched anyway for another reason, in
which case take the improvement as a rider rather than as its own change; or the
call volumes grow by an order of magnitude.

## Also recorded here: one permanent `none`

`repositories.latest_prices()` is already **2.11 ms** on a `SCAN … USING COVERING
INDEX`. A `ROW_NUMBER() OVER (PARTITION BY …)` rewrite — the obvious
modernisation — measured **15.10 ms**, seven times slower, with an extra temp
B-tree in the plan. Do not "improve" it. This one is a dead end rather than a
deferral, and it is in `declined.md` too.

## Notes

- Both numbers came from spikes against a staged copy of the real database, not
  from estimates.
- The point of filing them is the same as the point of `declined.md`: the
  expensive part of a sweep is rediscovering what a previous sweep already
  measured.

## Filed before the numbering change

This was issue #14 under the old scheme, where a ticket carried three unrelated numbers (ours, the issue's, the PR's). It now carries one — the merge request's — so `X#` and `X!` are the same work. The old issue is closed and points here; nothing about the finding below changed.
