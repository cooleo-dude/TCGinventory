"""Pure cost-basis allocation — the market-value-weighted split (Spec 02).

No SQL, no Qt, no I/O: given the total consideration and each incoming line's
per-unit market value and quantity, return the integer-cent cost basis for each
resulting **lot**. Kept isolated because it is the single most correctness-
critical piece of money math in the app, and this way it is trivially testable.

Rules (specs/02-data-model.md — "Cost allocation"):
- Split ``net_consideration_in`` across lines weighted by each line's *total*
  market value (``market_value_per_unit × quantity``).
- Work in integer cents; the per-line totals must sum **exactly** to
  ``net_consideration_in``. Any rounding residual is placed on the
  **largest-value line** (ties → earliest line).
- A lot stores a single ``unit_cost_cents``. When a line's total does not divide
  evenly by its quantity, the line yields **two** lots — ``qty − r`` units at the
  base unit cost and ``r`` units at ``base + 1`` — so ``Σ(unit_cost × qty)``
  still equals the line total exactly (invariant 6). Quantity-1 lines (the common
  case) always yield a single lot whose cost is the line total.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationInput:
    """One incoming line to allocate cost to."""

    market_value_cents: int  # per-unit market value at acquisition (> 0)
    quantity: int  # units in this line (>= 1)


@dataclass(frozen=True)
class LotCost:
    """A resulting lot: ``quantity`` units each costing ``unit_cost_cents``."""

    unit_cost_cents: int
    quantity: int


@dataclass(frozen=True)
class LineAllocation:
    """Allocation result for one input line: its total and the lot(s) it yields."""

    total_cents: int
    lots: tuple[LotCost, ...]


def allocate_cost(
    net_consideration_in: int, lines: list[AllocationInput]
) -> list[LineAllocation]:
    """Allocate ``net_consideration_in`` across ``lines`` by market-value weight.

    Returns one :class:`LineAllocation` per input line, in the same order.
    ``Σ (lot.unit_cost_cents × lot.quantity)`` over all returned lots equals
    ``net_consideration_in`` exactly.

    ``net_consideration_in`` is clamped at 0: a transaction where you were paid
    more cash than the value you gave up assigns incoming cards a $0 basis rather
    than a negative one (documented V1 rule; see decisions log).
    """
    if not lines:
        return []
    if any(line.quantity < 1 for line in lines):
        raise ValueError("every allocation line needs quantity >= 1")
    if any(line.market_value_cents <= 0 for line in lines):
        raise ValueError("every incoming line needs a positive market value")

    total = max(0, net_consideration_in)
    weights = [line.market_value_cents * line.quantity for line in lines]
    weight_sum = sum(weights)

    # Largest-remainder at the line level: floor each share, then place the whole
    # residual on the largest-value line (ties → earliest).
    line_totals = [total * w // weight_sum for w in weights]
    residual = total - sum(line_totals)
    if residual:
        largest = max(range(len(lines)), key=lambda i: (weights[i], -i))
        line_totals[largest] += residual

    return [
        LineAllocation(
            total_cents=line_total, lots=_split_line(line_total, line.quantity)
        )
        for line, line_total in zip(lines, line_totals, strict=True)
    ]


def _split_line(total_cents: int, quantity: int) -> tuple[LotCost, ...]:
    """Split a line total into 1–2 lots with integer unit costs summing exactly."""
    base = total_cents // quantity
    remainder = total_cents - base * quantity
    if remainder == 0:
        return (LotCost(unit_cost_cents=base, quantity=quantity),)
    # `remainder` units cost one cent more so the totals reconcile exactly.
    return (
        LotCost(unit_cost_cents=base, quantity=quantity - remainder),
        LotCost(unit_cost_cents=base + 1, quantity=remainder),
    )
