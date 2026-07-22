import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.refresh_readiness import build_refresh_readiness_dashboard
from kalshi_predictor.utils.time import utc_now


def test_dashboard_distinguishes_missing_source_from_valid_zero(tmp_path: Path) -> None:
    missing = build_refresh_readiness_dashboard(
        refresh_path=tmp_path / "missing.json",
        history_path=tmp_path / "missing.jsonl",
        manifest_path=tmp_path / "manifest.json",
    )
    assert missing["source"]["state"] == "NO_SOURCE_DATA"
    assert missing["empty_state"]["code"] == "NO_SOURCE_DATA"

    refresh = tmp_path / "refresh.json"
    refresh.write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "status": "PAPER_ONLY_SOAK_RUNNING",
                "active_linking": {"crypto_candidates": 0, "weather_candidates": 0},
            }
        ),
        encoding="utf-8",
    )
    valid_zero = build_refresh_readiness_dashboard(
        refresh_path=refresh,
        history_path=tmp_path / "missing.jsonl",
        manifest_path=tmp_path / "manifest.json",
    )
    assert valid_zero["source"]["state"] == "CURRENT"
    assert valid_zero["empty_state"]["code"] == "VALID_ZERO_ACTIVE_MARKETS"


def test_dashboard_builds_stage_blocker_trend_and_candidate_lifecycle(tmp_path: Path) -> None:
    refresh = tmp_path / "refresh.json"
    history = tmp_path / "history.jsonl"
    manifest = tmp_path / "manifest.json"
    refresh.write_text(
        json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "status": "PAPER_ONLY_SOAK_RUNNING",
                "websocket_drain": {"files_drained": 3},
                "cycle_telemetry": {
                    "stages": [
                        {"stage": "drain_websocket_stage", "duration_seconds": 1.25}
                    ]
                },
                "soak": {"healthy_cycle": True, "consecutive_healthy_cycles": 4},
                "paper_readiness": {"total_paper_ready_candidates": 0},
            }
        ),
        encoding="utf-8",
    )
    history.write_text(
        json.dumps(
            {
                "generated_at": (utc_now() - timedelta(minutes=15)).isoformat(),
                "healthy": True,
                "blocker_counts": {"snapshot_missing": 2},
            }
        )
        + "\n"
        + json.dumps(
            {
                "generated_at": utc_now().isoformat(),
                "healthy": True,
                "positive_ev_rows": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "ticker": "KXBTC-TEST",
                        "selection_tier": "MISSING_SNAPSHOT_RECOVERY",
                        "fresh": False,
                        "blocking_gates": ["snapshot_missing"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    dashboard = build_refresh_readiness_dashboard(
        refresh_path=refresh, history_path=history, manifest_path=manifest
    )

    assert dashboard["stages"][0]["duration"] == "1.25s"
    assert dashboard["blockers"][0]["trend"] == "IMPROVING"
    assert dashboard["candidates"][0]["lifecycle"] == "SNAPSHOT_NEEDED"
    assert dashboard["read_only"] is True


def test_refresh_readiness_routes_are_get_only_and_read_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(
        create_app(session_factory=get_session_factory(engine), settings=Settings())
    )

    page = client.get("/system/refresh-readiness")
    api = client.get("/api/system/refresh-readiness")
    roadmap_api = client.get("/api/system/live-roadmap")

    assert page.status_code == 200
    assert "Refresh & Readiness" in page.text
    assert "artifact-backed and read-only" in page.text
    assert api.status_code == 200
    assert api.json()["read_only"] is True
    assert roadmap_api.status_code == 200
    assert roadmap_api.json()["live_execution_enabled"] is False
    assert client.post("/api/system/refresh-readiness").status_code == 405
