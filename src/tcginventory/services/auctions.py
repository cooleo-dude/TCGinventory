"""eBay auction max-bid math — pure, integer-cents, and Qt-free.

Given the total market value of the cards bundled in an auction, a flat shipping
cost, and a sales-tax rate, this computes the **maximum bid** you can place at
each target percentage of the cards' value. The percentage is your *all-in*
ceiling: the winning bid plus shipping plus tax should not exceed ``p%`` of what
the cards are worth. Sales tax is charged on the bid **plus** shipping (the
standard eBay base), so:

    (max_bid + shipping) x (1 + rate) = p% x value
    max_bid = p% x value / (1 + rate) - shipping

All money is integer cents; the tax rate is integer **basis points**
(``600`` = 6.00%). Everything stays integer so there is no float drift, matching
the rest of the app's money discipline.
"""

from __future__ import annotations

from dataclasses import dataclass

# The fixed breakdown percentages shown for every auction.
PERCENTAGES: tuple[int, ...] = (70, 80, 90, 100)

# Default sales-tax rate (6.00%), editable per auction.
DEFAULT_TAX_RATE_BPS = 600


def max_bid_cents(
    value_cents: int, shipping_cents: int, tax_rate_bps: int, pct: int
) -> int:
    """Highest bid (cents) whose all-in cost equals ``pct``% of ``value_cents``.

    Clamped to ``>= 0`` — if shipping + tax already exceed the target, there is
    no room to bid.
    """
    if value_cents <= 0:
        return 0
    # target all-in = value * pct / 100
    # gross_of_tax  = target / (1 + rate) = value * pct / 100 * 10000/(10000+bps)
    #               = value * pct * 100 / (10000 + bps)
    denom = 10_000 + tax_rate_bps
    numer = value_cents * pct * 100
    gross_of_tax = (numer + denom // 2) // denom  # round half up
    return max(0, gross_of_tax - shipping_cents)


@dataclass(frozen=True)
class BidBreakdown:
    """The max bid at each :data:`PERCENTAGES` target for one auction."""

    value_cents: int
    shipping_cents: int
    tax_rate_bps: int
    bids: dict[int, int]  # pct -> max bid cents


def breakdown(value_cents: int, shipping_cents: int, tax_rate_bps: int) -> BidBreakdown:
    """Compute the full 70/80/90/100% max-bid breakdown."""
    return BidBreakdown(
        value_cents=value_cents,
        shipping_cents=shipping_cents,
        tax_rate_bps=tax_rate_bps,
        bids={
            pct: max_bid_cents(value_cents, shipping_cents, tax_rate_bps, pct)
            for pct in PERCENTAGES
        },
    )
