from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.timeline_export_status import (
    build_timeline_export_status,
    timeline_export_path,
)


def test_verified_bundle_exposes_only_fixed_read_only_links(tmp_path: Path) -> None:
    _bundle(tmp_path)
    status = build_timeline_export_status(tmp_path)
    assert status["status"] == "PASSED"
    assert status["read_only"] is True
    assert status["controls_available"] is False
    assert [item["href"] for item in status["exports"]] == [
        "/system/progress/certification-export/json",
        "/system/progress/certification-export/csv",
    ]
    assert timeline_export_path(tmp_path, "json").name == "certification_timeline.json"
    assert timeline_export_path(tmp_path, "../../etc/passwd") is None


def test_tampered_export_removes_download_link(tmp_path: Path) -> None:
    phase = _bundle(tmp_path)
    (phase / "certification_timeline.csv").write_text("tampered", encoding="utf-8")
    status = build_timeline_export_status(tmp_path)
    csv_row = next(item for item in status["exports"] if item["kind"] == "CSV")
    assert status["status"] == "FAILED"
    assert csv_row["verified"] is False
    assert csv_row["href"] is None
    assert "CSV_EXPORT_HASH_MISMATCH" in status["failures"]


def test_dashboard_download_routes_are_get_only(tmp_path: Path, monkeypatch) -> None:
    _bundle(tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps({"generated_at": "2026-07-19T22:00:00Z", "execution_enabled": False}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT", str(tmp_path))
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str(snapshot))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(
        create_app(session_factory=get_session_factory(engine), settings=Settings())
    )
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert "Certified timeline exports" in page.text
    assert "Download certification_timeline.json" in page.text
    download = client.get("/system/progress/certification-export/json")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert client.post("/system/progress/certification-export/json").status_code == 405
    assert client.get("/system/progress/certification-export/unknown").status_code == 404


def _bundle(root: Path) -> Path:
    phase = root / "phase_ui_obs5p"
    phase.mkdir(parents=True)
    json_bytes = b'{"events":[]}\n'
    csv_bytes = b"timestamp,subject,event_type,before,after,resolved\n"
    (phase / "certification_timeline.json").write_bytes(json_bytes)
    (phase / "certification_timeline.csv").write_bytes(csv_bytes)
    json_sha = hashlib.sha256(json_bytes).hexdigest()
    csv_sha = hashlib.sha256(csv_bytes).hexdigest()
    manifest = {
        "status": "PASSED",
        "generated_at": "2026-07-19T22:00:00Z",
        "bundle_sha256": "b" * 64,
        "transition_count": 16,
        "source": {"retention_limit": 96, "entry_count": 3, "sha256": "a" * 64},
        "exports": {
            "json": {"name": "certification_timeline.json", "sha256": json_sha},
            "csv": {"name": "certification_timeline.csv", "sha256": csv_sha},
        },
        "failures": [],
    }
    (phase / "ui_obs5p_certification_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return phase
