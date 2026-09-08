"""Synthetic paired originals exercise actual scoring and leakage/cohort checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_overnight_provenance import artifact

from kalshi_predictor.data_sources.tournament import PairedForecast, evaluate_tournament
from kalshi_predictor.overnight_paper.provenance import canonical_hash


def at(day, hour=1):
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def model():
    return artifact(
        dict(
            name="synthetic",
            version="1",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            frozen_at=at(1).isoformat(),
        )
    )


def policy(**changes):
    row = dict(
        kind="paired-source-policy-v1",
        source_id="source-trial",
        model_artifact_sha256=model().sha256,
        committed_at=at(2).isoformat(),
        holdout_start=at(3, 0).isoformat(),
        holdout_end=at(8).isoformat(),
        minimum_independent_events=2,
        calibration_bin_count=4,
        minimum_brier_improvement=0.05,
        minimum_log_loss_improvement=0.05,
        minimum_ece_improvement=0.05,
        minimum_mean_net_ev_delta=0.05,
        minimum_mean_counterfactual_pnl_delta=0.05,
        opportunity_minimum_net_ev=0.01,
        maximum_source_on_ece=0.4,
    )
    return artifact(row | changes)


def pair(day, *, cluster=None, probability=0.75, result="yes"):
    clock = at(day)
    snapshot = artifact(
        dict(
            id=f"book-{day}",
            ticker=f"T-{day}",
            captured_at=clock.isoformat(),
            available_at=clock.isoformat(),
        )
    )
    rule = artifact(dict(ticker=f"T-{day}", event_id=f"E-{day}", rule_version="v1"))
    anchor = artifact(
        dict(
            event_id=f"E-{day}",
            independent_event_id=cluster or f"DAY-{day}",
            ticker=f"T-{day}",
            snapshot_id=f"book-{day}",
            snapshot_sha256=snapshot.sha256,
            decision_at=clock.isoformat(),
            model_name="synthetic",
            model_version="1",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            model_frozen_at=at(1).isoformat(),
            model_artifact_sha256=model().sha256,
            rule_version="v1",
            rule_sha256=rule.sha256,
            side="BUY_YES",
            executable_price=0.5,
            estimated_fee=0.01,
            slippage=0.01,
            uncertainty=0.01,
            event_window_start=clock.isoformat(),
            event_window_end=at(day, 2).isoformat(),
        )
    )
    source_payload = {"value": 42}
    source = artifact(
        dict(
            kind="source-original-v1",
            source_id="source-trial",
            available_at=clock.isoformat(),
            received_at=clock.isoformat(),
            provider_payload=source_payload,
            provider_payload_sha256=canonical_hash(source_payload),
        )
    )
    feature = artifact(
        dict(
            kind="feature-v1",
            source_id="source-trial",
            observed_at=(clock - timedelta(minutes=1)).isoformat(),
            available_at=clock.isoformat(),
            generated_at=clock.isoformat(),
            source_original_sha256=source.sha256,
            value=42,
        )
    )
    common = anchor.decode() | dict(
        kind="paired-forecast-v1",
        anchor_sha256=anchor.sha256,
        decision_id=canonical_hash(anchor.decode()),
        source_id="source-trial",
        generated_at=clock.isoformat(),
        recorded_at=clock.isoformat(),
    )
    off = artifact(common | dict(source_enabled=False, probability=0.45, feature_hashes=[]))
    on = artifact(
        common | dict(source_enabled=True, probability=probability, feature_hashes=[feature.sha256])
    )
    original = dict(
        kind="final-original-v1",
        status="final",
        result=result,
        ticker=f"T-{day}",
        event_id=f"E-{day}",
        final_at=at(day, 2).isoformat(),
        available_at=at(day, 3).isoformat(),
        received_at=at(day, 3).isoformat(),
    )
    outcome = artifact(
        dict(
            kind="outcome-v1",
            decision_id=canonical_hash(anchor.decode()),
            event_id=f"E-{day}",
            ticker=f"T-{day}",
            rule_version="v1",
            result=result,
            final_at=at(day, 2).isoformat(),
            available_at=at(day, 3).isoformat(),
            provider_payload=original,
            provider_payload_sha256=canonical_hash(original),
        )
    )
    return PairedForecast(anchor, off, on, outcome, (feature,), (snapshot, model(), rule, source))


def evaluate(pairs, frozen=None, **kwargs):
    return evaluate_tournament(policy=frozen or policy(), pairs=tuple(pairs), as_of=at(9), **kwargs)


def test_actual_paired_scores_costs_and_counterfactual_opportunities():
    result = evaluate([pair(3), pair(4)])
    assert result.status == "POINT_ESTIMATES_MEET_POLICY_REVIEW_REQUIRED"
    assert result.independent_event_n == result.paired_decision_n == 2
    assert result.metrics["brier_improvement"] == pytest.approx(0.24)
    assert result.metrics["ece_improvement"] == pytest.approx(0.3)
    assert result.metrics["log_loss_improvement"] > 0
    assert result.metrics["opportunities_off"] == 0
    assert result.metrics["opportunities_on"] == 2
    assert result.metrics["mean_counterfactual_policy_pnl_delta"] == pytest.approx(0.48)
    assert result.actual_paper_pnl is result.actual_shadow_pnl is result.value_score is None
    assert all(value is None for value in result.measurements.values())
    assert result.purchase_verdict.startswith("UNAVAILABLE") and result.verified_hashes


def test_missing_evidence_never_becomes_fake_zero_metrics():
    for rows in ([], [pair(3)], [pair(3), replace(pair(4), outcome=None)]):
        result = evaluate(rows)
        assert result.status == "NOT_ENOUGH_DATA" and result.metrics is None
        assert result.value_score is None


def test_related_events_reduce_independent_n_and_exact_repeats_reject():
    result = evaluate([pair(3, cluster="same-day"), pair(4, cluster="same-day")])
    assert result.status == "NOT_ENOUGH_DATA"
    assert result.independent_event_n == 1 and len(result.purged_decision_ids) == 1
    repeated = pair(3)
    assert evaluate([repeated, repeated]).status == "INVALID_EVIDENCE"


@pytest.mark.parametrize(
    "key,value",
    [
        ("ticker", "different"),
        ("decision_id", "different"),
        ("snapshot_id", "different"),
        ("decision_at", "2026-09-03T01:01:00Z"),
        ("model_version", "different"),
        ("training_cutoff", "2026-09-04T00:00:00Z"),
        ("rule_version", "different"),
        ("executable_price", 0.01),
    ],
)
def test_on_off_exact_cohort_fields_are_required(key, value):
    changed = pair(3)
    on = artifact(changed.source_on.decode() | {key: value})
    assert evaluate([replace(changed, source_on=on), pair(4)]).status == "INVALID_EVIDENCE"


def test_feature_visibility_and_original_hash_are_rechecked():
    original = pair(3)
    future = artifact(original.features[0].decode() | dict(available_at=at(3, 2).isoformat()))
    on = artifact(original.source_on.decode() | dict(feature_hashes=[future.sha256]))
    assert (
        evaluate([replace(original, features=(future,), source_on=on), pair(4)]).status
        == "INVALID_EVIDENCE"
    )
    tampered = replace(original.features[0], payload=b"{}")
    assert evaluate([replace(original, features=(tampered,)), pair(4)]).status == "INVALID_EVIDENCE"
    assert evaluate([replace(original, context_originals=()), pair(4)]).status == "INVALID_EVIDENCE"


def test_source_enabled_marker_without_actual_ablation_is_rejected():
    original = pair(3)
    off = artifact(
        original.source_off.decode() | dict(feature_hashes=[original.features[0].sha256])
    )
    assert evaluate([replace(original, source_off=off), pair(4)]).status == "INVALID_EVIDENCE"


def test_holdout_must_be_precommitted_and_complete():
    rows = (pair(3), pair(4))
    assert evaluate(rows, policy(committed_at=at(5).isoformat())).status == "INVALID_EVIDENCE"
    incomplete = evaluate_tournament(policy=policy(), pairs=rows, as_of=at(5))
    assert incomplete.status == "NOT_ENOUGH_DATA" and incomplete.metrics is None
    assert "HOLDOUT_WINDOW_NOT_COMPLETE" in incomplete.blockers
    future_policy = evaluate_tournament(policy=policy(), pairs=(), as_of=at(1))
    assert future_policy.status == "INVALID_EVIDENCE"


def test_feature_available_after_forecast_before_decision_is_rejected():
    original = pair(3)
    on = artifact(
        original.source_on.decode() | dict(generated_at=(at(3) - timedelta(seconds=30)).isoformat())
    )
    assert evaluate([replace(original, source_on=on), pair(4)]).status == "INVALID_EVIDENCE"


def test_distinct_cluster_labels_do_not_make_overlapping_windows_independent():
    original = pair(4)
    anchor = artifact(original.anchor.decode() | dict(event_window_start=at(3).isoformat()))
    identity = dict(
        anchor_sha256=anchor.sha256,
        decision_id=canonical_hash(anchor.decode()),
        event_window_start=at(3).isoformat(),
    )
    changed = replace(
        original,
        anchor=anchor,
        source_off=artifact(original.source_off.decode() | identity),
        source_on=artifact(original.source_on.decode() | identity),
        outcome=artifact(original.outcome.decode() | dict(decision_id=identity["decision_id"])),
    )
    result = evaluate([pair(3), changed])
    assert result.status == "NOT_ENOUGH_DATA"
    assert result.independent_event_n == 1 and len(result.purged_decision_ids) == 1


def test_harmful_source_does_not_pass_or_suggest_purchase():
    result = evaluate(
        [pair(3, probability=0.9, result="no"), pair(4, probability=0.9, result="no")]
    )
    assert result.status == "POINT_ESTIMATES_FAIL_POLICY"
    assert result.metrics["brier_improvement"] < 0
    assert result.metrics["mean_counterfactual_policy_pnl_delta"] < 0
    assert result.value_score is None and result.purchase_verdict.startswith("UNAVAILABLE")


def test_measured_cost_reliability_remain_separate_and_zero_requests_unknown():
    measurements = artifact(
        dict(
            kind="provider-measurements-v1",
            source_id="source-trial",
            measured_at=at(9).isoformat(),
            monthly_cost_usd=25,
            latency_p95_ms=150,
            request_count=10,
            failure_count=2,
        )
    )
    result = evaluate([pair(3), pair(4)], measurements=measurements)
    assert result.measurements["failure_rate"] == 0.2
    assert result.measurements["monthly_cost_usd"] == 25
    assert result.measurements["coverage_fraction"] is None and result.value_score is None
    zero = artifact(measurements.decode() | dict(request_count=0, failure_count=0))
    assert evaluate([pair(3), pair(4)], measurements=zero).measurements["failure_rate"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"result": "no"},
        {"ticker": "OTHER"},
        {"event_id": "OTHER"},
        {"status": "open"},
        {"available_at": "2026-09-03T02:30:00Z"},
        {"received_at": "2026-09-03T02:30:00Z"},
    ],
)
def test_outcome_original_semantics_cannot_be_replaced_by_correct_hash(changes):
    original = pair(3)
    row = original.outcome.decode()
    payload = row["provider_payload"] | changes
    changed = artifact(
        row | dict(provider_payload=payload, provider_payload_sha256=canonical_hash(payload))
    )
    assert evaluate([replace(original, outcome=changed), pair(4)]).status == "INVALID_EVIDENCE"


def test_features_require_intact_source_originals():
    original = pair(3)
    assert (
        evaluate([replace(original, context_originals=original.context_originals[:-1])]).status
        == "INVALID_EVIDENCE"
    )
    corrupt = replace(original.context_originals[-1], payload=b"{}")
    assert (
        evaluate(
            [replace(original, context_originals=original.context_originals[:-1] + (corrupt,))]
        ).status
        == "INVALID_EVIDENCE"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "other"},
        {"received_at": "2026-09-03T02:00:00Z"},
        {"provider_payload_sha256": "0" * 64},
    ],
)
def test_feature_source_identity_clock_and_payload_hash_are_bound(changes):
    original = pair(3)
    source = artifact(original.context_originals[-1].decode() | changes)
    feature = artifact(original.features[0].decode() | dict(source_original_sha256=source.sha256))
    on = artifact(original.source_on.decode() | dict(feature_hashes=[feature.sha256]))
    changed = replace(
        original,
        context_originals=original.context_originals[:-1] + (source,),
        features=(feature,),
        source_on=on,
    )
    assert evaluate([changed, pair(4)]).status == "INVALID_EVIDENCE"
