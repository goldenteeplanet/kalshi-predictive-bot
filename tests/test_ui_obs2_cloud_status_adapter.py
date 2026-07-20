import copy
import json
from pathlib import Path

from kalshi_predictor.ui.cloud_status_adapter import (
    adapt_cloud_status_bundle,
    write_cloud_status_adapter_preview,
)


FIXTURE = Path(__file__).parent / "fixtures/ui_obs2/cloud_status_bundle.json"


def _bundle():
    return json.loads(FIXTURE.read_text())


def test_ui_obs2_adapts_captured_sources_to_ui_obs1_contract():
    report = adapt_cloud_status_bundle(_bundle())
    assert report["adapter_passed"] is True
    assert report["snapshot"]["active_process"]["pid"] == 336539
    assert report["snapshot"]["writer"]["lock_status"] == "BUSY_WRITER"
    assert report["snapshot"]["backup"]["state"] == "PASSED"
    assert report["snapshot"]["scheduler"]["cycle"] == "14 / 32"
    assert report["ui_compatibility"] == {
        "read_only": True, "valid_process_state": True,
        "execution_disabled": True, "required_workstreams_present": True,
    }
    assert report["cloud_access"] is False
    assert report["database_writes"] == 0


def test_ui_obs2_rejects_stale_contradictory_and_wrong_database_sources():
    bundle = _bundle()
    bundle["source_timestamps"]["db_locks"] = "2026-07-18T07:00:00Z"
    bundle["sources"]["db_locks"]["writer_active"] = False
    bundle["sources"]["db_writer_monitor"]["database_path"] = "/wrong.db"
    report = adapt_cloud_status_bundle(bundle)
    assert report["adapter_passed"] is False
    assert "SOURCE_STALE:db_locks" in report["diagnostics"]
    assert "WRITER_LOCK_CONTRADICTION" in report["diagnostics"]
    assert "DATABASE_PATH_MISMATCH" in report["diagnostics"]
    assert report["snapshot"]["writer"]["safe_to_start_write"] is False


def test_ui_obs2_requires_execution_off_and_verified_completion_evidence():
    bundle = _bundle()
    bundle["sources"]["execution"]["execution_enabled"] = True
    bundle["sources"]["process"]["state"] = "PASSED"
    bundle["sources"]["process"]["completion_evidence"] = "missing-report.json"
    report = adapt_cloud_status_bundle(bundle)
    assert "EXECUTION_NOT_EXPLICITLY_DISABLED" in report["diagnostics"]
    assert "PROCESS_COMPLETION_EVIDENCE_INVALID" in report["diagnostics"]
    assert report["snapshot"]["active_process"]["state"] == "BLOCKED"


def test_ui_obs2_rejects_missing_workstream_and_incomplete_backup():
    bundle = _bundle()
    bundle["workstreams"] = bundle["workstreams"][:-1]
    del bundle["sources"]["backup_report"]["sha256"]
    report = adapt_cloud_status_bundle(bundle)
    assert "WORKSTREAM_MISSING:Paper readiness" in report["diagnostics"]
    assert "BACKUP_EVIDENCE_INCOMPLETE" in report["diagnostics"]
    assert report["snapshot"]["backup"]["state"] == "WAITING"


def test_ui_obs2_is_deterministic_local_shadow(tmp_path):
    first = json.loads(write_cloud_status_adapter_preview(FIXTURE, tmp_path / "a").read_text())
    second = json.loads(write_cloud_status_adapter_preview(FIXTURE, tmp_path / "b").read_text())
    assert first == second
    assert first["database_access"] is False
    assert first["cloud_access"] is False
    assert first["execution_changed"] is False
