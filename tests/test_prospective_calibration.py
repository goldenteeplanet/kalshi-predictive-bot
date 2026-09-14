import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.crypto.prospective_calibration import (
    METHODS,
    calibration_dataset,
    finalize_decision,
    prospective_decision,
)

NOW = datetime(2026, 9, 13, tzinfo=UTC)


def encoded(value):
    return json.dumps(value).encode()


def decision(validation_criteria=None, **changes):
    plan = encoded(
        dict(
            schema="prospective-calibration-protocol-v1",
            declared_at=(NOW - timedelta(hours=1)).isoformat(),
            window_start=NOW.isoformat(),
            window_end=(NOW + timedelta(days=1)).isoformat(),
            model="independent-v1",
            segment="BTC:1h",
            methods=list(METHODS),
            selection_criterion="HELD_OUT_COVERAGE_STABILITY_PROSPECTIVE_VALIDITY",
            validation_criteria=validation_criteria,
        )
    )
    row = dict(
        schema="prospective-calibration-decision-v1",
        protocol_sha256=hashlib.sha256(plan).hexdigest(),
        decision_time=NOW.isoformat(),
        target_at=(NOW + timedelta(hours=1)).isoformat(),
        model="independent-v1",
        segment="BTC:1h",
        ticker="BTC-A",
        event="BTC-E",
        asset="BTC",
        settlement_event="BTC-E",
        rule_version="partial-v1",
        source_sha256="a" * 64,
        book_sha256="b" * 64,
        market_sha256="c" * 64,
        source_window_start=(NOW - timedelta(hours=1)).isoformat(),
        source_window_end=NOW.isoformat(),
        p_yes="0.7",
        executable_price="0.5",
        fee="0.02",
        snapshot_impact="0",
    )
    row.update(changes)
    return prospective_decision(encoded(row), protocol=plan, recorded_at=NOW)


def official(row, **changes):
    market = dict(
        ticker=row["ticker"],
        event_ticker=row["event"],
        status="finalized",
        result="yes",
        settlement_value_dollars="1.00",
        settlement_ts=(NOW + timedelta(hours=2)).isoformat(),
        close_time=row["target_at"],
    )
    market.update(changes)
    raw = encoded(dict(market=market))
    receipt = encoded(
        dict(
            url="https://api.elections.kalshi.com/trade-api/v2/markets/" + row["ticker"],
            method="GET",
            http_status=200,
            original_complete=True,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            requested_at=(NOW + timedelta(hours=2)).isoformat(),
            received_at=(NOW + timedelta(hours=2)).isoformat(),
        )
    )
    return finalize_decision(
        row, official_original=raw, official_receipt=receipt, as_of=NOW + timedelta(hours=3)
    )


def test_prospective_then_final_scores_without_probability_refit():
    row = decision()
    evaluation = official(row)
    report = calibration_dataset([row], [evaluation])
    assert evaluation["residual"] == "-0.3"
    assert evaluation["brier"] == "0.09"
    assert report["settled_decision_n"] == 1
    assert report["uncertainty_status"] == "UNCERTAINTY_PRELIMINARY"
    assert report["independent_cluster_n"] is None
    assert report["paper_eligible"] is False
    assert report["calibrated_uncertainty"] is None


def test_pretarget_commit_is_mandatory():
    with pytest.raises(ValueError, match="PRETARGET"):
        decision(target_at=NOW.isoformat())


@pytest.mark.parametrize("status", ["determined", "closed", "settled"])
def test_only_finalized_is_scored(status):
    with pytest.raises(ValueError, match="STRICT_OFFICIAL_FINAL"):
        official(decision(), status=status)


def test_payout_mismatch_not_scored():
    with pytest.raises(ValueError, match="EXACT_BINARY_PAYOUT"):
        official(decision(), settlement_value_dollars="0")


def test_related_strikes_and_shared_source_transitively_group():
    first = decision()
    second = decision(ticker="BTC-B", source_sha256="d" * 64)
    third = decision(
        ticker="ETH-C",
        event="ETH-E",
        settlement_event="ETH-E",
        asset="ETH",
        target_at=(NOW + timedelta(hours=3)).isoformat(),
        source_sha256="d" * 64,
    )
    result = calibration_dataset([first, second, third], [])
    assert result["contract_n"] == 3
    assert result["event_n"] == 2
    assert result["dependency_cluster_n"] == 1
    assert result["independent_cluster_n"] is None


