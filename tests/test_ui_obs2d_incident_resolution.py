import json
from pathlib import Path

from kalshi_predictor.ui.incident_resolution import build_incident_resolution_preview
from kalshi_predictor.ui.progress_history import record_progress_snapshot


FIXTURES = Path(__file__).parent / "fixtures"


def _history(tmp_path):
    path = tmp_path / "history.json"
    for snapshot in json.loads((FIXTURES / "ui_obs2c/history_sequence.json").read_text()):
        record_progress_snapshot(snapshot, path)
    return path


def test_ui_obs2d_acknowledges_but_never_hides_critical(tmp_path):
    preview = build_incident_resolution_preview(
        _history(tmp_path), FIXTURES / "ui_obs2d/acknowledgments.json",
        as_of="2026-07-18T08:10:00Z",
    )
    incident = next(row for row in preview["incidents"] if row["incident_id"] == "feadeff673c63204")
    assert incident["acknowledged"] is True
    assert incident["severity"] == "CRITICAL"
    assert incident["visible"] is True
    assert incident["suppression_allowed"] is False
    assert incident["critical_visible_despite_acknowledgment"] is True
    assert preview["summary"]["all_critical_visible"] is True
    assert preview["mutation_endpoints"] == 0


def test_ui_obs2d_requires_verified_resolution_evidence(tmp_path):
    preview = build_incident_resolution_preview(
        _history(tmp_path), FIXTURES / "ui_obs2d/acknowledgments.json",
        as_of="2026-07-18T08:10:00Z",
    )
    resolved = next(row for row in preview["incidents"] if row["incident_id"] == "c0380bce52dabf85")
    assert resolved["status"] == "RESOLVED"
    assert resolved["resolution"]["verified"] is True
    invalid = next(row for row in preview["incidents"] if row["incident_id"] == "dbe5ab24fc042027")
    assert invalid["status"] == "UNRESOLVED"
    assert invalid["resolution"] is None
    assert "RESOLUTION_EVIDENCE_INVALID:dbe5ab24fc042027" in preview["diagnostics"]


def test_ui_obs2d_escalates_unresolved_duration_deterministically(tmp_path):
    preview = build_incident_resolution_preview(
        _history(tmp_path), tmp_path / "missing.json", as_of="2026-07-18T09:10:00Z"
    )
    gap = next(row for row in preview["incidents"] if row["incident_id"] == "c0380bce52dabf85")
    assert gap["severity"] == "CRITICAL"
    assert gap["escalation_reason"] == "UNRESOLVED_60_MINUTES"
    assert gap["duration_minutes"] == 68


def test_ui_obs2d_is_deterministic_and_read_only(tmp_path):
    history = _history(tmp_path)
    first = build_incident_resolution_preview(history, FIXTURES / "ui_obs2d/acknowledgments.json", as_of="2026-07-18T09:10:00Z")
    second = build_incident_resolution_preview(history, FIXTURES / "ui_obs2d/acknowledgments.json", as_of="2026-07-18T09:10:00Z")
    assert first == second
    assert first["read_only"] is True
    assert first["mutation_endpoints"] == 0
