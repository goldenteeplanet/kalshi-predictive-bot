# ruff: noqa: F811
"""Miami original replay stays exact through generic analytical provenance."""

from copy import deepcopy
from datetime import timedelta

import pytest
from test_miami_source_gate import context, fixtures, grid_context  # noqa: F401

from kalshi_predictor.overnight_paper.miami_provenance import (
    miami_feature_record,
    miami_source_bundle,
    verify_miami_provenance_binding,
    verify_miami_provenance_source,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash, validate_source_visibility


@pytest.fixture
def bundle(grid_context):
    at = fixtures.CUTOFF + timedelta(minutes=30, seconds=2)
    return miami_source_bundle(grid_context, decision_at=at), at


def test_replay_bundle_enters_generic_visibility_without_provider_clock(bundle):
    source, at = bundle
    validate_source_visibility(source, decision_at=at, now=at)
    verified = verify_miami_provenance_source(source, decision_at=at, now=at)
    assert verified["inputs"]["forecast_probability"] == str(2 / 3)
    assert verified["inputs"]["model_name"] == "miami_prior_day_increment_grid30_v1"
    assert source["provider_generated_at"] is None
    assert not source["settlement_truth"]
    record = miami_feature_record(source, decision_at=at, now=at)
    assert record["source_sha256"] == canonical_hash(source)
    assert record["value"] == verified["forecast"]
    assert record["observed_at"] != record["available_at"]


def bound_rows(source, at):
    verified = verify_miami_provenance_source(source, decision_at=at, now=at)
    decision = verified["inputs"] | {"miami_input_sha256": verified["input_sha256"]}
    forecast = dict(
        model_name=decision["model_name"],
        model_version="1",
        probability=decision["forecast_probability"],
        generated_at=at.isoformat(),
        miami_input_sha256=verified["input_sha256"],
    )
    return decision, forecast


def test_exact_forecast_and_decision_binding(bundle):
    source, at = bundle
    decision, forecast = bound_rows(source, at)
    verify_miami_provenance_binding(source, decision=decision, forecast=forecast, now=at)


@pytest.mark.parametrize(
    "field,value",
    [
        ("clock_basis", "public_rest_receipt"),
        ("provider_generated_at", "2026-09-10T21:30:00Z"),
        ("historical_public_availability", "VERIFIED"),
        ("settlement_truth", True),
        ("available_at", "2026-09-10T21:30:00Z"),
        ("received_at", "2026-09-10T21:30:00Z"),
    ],
)
def test_bundle_rejects_fabricated_clock_or_authority(bundle, field, value):
    source, at = bundle
    source = deepcopy(source)
    source[field] = value
    with pytest.raises(ValueError):
        validate_source_visibility(source, decision_at=at, now=at)


@pytest.mark.parametrize("original", ["frozen_prediction", "recording_receipt"])
def test_raw_original_tamper_rejected(bundle, original):
    source, at = bundle
    source = deepcopy(source)
    source["body"][original]["payload_hex"] += "20"
    with pytest.raises(ValueError, match="ORIGINAL_HASH"):
        validate_source_visibility(source, decision_at=at, now=at)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ticker", "KXBTC-X-T1"),
        ("model_name", "weather_v2"),
        ("model_version", "2"),
        ("forecast_probability", "0.99"),
        ("origin_at", "2026-09-10T21:00:00Z"),
        ("observation_time", "2026-09-10T23:00:00Z"),
        ("miami_input_sha256", "f" * 64),
    ],
)
def test_different_decision_cannot_borrow_source(bundle, field, value):
    source, at = bundle
    decision, forecast = bound_rows(source, at)
    decision[field] = value
    with pytest.raises(ValueError):
        verify_miami_provenance_binding(source, decision=decision, forecast=forecast, now=at)


@pytest.mark.parametrize(
    "field,value",
    [
        ("probability", "0.99"),
        ("model_name", "weather_v2"),
        ("miami_input_sha256", "f" * 64),
        ("generated_at", "2026-09-10T21:35:00Z"),
        ("generated_at", "2026-09-10T22:00:01Z"),
    ],
)
def test_forecast_probability_and_actual_availability_binding(bundle, field, value):
    source, at = bundle
    decision, forecast = bound_rows(source, at)
    forecast[field] = value
    with pytest.raises(ValueError):
        verify_miami_provenance_binding(source, decision=decision, forecast=forecast, now=at)


def test_bundle_stale_at_requalification(bundle):
    source, at = bundle
    with pytest.raises(ValueError, match="REPLAY_OR_HEALTH"):
        validate_source_visibility(source, decision_at=at, now=at + timedelta(minutes=2))