def test_overlapping_asset_windows_group_even_if_hashes_differ():
    first = decision()
    second = decision(
        ticker="BTC-B",
        event="BTC-E2",
        settlement_event="BTC-E2",
        source_sha256="d" * 64,
        target_at=(NOW + timedelta(hours=2)).isoformat(),
    )
    assert calibration_dataset([first, second], [])["dependency_cluster_n"] == 1


def test_duplicates_and_tampered_scores_fail():
    row = decision()
    evaluated = official(row)
    with pytest.raises(ValueError, match="UNIQUE_BOUND"):
        calibration_dataset([row], [evaluated, evaluated])
    evaluated["brier"] = "0"
    with pytest.raises(ValueError, match="SCORE_MISMATCH"):
        calibration_dataset([row], [evaluated])


def test_missing_costs_remain_unknown_and_do_not_block_research_record():
    row = decision(fee=None, snapshot_impact=None)
    assert row["fee"] is None
    assert not row["paper_eligible"]
    report = calibration_dataset([row], [])
    assert all(item["reserve"] is None for item in report["comparisons"])
    assert report["fallback"] == "WORST_CASE_SUPPORT_BOUND:BINARY_SUPPORT_V1"


def criteria():
    return dict(
        split_at=(NOW + timedelta(minutes=75)).isoformat(),
        quantile="0.9",
        nominal_coverage="0.95",
        max_coverage_gap="0.02",
        max_stability_delta="0.1",
        min_independent_clusters=100,
        bootstrap_seed=17,
        bootstrap_draws=32,
    )


def test_five_descriptive_comparisons_do_not_certify_reserve():
    plan = criteria()
    first = decision(validation_criteria=plan, ensemble_probabilities=["0.6", "0.8"])
    second = decision(
        validation_criteria=plan,
        ticker="BTC-B",
        event="BTC-E2",
        settlement_event="BTC-E2",
        source_sha256="d" * 64,
        target_at=(NOW + timedelta(minutes=90)).isoformat(),
        source_window_start=(NOW - timedelta(hours=3)).isoformat(),
        source_window_end=(NOW - timedelta(hours=2)).isoformat(),
    )
    report = calibration_dataset([first, second], [official(first), official(second)])
    comparison = report["descriptive_comparison"][0]
    assert comparison["training_cluster_n"] == 1
    assert comparison["holdout_cluster_n"] == 1
    assert len(comparison["methods"]) == 5
    assert [m["descriptive_statistic"] for m in comparison["methods"]] == [
        "0",
        "0",
        "0.3",
        "0.2",
        "0.3",
    ]
    assert all(m["reserve"] is None for m in comparison["methods"])
    assert comparison["minimum_independent_support_pass"] is None
    assert comparison["leading_method"] is None


def test_missing_ensemble_not_manufactured():
    row = decision(validation_criteria=criteria())
    comparison = calibration_dataset([row], [official(row)])["descriptive_comparison"][0]
    assert comparison["methods"][3]["descriptive_statistic"] is None
    assert comparison["methods"][4]["descriptive_statistic"] is None


def test_dependency_cluster_crossing_split_is_not_in_training_or_holdout():
    first = decision(validation_criteria=criteria())
    second = decision(
        validation_criteria=criteria(),
        ticker="BTC-B",
        target_at=(NOW + timedelta(minutes=90)).isoformat(),
    )
    comparison = calibration_dataset([first, second], [official(first), official(second)])[
        "descriptive_comparison"
    ][0]
    assert comparison["crossing_cluster_n"] == 1
    assert comparison["training_cluster_n"] == comparison["holdout_cluster_n"] == 0


def test_unbounded_bootstrap_protocol_rejected():
    plan = criteria()
    plan["bootstrap_draws"] = 100000
    with pytest.raises(ValueError, match="BOUNDED_PREDECLARED"):
        decision(validation_criteria=plan)
