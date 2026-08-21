import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.ui.cohort_progress import load_cohort_progress


def _gate_module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_cohort_gate_runner.py"
    spec = importlib.util.spec_from_file_location("crypto_cohort_gate_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status_module():
    path = Path(__file__).parents[1] / "scripts" / "crypto_liquidity_coverage_status.py"
    spec = importlib.util.spec_from_file_location("crypto_liquidity_coverage_status", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_runs_once_when_threshold_is_reached():
    module = _gate_module()
    status = {"minimum_settled_interval_cohort": 10, "settled_interval_eligible_events": 10}
    assert module.should_rerun(status, {}) is True
    assert module.should_rerun(status, {"last_successful_threshold": 10}) is False


def test_cohort_dashboard_exposes_family_rejections(tmp_path):
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "minimum_settled_interval_cohort": 10,
                "settled_interval_eligible_events": 3,
                "unique_events_measured": 8,
                "interval_eligible_events": 4,
                "families": [
                    {
                        "family": "KXETH:LE25",
                        "rejection_reason_counts": {"COVERAGE": 2},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    payload = load_cohort_progress(status, tmp_path / "missing-result", tmp_path / "missing-state")
    assert payload["settled"] == 3
    assert payload["remaining"] == 7
    assert payload["families"][0]["rejection_reason_counts"] == {"COVERAGE": 2}


def test_eta_requires_history_then_projects_from_settlement_interval():
    module = _status_module()
    assert module.estimate_time_to_target([], current=0, target=10)["status"] == (
        "INSUFFICIENT_SETTLED_HISTORY"
    )
    start = datetime.now(UTC) - timedelta(days=2)
    eta = module.estimate_time_to_target(
        [start.isoformat(), (start + timedelta(days=1)).isoformat()],
        current=2,
        target=10,
    )
    assert eta["status"] == "ESTIMATED"
    assert eta["estimated_days"] == 8.0


def test_cohort_dashboard_exposes_targeted_forecast_telemetry(tmp_path):
    collector = tmp_path / "collector.json"
    collector.write_text(
        json.dumps(
            {
                "targeted_forecast_refresh": {
                    "policy": "TARGETED",
                    "rows": [
                        {
                            "event_ticker": "TARGET",
                            "family": "KXETH|buckets:LE25",
                            "status": "FORECASTED",
                            "midpoint_probe_failures": 2,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    alignment = tmp_path / "alignment.json"
    alignment.write_text(
        json.dumps(
            {
                "alignment_audit_rows": [
                    {
                        "event_ticker": "TARGET",
                        "family": "KXETH|buckets:LE25",
                        "aligned": True,
                        "forecast_capture_lag_seconds": 3.5,
                    },
                    {
                        "event_ticker": "BASELINE",
                        "family": "KXETH|buckets:LE25",
                        "aligned": False,
                        "forecast_capture_lag_seconds": 2400,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = load_cohort_progress(
        tmp_path / "missing-status",
        tmp_path / "missing-result",
        tmp_path / "missing-state",
        tmp_path / "missing-window",
        collector,
        alignment,
    )
    telemetry = payload["targeted_forecast_telemetry"]
    assert telemetry["success_rate"] == 1.0
    assert telemetry["midpoint_probe_failures"] == 2
    assert telemetry["mean_forecast_capture_lag_seconds"] == 3.5
    assert telemetry["families"][0]["aligned_yield_improvement_percentage_points"] == 100.0


def test_cohort_dashboard_prefers_rolling_telemetry(tmp_path):
    telemetry_path = tmp_path / "telemetry.json"
    interval = {
        "successes": 4,
        "sample_size": 5,
        "rate": 0.8,
        "lower": 0.4,
        "upper": 0.96,
    }
    telemetry_path.write_text(
        json.dumps(
            {
                "policy": "ROLLING",
                "observations": [{"observation_id": str(index)} for index in range(7)],
                "summary": {
                    "family": "ALL",
                    "targeted_observations": 5,
                    "forecast_success": interval,
                    "midpoint_probe_failures": 3,
                    "latency_sample_size": 4,
                    "mean_forecast_capture_lag_seconds": 2.5,
                    "maximum_forecast_capture_lag_seconds": 4.0,
                    "targeted_alignment": interval,
                    "historical_alignment": interval,
                    "aligned_yield_improvement": 0.0,
                    "aligned_yield_improvement_interval": {
                        "lower": -0.56,
                        "upper": 0.56,
                    },
                    "families": [],
                },
            }
        ),
        encoding="utf-8",
    )
    missing = tmp_path / "missing"
    payload = load_cohort_progress(
        missing, missing, missing, missing, missing, missing, telemetry_path
    )
    telemetry = payload["targeted_forecast_telemetry"]
    assert telemetry["policy"] == "ROLLING"
    assert telemetry["attempted"] == 5
    assert telemetry["success_interval"]["sample_size"] == 5
    assert telemetry["history_observations"] == 7