def full_gate_args(bundle):
    """Synthetic engine fixture exercises real gate9; establishes no release authority."""
    from dataclasses import replace

    from test_fixed_heuristic_provenance import fixed_inputs
    from test_overnight_provenance import artifact

    source, at = bundle
    args = fixed_inputs()
    context = args["context"]
    decision, forecast_binding = bound_rows(source, at)
    rows = {key: a.decode() for key, a in context.artifacts.items()}
    decision = args["decision"] | decision
    identity = {k: decision[k] for k in ("ticker", "event_id", "series")}
    source_artifact = artifact(source)
    source_hashes = [source_artifact.sha256]
    decision.update(source_hashes=source_hashes, close_time=decision["observation_time"])
    rows["model"].update(name=decision["model_name"], version="1")
    rows["forecast"].update(
        identity
        | forecast_binding
        | dict(
            available_at=at.isoformat(),
            source_hashes=source_hashes,
            model_artifact_sha256=artifact(rows["model"]).sha256,
        )
    )
    rows["snapshot"].update(
        identity | dict(captured_at=at.isoformat(), available_at=at.isoformat())
    )
    for role in ("phase3m", "phase3n"):
        args[role] = replace(args[role], decision_timestamp=at)
        rows[role] = args[role].as_dict()
        decision[role + "_hash"] = canonical_hash(rows[role])
    record = miami_feature_record(source, decision_at=at, now=at)
    feature = artifact(
        identity
        | dict(
            source_hashes=source_hashes,
            generated_at=at.isoformat(),
            available_at=at.isoformat(),
            records=[record],
        )
    )
    rows["forecast"]["features_artifact_sha256"] = feature.sha256
    decision["features_artifact_sha256"] = feature.sha256
    decision["feature_timestamps"] = [{k: v for k, v in record.items() if k != "value"}]
    decision["source_timestamps"] = [
        dict(
            sha256=source_artifact.sha256,
            **{
                k: source[k]
                for k in (
                    "provider_updated_at",
                    "provider_generated_at",
                    "available_at",
                    "received_at",
                    "clock_basis",
                )
            },
        )
    ]
    artifacts = {key: artifact(value) for key, value in rows.items()}
    decision.update({key + "_artifact_sha256": a.sha256 for key, a in artifacts.items()})
    args.update(
        decision=decision,
        decision_id=canonical_hash(decision),
        now=at,
        context=replace(
            context,
            artifacts=artifacts,
            source_artifacts=(source_artifact,),
            features_artifact=feature,
        ),
    )
    return args


def test_complete_gate9_miami_path_does_not_certify_rules_or_skill(bundle):
    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    result = verify_complete_provenance(**full_gate_args(bundle))
    assert result.passed, result.blockers
    assert not result.model_calibration_verified
    assert not result.settlement_rules_verified


@pytest.mark.parametrize("change", ["value", "missing", "duplicate", "clock", "bool", "float"])
def test_gate9_rehashed_empirical_feature_tamper_fails(bundle, change):
    from dataclasses import replace

    from test_overnight_provenance import artifact

    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    args = full_gate_args(bundle)
    context = args["context"]
    feature = context.features_artifact.decode()
    if change == "bool":
        assert feature["records"][0]["value"]["calibrated"] is False
        feature["records"][0]["value"]["calibrated"] = 0
    elif change == "float":
        model = feature["records"][0]["value"]["models"]["prior_day_increment_empirical"]
        samples = model["samples_f"]
        assert type(samples[0]) is float and samples[0] == int(samples[0])
        samples[0] = int(samples[0])
    elif change == "value":
        feature["records"][0]["value"]["models"]["prior_day_increment_empirical"]["samples_f"][
            0
        ] += 1
    elif change == "missing":
        feature["records"] = []
    elif change == "duplicate":
        feature["records"] *= 2
    else:
        feature["records"][0]["observed_at"] = feature["records"][0]["available_at"]
    changed = artifact(feature)
    forecast = context.artifacts["forecast"].decode()
    forecast["features_artifact_sha256"] = changed.sha256
    forecast_artifact = artifact(forecast)
    args["decision"].update(
        features_artifact_sha256=changed.sha256,
        forecast_artifact_sha256=forecast_artifact.sha256,
        feature_timestamps=[
            {k: v for k, v in r.items() if k != "value"} for r in feature["records"]
        ],
    )
    args["decision_id"] = canonical_hash(args["decision"])
    args["context"] = replace(
        context,
        features_artifact=changed,
        artifacts=context.artifacts | {"forecast": forecast_artifact},
    )
    result = verify_complete_provenance(**args)
    assert not result.passed
    assert "FEATURE" in str(result.blockers)


@pytest.mark.parametrize("change", ["missing_code", "duplicate_code", "context_bool", "body_type"])
def test_malformed_context_cannot_enter_provenance(bundle, change):
    source, at = bundle
    source = deepcopy(source)
    if change == "missing_code":
        source["body"]["code_originals"].pop()
    elif change == "duplicate_code":
        source["body"]["code_originals"][0] = source["body"]["code_originals"][1]
    elif change == "context_bool":
        source["body"]["current_capture"] = False
    else:
        source["body"] = []
    with pytest.raises((ValueError, TypeError)):
        validate_source_visibility(source, decision_at=at, now=at)


def test_large_exact_original_survives_generic_gate9(bundle):
    """About four real 650KB history pages, without inventing extra observations."""
    import hashlib
    import json

    from test_miami_source_gate import artifact

    source, at = bundle
    source = deepcopy(source)
    original = source["body"]["market"]["artifact"]
    market = json.loads(bytes.fromhex(original["payload_hex"]))
    market["test_padding_not_analytical_data"] = " " * 2_650_000
    raw = json.dumps(market).encode()
    original.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    rec = json.loads(bytes.fromhex(source["body"]["catalog_receipts"][0]["payload_hex"]))
    rec["sha256"] = original["sha256"]
    rec_artifact = artifact(rec)
    source["body"]["catalog_receipts"][0] = dict(
        sha256=rec_artifact.sha256, payload_hex=rec_artifact.payload.hex()
    )
    from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

    result = verify_complete_provenance(**full_gate_args((source, at)))
    assert result.passed, result.blockers
