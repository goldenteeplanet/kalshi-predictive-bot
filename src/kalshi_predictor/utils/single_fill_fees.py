"""Canonical one-contract buy, one fill, zero-accumulator fee arithmetic.

No rate, account-class, contract-policy certification or execution authority.
"""
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def single_buy_fill(
    *, price: Decimal, multiplier: Decimal, rate: Decimal, balance_precision: Decimal
) -> dict[str, Decimal]:
    if any(not isinstance(v, Decimal) or not v.is_finite() for v in (price, multiplier, rate)):
        raise ValueError("FINITE_DECIMAL_FEE_INPUT_REQUIRED")
    if not 0 <= price <= 1 or multiplier < 0 or rate < 0:
        raise ValueError("NONNEGATIVE_UNIT_BUY_FEE_INPUT_REQUIRED")
    if balance_precision not in (Decimal("0.01"), Decimal("0.0001")):
        raise ValueError("DOCUMENTED_ACCOUNT_PRECISION_REQUIRED")
    model = rate * multiplier * price * (1 - price)
    trade = model.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
    change = (-price - trade).quantize(balance_precision, rounding=ROUND_FLOOR)
    rounding = -price - trade - change
    return {
        "model_fee": model,
        "trade_fee": trade,
        "rounding_fee": rounding,
        "rebate": Decimal(0),
        "fee_cost": trade + rounding,
        "total_debit": -change,
    }
