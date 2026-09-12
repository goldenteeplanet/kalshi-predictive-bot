"""Synthetic lineage tests; these do not establish a tradable model."""

from dataclasses import replace

import pytest
from test_overnight_provenance import artifact, provenance_inputs

from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.provenance_gate import (
    ProvenanceContext,
    verify_complete_provenance,
)


def complete_inputs():
    args = provenance_inputs()
    decision = args["decision"]
    rows = {key: value.decode() for key, value in args["artifacts"].items()}
    stamp = decision["decision_at"]
    common = {key: decision[key] for key in ("ticker", "event_id", "series")}
    feature = {
        "name": "fixture_feature",
        "value": 2,
        "source_sha256": args["source_artifacts"][0].sha256,
        "observed_at": stamp,
        "available_at": stamp,
    }
    features = artifact(
        common
        | {
            "source_hashes": decision["source_hashes"],
            "generated_at": stamp,
            "available_at": stamp,
            "records": [feature],
        }
    )
    rows["model"]["name"] = "synthetic-fixture"
    rows["forecast"].update(
        {
            "model_name": "synthetic-fixture",
            "training_cutoff": rows["model"]["training_cutoff"],
            "rule_version": "fixture-rule-v1",
            "code_sha": "a" * 40,
            "features_artifact_sha256": features.sha256,
            "model_artifact_sha256": artifact(rows["model"]).sha256,
        }
    )
    for key in (
        "model_name",
        "training_cutoff",
        "rule_version",
        "code_sha",
        "features_artifact_sha256",
    ):
        decision[key] = rows["forecast"][key]
    decision["feature_timestamps"] = [
        {key: value for key, value in feature.items() if key != "value"}
    ]
    decision["source_timestamps"] = [
        {
            "sha256": source.sha256,
            **{
                key: source.decode()[key]
                for key in (
                    "provider_updated_at",
                    "provider_generated_at",
                    "available_at",
                    "received_at",
                )
            },
        }
        for source in args["source_artifacts"]
    ]
    artifacts = {key: artifact(row) for key, row in rows.items()}
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    return dict(
        decision=decision,
        decision_id=canonical_hash(decision),
        now=args["now"],
        phase3m=args["phase3m"],
        phase3n=args["phase3n"],
        context=ProvenanceContext(
            artifacts,
            args["source_artifacts"],
            args["training_artifacts"],
            args["model_code"],
            features,
            "a" * 40,
            "fixture-rule-v1",
        ),
    )


def test_complete_original_lineage_passes_without_claiming_skill_or_rule_authority():
    result = verify_complete_provenance(**complete_inputs())
    assert result.passed, result.blockers
    assert not result.model_calibration_verified
    assert not result.settlement_rules_verified


@pytest.mark.parametrize(
    "key,value",
    [
        ("model_version", "wrong-version"),
        ("model_name", "wrong-model"),
        ("snapshot_id", 999),
        ("snapshot_book_hash", "0" * 64),
        ("ticker", "wrong-market"),
        ("rule_version", "wrong-rule"),
        ("code_sha", "b" * 40),
        ("feature_timestamps", []),
        ("source_timestamps", []),
        ("features_artifact_sha256", "0" * 64),
        ("training_cutoff", "2026-09-06T00:00:00Z"),
    ],
)
def test_rehashed_decision_cannot_substitute_bound_inputs(key, value):
    args = complete_inputs()
    args["decision"][key] = value
    args["decision_id"] = canonical_hash(args["decision"])
    assert not verify_complete_provenance(**args).passed


def test_missing_original_context_and_actual_engines_fail():
    args = complete_inputs()
    assert not verify_complete_provenance(**(args | {"context": None})).passed
    assert not verify_complete_provenance(**(args | {"phase3n": None})).passed


def test_stale_provider_timestamp_fails_even_when_every_hash_is_rebound():
    args = complete_inputs()
    context = args["context"]
    old_hash = context.source_artifacts[0].sha256
    source = context.source_artifacts[0].decode()
    source["provider_updated_at"] = "2026-09-07T00:00:00Z"
    updated_source = artifact(source)
    # Rebind every reference, proving this is semantic rejection, not hash failure.
    import json

    def rebind(value):
        return json.loads(json.dumps(value).replace(old_hash, updated_source.sha256))

    features = artifact(rebind(context.features_artifact.decode()))
    rows = {key: rebind(value.decode()) for key, value in context.artifacts.items()}
    rows["forecast"]["features_artifact_sha256"] = features.sha256
    artifacts = {key: artifact(value) for key, value in rows.items()}
    decision = rebind(args["decision"])
    decision["features_artifact_sha256"] = features.sha256
    decision["source_timestamps"][0]["provider_updated_at"] = source["provider_updated_at"]
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    args.update(
        decision=decision,
        decision_id=canonical_hash(decision),
        context=replace(
            context,
            artifacts=artifacts,
            source_artifacts=(updated_source,),
            features_artifact=features,
        ),
    )
    result = verify_complete_provenance(**args)
    assert result.blockers == ("PROVIDER_CLOCK_STALE",)


def test_future_features_fail_even_with_updated_artifact_binding():
    args = complete_inputs()
    context = args["context"]
    row = context.features_artifact.decode()
    row["records"][0]["available_at"] = "2026-09-08T01:00:01Z"
    features = artifact(row)
    forecast = context.artifacts["forecast"].decode()
    forecast["features_artifact_sha256"] = features.sha256
    artifacts = context.artifacts | {"forecast": artifact(forecast)}
    args["decision"].update(
        features_artifact_sha256=features.sha256,
        forecast_artifact_sha256=artifacts["forecast"].sha256,
    )
    args["decision_id"] = canonical_hash(args["decision"])
    args["context"] = replace(context, artifacts=artifacts, features_artifact=features)
    assert verify_complete_provenance(**args).blockers == ("FEATURE_RECORD_VISIBILITY_INVALID",)
