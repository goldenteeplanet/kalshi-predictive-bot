"""Diagnostic spot and forecast horizon must share a provider-time origin."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.overnight_paper.discovery import add_crypto_research

NOW = datetime(2026, 9, 9, 5, 56, 10, tzinfo=UTC)


def fixture():
    row = {
        "raw_market": {
            "close_time": "2026-09-09T06:00:00Z",
            "strike_type": "greater",
            "floor_strike": 78920,
        },
        "crypto_terms": {"status": "EXACT_LINK"},
        "semantic_conflicts": [],
        "first_blocker": "SETTLEMENT_RULE_UNCERTIFIED",
        "paper_readiness": "PAPER_NOT_READY",
    }
    source = {
        "ticker": {"price": "78923.29", "time": "2026-09-09T05:56:08Z"},
        "ticker_evidence": {"received_at": "2026-09-09T05:56:09Z", "sha256": "original-ticker"},
        "candles_evidence": {"received_at": "2026-09-09T05:56:09.5Z", "sha256": "original-candles"},
        "latest_closed_candle_at": "2026-09-09T05:53:00Z",
        "collected_at": "2026-09-09T05:56:09.6Z",
        "closed_candle_count": 350,
        "features": {
            "price": "78865.99",
            "volatility_1h": "0.0003958211103597229",
            "return_1h": "-0.0034911802526815941262250562",
        },
    }
    return row, source


def test_ticker_origin_and_historical_features_remain_distinct_and_immutable():
    row, source = fixture()
    original = deepcopy(source)
    add_crypto_research(row, source, NOW)
    inputs = row["forecast_inputs"]
    assert inputs["spot"] == "78923.29"
    assert inputs["historical_candle_spot"] == "78865.99"
    assert inputs["horizon_minutes"] == pytest.approx(232 / 60)
    assert inputs["horizon_start_at"] == "2026-09-09T05:56:08+00:00"
    assert inputs["candle_feature_cutoff_at"] == "2026-09-09T05:53:00+00:00"
    assert inputs["ticker_evidence"] == source["ticker_evidence"]
    assert source == original
    assert row["forecast"] is not None
    assert row["first_blocker"] == "SETTLEMENT_RULE_UNCERTIFIED"
    assert row["paper_readiness"] == "PAPER_NOT_READY"
    assert "CF benchmark averaging" in row["model_scope_warning"]
    later, _ = fixture()
    add_crypto_research(later, source, NOW + timedelta(seconds=10))
    assert later["forecast"] == row["forecast"]
    assert later["forecast_inputs"]["decision_at"] != inputs["decision_at"]


@pytest.mark.parametrize(
    "key,value,reason",
    [
        ("price", "NaN", "INVALID_TICKER_PRICE"),
        ("price", "Infinity", "INVALID_TICKER_PRICE"),
        ("price", "-1", "INVALID_TICKER_PRICE"),
        ("price", "0", "INVALID_TICKER_PRICE"),
        ("price", True, "INVALID_TICKER_PRICE"),
        ("price", None, "INVALID_TICKER_PRICE"),
        ("time", "2026-09-09T05:56:11Z", "TICKER_VISIBILITY_INVALID"),
        ("time", "2026-09-09T05:55:00Z", "TICKER_STALE"),
        ("time", "2026-09-09T05:56:08", "INVALID_CLOCK"),
    ],
)
def test_invalid_ticker_never_falls_back_to_candle(key, value, reason):
    row, source = fixture()
    source["ticker"][key] = value
    add_crypto_research(row, source, NOW)
    assert row["forecast"] is None
    assert reason in row["forecast_diagnostic_blocker"]
    assert row["forecast_inputs"] is None


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda s: s["ticker_evidence"].update(received_at="2026-09-09T05:56:12Z"),
            "TICKER_VISIBILITY_INVALID",
        ),
        (
            lambda s: s.update(latest_closed_candle_at="2026-09-09T05:56:09Z"),
            "CANDLE_VISIBILITY_INVALID",
        ),
        (
            lambda s: s["candles_evidence"].update(received_at="2026-09-09T05:56:12Z"),
            "CANDLE_VISIBILITY_INVALID",
        ),
        (lambda s: s.update(closed_candle_count=2), "INSUFFICIENT_CANDLE_HISTORY"),
        (lambda s: s["features"].update(volatility_1h=None), "INSUFFICIENT_CANDLE_HISTORY"),
        (lambda s: s["features"].update(return_1h="NaN"), "INVALID_HISTORICAL_FEATURE"),
    ],
)
def test_unavailable_or_insufficient_candle_history_is_diagnostic_failure(mutation, reason):
    row, source = fixture()
    mutation(source)
    add_crypto_research(row, source, NOW)
    assert row["forecast"] is None
    assert reason in row["forecast_diagnostic_blocker"]
