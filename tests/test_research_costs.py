from decimal import Decimal as D

import pytest

from kalshi_predictor.crypto.research_costs import (
    CostComponent,
    DependenceCounts,
    EvidenceStatus,
    book_slippage,
    full_costs,
    single_buy_fee,
    unknown,
)

HASH = ("a" * 64,)


@pytest.mark.parametrize(
    "price,cent,direct",
    [
        ("0.01", "0.01", "0.0007"),
        ("0.4", "0.02", "0.0168"),
        ("0.5", "0.02", "0.0175"),
        ("0.54", "0.02", "0.0174"),
        ("0.99", "0.01", "0.0007"),
    ],
)
def test_documented_single_fill_precision(price, cent, direct):
    for precision, expected in [("0.01", cent), ("0.0001", direct)]:
        result = single_buy_fee(
            price=D(price), multiplier=D(1), rate=D(".07"), balance_precision=D(precision)
        )
        assert result["fee_cost"] == D(expected)
        assert result["rebate"] == 0


def test_subcent_price_alignment_is_on_balance_change():
    result = single_buy_fee(
        price=D(".055"), multiplier=D(1), rate=D(".07"), balance_precision=D(".01")
    )
    assert result["model_fee"] == D(".00363825")
    assert result["trade_fee"] == D(".003639")
    assert result["rounding_fee"] == D(".001361")
    assert result["fee_cost"] == D(".005")


def component(value):
    return CostComponent(D(value), EvidenceStatus.ESTIMATED, "test", HASH, "fixture")


def test_unknown_does_not_become_zero_and_gate_is_strict():
    args = dict(
        probability=D(".6"),
        executable_price=D(".5"),
        fee=component(".02"),
        slippage=component(".01"),
    )
    assert (
        full_costs(**args, uncertainty=unknown("calibration", "insufficient")).full_net_ev is None
    )
    result = full_costs(**args, uncertainty=component(".02"))
    assert result.full_net_ev == D(".05")
    assert not result.clears_ev_gate
    assert not result.execution_authority


def test_depth_and_quote_history_cannot_be_omitted():
    args = dict(
        executable_price=D(".4"),
        depth=((D(".4"), D(1)),),
        recent_asks=(D(".4"),),
        evidence_hashes=HASH,
        current_and_time_ordered=True,
    )
    assert book_slippage(**args).status == EvidenceStatus.UNKNOWN
    args["recent_asks"] = (D(".4"), D(".42"))
    assert book_slippage(**args).value == D(".02")
    args["depth"] = ((D(".4"), D(".5")), (D(".42"), D(".5")))
    assert book_slippage(**args).value == D(".03")


def test_invalid_evidence_and_counts_are_rejected():
    with pytest.raises(ValueError):
        CostComponent(D(0), EvidenceStatus.UNKNOWN, "test", (), "unknown")
    with pytest.raises(ValueError):
        component("-0.01")
    with pytest.raises(ValueError):
        DependenceCounts(2, 1, 1, 2, 1)
    assert DependenceCounts(10, 5, 5, None, 1).independent_events is None
