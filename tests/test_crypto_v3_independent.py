from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kalshi_predictor.forecasting.crypto_v2 import _bound_probability, _market_price_basis
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    compare_execution,
    forecast_independent,
)

NOW = datetime(2026, 9, 10, 19, tzinfo=UTC)


def history():
    return [
        PriceObservation(
            price=100 * (1 + (i % 5 - 2) * 0.001),
            observed_at=NOW - timedelta(minutes=300 - i),
            received_at=NOW - timedelta(minutes=300 - i),
            source="coinbase",
            symbol="BTC",
            source_sha256="a" * 64,
        )
        for i in range(301)
    ]


def target(**kwargs):
    return CryptoTarget("BTC", "ABOVE", NOW + timedelta(minutes=5), threshold=99.5, **kwargs)


@pytest.mark.parametrize("bid,ask", [("0.40", "0.62"), (None, "0.62"), ("0.40", None)])
@pytest.mark.parametrize("adjustment", ["-10", "-0.1", "0", "0.1", "10"])
def test_v2_cannot_outperform_available_executable_buy_side(bid, ask, adjustment):
    basis = _market_price_basis(
        SimpleNamespace(best_yes_bid=bid, best_yes_ask=ask, last_price_dollars=None)
    )
    p = _bound_probability(basis.anchor + Decimal(adjustment), lower=basis.lower, upper=basis.upper)
    if ask is not None:
        assert p - Decimal(ask) <= 0
    if bid is not None:
        assert (1 - p) - (1 - Decimal(bid)) <= 0


def test_independent_probability_can_disagree_but_is_never_paper_release():
    forecast = forecast_independent(history(), target(), decision_at=NOW)
    assert forecast["probability"] > 0.62
    assert not forecast["calibrated"] and not forecast["paper_eligible"]
    assert not forecast["execution_authority"]
    assert forecast["comparisons"]["empirical_matched_horizon"]["nonoverlapping_blocks"] == 60
    assert forecast["comparisons"]["existing_distribution_zero_drift"]["probability"] == (
        pytest.approx(forecast["probability"])
    )


def test_complete_partition_coherent_without_normalizing_incomplete_family():
    base = target()
    below = replace(base, comparator="BELOW", threshold=99.5)
    middle = replace(base, comparator="RANGE", threshold=None, lower=99.5, upper=100.5)
    above = replace(base, comparator="AT_OR_ABOVE", threshold=100.5)
    results = [forecast_independent(history(), t, decision_at=NOW) for t in (below, middle, above)]
    for name in ("gaussian_log_returns", "student_t_df3", "empirical_matched_horizon"):
        ps = [r["comparisons"][name]["probability"] for r in results]
        assert sum(ps) == pytest.approx(1)
        assert all(0 <= p <= 1 for p in ps)
    assert results[1]["probability"] < 1


@pytest.mark.parametrize("failure", ["future", "invisible", "stale", "gap", "short", "zero"])
def test_rejects_invalid_evidence(failure):
    rows = history()
    if failure == "future":
        rows[-1] = replace(rows[-1], observed_at=NOW + timedelta(seconds=1))
    elif failure == "invisible":
        rows[0] = replace(rows[0], received_at=NOW + timedelta(seconds=1))
    elif failure == "stale":
        rows = rows[:-10]
    elif failure == "gap":
        rows.pop(3)
    elif failure == "short":
        rows = rows[-30:]
    else:
        rows = [replace(r, price=100) for r in rows]
    with pytest.raises(ValueError):
        forecast_independent(rows, target(), decision_at=NOW)


def test_empirical_does_not_invent_unobserved_horizon_returns():
    result = forecast_independent(
        history(), replace(target(), observation_at=NOW + timedelta(seconds=91)), decision_at=NOW
    )
    assert result["comparisons"]["empirical_matched_horizon"]["probability"] is None


def test_cost_decomposition_no_double_counted_spread():
    result = compare_execution(
        0.69,
        yes_bid=0.60,
        yes_ask=0.62,
        yes_fee=0.01,
        no_fee=0.01,
        slippage=0.005,
        uncertainty=0.02,
    )
    assert result["YES"]["gross_edge"] == pytest.approx(0.07)
    assert result["YES"]["net_ev"] == pytest.approx(0.035)
    assert result["YES"]["forecast_edge"] - sum(
        result["YES"][k] for k in ("execution_spread", "fee", "slippage", "uncertainty")
    ) == pytest.approx(result["YES"]["net_ev"])
    assert result["NO"]["executable_price"] == pytest.approx(0.40)
    assert not result["paper_eligible"]


def test_input_receipt_hash_changes_with_target_and_receipt():
    rows = history()
    original = forecast_independent(rows, target(), decision_at=NOW)["input_sha256"]
    rows[0] = replace(rows[0], source_sha256="b" * 64)
    assert forecast_independent(rows, target(), decision_at=NOW)["input_sha256"] != original


def test_source_asset_must_match_target():
    rows = [replace(r, symbol="ETH") for r in history()]
    with pytest.raises(ValueError, match="SOURCE_SYMBOL_MISMATCH"):
        forecast_independent(rows, target(), decision_at=NOW)


def test_extreme_finite_strike_has_valid_student_probability():
    result = forecast_independent(history(), replace(target(), threshold=1e308), decision_at=NOW)
    assert 0 <= result["comparisons"]["student_t_df3"]["probability"] <= 1
