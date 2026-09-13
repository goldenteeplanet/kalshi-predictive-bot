from copy import deepcopy
from datetime import UTC, datetime

import pytest

from kalshi_predictor.phase_gh4 import build_source_reconnect_health


def latest_evidence():
    """Minimal observed GH2 2026-09-13 09:22 report fields, not provider fiction."""
    return {
        "generated_at": "2026-09-13T09:22:03.923377+00:00",
        "active_linking": {
            "weather_decision_candidates": 0,
            "rollover_catalog": {
                "status": "STALE_NOT_IMPORTED",
                "catalog_generated_at": "2026-09-11T11:30:10.033092+00:00",
            },
        },
        "decision_refresh": {
            "weather_features": [],
            "weather_forecasts": {"forecasts_inserted": 0, "skipped": 0, "snapshots_scanned": 0},
        },
        "weather_gate": {"status": "NO_CURRENT_WEATHER_LINKS"},
    }


def row(payload):
    result = build_source_reconnect_health(
        gh2_payload=payload, gh1_payload={}, now=datetime(2026, 9, 13, 9, 30, tzinfo=UTC)
    )
    assert result["status"] == "DEGRADED"
    return next(item for item in result["sources"] if item["source"] == "NOAA weather")


def test_actual_stale_catalog_is_diagnosed_without_claiming_noaa_health():
    result = row(latest_evidence())
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["status_kind"] == "blocked"
    assert "0 linked weather markets" in result["detail"]
    assert "fresh bounded active-market catalog" in result["recovery"]
    assert "do not measure NOAA ingestion" in result["recovery"]
    assert "Retry NOAA" not in result["recovery"]


def test_fresh_catalog_missing_links_does_not_claim_catalog_stale():
    payload = latest_evidence()
    payload["active_linking"]["rollover_catalog"]["status"] = "COMPLETE"
    result = row(payload)
    assert "missing current weather-market links" in result["recovery"]
    assert "catalog" not in result["recovery"]


@pytest.mark.parametrize(
    "generated", ["2026-09-12T09:22:00+00:00", "2026-09-14T09:22:00+00:00", None]
)
def test_stale_future_or_missing_decision_time_does_not_assert_current_link_blocker(generated):
    payload = latest_evidence()
    payload["generated_at"] = generated
    result = row(payload)
    assert "0 linked weather markets" not in result["detail"]
    assert "Retry NOAA" in result["recovery"]


def test_missing_scope_or_actual_forecast_work_preserves_default_diagnosis():
    original = latest_evidence()
    for key in ("active_linking", "weather_gate"):
        payload = deepcopy(original)
        del payload[key]
        assert "Retry NOAA" in row(payload)["recovery"]
    payload = deepcopy(original)
    payload["decision_refresh"]["weather_forecasts"]["snapshots_scanned"] = 1
    assert "Retry NOAA" in row(payload)["recovery"]


@pytest.mark.parametrize(
    "key", ["weather_decision_candidates", "snapshots_scanned", "forecasts_inserted"]
)
@pytest.mark.parametrize("value", [False, None, 5])
def test_bool_missing_or_positive_decisive_counts_do_not_assert_empty_scope(key, value):
    payload = latest_evidence()
    target = (
        payload["active_linking"]
        if key == "weather_decision_candidates"
        else payload["decision_refresh"]["weather_forecasts"]
    )
    if value is None:
        del target[key]
    else:
        target[key] = value
    result = row(payload)
    assert "0 linked weather markets" not in result["detail"]
    assert "Retry NOAA" in result["recovery"]
