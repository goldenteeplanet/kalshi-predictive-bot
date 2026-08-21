import importlib.util
import json
import sqlite3
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_event_multiclass_walk_forward.py"
    spec = importlib.util.spec_from_file_location("crypto_event_multiclass_walk_forward", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MULTI = _module()


def test_market_bounds_do_not_impute_missing_side_midpoint():
    result = MULTI._market_probability_bounds(
        {
            "true_bucket_index": 0,
            "buckets": [
                {"ticker": "A", "market_bid": 0.2, "market_ask": 0.3},
                {"ticker": "B", "market_bid": None, "market_ask": 0.8},
            ]
        }
    )
    assert result["bounds"][1] == {"ticker": "B", "lower": 0.0, "upper": 0.8}
    assert result["two_sided_coverage"] == 0.5
    assert result["gate_passed"] is False


def test_interval_scores_use_feasible_simplex_extrema():
    result = MULTI._interval_score_bounds(
        [
            {"ticker": "A", "lower": 0.2, "upper": 0.4},
            {"ticker": "B", "lower": 0.6, "upper": 0.8},
        ],
        true_index=0,
    )
    assert abs(result["optimistic_brier"] - 0.72) < 1e-9
    assert abs(result["pessimistic_brier"] - 1.28) < 1e-9
    assert abs(result["optimistic_log_loss"] - -MULTI.math.log(0.4)) < 1e-9
    assert abs(result["pessimistic_log_loss"] - -MULTI.math.log(0.2)) < 1e-9


def test_shadow_gate_requires_model_to_beat_market_best_case():
    count = MULTI.WF.MIN_POLICY_COHORT_N
    rows = [
        {
            "scores": {"student_t": {"brier": 0.10, "log_loss": 0.20}},
            "market_probability_bounds": {
                "gate_passed": True,
                "score_bounds": {
                    "optimistic_brier": 0.50,
                    "optimistic_log_loss": 0.80,
                },
            },
        }
        for _ in range(count)
    ]
    result = MULTI._interval_frozen_gate(rows, "student_t")
    assert result["passed"] is True
    rows[0]["scores"]["student_t"] = {"brier": 1.0, "log_loss": 2.0}
    for row in rows[1:]:
        row["scores"]["student_t"] = {"brier": 1.0, "log_loss": 2.0}
    assert MULTI._interval_frozen_gate(rows, "student_t")["passed"] is False


def test_aligned_event_uses_exact_captured_bounds_and_forecast_id():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE markets (ticker TEXT, raw_json TEXT, result TEXT);
        CREATE TABLE settlements (ticker TEXT, result TEXT);
        """
    )
    markets = [
        ("LOW", {"strike_type": "less", "cap_strike": 100}, "no"),
        (
            "MID",
            {"strike_type": "between", "floor_strike": 100, "cap_strike": 110},
            "yes",
        ),
        ("HIGH", {"strike_type": "greater", "floor_strike": 110}, "no"),
    ]
    connection.executemany(
        "INSERT INTO markets VALUES (?,?,?)",
        [(ticker, json.dumps(raw), result) for ticker, raw, result in markets],
    )
    alignment = {
        "event_ticker": "EVENT",
        "forecast_id": 7,
        "bounds": [
            {"ticker": "LOW", "lower": 0.0, "upper": 0.1, "has_bid": False, "has_ask": True},
            {"ticker": "MID", "lower": 0.7, "upper": 0.8, "has_bid": True, "has_ask": True},
            {"ticker": "HIGH", "lower": 0.0, "upper": 0.1, "has_bid": False, "has_ask": True},
        ],
    }
    events = MULTI._load_aligned_events(
        connection,
        {7: {"forecast_id": 7, "forecast_jd": 1.0}},
        [alignment],
    )
    connection.close()
    assert len(events) == 1
    assert events[0]["eligible"] is True
    assert events[0]["representative"]["forecast_id"] == 7
    assert events[0]["buckets"][0]["market_bid"] is None
    assert events[0]["buckets"][0]["market_ask"] == 0.1
