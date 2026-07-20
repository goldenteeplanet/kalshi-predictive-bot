from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.prov16 import certify_provenance_export, write_report


def _inputs(tmp_path: Path, *, bad_chain: bool = False, stale: bool = False) -> tuple[Path, Path]:
    created = "2026-01-01T00:00:00Z" if stale else "2026-07-18T00:00:00Z"
    events = [
        {
            "event_id": 1,
            "ticker": "BTC-A",
            "model_name": "crypto_v2",
            "created_at": created,
            "digest": "d1",
            "previous_digest": "BAD" if bad_chain else "GENESIS",
            "observation_id": "obs-1",
            "market_snapshot_id": "snap-1",
            "feature_set_id": "feat-1",
            "forecast_id": "fc-1",
        },
        {
            "event_id": 2,
            "ticker": "NYC-A",
            "model_name": "weather_v2",
            "created_at": "2026-07-18T01:00:00Z",
            "digest": "d2",
            "previous_digest": "d1",
            "observation_id": "obs-2",
            "market_snapshot_id": "snap-2",
            "feature_set_id": "feat-2",
            "forecast_id": "fc-2",
        },
    ]
    event_path = tmp_path / "events.json"
    dashboard_path = tmp_path / "dashboard.json"
    event_path.write_text(json.dumps({"events": events}), encoding="utf-8")
    dashboard_path.write_text(
        json.dumps(
            {
                "event_count": 2,
                "complete_reference_count": 2,
                "model_counts": {"crypto_v2": 1, "weather_v2": 1},
            }
        ),
        encoding="utf-8",
    )
    return event_path, dashboard_path


def test_prov16_passes_exact_export(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path)
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    assert report["status"] == "PASSED"
    assert all(report["gates"].values())
    assert report["guardrails"]["database_opened"] is False


def test_prov16_rejects_invalid_chain(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path, bad_chain=True)
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    assert report["status"] == "FAILED"
    assert report["gates"]["chain_valid"] is False


def test_prov16_rejects_retention_violation(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path, stale=True)
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
        retention_days=30,
    )
    assert report["gates"]["retention_valid"] is False


def test_prov16_rejects_dashboard_drift(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path)
    dashboard.write_text(json.dumps({"event_count": 1}), encoding="utf-8")
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    assert report["gates"]["dashboard_parity"] is False


def test_prov16_report_is_deterministic_except_measured_latency(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path)
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    path = write_report(report, tmp_path / "out")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["report_sha256"] == report["report_sha256"]


def test_prov16_rejects_empty_export_even_when_dashboard_matches(tmp_path: Path) -> None:
    events = tmp_path / "events.json"
    dashboard = tmp_path / "dashboard.json"
    events.write_text(json.dumps({"events": []}), encoding="utf-8")
    dashboard.write_text(
        json.dumps({
            "event_count": 0,
            "complete_reference_count": 0,
            "model_counts": {},
        }),
        encoding="utf-8",
    )
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    assert report["status"] == "FAILED"
    assert report["gates"]["event_export_nonempty"] is False
    assert report["gates"]["required_model_coverage"] is False


def test_prov16_rejects_future_events(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path)
    payload = json.loads(events.read_text(encoding="utf-8"))
    payload["events"][1]["created_at"] = "2026-07-20T01:00:00Z"
    events.write_text(json.dumps(payload), encoding="utf-8")
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    assert report["gates"]["no_future_events"] is False
    assert report["gates"]["retention_valid"] is False


def test_prov16_requires_complete_references_and_both_models(tmp_path: Path) -> None:
    events, dashboard = _inputs(tmp_path)
    payload = json.loads(events.read_text(encoding="utf-8"))
    payload["events"] = payload["events"][:1]
    payload["events"][0]["observation_id"] = None
    events.write_text(json.dumps(payload), encoding="utf-8")
    dashboard.write_text(
        json.dumps({
            "event_count": 1,
            "complete_reference_count": 0,
            "model_counts": {"crypto_v2": 1},
        }),
        encoding="utf-8",
    )
    report = certify_provenance_export(
        events_path=events,
        dashboard_path=dashboard,
        as_of=datetime(2026, 7, 19, tzinfo=UTC),
    )
    assert report["gates"]["complete_reference_coverage"] is False
    assert report["gates"]["required_model_coverage"] is False
