import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_forecast_polytope_alignment.py"
    spec = importlib.util.spec_from_file_location("crypto_forecast_polytope_alignment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coverage(now):
    return {
        "id": 7,
        "event_ticker": "EVENT",
        "family": "KXETH|buckets:LE25",
        "captured_at": now.isoformat(),
        "coherence_ms": 100,
        "bounds_json": json.dumps(
            {
                "buckets": [
                    {"ticker": "A", "lower": 0.30, "upper": 0.35},
                    {"ticker": "B", "lower": 0.30, "upper": 0.35},
                    {"ticker": "C", "lower": 0.30, "upper": 0.40},
                ]
            }
        ),
    }


def test_exact_capture_alignment_accepts_bounded_prior_forecast():
    module = _module()
    now = datetime.now(UTC)
    result = module.build_alignment(
        _coverage(now),
        {
            "id": 11,
            "ticker": "A",
            "model_name": "crypto_v2",
            "forecasted_at": (now - timedelta(minutes=10)).isoformat(),
            "yes_probability": "0.31",
            "market_mid_probability": "0.32",
            "feature_json": json.dumps({"crypto_feature_id": 9}),
        },
        max_lag_minutes=30,
        max_coherence_ms=2500,
    )
    assert result["aligned"] is True
    assert result["forecast_capture_lag_seconds"] == 600
    assert result["coverage_id"] == 7
    assert len(result["bounds_sha256"]) == 64


def test_stale_forecast_is_rejected_before_cohort_accumulation():
    module = _module()
    now = datetime.now(UTC)
    result = module.build_alignment(
        _coverage(now),
        {
            "id": 11,
            "ticker": "A",
            "model_name": "crypto_v2",
            "forecasted_at": (now - timedelta(minutes=31)).isoformat(),
            "yes_probability": "0.31",
            "market_mid_probability": "0.32",
            "feature_json": "{}",
        },
        max_lag_minutes=30,
        max_coherence_ms=2500,
    )
    assert result["aligned"] is False
    assert "FORECAST_CAPTURE_LAG_EXCEEDED" in result["rejection_reasons"]
