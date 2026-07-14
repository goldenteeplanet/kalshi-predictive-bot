from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
ONE_DOLLAR = Decimal("1.00")


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimal_to_str(value: Any) -> str | None:
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return None
    return format(decimal_value, "f")


def clamp_probability(value: Decimal) -> Decimal:
    if value < ZERO:
        return ZERO
    if value > ONE:
        return ONE
    return value


def midpoint(left: Decimal, right: Decimal) -> Decimal:
    return (left + right) / Decimal("2")
