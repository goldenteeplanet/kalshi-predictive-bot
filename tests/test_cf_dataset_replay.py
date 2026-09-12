import json
from dataclasses import replace
from datetime import timedelta

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


def observation():
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
    return build_observation(
        provenance_args=args, independent_event_id="fixture-sol-event",
        event_window_start=at, event_window_end=at+timedelta(hours=1), rule_artifact=rule,
        market_probability=0.5, executable_price=0.4, estimated_fee=0.01,
        slippage=0.01, uncertainty=0.01,
    )


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
