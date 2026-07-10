"""Typed domain exceptions raised by the service layer.

The UI maps these to friendly dialogs (specs/01-architecture.md). All inherit
from :class:`ServiceError` so callers can catch the whole family.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for all service-layer errors."""


class InsufficientQuantityError(ServiceError):
    """A disposal asked for more units than are on hand for an item."""


class EmptyTransactionError(ServiceError):
    """A transaction was posted with no cards on either side."""


class InvalidTransactionError(ServiceError):
    """A transaction violated a posting precondition (bad qty, missing value…)."""


class RippingNotSellableError(ServiceError):
    """An outgoing line tried to consume a ripping-intent lot (never sellable)."""


class TransactionStateError(ServiceError):
    """An operation is invalid for the transaction's current state.

    e.g. voiding an already-voided transaction, or voiding one whose created
    lots have since been (partially) consumed by a later transaction.
    """
