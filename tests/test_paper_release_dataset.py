from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_overnight_provenance import artifact
from test_paper_release_provenance import complete_inputs

from kalshi_predictor.overnight_paper.evaluation_dataset import (
    append_record,
    build_observation,
    build_policy,
    join_outcome,
    read_records,
)
from kalshi_predictor.overnight_paper.evaluation_dataset import (
    evaluate_dataset as evaluate_dataset_at,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash


def evaluate_dataset(records):
    return evaluate_dataset_at(records, as_of=stamp(14))


def stamp(day, hour=1):
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def observation(day, event=None):
    args = complete_inputs()
    context = args["context"]
    at = stamp(day)
    decision = args["decision"]
    rows = {key: value.decode() for key, value in context.artifacts.items()}
    training = context.training_artifacts[0].decode()
    training["records"][0].update(
        independent_event_id="training-event-6",
        event_window_start=stamp(6).isoformat(),
        event_window_end=stamp(6, 2).isoformat(),
    )
    training_artifacts = (artifact(training),)
    rows["model"]["training_dataset_hashes"] = [training_artifacts[0].sha256]
    rows["forecast"]["model_artifact_sha256"] = artifact(rows["model"]).sha256
    for key in ("phase3m", "phase3n"):
        args[key] = replace(args[key], decision_timestamp=at)
        rows[key] = args[key].as_dict()
        decision[key + "_hash"] = canonical_hash(rows[key])
    source = context.source_artifacts[0].decode()
    for key in ("provider_updated_at", "provider_generated_at", "available_at", "received_at"):
        source[key] = at.isoformat()
    sources = (artifact(source),)
    features = context.features_artifact.decode()
    features.update(
        generated_at=at.isoformat(), available_at=at.isoformat(), source_hashes=[sources[0].sha256]
    )
    features["records"][0].update(
        source_sha256=sources[0].sha256, observed_at=at.isoformat(), available_at=at.isoformat()
    )
    features = artifact(features)
    rows["forecast"].update(
        generated_at=at.isoformat(),
        available_at=at.isoformat(),
        source_hashes=[sources[0].sha256],
        features_artifact_sha256=features.sha256,
    )
    rows["snapshot"].update(
        captured_at=at.isoformat(), available_at=at.isoformat(), market_implied_probability=0.5
    )
    artifacts = {key: artifact(value) for key, value in rows.items()}
    rule = artifact(
        {key: decision[key] for key in ("ticker", "event_id", "series", "rule_version")}
    )
    decision.update(
        decision_at=at.isoformat(),
        close_time=(at + timedelta(hours=1)).isoformat(),
        rule_artifact_sha256=rule.sha256,
        executable_price=0.4,
        estimated_fee=0.01,
        slippage=0.01,
        uncertainty=0.01,
        side="BUY_YES",
        source_hashes=[sources[0].sha256],
        features_artifact_sha256=features.sha256,
    )
    decision["source_timestamps"] = [
        {
            "sha256": sources[0].sha256,
            **{
                key: source[key]
                for key in (
                    "provider_updated_at",
                    "provider_generated_at",
                    "available_at",
                    "received_at",
                )
            },
        }
    ]
    decision["feature_timestamps"] = [
        {key: value for key, value in features.decode()["records"][0].items() if key != "value"}
    ]
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    args.update(
        decision_id=canonical_hash(decision),
        now=at,
        context=replace(
            context,
            artifacts=artifacts,
            source_artifacts=sources,
            features_artifact=features,
            training_artifacts=training_artifacts,
        ),
    )
    return build_observation(
        provenance_args=args,
        independent_event_id=event or f"event-{day}",
        event_window_start=at,
        event_window_end=at + timedelta(hours=1),
        rule_artifact=rule,
        market_probability=0.5,
        executable_price=0.4,
        estimated_fee=0.01,
        slippage=0.01,
        uncertainty=0.01,
    )


def outcome(obs, *, result="yes"):
    row = obs.decode()
    at = datetime.fromisoformat(row["event_window_end"])
    raw = artifact(
        row["identity"]
        | {
            "result": result,
            "status": "final",
            "final_at": at.isoformat(),
            "available_at": at.isoformat(),
            "source_url": "https://example.org/fixture",
            "provider_payload": {"result": result},
            "provider_payload_sha256": canonical_hash({"result": result}),
        }
    )
    return join_outcome(observation=obs, outcome_artifact=raw)


def policy(**changes):
    return build_policy(
        **(
            dict(
                committed_at=stamp(9),
                train_end=stamp(8, 3),
                holdout_start=stamp(10, 0),
                holdout_end=stamp(13, 0),
                model_name="synthetic-fixture",
                model_version="fixture-v1",
                minimum_train_events=1,
                minimum_holdout_events=2,
                minimum_brier_improvement=0.01,
                minimum_log_loss_improvement=0.01,
                maximum_ece=0.5,
                minimum_mean_net_ev=0.01,
                minimum_mean_simulated_pnl=0.01,
                calibration_bin_count=5,
            )
            | changes
        )
    )


def dataset(*, overlapping=False, pending=False, bad_result=False):
    train = observation(8)
    chain = append_record((), train, recorded_at=stamp(8))
    chain = append_record(chain, outcome(train), recorded_at=stamp(8, 2))
    chain = append_record(chain, policy(), recorded_at=stamp(9))
    for day in (10, 11):
        obs = observation(day, "same-event" if overlapping else None)
        chain = append_record(chain, obs, recorded_at=stamp(day))
        if not pending:
            chain = append_record(
                chain, outcome(obs, result="no" if bad_result else "yes"), recorded_at=stamp(day, 2)
            )
    return chain


def test_prospective_dataset_full_chronological_evaluation():
    result = evaluate_dataset(dataset())
    assert result.ready, result.blockers
    assert result.metrics["independent_holdout_events"] == 2
    assert result.metrics["brier"] == pytest.approx(0.16)
    assert result.metrics["baseline_brier"] == pytest.approx(0.25)
    assert result.metrics["mean_after_cost_predicted_ev"] == pytest.approx(0.17)
    assert result.metrics["mean_hypothetical_one_contract_pnl"] == pytest.approx(0.58)
    assert "realized_pnl" not in result.metrics


def test_append_only_outcome_preserves_observation_and_duplicate_replay():
    obs = observation(8)
    original = obs.payload
    chain = append_record((), obs, recorded_at=stamp(8))
    assert append_record(chain, obs, recorded_at=stamp(8)) == chain
    complete = append_record(chain, outcome(obs), recorded_at=stamp(8, 2))
    assert complete[:1] == chain and obs.payload == original
    assert len(read_records(complete)) == 2


def test_pending_results_never_create_calibrated_readiness():
    result = evaluate_dataset(dataset(pending=True))
    assert not result.ready
    assert "INSUFFICIENT_INDEPENDENT_HOLDOUT_EVENTS" in result.blockers
    assert result.metrics["unsettled_observations"] == 2
    assert "HOLDOUT_OUTCOMES_INCOMPLETE" in result.blockers


def test_early_or_missing_evaluation_clock_cannot_release_model():
    chain = dataset()
    assert evaluate_dataset_at(chain).blockers == ("EVALUATION_CLOCK_REQUIRED",)
    result = evaluate_dataset_at(chain, as_of=stamp(11, 3))
    assert not result.ready and "HOLDOUT_WINDOW_NOT_COMPLETE" in result.blockers


def test_related_events_are_purged_and_reduce_independent_sample_count():
    result = evaluate_dataset(dataset(overlapping=True))
    assert not result.ready and len(result.purged_decision_ids) == 1
    assert result.metrics["independent_holdout_events"] == 1


def test_no_edge_is_failed_even_with_complete_dataset():
    result = evaluate_dataset(dataset(bad_result=True))
    assert not result.ready
    assert "BRIER_ACCEPTANCE_FAILED" in result.blockers
    assert "SIMULATED_PNL_ACCEPTANCE_FAILED" in result.blockers


def test_policy_cannot_be_frozen_after_holdout():
    with pytest.raises(ValueError, match="POLICY_NOT_PRECOMMITTED"):
        policy(committed_at=stamp(10, 1))


def test_hash_chain_reorder_or_mutation_is_detected():
    chain = dataset()
    assert not evaluate_dataset((chain[1], chain[0], *chain[2:])).ready
    raw = chain[-1].decode()
    raw["record"]["outcome"]["result"] = "no"
    assert not evaluate_dataset((*chain[:-1], artifact(raw))).ready


def test_late_import_is_not_mislabeled_prospective():
    with pytest.raises(ValueError, match="PROSPECTIVE_APPEND_DELAY"):
        append_record((), observation(8), recorded_at=stamp(9))


def test_missing_precommitted_policy_fails_closed():
    assert evaluate_dataset(()).blockers == ("ONE_PRECOMMITTED_POLICY_REQUIRED",)


def test_outcome_exact_rule_identity_is_required():
    obs = observation(8)
    result = outcome(obs).decode()["outcome"]
    result["rule_version"] = "other"
    with pytest.raises(ValueError, match="OUTCOME_IDENTITY_MISMATCH"):
        join_outcome(observation=obs, outcome_artifact=artifact(result))


def test_different_event_ids_with_overlapping_windows_are_purged():
    chain = dataset()[:5]
    row = observation(11).decode()
    row["event_window_start"] = stamp(10).isoformat()
    overlapping = artifact(row)
    chain = append_record(chain, overlapping, recorded_at=stamp(11))
    chain = append_record(chain, outcome(overlapping), recorded_at=stamp(11, 2))
    result = evaluate_dataset(chain)
    assert not result.ready
    assert result.metrics["purged_holdout_observations"] == 1


def test_later_policy_append_cannot_retroactively_precommit_holdout():
    chain = dataset()
    # Keep a valid chain but append a replacement policy after observation.
    later = policy(committed_at=stamp(11, 2), holdout_start=stamp(12), holdout_end=stamp(13))
    chain = append_record(chain, later, recorded_at=stamp(11, 2))
    assert evaluate_dataset(chain).blockers == ("ONE_PRECOMMITTED_POLICY_REQUIRED",)


def test_rehashed_future_source_is_not_accepted_as_original_visibility():
    from kalshi_predictor.overnight_paper.evaluation_dataset import _validate_stored_observation

    row = observation(10).decode()
    source = row["sources"][0]
    source["payload"]["available_at"] = stamp(11).isoformat()
    source["sha256"] = canonical_hash(source["payload"])
    row["decision"]["source_hashes"] = [source["sha256"]]
    row["decision_id"] = canonical_hash(row["decision"])
    with pytest.raises(ValueError, match="STORED_FUTURE_SOURCE"):
        _validate_stored_observation(row)
