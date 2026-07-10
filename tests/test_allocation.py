"""Tests for the pure cost-allocation math (services/allocation.py)."""

from __future__ import annotations

import pytest

from tcginventory.services.allocation import (
    AllocationInput,
    allocate_cost,
)


def _totals(result):
    return [la.total_cents for la in result]


def _all_lots(result):
    return [lot for la in result for lot in la.lots]


def _sum_lots(result):
    return sum(lot.unit_cost_cents * lot.quantity for lot in _all_lots(result))


def test_scenario_1_clean_split():
    # $80 across market values $60/$30/$10 → $48/$24/$8.
    result = allocate_cost(
        8000,
        [AllocationInput(6000, 1), AllocationInput(3000, 1), AllocationInput(1000, 1)],
    )
    assert _totals(result) == [4800, 2400, 800]
    assert _sum_lots(result) == 8000


def test_scenario_3_rounding_residual_to_largest_value_line():
    # $100 across $12/$11/$10 → 3637/3333/3030 (residual cent to the $12 line).
    result = allocate_cost(
        10000,
        [AllocationInput(1200, 1), AllocationInput(1100, 1), AllocationInput(1000, 1)],
    )
    assert _totals(result) == [3637, 3333, 3030]
    assert _sum_lots(result) == 10000


def test_pure_purchase_single_line_gets_whole_total():
    result = allocate_cost(9000, [AllocationInput(9000, 1)])
    assert _totals(result) == [9000]
    assert result[0].lots[0].unit_cost_cents == 9000
    assert result[0].lots[0].quantity == 1


def test_quantity_gt_one_indivisible_total_splits_into_two_lots():
    # $1.00 across 3 identical units: 33,33,34 → two lots (33×2, 34×1).
    result = allocate_cost(100, [AllocationInput(100, 3)])
    (line,) = result
    assert line.total_cents == 100
    assert line.lots == tuple(line.lots)  # sanity
    assert sorted((lot.unit_cost_cents, lot.quantity) for lot in line.lots) == [
        (33, 2),
        (34, 1),
    ]
    assert _sum_lots(result) == 100


def test_quantity_gt_one_divisible_stays_one_lot():
    result = allocate_cost(9000, [AllocationInput(3000, 3)])
    (line,) = result
    assert len(line.lots) == 1
    assert line.lots[0].unit_cost_cents == 3000
    assert line.lots[0].quantity == 3


def test_negative_consideration_clamps_to_zero():
    result = allocate_cost(-500, [AllocationInput(4000, 1)])
    assert _totals(result) == [0]
    assert _sum_lots(result) == 0


def test_sum_is_always_exact_across_many_shapes():
    cases = [
        (10000, [(1200, 1), (1100, 1), (1000, 1)]),
        (7777, [(100, 1), (100, 1), (100, 1), (100, 1)]),
        (12345, [(500, 2), (333, 3), (999, 1)]),
        (1, [(10, 1), (10, 1)]),
        (100000, [(1, 7), (2, 5), (3, 11)]),
    ]
    for total, spec in cases:
        result = allocate_cost(total, [AllocationInput(mv, q) for mv, q in spec])
        assert _sum_lots(result) == total, (total, spec)


def test_empty_lines_returns_empty():
    assert allocate_cost(1000, []) == []


def test_zero_market_value_rejected():
    with pytest.raises(ValueError):
        allocate_cost(1000, [AllocationInput(0, 1)])


def test_zero_quantity_rejected():
    with pytest.raises(ValueError):
        allocate_cost(1000, [AllocationInput(1000, 0)])
