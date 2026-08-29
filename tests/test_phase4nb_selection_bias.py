from __future__ import annotations

import copy

from scripts.local.phase4nb_selection_bias import audit_attempts, validate_nested_selection


def _attempt(index, p, *, status="PASSED", stage="INNER_SELECTION", holdout=None, **overrides):
    row = {
        "attempt_id": f"attempt-{index}",
        "strategy": f"strategy-{index}",
        "parameter_sha256": f"{index % 10}" * 64,
        "feature_family": "weather",
        "correlation_family": f"family-{index // 2}",
        "threshold": "0.05",
        "market_subset": "all",
        "evaluation_id": f"eval-{index}",
        "status": status,
        "metric": "net_pnl",
        "seed": index,
        "p_value": p,
        "effect": 0.12,
        "standard_error": 0.05,
        "holdout_id": holdout,
        "stage": stage,
    }
    row.update(overrides)
    return row


def test_all_corrections_are_deterministic_and_stricter_than_raw() -> None:
    attempts = [_attempt(i, p) for i, p in enumerate((0.01, 0.03, 0.20, 0.50))]
    first = audit_attempts(attempts, declared_attempt_count=4)
    assert first == audit_attempts(attempts, declared_attempt_count=4)
    assert first["verdict"] == "PASS"
    best = first["corrections"][0]
    assert best["raw_significant"] is True
    assert best["bonferroni_p_value"] >= best["raw_p_value"]
    assert "holm_p_value" in best and "benjamini_hochberg_q_value" in best


def test_apparent_edge_that_fails_deflation_refuses_readiness() -> None:
    attempts = [_attempt(i, 0.01 + i * 0.01, effect=0.03, standard_error=0.05) for i in range(5)]
    result = audit_attempts(attempts, declared_attempt_count=5)
    assert result["readiness"] == "REFUSE"
    assert not any(row["survives_all_corrections"] for row in result["corrections"])


def test_failed_abandoned_seed_subset_and_grid_trials_all_count() -> None:
    attempts = [
        _attempt(0, 0.01),
        _attempt(1, 0.8, status="FAILED", market_subset="rain"),
        _attempt(2, 0.9, status="ABANDONED", threshold="0.10"),
    ]
    result = audit_attempts(attempts, declared_attempt_count=3)
    assert result["failed_or_abandoned_count"] == 2
    assert result["seed_count"] == 3
    assert result["market_subset_count"] == 2
    assert len(result["corrections"]) == 3


def test_hidden_abandoned_attempts_and_duplicate_strategies_refuse() -> None:
    attempts = [_attempt(0, 0.01), _attempt(1, 0.02)]
    assert (
        "HIDDEN_OR_MISSING_ATTEMPTS" in audit_attempts(attempts, declared_attempt_count=3)["errors"]
    )
    duplicate = copy.deepcopy(attempts[0])
    duplicate["attempt_id"] = "other-id"
    duplicate["evaluation_id"] = "other-eval"
    result = audit_attempts([attempts[0], duplicate], declared_attempt_count=2)
    assert "DUPLICATE_STRATEGY_ATTEMPT" in " ".join(result["errors"])


def test_post_hoc_metric_switching_refuses() -> None:
    first = _attempt(0, 0.1)
    second = _attempt(
        1,
        0.01,
        strategy=first["strategy"],
        parameter_sha256=first["parameter_sha256"],
        metric="brier",
    )
    result = audit_attempts([first, second], declared_attempt_count=2)
    assert "POST_HOC_METRIC_SWITCHING" in result["errors"]


def test_correlated_variants_remain_individual_trials() -> None:
    attempts = [_attempt(i, 0.02 + i * 0.01, correlation_family="shared") for i in range(4)]
    result = audit_attempts(attempts, declared_attempt_count=4)
    assert result["correlation_family_count"] == 1
    assert len(result["corrections"]) == 4


def test_nested_selection_accepts_one_untouched_matching_holdout() -> None:
    selected = _attempt(0, 0.01)
    final = _attempt(
        99,
        0.04,
        stage="FINAL_HOLDOUT",
        holdout="final-1",
        strategy=selected["strategy"],
        parameter_sha256=selected["parameter_sha256"],
        feature_family=selected["feature_family"],
        threshold=selected["threshold"],
        market_subset=selected["market_subset"],
    )
    result = validate_nested_selection(
        [selected, final], selected_attempt_id="attempt-0", final_holdout_id="final-1"
    )
    assert result["verdict"] == "PASS"
    assert result["holdout_accepted"] is True


def test_holdout_peeking_reuse_and_strategy_substitution_refuse() -> None:
    selected = _attempt(0, 0.01, holdout="final-1")
    final = _attempt(99, 0.04, stage="FINAL_HOLDOUT", holdout="final-1", strategy="substitute")
    result = validate_nested_selection(
        [selected, final], selected_attempt_id="attempt-0", final_holdout_id="final-1"
    )
    assert "HOLDOUT_PEEK_OR_REUSE" in result["errors"]
    assert "FINAL_STRATEGY_SUBSTITUTION" in result["errors"]
    duplicate = copy.deepcopy(final)
    duplicate["attempt_id"] = "attempt-100"
    assert (
        "FINAL_HOLDOUT_MUST_RUN_EXACTLY_ONCE"
        in validate_nested_selection(
            [_attempt(0, 0.01), final, duplicate],
            selected_attempt_id="attempt-0",
            final_holdout_id="final-1",
        )["errors"]
    )


def test_cherry_picked_winner_loses_significance_after_grid_expansion() -> None:
    attempts = [_attempt(0, 0.01)] + [_attempt(i, 0.5, status="FAILED") for i in range(1, 20)]
    result = audit_attempts(attempts, declared_attempt_count=20)
    winner = next(row for row in result["corrections"] if row["attempt_id"] == "attempt-0")
    assert winner["raw_significant"] is True
    assert winner["bonferroni_p_value"] == 0.2
    assert winner["survives_all_corrections"] is False


def test_audit_has_no_persistence_network_or_execution_capability() -> None:
    safety = audit_attempts([_attempt(0, 0.01)], declared_attempt_count=1)["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
