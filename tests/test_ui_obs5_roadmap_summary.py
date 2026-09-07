from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.progress import build_progress_dashboard
from kalshi_predictor.ui.roadmap_summary import normalize_roadmap_summary


def lanes():
    return [
        {
            "id": "r5",
            "state": "RUNNING",
            "current_phase": "R5-RECOVERY-3",
            "progress_label": "Integrity check running",
            "blocker": "Integrity and SHA pending",
            "next_phase": "R5-RECOVERY-6",
            "metrics": {"runtime": "2h 26m"},
            "evidence": [{"path": "reports/r5.json"}],
        },
        {
            "id": "provenance",
            "state": "BLOCKED",
            "current_phase": "PROV-14B",
            "blocker": "R5 writer lane",
            "next_phase": "PROV-14C",
        },
        {
            "id": "liquidity",
            "state": "WAITING",
            "current_phase": "GH-1V",
            "blocker": "Two windows",
            "next_phase": "GH-1X",
        },
        {
            "id": "weather",
            "state": "BLOCKED",
            "current_phase": "NYC-W10",
            "blocker": "Live windows",
            "next_phase": "NYC-W11",
        },
        {
            "id": "readiness",
            "state": "BLOCKED",
            "current_phase": "READINESS-1",
            "blocker": "Upstream gates",
            "next_phase": "READINESS-2",
        },
    ]


def test_all_five_lanes_are_independent_and_bounded() -> None:
    summary = normalize_roadmap_summary({"roadmap_summary": lanes()})
    assert summary["state"] == "RUNNING"
    assert summary["reported_lanes"] == 5
    assert {lane["id"] for lane in summary["lanes"]} == {
        "r5",
        "provenance",
        "liquidity",
        "weather",
        "readiness",
    }


def test_passed_lane_without_evidence_is_blocked() -> None:
    payload = lanes()
    payload[1]["state"] = "PASSED"
    summary = normalize_roadmap_summary({"roadmap_summary": payload})
    provenance = next(lane for lane in summary["lanes"] if lane["id"] == "provenance")
    assert provenance["state"] == "BLOCKED"
    assert "ROADMAP_SUCCESS_WITHOUT_EVIDENCE:provenance" in summary["diagnostics"]


def test_missing_lanes_fail_visible() -> None:
    summary = normalize_roadmap_summary({"roadmap_summary": lanes()[:1]})
    assert summary["reported_lanes"] == 1
    assert all(lane["state"] == "BLOCKED" for lane in summary["lanes"][1:])


def test_dashboard_api_and_html_include_roadmap(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "generated_at": "2099-01-01T00:00:00Z",
                "execution_enabled": False,
                "active_process": {"state": "RUNNING", "name": "integrity", "stage": "scan"},
                "roadmap_summary": lanes(),
            }
        )
    )
    progress = build_progress_dashboard(snapshot)
    assert progress["roadmap_summary"]["reported_lanes"] == 5
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str(snapshot))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(
        create_app(session_factory=get_session_factory(engine), settings=Settings())
    )
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert "Trading-bot roadmap" in page.text
    assert "R5 scheduler recovery" in page.text
    api = client.get("/api/system/progress")
    assert api.json()["roadmap_summary"]["required_lanes"] == 5
    assert client.post("/api/system/progress").status_code == 405
