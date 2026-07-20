from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.certification_status import build_ci_certification_status


ROOT = Path(__file__).resolve().parents[1]


def test_current_certifications_are_exposed_read_only() -> None:
    status = build_ci_certification_status(ROOT / "reports")
    assert status["status"] == "PASSED"
    assert status["gate"]["status"] == "PASSED"
    assert status["workflow"]["status"] == "PASSED"
    assert status["gate"]["bundle_digest"]
    assert status["workflow"]["workflow_sha256"]
    assert status["drift_failures"] == []
    assert status["retention"] == {"days": 30, "history_limit": 10, "artifact_on_failure": True}
    assert status["controls_available"] is False
    assert status["cloud_access"] is False


def test_missing_reports_block_without_inferred_success(tmp_path: Path) -> None:
    status = build_ci_certification_status(tmp_path)
    assert status["status"] == "BLOCKED"
    assert status["gate"] is None
    assert "UI_OBS_2H_REPORT_MISSING" in status["diagnostics"]


def test_drift_failure_is_visible_in_bounded_history(tmp_path: Path) -> None:
    history = tmp_path / "ui_obs2h/history"
    history.mkdir(parents=True)
    for index in range(12):
        payload = {"phase": "UI-OBS-2H", "status": "FAILED", "diagnostics": ["GOLDEN_DRIFT_DETECTED"]}
        (history / f"{index:02d}.json").write_text(json.dumps(payload), encoding="utf-8")
    status = build_ci_certification_status(tmp_path)
    assert status["status"] == "BLOCKED"
    assert status["history_count"] == 10
    assert len(status["drift_failures"]) == 10


def test_dashboard_page_and_api_include_certification_panel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str((ROOT / "tests/fixtures/ui_obs1/progress_snapshot.json").resolve()))
    monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT", str((ROOT / "reports").resolve()))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(create_app(session_factory=get_session_factory(engine), settings=Settings()))
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert "Offline CI certification" in page.text
    assert "Golden pipeline gate" in page.text
    api = client.get("/api/system/progress")
    assert api.status_code == 200
    assert api.json()["ci_certification"]["status"] == "PASSED"
    assert client.post("/api/system/progress").status_code == 405
