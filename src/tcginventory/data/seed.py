"""Demo data seeding — a realistic spread for exercising the read views.

Qt-free: posts through the service layer so seeded data obeys every invariant.
Used by dev/verification (e.g. ``TCGINVENTORY_SEED=1``); not run in normal use.
"""

from __future__ import annotations

import sqlite3

from tcginventory.domain.enums import (
    Category,
    Condition,
    GradingCompany,
    Intent,
    Language,
)
from tcginventory.domain.models import Item
from tcginventory.services.transactions import (
    IncomingCard,
    OutgoingCard,
    TransactionRequest,
    TransactionService,
)


def _single(name: str, **kw) -> Item:
    return Item(name=name, category=Category.SINGLE, **kw)


def seed_demo(conn: sqlite3.Connection) -> None:
    """Populate ``conn`` with a spread of purchases, a sale, a trade, a rip box."""
    svc = TransactionService(conn)

    # A multi-card lot bought at 80% of market ($80 for $100 of cards).
    lot = svc.post(
        TransactionRequest(
            occurred_at="2026-01-05",
            cash_out_cents=8000,
            incoming=[
                IncomingCard(
                    _single(
                        "Charizard VMAX",
                        set_name="Champion's Path",
                        set_code="CPA",
                        card_number="074/073",
                        language=Language.EN,
                        condition=Condition.NM,
                        tcgplayer_url="https://www.tcgplayer.com/",
                    ),
                    1,
                    6000,
                    Intent.RESALE,
                ),
                IncomingCard(
                    _single(
                        "Pikachu V",
                        set_name="Vivid Voltage",
                        card_number="043/185",
                        condition=Condition.NM,
                    ),
                    1,
                    3000,
                    Intent.PERSONAL,
                ),
                IncomingCard(
                    _single(
                        "Snorlax",
                        set_name="Base",
                        card_number="011/102",
                        condition=Condition.LP,
                    ),
                    1,
                    1000,
                    Intent.RESALE,
                ),
            ],
        )
    )
    charizard_id = lot.lots[0].item_id

    # A graded slab (identity keyed on grade, not condition).
    svc.post(
        TransactionRequest(
            occurred_at="2026-01-20",
            cash_out_cents=45000,
            incoming=[
                IncomingCard(
                    _single(
                        "Charizard",
                        set_name="Base",
                        card_number="004/102",
                        is_graded=True,
                        grading_company=GradingCompany.PSA,
                        grade="9",
                        cert_number="12345678",
                    ),
                    1,
                    45000,
                    Intent.PERSONAL,
                ),
            ],
        )
    )

    # A Japanese card (language is part of identity).
    svc.post(
        TransactionRequest(
            occurred_at="2026-02-01",
            cash_out_cents=2500,
            incoming=[
                IncomingCard(
                    _single(
                        "Rayquaza",
                        set_name="Shiny Star V",
                        card_number="076/190",
                        language=Language.JP,
                        condition=Condition.NM,
                    ),
                    1,
                    2500,
                    Intent.RESALE,
                ),
            ],
        )
    )

    # A sealed booster box bought to rip (valued at $0 → shows as a loss).
    svc.post(
        TransactionRequest(
            occurred_at="2026-02-10",
            cash_out_cents=11000,
            incoming=[
                IncomingCard(
                    Item(
                        name="Brilliant Stars Booster Box",
                        category=Category.SEALED,
                        condition=Condition.SEALED,
                    ),
                    1,
                    12000,
                    Intent.RIPPING,
                ),
            ],
        )
    )

    # Sell the Charizard VMAX for a profit.
    svc.post(
        TransactionRequest(
            occurred_at="2026-03-01",
            outgoing=[
                OutgoingCard(item_id=charizard_id, quantity=1, agreed_value_cents=9000)
            ],
            cash_in_cents=9000,
            counterparty="eBay buyer",
        )
    )

    # Trade the Snorlax away for a new card + a little cash back.
    snorlax_id = None
    for row in TransactionService(conn).items.list_all():
        if row.name == "Snorlax":
            snorlax_id = row.id
            break
    if snorlax_id is not None:
        svc.post(
            TransactionRequest(
                occurred_at="2026-03-15",
                outgoing=[
                    OutgoingCard(
                        item_id=snorlax_id, quantity=1, agreed_value_cents=1500
                    )
                ],
                incoming=[
                    IncomingCard(
                        _single(
                            "Gengar",
                            set_name="Fossil",
                            card_number="005/62",
                            condition=Condition.MP,
                        ),
                        1,
                        1200,
                        Intent.RESALE,
                    ),
                ],
                cash_in_cents=300,
                counterparty="Local trade",
            )
        )
