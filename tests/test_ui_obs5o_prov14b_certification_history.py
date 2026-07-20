from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_predictor.ui.progress import build_progress_dashboard
from kalshi_predictor.ui.progress_history import history_path_for, record_progress_snapshot
from kalshi_predictor.ui.prov14b_certification_history import (
    load_prov14b_certification_timeline,
)

START = datetime(2026, 7, 19, 20, 0, tzinfo=UTC)


def _snapshot(offset: int, states: tuple[str, str, str, str]) -> dict[str, object]:
    captured = START + timedelta(seconds=offset)
    gates = {}
    for gate, state, character in zip(("R2A", "R2B", "R2C", "R2D"), states, "abcd", strict=True):
        gates[gate] = {
            "state": state,
            "report_sha256": character * 64,
            "failed_count": 0,
            "generated_at": captured.isoformat(),
            "artifact_id": f"prov14b-{gate.lower()}",
        }
    return {
        "generated_at": captured.isoformat(),
        "execution_enabled": False,
        "prov14b_certification_pipeline": {
            "captured_at": captured.isoformat(),
            "current_stage": "certification",
            "backup_stages": {
                stage: {"state": "PASSED", "evidence": stage}
                for stage in ("backup_copy", "quick_check", "sha256", "integrity_check")
            },
            "gates": gates,
        },
    }


def test_timeline_tracks_gate_transitions_and_certification_duration(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    record_progress_snapshot(_snapshot(0, ("WAITING",) * 4), history)
    record_progress_snapshot(_snapshot(30, ("PASSED", "RUNNING", "WAITING", "WAITING")), history)
    record_progress_snapshot(_snapshot(90, ("PASSED",) * 4), history)
    timeline = load_prov14b_certification_timeline(history)
    assert timeline["state"] == "PASSED"
    assert timeline["duration_seconds"] == 90
    assert timeline["duration_state"] == "CERTIFIED"
    assert any(
        event["subject"] == "R2B"
        and event["event_type"] == "GATE_TRANSITION"
        and event["after"] == "PASSED"
        for event in timeline["events"]
    )


def test_timeline_marks_resolved_freshness_alert(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    stale = _snapshot(0, ("PASSED",) * 4)
    stale["prov14b_certification_pipeline"]["gates"]["R2C"]["generated_at"] = (
        START - timedelta(seconds=3601)
    ).isoformat()
    record_progress_snapshot(stale, history)
    record_progress_snapshot(_snapshot(30, ("PASSED",) * 4), history)
    timeline = load_prov14b_certification_timeline(history)
    assert any(
        event["event_type"] == "ALERT_RESOLVED"
        and event["before"] == "PROV14B_R2C_STALE_EVIDENCE"
        and event["resolved"] is True
        for event in timeline["events"]
    )
    assert any(
        event["event_type"] == "FRESHNESS_CHANGED"
        and event["before"] == "STALE"
        and event["after"] == "FRESH"
        for event in timeline["events"]
    )


def test_timeline_is_bounded_and_malformed_history_fails_closed(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    history.write_text("not json", encoding="utf-8")
    failed = load_prov14b_certification_timeline(history)
    assert failed["state"] == "WAITING"
    assert failed["events"] == []
    assert "PROV14B_HISTORY_UNREADABLE" in failed["diagnostics"]
    history.write_text(json.dumps({"entries": "wrong"}), encoding="utf-8")
    invalid = load_prov14b_certification_timeline(history)
    assert "PROV14B_HISTORY_ENTRIES_INVALID" in invalid["diagnostics"]


def test_progress_dashboard_exposes_read_only_timeline(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshots = [
        _snapshot(0, ("WAITING",) * 4),
        _snapshot(30, ("PASSED", "RUNNING", "WAITING", "WAITING")),
    ]
    snapshot_path.write_text(json.dumps(snapshots[-1]), encoding="utf-8")
    for snapshot in snapshots:
        record_progress_snapshot(snapshot, history_path_for(snapshot_path))
    dashboard = build_progress_dashboard(snapshot_path)
    assert dashboard["prov14b_timeline"]["reported"] is True
    assert dashboard["prov14b_timeline"]["read_only"] is True
    assert dashboard["prov14b_timeline"]["controls_available"] is False
    assert dashboard["prov14b_timeline"]["event_count"] > 0
