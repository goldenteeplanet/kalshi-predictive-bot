from decimal import Decimal
from fractions import Fraction

import pytest

from kalshi_predictor.crypto.research_costs import single_buy_fee
from kalshi_predictor.paper.fees import single_buy_fees
from kalshi_predictor.utils.single_fill_fees import single_buy_fill


@pytest.mark.parametrize("precision", ["0.01", "0.0001"])
def test_exact_rational_balance_oracle_and_adapter_agreement(precision):
    grid = Fraction(precision)
    for tick in (1, 49, 50, 51, 99, 100, 550, 2499, 2500, 4999, 5000, 5001, 7500, 9950, 9999):
        price = Decimal(tick) / 10000
        for rate in (Decimal(0), Decimal("0.0175"), Decimal("0.07")):
            for multiplier in (Decimal(0), Decimal(1), Decimal(2)):
                model = (
                    Fraction(rate) * Fraction(multiplier) * Fraction(price) * (1 - Fraction(price))
                )
                micros = -(-(model * 1_000_000).numerator // (model * 1_000_000).denominator)
                raw_debit = Fraction(price) + Fraction(micros, 1_000_000)
                units = -(-(raw_debit / grid).numerator // (raw_debit / grid).denominator)
                expected_debit = units * grid
                result = single_buy_fill(
                    price=price,
                    rate=rate,
                    multiplier=multiplier,
                    balance_precision=Decimal(precision),
                )
                assert Fraction(result["trade_fee"]) == Fraction(micros, 1_000_000)
                assert Fraction(result["total_debit"]) == expected_debit
                assert Fraction(result["fee_cost"]) == expected_debit - Fraction(price)
                research = single_buy_fee(
                    price=price,
                    rate=rate,
                    multiplier=multiplier,
                    balance_precision=Decimal(precision),
                )
                assert research == {k: v for k, v in result.items() if k != "total_debit"}
                if precision == "0.01":
                    paper = single_buy_fees(price, multiplier, rate)
                    assert paper["estimated_fee"] == research["fee_cost"]
                    assert paper["trade_fee"] == research["trade_fee"]


def test_documented_fractional_price_example_and_legacy_serialization():
    result = single_buy_fees(Decimal("0.055"), Decimal(1), Decimal("0.07"))
    assert {key: str(value) for key, value in result.items()} == {
        "trade_fee": "0.003639",
        "rounding_allowance": "0.001361",
        "estimated_fee": "0.005",
        "total_debit": "0.06",
    }


@pytest.mark.parametrize("price", [Decimal(0), Decimal(1)])
def test_existing_endpoint_scope_difference_preserved(price):
    assert single_buy_fees(price, Decimal(1), Decimal("0.07"))["estimated_fee"] == 0
    with pytest.raises(ValueError, match="OPEN_UNIT_PRICE_REQUIRED"):
        single_buy_fee(
            price=price,
            rate=Decimal("0.07"),
            multiplier=Decimal(1),
            balance_precision=Decimal("0.01"),
        )
