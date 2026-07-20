from datetime import UTC, datetime

import pytest

from kalshi_predictor.provenance.export_adapter import (
    adapt_runtime_provenance_export,
    compare_runtime_export_to_golden,
)

NOW = datetime(2026, 7, 17, 21, 0, tzinfo=UTC)


def test_prov15b_adapts_prov2_envelopes_without_database_access():
    payload = {"phase": "PROV-2", "database_writes": 0, "rows": [
        {"digest": "abc", "attribution": {
            "model_name": "crypto_v2", "model_version": "2.0.0",
            "forecast_generated_at": "2026-07-17T20:59:00+00:00",
            "ranking_generated_at": "2026-07-17T21:00:00+00:00",
            "observation_id": "crypto_price:7",
            "observation_timestamp": "2026-07-17T20:58:00+00:00",
            "orderbook_snapshot_id": "snapshot:8",
            "orderbook_timestamp": "2026-07-17T20:59:30+00:00",
        }},
    ]}
    report = compare_runtime_export_to_golden(
        payload, expected_model_versions={"crypto_v2": ["2.0.0"]}, generated_at=NOW
    )
    assert report["summary"]["passed"] is True
    assert report["rows"][0]["observation_age_seconds"] == 120.0
    assert report["rows"][0]["snapshot_age_seconds"] == 30.0
    assert report["database_access"] is False
    assert report["source_database_writes"] == 0


def test_prov15b_adapts_runtime_event_export_and_surfaces_missing_snapshot_time():
    payload = {"phase": "PROV-12", "rows": [{
        "event_key": "FORECAST_CREATED:1:-", "model_name": "weather_v2",
        "model_version": "2.0.0", "event_at": "2026-07-17T21:00:00+00:00",
        "source_observation_ref_json": (
            '{"table":"weather_forecasts","id":9,'
            '"forecast_generated_at":"2026-07-17T20:50:00+00:00"}'
        ),
        "market_snapshot_id": 10,
    }]}
    events = adapt_runtime_provenance_export(payload)
    report = compare_runtime_export_to_golden(
        payload, expected_model_versions={"weather_v2": ["2.0.0"]}, generated_at=NOW
    )
    assert events[0]["market_snapshot_ref"]["id"] == 10
    assert report["summary"]["failure_counts"] == {"SNAPSHOT_TIMESTAMP_INVALID": 1}


def test_prov15b_rejects_unbounded_or_malformed_exports():
    with pytest.raises(ValueError, match="exceeds max_rows"):
        adapt_runtime_provenance_export({"rows": [{}, {}]}, max_rows=1)
    with pytest.raises(ValueError, match="rows list"):
        adapt_runtime_provenance_export({"records": []})
