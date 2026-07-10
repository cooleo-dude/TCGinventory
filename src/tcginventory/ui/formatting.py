"""Display formatting helpers for the UI (money, percent).

Kept tiny and Qt-free so it can be unit-tested. Money is always formatted from
integer cents — the UI never does money math, it only renders.
"""

from __future__ import annotations


def money(cents: int | None) -> str:
    """Format integer cents as USD, e.g. ``-500`` → ``-$5.00``; ``None`` → ``""``."""
    if cents is None:
        return ""
    negative = cents < 0
    whole, frac = divmod(abs(cents), 100)
    text = f"${whole:,}.{frac:02d}"
    return f"-{text}" if negative else text


def percent(value: float | None, digits: int = 1) -> str:
    """Format a percentage value, e.g. ``33.333`` → ``33.3%``; ``None`` → ``""``."""
    if value is None:
        return ""
    return f"{value:.{digits}f}%"
