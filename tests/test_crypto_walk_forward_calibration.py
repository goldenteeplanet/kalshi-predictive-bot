from __future__ import annotations

import importlib.util
import json
import math
import sqlite3
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_distribution_walk_forward.py"
    spec = importlib.util.spec_from_file_location("crypto_walk_forward", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _harvester_module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_exact_settlement_harvest.py"
    spec = importlib.util.spec_from_file_location("crypto_exact_harvester", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event_module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_event_multiclass_walk_forward.py"
    spec = importlib.util.spec_from_file_location("crypto_event_walk_forward", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_horizon_and_probability_bands_are_stable() -> None:
    module = _module()
    assert module._horizon_band(60) == "00-01h"
    assert module._horizon_band(61) == "01-06h"
    assert module._horizon_band(1441) == "01-03d"
    assert module._probability_band(0.049) == "00-05%"
    assert module._probability_band(0.50) == "40-60%"
    assert module._probability_band(0.95) == "95-100%"


def test_calibration_history_falls_back_without_leaking_future_rows() -> None:
    module = _module()
    history = [
        {"symbol": "BTC", "horizon_band": "00-01h", "outcome": 1.0}
        for _ in range(module.MIN_SEGMENT_TRAIN)
    ]
    selected, level = module._calibration_history(
        history, {"symbol": "BTC", "horizon_band": "00-01h"}
    )
    assert selected == history
    assert level == "SYMBOL_HORIZON"

    selected, level = module._calibration_history(
        history, {"symbol": "ETH", "horizon_band": "00-01h"}
    )
    assert selected == history
    assert level == "GLOBAL"


def test_restricted_shadow_policy_blocks_small_cohort() -> None:
    module = _module()
    row = {
        "symbol": "ETH",
        "market_implied": 0.40,
        "distribution": 0.70,
        "yes_ask": 0.50,
        "no_ask": 0.51,
        "outcome": 1.0,
        "calibration_confidence": {"passed": True},
    }

    result = module._restricted_shadow_policy([row])

    assert result["cohort_n"] == 1
    assert result["accepted_trades"] == 0
    assert result["rejection_counts"]["COHORT_N_BELOW_MINIMUM"] == 1
    assert result["continuous_shadow_enabled"] is False


def test_calibration_confidence_requires_sample_and_advantage() -> None:
    module = _module()
    history = [
        {
            "features": {"price": 100, "volatility_1h": 0.001, "return_1h": 0},
            "horizon_minutes": 60,
            "comparator": "ABOVE",
            "threshold": 100,
            "lower": None,
            "upper": None,
            "outcome": 1.0,
            "market_implied": 0.5,
        }
    ]

    confidence = module._calibration_confidence(history, 1.0)

    assert confidence["passed"] is False
    assert confidence["checks"]["train_n"] is False


def test_targeted_harvester_selects_only_eth_mid_probability(tmp_path: Path) -> None:
    module = _harvester_module()
    database = tmp_path / "history.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE crypto_market_links (ticker TEXT);
        CREATE TABLE forecasts (
          id INTEGER, ticker TEXT, forecasted_at TEXT, feature_json TEXT,
          market_mid_probability TEXT, model_name TEXT
        );
        CREATE TABLE settlements (ticker TEXT);
        CREATE TABLE crypto_features (id INTEGER, symbol TEXT);
        CREATE TABLE markets (
          ticker TEXT, settlement_ts TEXT, expiration_time TEXT, close_time TEXT
        );
        """
    )
    for index, (ticker, symbol, probability) in enumerate(
        [("ETH-MID", "ETH", 0.50), ("ETH-TAIL", "ETH", 0.10), ("BTC-MID", "BTC", 0.50)],
        start=1,
    ):
        connection.execute("INSERT INTO crypto_market_links VALUES (?)", (ticker,))
        connection.execute("INSERT INTO crypto_features VALUES (?,?)", (index, symbol))
        connection.execute(
            "INSERT INTO forecasts VALUES (?,?,?,?,?,?)",
            (
                index,
                ticker,
                "2025-01-01T00:00:00Z",
                json.dumps({"crypto_feature_id": index}),
                str(probability),
                "crypto_v2",
            ),
        )
        connection.execute(
            "INSERT INTO markets VALUES (?,?,?,?)",
            (ticker, "2025-01-02T00:00:00Z", None, None),
        )
    connection.commit()
    connection.close()

    rows = module._candidates(
        database,
        set(),
        10,
        symbol="ETH",
        min_market_probability=0.20,
        max_market_probability=0.80,
    )

    assert [row["ticker"] for row in rows] == ["ETH-MID"]


def test_settled_history_excludes_rows_unavailable_at_forecast_time() -> None:
    module = _module()
    history = [
        {"ticker": "settled", "settlement_jd": 10.0},
        {"ticker": "future", "settlement_jd": 12.0},
    ]

    selected = module._settled_history(history, {"forecast_jd": 11.0})

    assert [row["ticker"] for row in selected] == ["settled"]


def test_regularized_volatility_scale_shrinks_toward_one() -> None:
    module = _module()
    history = [
        {
            "realized_log_move": 0.02,
            "forecast_drift": 0.0,
            "forecast_sigma": 0.01,
        }
        for _ in range(30)
    ]

    scale, evidence = module._regularized_volatility_scale(history)

    assert evidence["raw_scale"] == 2.0
    assert 1.0 < scale < 2.0
    assert evidence["realized_move_n"] == 30


def test_contract_audit_reconstructs_exact_range_result() -> None:
    module = _module()
    row = {
        "ticker": "KXETH-TEST-B1800",
        "outcome": 1.0,
        "terminal_spot": 1801.5,
        "market_raw_json": json.dumps(
            {
                "floor_strike": 1800,
                "cap_strike": 1819.99,
                "expiration_value": "1805.25",
                "rules_primary": (
                    "If the CF Benchmarks Ethereum Real-Time Index is between "
                    "1,800-1,819.99, the market resolves Yes."
                ),
            }
        ),
    }

    audit = module._contract_audit(row)

    assert audit["eligible"] is True
    assert audit["kalshi_terminal_reference"] == 1805.25
    assert audit["harvested_spot_basis_difference"] == -3.75


def test_contract_audit_excludes_result_mismatch() -> None:
    module = _module()
    row = {
        "ticker": "KXETH-TEST-B1800",
        "outcome": 1.0,
        "terminal_spot": None,
        "market_raw_json": json.dumps(
            {
                "floor_strike": 1800,
                "cap_strike": 1819.99,
                "expiration_value": "1900",
                "rules_primary": (
                    "If the CF Benchmarks Ethereum Real-Time Index is between "
                    "1800-1819.99, the market resolves Yes."
                ),
            }
        ),
    }

    audit = module._contract_audit(row)

    assert audit["eligible"] is False
    assert "TERMINAL_REFERENCE_RESULT_MISMATCH" in audit["exclusion_reasons"]


def test_student_t_cdf_is_symmetric_and_has_heavier_tail() -> None:
    module = _module()

    assert module._student_t_cdf(0.0, 5.0) == 0.5
    assert abs(
        module._student_t_cdf(-2.0, 5.0)
        - (1.0 - module._student_t_cdf(2.0, 5.0))
    ) < 1e-12
    assert module._student_t_cdf(-3.0, 5.0) > 0.001


def test_student_df_is_regularized_toward_prior() -> None:
    module = _module()
    history = [
        {
            "realized_log_move": value,
            "forecast_drift": 0.0,
            "forecast_sigma": 1.0,
        }
        for value in (-6.0, -1.0, 0.0, 1.0, 6.0)
    ]

    degrees_of_freedom, evidence = module._regularized_student_df(history)

    assert module.MIN_STUDENT_DF <= degrees_of_freedom <= module.MAX_STUDENT_DF
    assert evidence["residual_n"] == 5
    assert evidence["shrinkage_weight"] < 0.2


def test_location_bias_is_shrunk_and_bounded() -> None:
    module = _module()
    history = [
        {
            "realized_log_move": -0.02,
            "forecast_drift": 0.0,
            "forecast_sigma": 0.01,
        }
        for _ in range(30)
    ]

    bias, evidence = module._regularized_location_bias(history)

    assert bias == -1.0
    assert evidence["raw_bias_z"] == -2.0
    assert evidence["shrinkage_weight"] == 0.5


def test_gaussian_mixture_learns_asymmetric_components() -> None:
    module = _module()
    history = [
        {
            "realized_log_move": value,
            "forecast_drift": 0.0,
            "forecast_sigma": 1.0,
        }
        for value in (-3.0, -2.5, -2.0, -0.2, 0.0, 0.1, 0.2, 0.3) * 5
    ]

    parameters = module._regularized_gaussian_mixture(history, 0.0)

    assert parameters["mean_1"] < parameters["mean_2"]
    assert parameters["weight"] < 0.5
    assert parameters["residual_n"] == 40


def test_event_bucket_coverage_requires_both_tails_and_contiguity() -> None:
    module = _event_module()
    buckets = [
        {"lower": None, "upper": 100.0},
        {"lower": 100.0, "upper": 109.99},
        {"lower": 110.0, "upper": 119.99},
        {"lower": 119.99, "upper": None},
    ]

    assert module._coverage_reasons(buckets) == []
    assert "LOWER_TAIL_MISSING" in module._coverage_reasons(buckets[1:])


def test_multiclass_scores_use_full_distribution() -> None:
    module = _event_module()

    scores = module._multiclass_scores([0.1, 0.7, 0.2], 1)

    assert abs(scores["brier"] - 0.14) < 1e-12
    assert abs(scores["log_loss"] + math.log(0.7)) < 1e-12
