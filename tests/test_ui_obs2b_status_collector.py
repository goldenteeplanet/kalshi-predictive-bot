import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.ui.status_collector import (
    ScriptedSyntheticRunner,
    run_status_collector,
    write_collector_resilience_preview,
)


BUNDLE_PATH = Path(__file__).parent / "fixtures/ui_obs2/cloud_status_bundle.json"
FIXTURE = Path(__file__).parent / "fixtures/ui_obs2b/collector_fixture.json"
NOW = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)


def _inputs():
    bundle = json.loads(BUNDLE_PATH.read_text())
    spec = {key: bundle[key] for key in ("collected_at", "alerts", "reports", "workstreams")}
    results = {
        name: {"captured_at": bundle["source_timestamps"].get(name, bundle["collected_at"]), "payload": payload}
        for name, payload in bundle["sources"].items()
    }
    return spec, results


def test_ui_obs2b_publishes_atomically_after_all_sources_pass(tmp_path):
    spec, results = _inputs()
    destination = tmp_path / "progress.json"
    report = run_status_collector(spec, ScriptedSyntheticRunner(results), destination, now=NOW, pid=42)
    assert report["status"] == "PASSED"
    assert report["published"] is True
    assert report["atomic_temporary_absent"] is True
    assert len(report["snapshot_sha256"]) == 64
    snapshot = json.loads(destination.read_text())
    assert snapshot["execution_enabled"] is False
    assert snapshot["active_process"]["pid"] == 336539


def test_ui_obs2b_timeout_or_partial_failure_never_replaces_snapshot(tmp_path):
    spec, results = _inputs()
    destination = tmp_path / "progress.json"
    destination.write_text('{"sentinel":"unchanged"}\n')
    results["scheduler"] = {"behavior": "timeout"}
    results["backup_report"] = {"behavior": "failure"}
    report = run_status_collector(spec, ScriptedSyntheticRunner(results), destination, now=NOW, pid=42)
    assert report["published"] is False
    assert "SOURCE_TIMEOUT:scheduler" in report["diagnostics"]
    assert "SOURCE_FAILURE:backup_report:RuntimeError" in report["diagnostics"]
    assert json.loads(destination.read_text()) == {"sentinel": "unchanged"}


def test_ui_obs2b_blocks_overlapping_collector(tmp_path):
    spec, results = _inputs()
    destination = tmp_path / "progress.json"
    lock = destination.with_suffix(".json.collector.lock")
    lock.write_text(json.dumps({"pid": 99, "started_at": "2026-07-18T07:59:30Z"}))
    report = run_status_collector(spec, ScriptedSyntheticRunner(results), destination, now=NOW, pid=42)
    assert report == {
        "phase": "UI-OBS-2B", "status": "BLOCKED", "published": False,
        "diagnostics": ["COLLECTOR_OVERLAP_BLOCKED"], "recovered_stale_lock": False,
    }


def test_ui_obs2b_recovers_dead_stale_lock_and_orphan_temporary(tmp_path):
    spec, results = _inputs()
    destination = tmp_path / "progress.json"
    lock = destination.with_suffix(".json.collector.lock")
    temporary = destination.with_suffix(".json.tmp")
    lock.write_text(json.dumps({"pid": 99, "started_at": "2026-07-18T07:00:00Z"}))
    temporary.write_text("partial")
    report = run_status_collector(
        spec, ScriptedSyntheticRunner(results), destination, now=NOW, pid=42,
        owner_alive=lambda pid: False,
    )
    assert report["published"] is True
    assert report["recovered_stale_lock"] is True
    assert not temporary.exists()
    assert not lock.exists()


def test_ui_obs2b_stale_capture_fails_adapter_and_preserves_previous(tmp_path):
    spec, results = _inputs()
    destination = tmp_path / "progress.json"
    destination.write_text('{"previous":true}\n')
    results["db_locks"]["captured_at"] = "2026-07-18T07:00:00Z"
    report = run_status_collector(spec, ScriptedSyntheticRunner(results), destination, now=NOW, pid=42)
    assert report["published"] is False
    assert "SOURCE_STALE:db_locks" in report["diagnostics"]
    assert json.loads(destination.read_text()) == {"previous": True}


def test_ui_obs2b_preview_is_deterministic_and_local(tmp_path):
    first = json.loads(write_collector_resilience_preview(FIXTURE, tmp_path / "a").read_text())
    second = json.loads(write_collector_resilience_preview(FIXTURE, tmp_path / "b").read_text())
    assert first == second
    assert first["database_writes"] == 0
    assert first["cloud_access"] is False
    assert first["execution_changed"] is False
