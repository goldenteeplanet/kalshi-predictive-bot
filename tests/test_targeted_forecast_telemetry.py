from kalshi_predictor.research.targeted_forecast_telemetry import (
    update_history,
    wilson_interval,
)


def test_history_is_event_level_and_retry_idempotent():
    collector = {
        "generated_at": "2026-08-19T12:00:00+00:00",
        "targeted_forecast_refresh": {
            "rows": [
                {
                    "event_ticker": "TARGET",
                    "family": "KXETH|buckets:LE25",
                    "status": "FORECASTED",
                    "ticker": "TARGET-B100",
                    "forecast_timestamp": "2026-08-19T12:00:00+00:00",
                    "buckets_probed": 2,
                    "midpoint_probe_failures": 1,
                    "one_sided_bound_uses": 1,
                }
            ]
        },
    }
    alignment = {
        "alignment_audit_rows": [
            {
                "event_ticker": "TARGET",
                "family": "KXETH|buckets:LE25",
                "coverage_id": 2,
                "capture_timestamp": "2026-08-19T12:00:02+00:00",
                "aligned": True,
                "forecast_capture_lag_seconds": 2.0,
                "forecast_ticker": "TARGET-B100",
                "forecast_timestamp": "2026-08-19T12:00:00+00:00",
            },
            {
                "event_ticker": "BASELINE",
                "family": "KXETH|buckets:LE25",
                "coverage_id": 1,
                "capture_timestamp": "2026-08-18T12:00:00+00:00",
                "aligned": False,
                "forecast_capture_lag_seconds": 2400.0,
            },
        ]
    }
    first = update_history({}, collector, alignment)
    second = update_history(first, collector, alignment)
    assert len(first["observations"]) == 2
    assert len(second["observations"]) == 2
    family = second["summary"]["families"][0]
    assert family["forecast_success"]["sample_size"] == 1
    assert family["one_sided_bound_uses"] == 1
    assert family["targeted_alignment"]["rate"] == 1.0
    assert family["historical_alignment"]["rate"] == 0.0
    assert family["aligned_yield_improvement"] == 1.0


def test_targeted_alignment_does_not_credit_an_older_forecast():
    collector = {
        "generated_at": "2026-08-19T12:00:00+00:00",
        "targeted_forecast_refresh": {
            "rows": [
                {
                    "event_ticker": "EVENT",
                    "family": "KXETH|buckets:LE25",
                    "ticker": "EVENT-B100",
                    "status": "FORECASTED",
                    "forecast_timestamp": "2026-08-19T12:00:00+00:00",
                }
            ]
        },
    }
    alignment = {
        "alignment_audit_rows": [
            {
                "event_ticker": "EVENT",
                "family": "KXETH|buckets:LE25",
                "forecast_ticker": "EVENT-B100",
                "forecast_timestamp": "2026-08-19T11:00:00+00:00",
                "aligned": True,
            }
        ]
    }
    payload = update_history({}, collector, alignment)
    assert payload["observations"][0]["aligned"] is None
    assert payload["summary"]["targeted_alignment"]["sample_size"] == 0


def test_targeted_alignment_rejects_capture_outside_latency_budget():
    collector = {
        "generated_at": "2026-08-19T12:00:00+00:00",
        "targeted_forecast_refresh": {
            "rows": [
                {
                    "event_ticker": "EVENT",
                    "family": "KXETH|buckets:LE25",
                    "ticker": "EVENT-B100",
                    "status": "FORECASTED",
                    "forecast_timestamp": "2026-08-19T12:00:00+00:00",
                    "immediate_capture": {
                        "attempted": True,
                        "coverage_id": 3,
                        "within_latency_budget": False,
                    },
                }
            ]
        },
    }
    alignment = {
        "alignment_audit_rows": [
            {
                "event_ticker": "EVENT",
                "family": "KXETH|buckets:LE25",
                "coverage_id": 3,
                "forecast_ticker": "EVENT-B100",
                "forecast_timestamp": "2026-08-19T12:00:00+00:00",
                "aligned": True,
            }
        ]
    }
    payload = update_history({}, collector, alignment)
    assert payload["observations"][0]["aligned"] is None
    assert payload["summary"]["immediate_captures_attempted"] == 1
    assert payload["summary"]["immediate_captures_within_budget"] == 0


def test_wilson_interval_reports_sample_size_and_bounded_limits():
    interval = wilson_interval(8, 10)
    assert interval["sample_size"] == 10
    assert interval["rate"] == 0.8
    assert 0.0 < interval["lower"] < interval["rate"]
    assert interval["rate"] < interval["upper"] < 1.0
