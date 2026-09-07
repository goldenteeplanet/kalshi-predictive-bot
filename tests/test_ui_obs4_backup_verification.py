from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.backup_verification import normalize_backup_verification
from kalshi_predictor.ui.progress import build_progress_dashboard


def verification(**updates):
    row = {
        "state": "RUNNING",
        "stage": "integrity_check",
        "pid": 367918,
        "elapsed_seconds": 4560,
        "database_bytes": 22_318_972_928,
        "read_bytes": 11_159_486_464,
        "progress_percent_lower_bound": 50,
        "estimated_remaining_seconds": 4560,
        "io_advanced": True,
        "stale": False,
        "integrity_status": "PENDING",
        "sha256_status": "PENDING",
        "path": "/mnt/backup/database.db",
    }
    row.update(updates)
    return row


def test_running_verification_is_normalized_conservatively() -> None:
    result, diagnostics = normalize_backup_verification(verification())
    assert diagnostics == []
    assert result["elapsed_label"] == "1h 16m"
    assert result["eta_confidence"] == "LOWER_BOUND_ONLY"
    assert result["deployment_blocked"] is True
    assert result["safe_to_interrupt"] is False


def test_success_without_integrity_and_hash_is_blocked() -> None:
    result, diagnostics = normalize_backup_verification(verification(state="PASSED"))
    assert result["state"] == "BLOCKED"
    assert "BACKUP_SUCCESS_WITHOUT_VERIFICATION" in diagnostics


def test_verified_completion_unblocks_deployment_gate() -> None:
    result, diagnostics = normalize_backup_verification(
        verification(
            state="PASSED",
            stage="certified",
            integrity_status="OK",
            sha256_status="VERIFIED",
            progress_percent_lower_bound=100,
        )
    )
    assert diagnostics == []
    assert result["deployment_blocked"] is False


def test_stale_io_emits_diagnostic_but_never_authorizes_interrupt() -> None:
    result, diagnostics = normalize_backup_verification(verification(stale=True, io_advanced=False))
    assert "BACKUP_VERIFICATION_IO_STALE" in diagnostics
    assert result["safe_to_interrupt"] is False
    assert result["eta_confidence"] == "UNAVAILABLE"


def test_dashboard_api_and_html_expose_read_only_verification(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "generated_at": "2099-01-01T00:00:00Z",
                "execution_enabled": False,
                "active_process": {"state": "RUNNING", "name": "integrity", "stage": "scan"},
                "backup_verification": verification(),
            }
        ),
        encoding="utf-8",
    )
    progress = build_progress_dashboard(snapshot)
    assert progress["backup_verification"]["pid"] == 367918
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str(snapshot))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(
        create_app(session_factory=get_session_factory(engine), settings=Settings())
    )
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert "Backup verification progress" in page.text
    assert "Safe to interrupt" in page.text
    api = client.get("/api/system/progress")
    assert api.json()["backup_verification"]["deployment_blocked"] is True
    assert client.post("/api/system/progress").status_code == 405
