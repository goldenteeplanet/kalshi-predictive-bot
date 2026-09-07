from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.research.fixed_rate_health import update_health, write_health


def test_unified_health_records_sources_stages_and_timeout(tmp_path: Path) -> None:
    started = "2026-08-20T12:00:00+00:00"
    payload = update_health(
        {}, action="start", cycle_id="123", cycle_started_at=started
    )

    payload = update_health(
        payload,
        action="stage",
        stage="coinbase_stage",
        stage_started_at=started,
        exit_code=0,
        timeout_seconds=45,
        coinbase_report={
            "generated_at": "2026-08-20T12:00:02+00:00",
            "jobs": [
                {"symbol": "BTC", "quote_count": 2, "errors": []},
                {"symbol": "ETH", "quote_count": 3, "errors": []},
            ],
            "errors": [],
        },
    )
    payload = update_health(
        payload,
        action="stage",
        stage="gh2_decision_refresh",
        stage_started_at="2026-08-20T12:01:00+00:00",
        exit_code=124,
        timeout_seconds=330,
        gh2_report={"generated_at": "2026-08-19T12:00:00+00:00"},
    )
    payload = update_health(payload, action="finish")

    assert payload["sources"]["websocket"]["status"] == "NOT_APPLICABLE"
    assert payload["sources"]["coinbase"]["status"] == "HEALTHY"
    assert payload["sources"]["coinbase"]["prices_imported"] == 5
    assert payload["sources"]["noaa"]["status"] == "UNAVAILABLE_DUE_TO_STAGE_TIMEOUT"
    assert payload["stages"]["gh2_decision_refresh"]["started_at"]
    assert payload["stages"]["gh2_decision_refresh"]["completed_at"]
    assert payload["timeout_reasons"] == [
        "GH2_DECISION_REFRESH_TIMEOUT_AFTER_330_SECONDS"
    ]
    assert payload["scheduler"]["status"] == "COMPLETE_WITH_ATTENTION"
    assert payload["overall_status"] == "DEGRADED"

    output = tmp_path / "health.json"
    write_health(output, payload)
    assert output.exists()
    assert not output.with_suffix(".json.tmp").exists()


def test_noaa_success_rejects_a_report_from_before_the_cycle() -> None:
    started = datetime(2026, 8, 20, 12, tzinfo=UTC).isoformat()
    payload = update_health(
        {}, action="start", cycle_id="124", cycle_started_at=started
    )

    payload = update_health(
        payload,
        action="stage",
        stage="gh2_decision_refresh",
        stage_started_at=started,
        exit_code=0,
        timeout_seconds=330,
        gh2_report={
            "generated_at": "2026-08-20T11:59:59+00:00",
            "decision_refresh": {
                "weather_features": [{"features_inserted": 10}],
                "weather_forecasts": {"forecasts_inserted": 5},
            },
        },
    )

    assert payload["sources"]["noaa"]["status"] == "STALE_OR_NOT_PUBLISHED"
    assert (
        payload["sources"]["noaa"]["reason"]
        == "GH2_REPORT_NOT_UPDATED_IN_CURRENT_CYCLE"
    )


def test_noaa_accepts_current_reused_feature_evidence_for_every_location() -> None:
    started = "2026-08-20T12:00:00+00:00"
    payload = update_health({}, action="start", cycle_id="125", cycle_started_at=started)
    payload = update_health(
        payload,
        action="stage",
        stage="gh2_decision_refresh",
        stage_started_at=started,
        exit_code=0,
        timeout_seconds=300,
        gh2_report={
            "generated_at": "2026-08-20T12:02:00+00:00",
            "decision_refresh": {
                "weather_features": [
                    {
                        "mode": "DEDICATED_RUNTIME_OWNER_REUSE",
                        "location_count": 2,
                        "features_reused": 2,
                        "fresh_location_count": 2,
                    }
                ],
                "weather_forecasts": {"forecasts_inserted": 4},
            },
        },
    )
    assert payload["sources"]["noaa"]["status"] == "HEALTHY"
    assert payload["sources"]["noaa"]["features"] == 2
    assert payload["sources"]["noaa"]["expected_locations"] == 2
    assert payload["sources"]["noaa"]["forecasts"] == 4
