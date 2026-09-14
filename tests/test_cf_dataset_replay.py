import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_cf_feature_provenance import complete_cf_inputs
from test_overnight_activation import baseline_template  # noqa: F401
from test_overnight_provenance import artifact
from test_paper_release_dataset_store import factory  # noqa: F401

from kalshi_predictor.overnight_paper.evaluation_dataset import (
    _validate_stored_observation,
    build_observation,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash


def observation(*, cost_evidence=False, return_inputs=False):
    args = complete_cf_inputs()
    context = args["context"]
    at = args["now"]
    rows = {key: value.decode() for key, value in context.artifacts.items()}
    training = context.training_artifacts[0].decode()
    training["records"][0].update(
        independent_event_id="fixture-training",
        event_window_start=(at-timedelta(days=3)).isoformat(),
        event_window_end=(at-timedelta(days=2)).isoformat(),
    )
    trained = (artifact(training),)
    rows["model"]["training_dataset_hashes"] = [trained[0].sha256]
    rows["forecast"]["model_artifact_sha256"] = artifact(rows["model"]).sha256
    rows["snapshot"]["market_implied_probability"] = 0.5
    artifacts = {key: artifact(value) for key, value in rows.items()}
    rule = artifact({key: args["decision"][key] for key in (
        "ticker", "event_id", "series", "rule_version",
    )})
    args["decision"].update(
        rule_artifact_sha256=rule.sha256, executable_price=0.4, estimated_fee=0.01,
        slippage=0.01, uncertainty=0.01, side="BUY_YES",
    )
    args["decision"].update({
        key+"_artifact_sha256": value.sha256 for key,value in artifacts.items()
    })
    args["decision_id"] = canonical_hash(args["decision"])
    args["context"] = replace(context, artifacts=artifacts, training_artifacts=trained)
    cost_args = {}
    if cost_evidence:
        from kalshi_predictor.crypto.cost_record import (
            build_cost_record,
            cost_decision_from_qualification,
        )

        decision = cost_decision_from_qualification(args["decision"])
        cost_args["cost_record"] = build_cost_record(
            decision=decision, selected_probability=Decimal(decision["selected_probability"]),
            executable_price=Decimal("0.4"), side="YES",
        )
    result = build_observation(
        provenance_args=args, independent_event_id="fixture-sol-event",
        event_window_start=at, event_window_end=at+timedelta(hours=1), rule_artifact=rule,
        market_probability=0.5, executable_price=0.4,
        estimated_fee=None if cost_evidence else 0.01,
        slippage=None if cost_evidence else 0.01, uncertainty=None if cost_evidence else 0.01,
        **cost_args,
    )
    return (result, args) if return_inputs else result


def test_persisted_cf_observation_replays_without_original_in_memory_context():
    raw = observation().payload
    row = json.loads(raw)
    _validate_stored_observation(row)
    assert row["cf_context"]["target"]["settlement_rule_binding"]["unresolved_fields"]


def test_cf_context_survives_ledger_commit_and_new_session(factory):  # noqa: F811
    from sqlalchemy import text

    from kalshi_predictor.overnight_paper.dataset_store import load_dataset, persist_dataset_record
    from kalshi_predictor.overnight_paper.source_health import aware

    item = observation()
    at = aware(item.decode()["decision"]["decision_at"])
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        persist_dataset_record(session, dataset="cf-fixture", record=item, recorded_at=at)
        session.commit()
    del item
    with factory() as session:
        stored = load_dataset(session, dataset="cf-fixture")
        assert len(stored) == 1
        row = stored[0].decode()["record"]
        _validate_stored_observation(row)
        assert row["cf_context"]["target"]["symbol"] == "SOL"


def test_stored_cf_context_cannot_be_omitted_or_certified_by_relabelling():
    row = observation().decode()
    del row["cf_context"]
    with pytest.raises(ValueError, match="STORED_CF_CONTEXT_REQUIRED"):
        _validate_stored_observation(row)
    row = observation().decode()
    row["cf_context"]["target"]["settlement_rule_binding"]["status"] = "CERTIFIED"
    with pytest.raises(ValueError, match="RECONSTRUCTION"):
        _validate_stored_observation(row)


def test_stored_cf_feature_semantics_checked_after_all_hashes_rebound():
    row = observation().decode()
    feature = row["features"]["payload"]
    feature["records"][0]["value"]["level"] = "999999"
    row["features"]["sha256"] = canonical_hash(feature)
    forecast = row["originals"]["forecast"]["payload"]
    forecast["features_artifact_sha256"] = row["features"]["sha256"]
    row["originals"]["forecast"]["sha256"] = canonical_hash(forecast)
    row["decision"].update(
        features_artifact_sha256=row["features"]["sha256"],
        forecast_artifact_sha256=canonical_hash(forecast),
    )
    row["decision_id"] = canonical_hash(row["decision"])
    with pytest.raises(ValueError, match="STORED_CF_FEATURE_ORIGINAL_BINDING"):
        _validate_stored_observation(row)


def test_unknown_cost_observation_replays_without_overwriting_frozen_decision():
    row = observation(cost_evidence=True).decode()
    _validate_stored_observation(row)
    assert row["estimated_fee"] is row["slippage"] is row["uncertainty"] is None
    assert row["decision"]["estimated_fee"] == 0.01  # Preserved synthetic original input.
    assert row["cost_record"]["assessment"]["full_net_ev"] is None


@pytest.mark.parametrize("field", ["estimated_fee", "slippage", "uncertainty"])
def test_unknown_cost_cannot_be_relabelled_as_zero(field):
    row = observation(cost_evidence=True).decode()
    row[field] = 0.0
    with pytest.raises(ValueError, match="STORED_REPLAYED_COST_BINDING_MISMATCH"):
        _validate_stored_observation(row)


def test_relabelled_full_net_cost_record_fails_original_replay():
    row = observation(cost_evidence=True).decode()
    row["cost_record"]["assessment"]["full_net_ev"] = "0.10"
    with pytest.raises(ValueError, match="COST_RECORD_RECOMPUTATION_MISMATCH"):
        _validate_stored_observation(row)


def test_unknown_cost_holdout_keeps_forecast_metrics_but_blocks_economic_acceptance():
    from kalshi_predictor.overnight_paper.evaluation_dataset import (
        append_record,
        build_policy,
        evaluate_dataset,
        join_outcome,
    )
    from kalshi_predictor.overnight_paper.source_health import aware

    item = observation(cost_evidence=True)
    row = item.decode()
    at = aware(row["decision"]["decision_at"])
    committed = at - timedelta(days=1)
    policy = build_policy(
        committed_at=committed, train_end=committed, holdout_start=at-timedelta(hours=1),
        holdout_end=at+timedelta(hours=2), model_name=row["decision"]["model_name"],
        model_version=row["decision"]["model_version"], minimum_train_events=1,
        minimum_holdout_events=1, minimum_brier_improvement=0, minimum_log_loss_improvement=0,
        maximum_ece=1, minimum_mean_net_ev=0.05, minimum_mean_simulated_pnl=0,
        calibration_bin_count=2,
    )
    final = at+timedelta(hours=1)
    payload = {"synthetic": "final yes"}
    outcome = artifact(row["identity"] | dict(
        result="yes", status="final", source_url="https://example.invalid/synthetic",
        provider_payload=payload, provider_payload_sha256=canonical_hash(payload),
        final_at=final.isoformat(), available_at=final.isoformat(),
    ))
    records = append_record((), policy, recorded_at=committed)
    records = append_record(records, item, recorded_at=at)
    records = append_record(records, join_outcome(
        observation=item, outcome_artifact=outcome,
    ), recorded_at=final)
    result = evaluate_dataset(records, as_of=at+timedelta(hours=3))
    assert not result.ready
    assert "FULL_COST_EVIDENCE_INCOMPLETE" in result.blockers
    assert result.metrics["unknown_cost_holdout_observations"] == 1
    assert "brier" in result.metrics and "baseline_brier" in result.metrics
    assert "mean_after_cost_predicted_ev" not in result.metrics
    assert "mean_hypothetical_one_contract_pnl" not in result.metrics
