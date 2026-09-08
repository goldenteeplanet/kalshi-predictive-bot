from __future__ import annotations

import copy

from scripts.local.phase4na_purged_walk_forward import (
    audit_fold_leakage,
    build_purged_folds,
    compare_ordinary_to_purged,
    evaluate_walk_forward,
)


def _samples():
    rows = []
    outcomes = [0, 1, 0, 1, 0, 1, 0, 1]
    for index, outcome in enumerate(outcomes):
        day = index + 1
        rows.append(
            {
                "sample_id": f"s-{index}",
                "event_group": f"event-{index // 2}",
                "label_start": f"2026-07-{day:02d}T00:00:00Z",
                "label_end": f"2026-07-{day:02d}T12:00:00Z",
                "available_at": f"2026-07-{day:02d}T13:00:00Z",
                "feature_value": 0.48 + (index % 3) * 0.01,
                "revision_value": float(outcome),
                "outcome": outcome,
            }
        )
    return rows


def test_expanding_folds_are_deterministic_purged_and_embargoed() -> None:
    first = build_purged_folds(_samples(), embargo_seconds=3600)
    assert first == build_purged_folds(_samples(), embargo_seconds=3600)
    assert first["verdict"] == "PASS"
    assert audit_fold_leakage(first["folds"])["verdict"] == "PASS"
    for fold in first["folds"]:
        assert not (
            {row["event_group"] for row in fold["train"]}
            & {row["event_group"] for row in fold["test"]}
        )


def test_overlapping_labels_and_adjacent_embargo_rows_are_purged() -> None:
    rows = _samples()
    rows[2]["label_end"] = rows[4]["label_end"]
    result = build_purged_folds(rows, embargo_seconds=86400)
    target = next(fold for fold in result["folds"] if fold["test_event_group"] == "event-2")
    assert "s-2" not in target["train_sample_ids"]
    assert all(row["available_at"] <= target["embargo_cutoff"] for row in target["train"])


def test_duplicate_events_stay_together_and_duplicate_ids_refuse() -> None:
    result = build_purged_folds(_samples(), embargo_seconds=0)
    assert all(len(fold["test"]) == 2 for fold in result["folds"])
    rows = _samples()
    rows[1]["sample_id"] = rows[0]["sample_id"]
    assert build_purged_folds(rows, embargo_seconds=0)["verdict"] == "REFUSE"


def test_revision_and_late_settlement_availability_cannot_enter_training() -> None:
    rows = _samples()
    rows[1]["available_at"] = "2026-08-01T00:00:00Z"
    result = build_purged_folds(rows, embargo_seconds=0)
    for fold in result["folds"]:
        if fold["test_start"] < "2026-08-01T00:00:00+00:00":
            assert "s-1" not in fold["train_sample_ids"]


def test_rolling_window_limits_training_and_expanding_does_not() -> None:
    expanding = build_purged_folds(_samples(), embargo_seconds=0, minimum_train_size=1)
    rolling = build_purged_folds(
        _samples(), embargo_seconds=0, mode="rolling", rolling_train_limit=2, minimum_train_size=1
    )
    assert max(len(fold["train"]) for fold in rolling["folds"]) <= 2
    assert max(len(fold["train"]) for fold in expanding["folds"]) > 2


def test_sparse_folds_refuse_evaluation_without_fabricating_samples() -> None:
    folds = build_purged_folds(_samples()[:2], embargo_seconds=0, minimum_train_size=2)
    assert folds["sparse_fold_count"] == 1
    evaluation = evaluate_walk_forward(folds["folds"])
    assert evaluation["prediction_count"] == 0


def test_shuffled_input_has_same_fold_identity_and_class_imbalance_is_supported() -> None:
    rows = _samples()
    first = build_purged_folds(rows, embargo_seconds=0)
    second = build_purged_folds(list(reversed(rows)), embargo_seconds=0)
    assert first["fold_set_sha256"] == second["fold_set_sha256"]
    imbalanced = copy.deepcopy(rows)
    for row in imbalanced[:-1]:
        row["outcome"] = 0
    evaluation = evaluate_walk_forward(build_purged_folds(imbalanced, embargo_seconds=0)["folds"])
    assert evaluation["verdict"] == "PASS"


def test_training_statistics_are_fold_local_not_global() -> None:
    folds = build_purged_folds(_samples(), embargo_seconds=0, minimum_train_size=1)
    evaluation = evaluate_walk_forward(folds["folds"])
    means = {row["fold_sha256"]: row["train_mean"] for row in evaluation["predictions"]}
    assert len(means) >= 2
    assert len(set(means.values())) >= 2


def test_deliberate_future_revision_control_quantifies_leakage_inflation() -> None:
    comparison = compare_ordinary_to_purged(_samples(), embargo_seconds=0)
    assert comparison["ordinary"]["brier_score"] < comparison["purged"]["brier_score"]
    assert comparison["brier_inflation_from_leakage"] > 0
    assert comparison["ordinary_uses_future_revision_control"] is True


def test_injected_fold_leakage_is_detected() -> None:
    result = build_purged_folds(_samples(), embargo_seconds=0)
    fold = next(fold for fold in result["folds"] if fold["train"])
    fold["train"][0]["available_at"] = "2026-12-01T00:00:00Z"
    assert audit_fold_leakage(result["folds"])["verdict"] == "REFUSE"


def test_splitter_has_no_persistence_network_or_execution_capability() -> None:
    safety = build_purged_folds(_samples(), embargo_seconds=0)["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
