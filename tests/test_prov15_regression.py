import json
from datetime import UTC, datetime, timedelta

from kalshi_predictor.provenance.regression import build_attribution_regression_report

NOW = datetime(2026, 7, 17, 20, 0, tzinfo=UTC)


def test_prov15_detects_missing_and_stale_references():
    events = [
        _event("missing", "crypto_v2", "2.0.0", observation=None, snapshot=None),
        _event("stale", "weather_v2", "2.0.0", observation_age=901, snapshot_age=121),
    ]
    report = build_attribution_regression_report(
        events,
        expected_model_versions={"crypto_v2": ["2.0.0"], "weather_v2": ["2.0.0"]},
        generated_at=NOW,
    )
    failures = report["summary"]["failure_counts"]
    assert report["summary"]["passed"] is False
    assert failures == {
        "OBSERVATION_REFERENCE_MISSING": 1,
        "OBSERVATION_STALE": 1,
        "SNAPSHOT_REFERENCE_MISSING": 1,
        "SNAPSHOT_STALE": 1,
    }


def test_prov15_requires_crypto_and_weather_model_revision_coverage():
    report = build_attribution_regression_report(
        [_event("crypto-old", "crypto_v2", "2.0.0")],
        expected_model_versions={
            "crypto_v2": ["2.0.0", "2.1.0"],
            "weather_v2": ["2.0.0", "2.1.0"],
        },
        generated_at=NOW,
    )
    assert report["summary"]["coverage_ratio"] == 0.25
    assert report["summary"]["missing_revisions"] == [
        {"model_name": "crypto_v2", "model_version": "2.1.0"},
        {"model_name": "weather_v2", "model_version": "2.0.0"},
        {"model_name": "weather_v2", "model_version": "2.1.0"},
    ]


def test_prov15_matches_deterministic_golden_report():
    events = [
        _event("weather-21", "weather_v2", "2.1.0", observation_age=300, snapshot_age=20),
        _event("crypto-20", "crypto_v2", "2.0.0", observation_age=60, snapshot_age=10),
        _event("weather-20", "weather_v2", "2.0.0", observation_age=600, snapshot_age=30),
        _event("crypto-21", "crypto_v2", "2.1.0", observation_age=120, snapshot_age=15),
    ]
    report = build_attribution_regression_report(
        reversed(events),
        expected_model_versions={
            "weather_v2": ["2.1.0", "2.0.0"],
            "crypto_v2": ["2.1.0", "2.0.0"],
        },
        generated_at=NOW,
    )
    golden = json.loads(
        (__import__("pathlib").Path(__file__).parent / "golden" / "prov15_attribution_report.json")
        .read_text(encoding="utf-8")
    )
    assert report == golden


def _event(
    key, model, version, *, observation_age=60, snapshot_age=10,
    observation=..., snapshot=...,
):
    event_at = NOW
    if observation is ...:
        observation = {
            "table": "crypto_prices" if model == "crypto_v2" else "weather_forecasts",
            "id": 1,
            "observed_at": (
                event_at - timedelta(seconds=observation_age)
            ).isoformat(),
        }
    if snapshot is ...:
        snapshot = {
            "table": "market_snapshots", "id": 2,
            "captured_at": (
                event_at - timedelta(seconds=snapshot_age)
            ).isoformat(),
        }
    return {
        "event_key": key, "model_name": model, "model_version": version,
        "event_at": event_at.isoformat(), "source_observation_ref": observation,
        "market_snapshot_ref": snapshot,
    }
