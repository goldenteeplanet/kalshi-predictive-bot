from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.progress import build_progress_dashboard


FIXTURE = Path(__file__).parent / "fixtures/ui_obs1/progress_snapshot.json"


def test_ui_obs1_normalizes_process_workstreams_and_execution_lock():
    progress = build_progress_dashboard(FIXTURE)
    assert progress["active_process"]["state"] == "RUNNING"
    assert progress["active_process"]["pid"] == 336539
    assert progress["writer"]["lock_status"] == "BUSY_WRITER"
    assert progress["backup"]["integrity"] == "ok"
    assert progress["execution"]["label"] == "DISABLED"
    assert {item["name"] for item in progress["workstreams"]} == {
        "PMB evaluation", "PROV attribution", "NYC weather", "GH liquidity", "Paper readiness",
        "Backup certification", "Scheduler cycles",
    }
    assert progress["workstream_registry"]["coverage"]["complete"] is True
    assert progress["read_only"] is True


def test_ui_obs1_never_infers_success_without_evidence(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"generated_at":"2099-01-01T00:00:00Z","active_process":{"state":"PASSED","name":"gone"},"execution_enabled":false}')
    progress = build_progress_dashboard(path)
    assert progress["active_process"]["state"] == "BLOCKED"
    assert "PROCESS_SUCCESS_WITHOUT_EVIDENCE" in progress["diagnostics"]


def test_ui_obs1_missing_snapshot_is_conservative(tmp_path):
    progress = build_progress_dashboard(tmp_path / "missing.json")
    assert progress["active_process"]["state"] == "BLOCKED"
    assert progress["writer"]["safe_to_start_write"] is False
    assert "STATUS_SNAPSHOT_MISSING" in progress["diagnostics"]


def test_ui_obs1_html_and_api_are_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str(FIXTURE.resolve()))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(create_app(session_factory=get_session_factory(engine), settings=Settings()))
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert "Process & Phase Progress" in page.text
    assert "Paper / live execution" in page.text
    assert "NO CONTROLS" in page.text
    api = client.get("/api/system/progress")
    assert api.status_code == 200
    assert api.json()["read_only"] is True
    assert api.json()["polling"] == {"interval_seconds":15,"timeout_seconds":5,"max_consecutive_failures":3}
    assert client.post("/api/system/progress").status_code == 405
