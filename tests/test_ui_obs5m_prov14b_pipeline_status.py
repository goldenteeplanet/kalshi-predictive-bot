from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app
from kalshi_predictor.ui.prov14b_pipeline_status import normalize_prov14b_pipeline

NOW = datetime(2026, 7, 19, 19, 0, tzinfo=UTC)


def _pipeline(*, integrity="RUNNING", gate_state="PASSED"):
    stages = {
        "backup_copy": {"state": "PASSED", "evidence": "backup.db", "detail": "23.1 GB"},
        "quick_check": {"state": "PASSED", "evidence": "quick.log", "detail": "ok"},
        "sha256": {"state": "PASSED", "evidence": "sha.txt", "detail": "verified"},
        "integrity_check": {
            "state": integrity,
            "evidence": "integrity.log" if integrity == "PASSED" else "",
            "detail": "actively reading" if integrity == "RUNNING" else "ok",
        },
    }
    gates = {
        gate: {
            "state": gate_state,
            "report_sha256": character * 64,
            "failed_count": 0,
            "generated_at": (NOW - timedelta(seconds=45)).isoformat(),
            "artifact_id": f"prov14b-{gate.lower()}-fixture",
            "runtime_certified": gate == "R2A" and gate_state == "PASSED",
            "detail": f"{gate} exact evidence",
            "evidence_details": [
                {"label": "Report", "value": f"reports/phase_prov14b_{gate.lower()}.json"},
                {"label": "Gate result", "value": "0 failures"},
            ],
        }
        for gate, character in zip(("R2A", "R2B", "R2C", "R2D"), "abcd", strict=True)
    }
    return {
        "captured_at": (NOW - timedelta(seconds=30)).isoformat(),
        "current_stage": "integrity_check",
        "backup_stages": stages,
        "gates": gates,
    }


def test_running_integrity_stage_is_visible_and_blocks_deployment() -> None:
    status, diagnostics = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": _pipeline()}, reference_time=NOW
    )
    assert diagnostics == []
    assert status["state"] == "RUNNING"
    assert status["deployment_blocked"] is True
    assert status["backup_stages"][-1]["state"] == "RUNNING"
    assert [gate["state"] for gate in status["gates"]] == ["PASSED"] * 4
    assert status["controls_available"] is False


def test_pipeline_passes_only_with_backup_and_exact_gate_evidence() -> None:
    status, diagnostics = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": _pipeline(integrity="PASSED")},
        reference_time=NOW,
    )
    assert diagnostics == []
    assert status["state"] == "PASSED"
    assert status["deployment_blocked"] is False
    assert status["runtime_certified"] is True
    assert status["alert_count"] == 0
    assert status["gates"][0]["artifact_href"].startswith("/system/evidence/")


def test_stale_pipeline_fails_closed() -> None:
    pipeline = _pipeline(integrity="PASSED")
    pipeline["captured_at"] = (NOW - timedelta(seconds=301)).isoformat()
    status, diagnostics = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": pipeline}, reference_time=NOW
    )
    assert status["state"] == "BLOCKED"
    assert status["deployment_blocked"] is True
    assert "PROV14B_PIPELINE_EVIDENCE_STALE" in diagnostics


def test_passed_gate_without_hash_is_downgraded_to_blocked() -> None:
    pipeline = _pipeline(integrity="PASSED")
    pipeline["gates"]["R2C"]["report_sha256"] = ""
    status, diagnostics = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": pipeline}, reference_time=NOW
    )
    r2c = next(item for item in status["gates"] if item["id"] == "R2C")
    assert r2c["state"] == "BLOCKED"
    assert status["deployment_blocked"] is True
    assert "PROV14B_R2C_PASS_WITHOUT_EXACT_EVIDENCE" in diagnostics


def test_stale_gate_evidence_is_alerted_and_blocks_success() -> None:
    pipeline = _pipeline(integrity="PASSED")
    pipeline["gates"]["R2B"]["generated_at"] = (NOW - timedelta(seconds=3601)).isoformat()
    status, diagnostics = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": pipeline}, reference_time=NOW
    )
    r2b = next(item for item in status["gates"] if item["id"] == "R2B")
    assert r2b["state"] == "BLOCKED"
    assert r2b["evidence_stale"] is True
    assert status["deployment_blocked"] is True
    assert any(alert["code"] == "PROV14B_R2B_STALE_EVIDENCE" for alert in status["alerts"])
    assert "PROV14B_R2B_PASS_WITHOUT_EXACT_EVIDENCE" in diagnostics


def test_unsafe_artifact_id_is_not_linked() -> None:
    pipeline = _pipeline(integrity="PASSED")
    pipeline["gates"]["R2D"]["artifact_id"] = "../../secrets"
    status, _ = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": pipeline}, reference_time=NOW
    )
    r2d = next(item for item in status["gates"] if item["id"] == "R2D")
    assert r2d["artifact_href"] is None
    assert r2d["state"] == "BLOCKED"
    assert any(alert["code"] == "PROV14B_R2D_MISSING_ARTIFACT" for alert in status["alerts"])


def test_evidence_drill_down_is_bounded() -> None:
    pipeline = _pipeline()
    pipeline["gates"]["R2A"]["evidence_details"] = [
        {"label": f"Evidence {index}", "value": "x" * 250} for index in range(12)
    ]
    status, _ = normalize_prov14b_pipeline(
        {"prov14b_certification_pipeline": pipeline}, reference_time=NOW
    )
    details = status["gates"][0]["evidence_details"]
    assert len(details) == 8
    assert all(len(item["value"]) == 200 for item in details)


def test_unreported_pipeline_is_waiting_without_inferred_success() -> None:
    status, diagnostics = normalize_prov14b_pipeline({}, reference_time=NOW)
    assert diagnostics == []
    assert status["reported"] is False
    assert status["state"] == "WAITING"
    assert status["deployment_blocked"] is True


def test_dashboard_html_and_api_show_read_only_pipeline(tmp_path: Path, monkeypatch) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "generated_at": NOW.isoformat(),
        "execution_enabled": False,
        "active_process": {
            "state": "RUNNING",
            "name": "SQLite integrity verification",
            "stage": "integrity_check",
            "pid": 432831,
        },
        "prov14b_certification_pipeline": _pipeline(),
    }), encoding="utf-8")
    monkeypatch.setenv("KALSHI_PROGRESS_SNAPSHOT_PATH", str(snapshot))
    engine = init_db(f"sqlite:///{tmp_path / 'ui.db'}")
    client = TestClient(
        create_app(session_factory=get_session_factory(engine), settings=Settings())
    )
    page = client.get("/system/progress")
    assert page.status_code == 200
    assert "PROV-14B certification pipeline" in page.text
    assert 'data-pipeline-gate="R2A"' in page.text
    assert 'href="/system/evidence/prov14b-r2a-fixture"' in page.text
    assert "Evidence age:" in page.text
    section = page.text.split('data-prov14b-pipeline', 1)[1].split("</section>", 1)[0]
    assert "<button" not in section
    assert "Execution controls: <strong>NONE</strong>" in section
    api = client.get("/api/system/progress").json()
    assert api["prov14b_pipeline"]["state"] == "RUNNING"
    assert api["prov14b_pipeline"]["read_only"] is True
    assert client.post("/api/system/progress").status_code == 405
