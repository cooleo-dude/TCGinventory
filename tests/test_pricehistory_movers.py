"""Catalog-wide movement ranking, and the guards that make it mean anything.

`movers_since` is the first aggregate over `pricehistory.db` — everything else there
reads one card at a time. Ranking is by **percent**, which is the only sane basis
across 49,709 cards of wildly different value, and percent is also what makes the
query hypersensitive to bad data. Each guard here exists because measuring the real
file showed the ranking was reporting an artifact, not a market:

* the **baseline fallback**: catalog history reaches back ~26 days, so every range
  pill asks for a cutoff older than the data. Without falling back to a card's
  earliest snapshot the query returned **nothing at all**, at every range.
* the **floor on both endpoints**: flooring only the current price put a card that
  went 26c to $1283.67 at the top, at +493,619%.
* the **step-ratio guard**: the top of the ranking was entirely cards whose sweep
  mapping changed mid-series — `ex7-36` reading 271.64, 294.97, then 6.34 and flat
  after. One step, then it holds, so a least-squares slope would not have caught it
  either. 52 cards moved that way on 2026-07-28 and 52 more on 2026-08-04.
"""

from __future__ import annotations

from tcginventory.data.pricehistory import PriceHistoryRepository, open_history


def _repo(series: dict[str, list[tuple[str, int]]]) -> PriceHistoryRepository:
    """A history built from `{card_id: [(as_of, cents), …]}`, oldest first.

    Written through `record`, so the dedup-on-change behaviour is the real one: a
    repeated price writes no snapshot, which is exactly the shape `min_points` has to
    reason about.
    """
    repo = PriceHistoryRepository(open_history(":memory:"))
    for card_id, points in series.items():
        for as_of, cents in points:
            repo.record(card_id, cents, as_of)
    return repo


def _by_id(rows):
    return {r[0]: r for r in rows}


def test_ranks_by_percent_not_by_absolute_cents():
    """The whole reason global cannot reuse the owned path's ranking. In cents the
    expensive card wins by 20x; in percent the cheap one has actually moved more."""
    repo = _repo(
        {
            "cheap": [("2026-07-01", 200), ("2026-08-01", 600)],  # +400%, +$4
            "dear": [("2026-07-01", 40_000), ("2026-08-01", 48_000)],  # +20%, +$80
        }
    )

    rows = repo.movers_since("2026-07-01", floor_cents=0)

    assert [r[0] for r in rows] == ["cheap", "dear"], rows
    assert _by_id(rows)["cheap"][3] == 200.0  # 200 -> 600 over the window
    assert _by_id(rows)["dear"][2] == 8_000, "change is still integer cents"


def test_a_cutoff_older_than_all_history_falls_back_to_the_earliest_point():
    """The bug that returned an empty panel at every range.

    The baseline is "latest snapshot at or before the cutoff". Catalog history is
    younger than every range pill, so that set is empty and the join produced nothing.
    Falling back to the card's earliest snapshot is what `_baseline_prices` does for
    the owned path, and it has to happen here too.
    """
    repo = _repo({"a": [("2026-07-16", 100), ("2026-08-01", 250)]})

    # A cutoff four days before any recorded price.
    rows = repo.movers_since("2026-07-12", floor_cents=0)

    assert rows, "no baseline before the cutoff meant no rows at all"
    assert rows[0][1] == 250 and rows[0][2] == 150


def test_the_floor_applies_to_the_baseline_as_well_as_the_current_price():
    """`basep-41`, the real one: 26c to $1283.67 is not a 493,619% gain, it is a bad
    first reading. A floor on the current price alone lets it through, because the
    cheap end is the one that was wrong."""
    repo = _repo({"relabel": [("2026-07-16", 26), ("2026-07-20", 128_367)]})

    # The step guard is switched off throughout, so the floor is the only thing that
    # can reject this — otherwise the test passes for the wrong reason.
    assert repo.movers_since("2026-07-01", floor_cents=100, max_step_ratio=1e9) == [], (
        "ranked a 26c baseline against a $1283 price"
    )
    # With no floor at all it comes back, which pins the rejection to the floor.
    assert repo.movers_since("2026-07-01", floor_cents=0, max_step_ratio=1e9)


