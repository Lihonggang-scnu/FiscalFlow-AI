"""Decimal-to-cents conversion used by every financial calculation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .errors import InvalidInputError

_CENT = Decimal("0.01")


def amount_to_cents(value: Any) -> int:
    """Convert a non-negative amount to integer cents without binary float math."""

    if isinstance(value, bool) or value is None:
        raise InvalidInputError("金额必须是非负数。")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise InvalidInputError("金额不是有效数字。") from error
    if not decimal.is_finite() or decimal < 0:
        raise InvalidInputError("金额必须是有限的非负数。")
    return int(decimal.quantize(_CENT, rounding=ROUND_HALF_UP) * 100)


def cents_to_decimal(cents: int) -> Decimal:
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise InvalidInputError("内部金额必须是整数分。")
    return (Decimal(cents) / Decimal(100)).quantize(_CENT)


def cents_to_text(cents: int) -> str:
    return f"{cents_to_decimal(cents):.2f}"
