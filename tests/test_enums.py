"""Sanity checks that domain enum values match the persisted-string contract."""

from __future__ import annotations

from tcginventory.domain.enums import Intent, TransactionType


def test_intent_values():
    assert {i.value for i in Intent} == {"personal", "resale", "ripping"}


def test_transaction_type_values():
    assert {t.value for t in TransactionType} == {
        "purchase",
        "sale",
        "trade",
        "adjustment",
    }
