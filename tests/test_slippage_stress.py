from decimal import Decimal as D

import pytest

from kalshi_predictor.crypto.slippage_stress import one_contract_range_stress


def stress(asks, depth=None):
    return one_contract_range_stress(
        executable_price=asks[-1],
        depth=depth or ((asks[-1], D(10)),),
        recent_asks=asks,
        evidence_hashes=("a" * 64,),
        current_and_time_ordered=True,
    )


def test_jump_to_current_ask_does_not_erase_quote_risk():
    assert stress((D(".18"), D(".36"))).value == D(".18")
    assert stress((D(".36"), D(".18"))).value == D(".18")


def test_static_book_zero_is_estimated_not_certified():
    value = stress((D(".4"), D(".4")))
    assert value.value == 0
    assert value.status == "ESTIMATED"
    assert "NOT_EXPECTED" in value.reason


def test_one_contract_depth_and_missing_history():
    assert stress((D(".4"),)).value is None
    assert stress((D(".39"), D(".4")), ((D(".4"), D(".5")),)).value is None
    assert stress((D(".39"), D(".4")), ((D(".4"), D(".5")), (D(".42"), D(".5")))).value == D(".02")


def test_unbound_current_price_rejected():
    with pytest.raises(ValueError, match="CURRENT_PRICE"):
        one_contract_range_stress(
            executable_price=D(".4"),
            depth=((D(".4"), D(10)),),
            recent_asks=(D(".4"), D(".42")),
            evidence_hashes=("a" * 64,),
            current_and_time_ordered=True,
        )
