from decimal import Decimal

from kalshi_predictor.kalshi.protocol_math import (
    fee_adjusted_expected_value,
    is_valid_market_price,
    price_ranges_from_market,
    reciprocal_price,
    tick_size_for_price,
    trading_fee,
)


def test_reciprocal_orderbook_price_uses_binary_complement() -> None:
    assert reciprocal_price("0.3700") == Decimal("0.6300")
    assert reciprocal_price("1.01") is None


def test_market_price_ranges_follow_current_fixed_point_structures() -> None:
    tapered = {"price_level_structure": "tapered_deci_cent"}
    assert tick_size_for_price(tapered, "0.055") == Decimal("0.001")
    assert tick_size_for_price(tapered, "0.50") == Decimal("0.01")
    assert tick_size_for_price(tapered, "0.955") == Decimal("0.001")
    assert is_valid_market_price(tapered, "0.055")
    assert not is_valid_market_price(tapered, "0.0555")


def test_explicit_market_price_ranges_override_structure_defaults() -> None:
    market = {
        "price_level_structure": "linear_cent",
        "price_ranges": [{"start": 0, "end": 1, "step": "0.0025"}],
    }
    assert price_ranges_from_market(market)[0].step == Decimal("0.0025")
    assert is_valid_market_price(market, "0.0525")


def test_general_taker_and_maker_fees_round_up_to_centicent() -> None:
    assert trading_fee(price="0.055", contracts="1") == Decimal("0.0037")
    assert trading_fee(price="0.50", contracts="100") == Decimal("1.7500")
    assert trading_fee(price="0.50", contracts="100", maker=True) == Decimal("0.4375")


def test_fee_adjusted_ev_is_stricter_than_gross_probability_edge() -> None:
    assert fee_adjusted_expected_value(probability="0.56", price="0.55") == Decimal(
        "-0.0074"
    )
