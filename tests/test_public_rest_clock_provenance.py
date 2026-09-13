"""REST receipts are honest metadata visibility, never provider forecast clocks."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_overnight_provenance import artifact
from test_paper_release_dataset import observation, stamp
from test_paper_release_provenance import complete_inputs

from kalshi_predictor.overnight_paper.evaluation_dataset import _validate_stored_observation
from kalshi_predictor.overnight_paper.provenance import canonical_hash, validate_source_visibility
from kalshi_predictor.overnight_paper.provenance_gate import verify_complete_provenance

URL = "https://external-api.kalshi.com/trade-api/v2/markets/T/orderbook"


def metadata_source(url=URL):
    return dict(
        url=url,
        body={"fixture": "metadata"},
        clock_basis="public_rest_receipt",
        provider_generated_at=None,
        provider_updated_at=None,
        available_at=stamp(8).isoformat(),
        received_at=stamp(8).isoformat(),
    )


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://external-api.kalshi.com/trade-api/v2/markets/KXBTC-26SEP0804-T88299.99",
        "https://external-api.kalshi.com/trade-api/v2/events/E",
        "https://external-api.kalshi.com/trade-api/v2/series/S",
        "https://api.weather.gov/stations/KNYC",
        "https://api.weather.gov/points/40.7789,-73.9692",
    ],
)
def test_exact_public_metadata_endpoints_allow_null_provider_clocks(url):
    validate_source_visibility(metadata_source(url), decision_at=stamp(8), now=stamp(8))


@pytest.mark.parametrize(
    "url",
    [
        "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly",
        "https://api.weather.gov/stations/KNYC/observations/latest",
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        "https://www.cfbenchmarks.com/data/indices/BRTI",
        "https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI",
        "https://external-api.kalshi.com/trade-api/v2/portfolio/orders",
        "https://external-api.kalshi.com.evil.invalid/trade-api/v2/markets/T",
        "https://external-api.kalshi.com/trade-api/v2/markets/T?url=forecast",
        "https://api.weather.gov/points/100.0,-73.0",
    ],
)
def test_receipt_basis_never_substitutes_for_analytical_source_or_other_endpoint(url):
    with pytest.raises(ValueError, match="RECEIPT_BASIS_METADATA_ENDPOINT_REQUIRED"):
        validate_source_visibility(metadata_source(url), decision_at=stamp(8), now=stamp(8))


@pytest.mark.parametrize("field", ["provider_generated_at", "provider_updated_at"])
def test_receipt_basis_cannot_fill_provider_clocks_with_download_time(field):
    source = metadata_source()
    source[field] = source["received_at"]
    with pytest.raises(ValueError, match="RECEIPT_BASIS_PROVIDER_CLOCKS_MUST_BE_NULL"):
        validate_source_visibility(source, decision_at=stamp(8), now=stamp(8))


def test_receipt_basis_requires_original_equal_visibility_and_freshness():
    source = metadata_source()
    with pytest.raises(ValueError, match="RECEIPT_BASIS_VISIBILITY_INVALID"):
        validate_source_visibility(
            source, decision_at=stamp(8), now=stamp(8) + timedelta(seconds=61)
        )
    source["available_at"] = (stamp(8) - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="RECEIPT_BASIS_VISIBILITY_INVALID"):
        validate_source_visibility(source, decision_at=stamp(8), now=stamp(8))


def test_provider_branch_still_requires_both_original_clocks():
    source = metadata_source("https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly")
    source.pop("clock_basis")
    with pytest.raises(ValueError, match="TIMEZONE_REQUIRED"):
        validate_source_visibility(source, decision_at=stamp(8), now=stamp(8))


def metadata_inputs():
    args = complete_inputs()
    context = args["context"]
    old = context.source_artifacts[0].sha256
    source = artifact(metadata_source())

    def rebind(row):
        return json.loads(json.dumps(row).replace(old, source.sha256))

    features = artifact(rebind(context.features_artifact.decode()))
    rows = {key: rebind(value.decode()) for key, value in context.artifacts.items()}
    rows["forecast"]["features_artifact_sha256"] = features.sha256
    artifacts = {key: artifact(value) for key, value in rows.items()}
    decision = rebind(args["decision"])
    decision["features_artifact_sha256"] = features.sha256
    decision["source_timestamps"] = [
        {
            "sha256": source.sha256,
            **{key: value for key, value in source.decode().items() if key not in ("body", "url")},
        }
    ]
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    args.update(
        decision=decision,
        decision_id=canonical_hash(decision),
        context=replace(
            context, artifacts=artifacts, source_artifacts=(source,), features_artifact=features
        ),
    )
    return args


def test_gate9_recomputes_clock_basis_and_does_not_claim_analytical_authority():
    args = metadata_inputs()
    verified = verify_complete_provenance(**args)
    assert verified.passed, verified.blockers
    assert not verified.model_calibration_verified and not verified.settlement_rules_verified
    args["decision"]["source_timestamps"][0].pop("clock_basis")
    args["decision_id"] = canonical_hash(args["decision"])
    assert verify_complete_provenance(**args).blockers == ("SOURCE_TIMESTAMP_BINDING_MISMATCH",)


def test_dataset_replay_accepts_same_explicit_receipt_basis():
    row = observation(8).decode()
    old_hash = row["sources"][0]["sha256"]
    source = artifact(metadata_source())
    row = json.loads(json.dumps(row).replace(old_hash, source.sha256))
    row["sources"] = [{"sha256": source.sha256, "payload": source.decode()}]
    features = artifact(row["features"]["payload"])
    row["features"]["sha256"] = features.sha256
    row["originals"]["forecast"]["payload"]["features_artifact_sha256"] = features.sha256
    forecast = artifact(row["originals"]["forecast"]["payload"])
    row["originals"]["forecast"]["sha256"] = forecast.sha256
    row["decision"].update(
        features_artifact_sha256=features.sha256, forecast_artifact_sha256=forecast.sha256
    )
    row["decision"]["source_timestamps"] = [
        {
            "sha256": source.sha256,
            **{key: value for key, value in source.decode().items() if key not in ("body", "url")},
        }
    ]
    row["decision_id"] = canonical_hash(row["decision"])
    _validate_stored_observation(row)
