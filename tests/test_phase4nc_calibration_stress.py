from __future__ import annotations

import copy
import math

from scripts.local.phase4nc_calibration_stress import (
    apply_calibrator,
    calibration_report,
    compare_calibrators,
    fit_isotonic,
    fit_platt,
)


def _rows(count=40):
    rows = []
    for index in range(count):
        probability = (index + 1) / (count + 1)
        rows.append(
            {
                "sample_id": f"s-{index}",
                "event_group": f"g-{index // 2}",
                "probability": probability,
                "outcome": 1 if index % 3 != 0 and probability > 0.35 else 0,
                "weight": 1.0,
            }
        )
    return rows


def test_reliability_decomposition_and_metrics_are_deterministic() -> None:
    first = calibration_report(_rows(), minimum_tail_support=2)
    assert first == calibration_report(_rows(), minimum_tail_support=2)
    assert first["verdict"] == "PASS"
    assert math.isclose(first["brier_score"], first["brier_decomposition_check"], abs_tol=0.03)
    for field in (
        "log_loss",
        "expected_calibration_error",
        "maximum_calibration_error",
        "calibration_slope",
        "calibration_intercept",
    ):
        assert first[field] is not None


def test_sparse_tail_and_wide_confidence_intervals_refuse_claims() -> None:
    middle = [
        {"sample_id": str(i), "event_group": str(i), "probability": 0.5, "outcome": i % 2}
        for i in range(4)
    ]
    result = calibration_report(middle, minimum_tail_support=2, maximum_interval_width=0.2)
    assert result["calibration_claim"] == "REFUSE"
    assert "TAIL_SUPPORT_INADEQUATE" in result["claim_errors"]
    assert "CONFIDENCE_INTERVAL_TOO_WIDE" in result["claim_errors"]


def test_zero_one_probabilities_are_clipped_for_finite_log_loss() -> None:
    rows = [
        {"sample_id": "a", "event_group": "a", "probability": 0.0, "outcome": 1},
        {"sample_id": "b", "event_group": "b", "probability": 1.0, "outcome": 0},
    ]
    result = calibration_report(rows, minimum_tail_support=0, maximum_interval_width=1.0)
    assert math.isfinite(result["log_loss"])
    assert result["log_loss"] > 10


def test_correlated_duplicates_can_be_collapsed_by_group_weight() -> None:
    rows = _rows(20)
    duplicate = copy.deepcopy(rows[0])
    duplicate["sample_id"] = "duplicate"
    rows.append(duplicate)
    ordinary = calibration_report(rows, minimum_tail_support=0, maximum_interval_width=1.0)
    collapsed = calibration_report(
        rows, minimum_tail_support=0, maximum_interval_width=1.0, collapse_correlated_groups=True
    )
    assert collapsed["effective_weight"] < ordinary["effective_weight"]


def test_sample_weights_class_imbalance_and_rare_outcomes_are_supported() -> None:
    rows = _rows(20)
    for row in rows:
        row["outcome"] = 0
        row["weight"] = 2.0 if row["probability"] < 0.2 else 0.5
    rows[-1]["outcome"] = 1
    result = calibration_report(rows, minimum_tail_support=1, maximum_interval_width=1.0)
    assert result["verdict"] == "PASS"
    assert 0 <= result["uncertainty"] <= 0.25


def test_platt_and_isotonic_fits_are_training_only_and_deterministic() -> None:
    train, test = _rows(30)[:20], _rows(30)[20:]
    platt, isotonic = fit_platt(train), fit_isotonic(train)
    assert platt == fit_platt(train)
    assert isotonic == fit_isotonic(train)
    train_ids = {row["sample_id"] for row in train}
    test_ids = {row["sample_id"] for row in test}
    assert set(platt["training_sample_ids"]) == train_ids
    assert not (set(platt["training_sample_ids"]) & test_ids)
    assert all(0 <= row["probability"] <= 1 for row in apply_calibrator(test, isotonic))


def test_leaky_isotonic_control_is_labeled_and_not_accepted() -> None:
    train, test = _rows(40)[:25], _rows(40)[25:]
    comparison = compare_calibrators(train, test)
    assert comparison["leaky_control_accepted"] is False
    assert comparison["leaky_fit"]["training_sample_ids"] == [
        row["sample_id"]
        for row in sorted(test, key=lambda row: (row["probability"], row["sample_id"]))
    ]


def test_regime_shift_and_window_drift_change_out_of_sample_calibration() -> None:
    train = _rows(30)[:20]
    test = _rows(30)[20:]
    shifted = copy.deepcopy(test)
    for row in shifted:
        row["outcome"] = 1 - row["outcome"]
    fit = fit_platt(train)
    normal = calibration_report(
        apply_calibrator(test, fit), minimum_tail_support=0, maximum_interval_width=1.0
    )
    drifted = calibration_report(
        apply_calibrator(shifted, fit), minimum_tail_support=0, maximum_interval_width=1.0
    )
    assert normal["brier_score"] != drifted["brier_score"]


def test_isotonic_sparse_tail_overfit_does_not_create_support() -> None:
    train = [
        {
            "sample_id": str(i),
            "event_group": str(i),
            "probability": 0.45 + i * 0.01,
            "outcome": i % 2,
        }
        for i in range(5)
    ]
    test = [{"sample_id": "tail", "event_group": "tail", "probability": 0.99, "outcome": 0}]
    calibrated = apply_calibrator(test, fit_isotonic(train))
    result = calibration_report(calibrated, minimum_tail_support=3, maximum_interval_width=1.0)
    assert result["calibration_claim"] == "REFUSE"
    assert "TAIL_SUPPORT_INADEQUATE" in result["claim_errors"]


def test_calibration_has_no_persistence_network_or_execution_capability() -> None:
    safety = calibration_report(_rows(), minimum_tail_support=0, maximum_interval_width=1.0)[
        "safety"
    ]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