def test_a_single_relabel_step_disqualifies_the_card():
    """`ex7-36`'s real series. The move is one jump that then holds, so it is the
    sweep pointing at a different product — not a price history."""
    repo = _repo(
        {
            "ex7-36": [
                ("2026-07-16", 27_164),
                ("2026-07-28", 29_497),
                ("2026-08-04", 634),
                ("2026-08-11", 657),
            ]
        }
    )

    assert repo.movers_since("2026-07-01", floor_cents=100) == []
    # Raising the tolerance above the step readmits it, which is what pins the guard
    # to the step and not to something else about this card.
    assert repo.movers_since("2026-07-01", floor_cents=100, max_step_ratio=1e9)


def test_a_distributed_move_of_the_same_size_is_kept():
    """The discriminator has to be the *step*, not the total. This card triples over
    the window in believable increments and must survive."""
    repo = _repo(
        {
            "real": [
                ("2026-07-16", 1_000),
                ("2026-07-20", 1_400),
                ("2026-07-25", 1_900),
                ("2026-08-01", 2_500),
                ("2026-08-08", 3_000),
            ]
        }
    )

    rows = repo.movers_since("2026-07-01", floor_cents=100)

    assert [r[0] for r in rows] == ["real"], "threw away a genuine 200% move"
    assert rows[0][3] == 200.0


def test_a_card_seen_only_once_is_not_a_mover():
    """Dedup-on-change means a never-moved card has exactly one row. It has no
    movement to report, and reporting one would be inventing it."""
    repo = _repo({"flat": [("2026-07-16", 500), ("2026-07-20", 500)]})

    assert repo.movers_since("2026-07-01", floor_cents=0) == []


def test_min_points_requires_that_many_observations():
    repo = _repo({"thin": [("2026-07-16", 500), ("2026-08-01", 700)]})

    assert repo.movers_since("2026-07-01", floor_cents=0, min_points=2)
    assert repo.movers_since("2026-07-01", floor_cents=0, min_points=3) == []


def test_gainers_and_losers_come_from_one_sorted_pass():
    """The dashboard shows both, and `limit` has to cap each end. Slicing the middle
    out of the sort would drop every loser in a rising catalog."""
    repo = _repo(
        {
            "up1": [("2026-07-01", 100), ("2026-08-01", 400)],
            "up2": [("2026-07-01", 100), ("2026-08-01", 300)],
            "up3": [("2026-07-01", 100), ("2026-08-01", 200)],
            "down1": [("2026-07-01", 400), ("2026-08-01", 100)],
            "down2": [("2026-07-01", 300), ("2026-08-01", 120)],
        }
    )

    rows = repo.movers_since("2026-07-01", floor_cents=0, limit=1)

    assert len(rows) == 2, rows
    assert rows[0][0] == "up1", "biggest riser missing from the head"
    assert rows[-1][0] == "down1", "biggest faller missing from the tail"
    assert rows[0][3] > 0 > rows[-1][3]


def test_limit_none_returns_everything_sorted():
    repo = _repo(
        {
            "up": [("2026-07-01", 100), ("2026-08-01", 400)],
            "down": [("2026-07-01", 400), ("2026-08-01", 100)],
        }
    )

    rows = repo.movers_since("2026-07-01", floor_cents=0, limit=None)

    assert [r[0] for r in rows] == ["up", "down"]


def test_all_time_uses_each_cards_earliest_price():
    """`cutoff=None` is the All-time pill: measure from the first thing ever recorded
    for that card, not from a shared date."""
    repo = _repo(
        {
            "early": [("2026-06-01", 100), ("2026-08-01", 300)],
            "late": [("2026-07-20", 500), ("2026-08-01", 600)],
        }
    )

    rows = _by_id(repo.movers_since(None, floor_cents=0))

    assert rows["early"][3] == 200.0
    assert rows["late"][3] == 20.0


def test_an_empty_history_ranks_nothing_rather_than_raising():
    """The ordinary state on a fresh install: the sweep has never run."""
    repo = PriceHistoryRepository(open_history(":memory:"))

    assert repo.movers_since("2026-07-01", floor_cents=100) == []
    assert repo.movers_since(None, floor_cents=100, limit=5) == []
