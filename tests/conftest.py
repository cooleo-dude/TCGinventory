"""Shared pytest fixtures and helpers for the service/data tests."""

from __future__ import annotations

import pytest

from tcginventory.data.db import connect
from tcginventory.data.migrator import migrate
from tcginventory.domain.enums import Category, Intent
from tcginventory.domain.models import Item
from tcginventory.services.transactions import (
    IncomingCard,
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    yield c
    c.close()


def make_item(name: str, **overrides) -> Item:
    """A minimal single-card item for building transactions."""
    base = dict(name=name, category=Category.SINGLE)
    base.update(overrides)
    return Item(**base)


def incoming(
    name: str,
    market_value_cents: int,
    *,
    quantity: int = 1,
    intent: Intent = Intent.RESALE,
    **item_overrides,
) -> IncomingCard:
    return IncomingCard(
        item=make_item(name, **item_overrides),
        quantity=quantity,
        market_value_cents=market_value_cents,
        intent=intent,
    )


def buy(
    svc: TransactionService,
    name: str,
    price_cents: int,
    market_value_cents: int,
    occurred_at: str,
    *,
    quantity: int = 1,
    intent: Intent = Intent.RESALE,
    **item_overrides,
):
    """Post a single-card purchase; returns the PostResult."""
    req = TransactionRequest(
        occurred_at=occurred_at,
        cash_out_cents=price_cents,
        incoming=[
            incoming(
                name,
                market_value_cents,
                quantity=quantity,
                intent=intent,
                **item_overrides,
            )
        ],
    )
    return svc.post(req)


def sell(
    svc: TransactionService,
    item_id: int,
    quantity: int,
    proceeds_cents: int,
    occurred_at: str,
):
    """Post a single-card sale (cash in); returns the PostResult.

    Proceeds are the cash received (the value rule derives per-card proceeds
    from the deal — for a pure sale that's the cash).
    """
    req = TransactionRequest(
        occurred_at=occurred_at,
        outgoing=[OutgoingCard(item_id=item_id, quantity=quantity)],
        cash_in_cents=proceeds_cents,
    )
    return svc.post(req)
