import json
from pathlib import Path

from kalshi_predictor.ui.progress import build_progress_dashboard
from kalshi_predictor.ui.progress_history import (
    history_path_for,
    load_progress_timeline,
    record_progress_snapshot,
)


SEQUENCE = Path(__file__).parent / "fixtures/ui_obs2c/history_sequence.json"


def _sequence():
    return json.loads(SEQUENCE.read_text())


def test_ui_obs2c_records_idempotently_and_derives_incidents(tmp_path):
    history = tmp_path / "progress.json.history.json"
    snapshots = _sequence()
    assert record_progress_snapshot(snapshots[0], history)["appended"] is True
    assert record_progress_snapshot(snapshots[0], history)["appended"] is False
    record_progress_snapshot(snapshots[1], history)
    result = record_progress_snapshot(snapshots[2], history)
    incidents = result["entries"][-1]["incidents"]
    codes = {item["code"] for item in incidents}
    assert "COLLECTION_GAP" in codes
    assert "PROCESS_STATE_CHANGED" in codes
    assert "PROCESS_DISAPPEARED_WITHOUT_EVIDENCE" in codes
    assert "ALERT_OPENED" in codes


def test_ui_obs2c_retains_only_bounded_newest_entries(tmp_path):
    history = tmp_path / "history.json"
    snapshots = _sequence()
    for snapshot in snapshots:
        record_progress_snapshot(snapshot, history, limit=3)
    payload = json.loads(history.read_text())
    assert len(payload["entries"]) == 3
    assert payload["entries"][0]["generated_at"] == snapshots[1]["generated_at"]
    assert payload["retention_limit"] == 3


def test_ui_obs2c_recovers_corrupt_history_and_orphan_temporary(tmp_path):
    history = tmp_path / "history.json"
    history.write_text("not-json")
    temporary = history.with_suffix(".json.tmp")
    temporary.write_text("partial")
    result = record_progress_snapshot(_sequence()[0], history)
    assert result["appended"] is True
    assert len(result["entries"]) == 1
    assert not temporary.exists()
    assert json.loads(history.read_text())["schema_version"] == 1


def test_ui_obs2c_timeline_is_newest_first_and_bounded(tmp_path):
    history = tmp_path / "history.json"
    for snapshot in _sequence():
        record_progress_snapshot(snapshot, history)
    timeline = load_progress_timeline(history, limit=2)
    assert timeline["count"] == 2
    assert timeline["entries"][0]["generated_at"] == "2026-07-18T08:02:15Z"
    assert timeline["entries"][1]["highest_severity"] == "CRITICAL"


def test_ui_obs2c_dashboard_loads_sibling_history(tmp_path):
    snapshot_path = tmp_path / "progress.json"
    snapshots = _sequence()
    snapshot_path.write_text(json.dumps(snapshots[-1]))
    history = history_path_for(snapshot_path)
    for snapshot in snapshots:
        record_progress_snapshot(snapshot, history)
    dashboard = build_progress_dashboard(snapshot_path)
    assert dashboard["timeline"]["count"] == 4
    assert dashboard["timeline"]["entries"][0]["process_state"] == "PASSED"
