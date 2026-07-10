"""Enumerations for the domain model.

Values are the exact strings persisted in SQLite (see Spec 02). We use a
``str`` mixin instead of 3.11's ``StrEnum`` to keep the 3.10 floor.
"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """Why a lot is held. Set per acquisition, editable later (no history in V1)."""

    PERSONAL = "personal"
    RESALE = "resale"
    RIPPING = "ripping"  # sealed product to be opened; valued at $0 (a loss)


class TransactionType(str, Enum):
    """Inferred from which sides carry cards; adjustment is explicit."""

    PURCHASE = "purchase"
    SALE = "sale"
    TRADE = "trade"
    ADJUSTMENT = "adjustment"


class TransactionStatus(str, Enum):
    POSTED = "posted"
    VOIDED = "voided"


class Direction(str, Enum):
    IN = "in"
    OUT = "out"


class Category(str, Enum):
    SINGLE = "single"
    SEALED = "sealed"
    ACCESSORY = "accessory"
    OTHER = "other"


class Condition(str, Enum):
    """For ungraded cards; NULL/absent when a card is graded."""

    NM = "NM"
    LP = "LP"
    MP = "MP"
    HP = "HP"
    DMG = "DMG"
    SEALED = "sealed"


class GradingCompany(str, Enum):
    PSA = "PSA"
    BGS = "BGS"
    CGC = "CGC"
    SGC = "SGC"


class AuditAction(str, Enum):
    CREATE = "create"
    EDIT = "edit"
    VOID = "void"
