from __future__ import annotations

import copy
from decimal import Decimal

from scripts.local.phase4mz_adversarial_backtest import (
    SCENARIOS,
    run_adversarial_backtest,
    run_scenario_matrix,
)


def _records():
    return [
        {
            "ticker": "A",
            "market_group": "rain",
            "decision_time": "2026-08-01T12:00:00Z",
            "feature_times": ["2026-08-01T11:00:00Z"],
            "quote_time": "2026-08-01T11:59:00Z",
            "settlement_time": "2026-08-02T00:00:00Z",
            "yes_probability": "0.75",
            "yes_ask": "0.55",
            "no_ask": "0.47",
            "outcome": "yes",
            "quantity": 1,
        },
        {
            "ticker": "B",
            "market_group": "crypto",
            "decision_time": "2026-08-02T12:00:00Z",
            "feature_times": ["2026-08-02T11:30:00Z"],
            "quote_time": "2026-08-02T11:59:00Z",
            "settlement_time": "2026-08-03T00:00:00Z",
            "yes_probability": "0.30",
            "yes_ask": "0.40",
            "no_ask": "0.55",
            "outcome": "no",
            "quantity": 1,
        },
        {
            "ticker": "C",
            "market_group": "rain",
            "decision_time": "2026-08-03T12:00:00Z",
            "feature_times": ["2026-08-03T11:00:00Z"],
            "quote_time": "2026-08-03T11:59:00Z",
            "settlement_time": "2026-08-04T00:00:00Z",
            "yes_probability": "0.80",
            "yes_ask": "0.60",
            "no_ask": "0.42",
            "outcome": "no",
            "quantity": 1,
        },
        {
            "ticker": "D",
            "market_group": "crypto",
            "decision_time": "2026-08-04T12:00:00Z",
            "feature_times": ["2026-08-04T11:00:00Z"],
            "quote_time": "2026-08-04T11:59:00Z",
            "settlement_time": "2026-08-05T00:00:00Z",
            "yes_probability": "0.25",
            "yes_ask": "0.38",
            "no_ask": "0.58",
            "outcome": "yes",
            "quantity": 1,
        },
    ]


def test_baseline_is_deterministic_read_only_and_reports_required_metrics() -> None:
    rows = _records()
    original = copy.deepcopy(rows)
    first = run_adversarial_backtest(rows, scenario="BASELINE")
    assert first == run_adversarial_backtest(rows, scenario="BASELINE")
    assert rows == original
    assert first["verdict"] == "PASS"
    for field in (
        "gross_pnl",
        "net_pnl",
        "max_drawdown",
        "calibration_brier",
        "turnover",
        "fill_rate",
        "exposure",
        "leakage_violations",
    ):
        assert field in first


def test_future_feature_and_timestamp_shift_fail_leakage_firewall() -> None:
    for scenario in ("FUTURE_DATA_INJECTION", "TIMESTAMP_SHIFT"):
        result = run_adversarial_backtest(_records(), scenario=scenario)
        assert result["verdict"] == "REFUSE"
        assert "INFORMATION_LEAKAGE_DETECTED" in result["errors"]
        assert result["leakage_violations"]


def test_direct_future_quote_and_invalid_settlement_boundary_refuse() -> None:
    rows = _records()
    rows[0]["quote_time"] = "2026-08-01T12:00:01Z"
    rows[1]["settlement_time"] = rows[1]["decision_time"]
    result = run_adversarial_backtest(rows, scenario="BASELINE")
    assert result["verdict"] == "REFUSE"
    violations = " ".join(
        item for row in result["leakage_violations"] for item in row["violations"]
    )
    assert "quote_AFTER_DECISION" in violations
    assert "SETTLEMENT_NOT_AFTER_DECISION" in violations


def test_stale_and_missing_feeds_produce_explicit_failure_reasons() -> None:
    stale = run_adversarial_backtest(_records(), scenario="STALE_QUOTES")
    assert stale["trade_count"] == 0
    assert all("STALE_QUOTE" in error for error in stale["errors"])
    missing = run_adversarial_backtest(_records(), scenario="MISSING_FEED")
    assert any("MISSING_FEED" in error for error in missing["errors"])


def test_execution_stresses_reduce_fill_or_economics() -> None:
    baseline = run_adversarial_backtest(_records(), scenario="BASELINE")
    partial = run_adversarial_backtest(_records(), scenario="PARTIAL_FILLS")
    rejected = run_adversarial_backtest(_records(), scenario="REJECTED_FILLS")
    queue = run_adversarial_backtest(_records(), scenario="QUEUE_POSITION")
    latency = run_adversarial_backtest(_records(), scenario="LATENCY")
    spread = run_adversarial_backtest(_records(), scenario="WIDENED_SPREAD")
    assert Decimal(partial["exposure"]) < Decimal(baseline["exposure"])
    assert rejected["fill_rate"] == 0
    assert queue["fill_rate"] < baseline["fill_rate"]
    assert Decimal(latency["net_pnl"]) <= Decimal(baseline["net_pnl"])
    assert Decimal(spread["net_pnl"]) <= Decimal(baseline["net_pnl"])


def test_outcome_regime_correlation_and_unprofitable_controls_are_adversarial() -> None:
    baseline = run_adversarial_backtest(_records(), scenario="BASELINE")
    reports = [
        run_adversarial_backtest(_records(), scenario=name)
        for name in (
            "SHUFFLED_OUTCOMES",
            "EXTREME_MOVE",
            "CORRELATED_MARKETS",
            "REGIME_CHANGE",
            "UNPROFITABLE_CONTROL",
        )
    ]
    assert any(report["net_pnl"] != baseline["net_pnl"] for report in reports)
    assert Decimal(reports[-1]["net_pnl"]) < 0


def test_parameter_perturbation_and_fees_are_visible() -> None:
    baseline = run_adversarial_backtest(_records(), scenario="BASELINE")
    perturbed = run_adversarial_backtest(_records(), scenario="PARAMETER_PERTURBATION")
    fees = run_adversarial_backtest(_records(), scenario="FEES", fee_per_contract="0.10")
    assert perturbed["trade_count"] <= baseline["trade_count"]
    assert Decimal(fees["net_pnl"]) < Decimal(fees["gross_pnl"])


def test_scenario_matrix_exercises_every_named_attack_deterministically() -> None:
    first = run_scenario_matrix(_records())
    assert first == run_scenario_matrix(_records())
    assert first["scenario_count"] == len(SCENARIOS) == 17
    assert first["all_scenarios_exercised"] is True


def test_harness_has_no_database_network_order_or_execution_capability() -> None:
    safety = run_adversarial_backtest(_records(), scenario="BASELINE")["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
