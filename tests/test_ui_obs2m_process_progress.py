from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ui.process_progress import normalize_process_progress
from kalshi_predictor.ui.progress import build_progress_dashboard


REFERENCE = datetime(2026, 7, 18, 0, 10, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def test_runtime_progress_and_eta_derive_from_exact_evidence() -> None:
    process, diagnostics = normalize_process_progress({"name":"job","pid":42,"state":"RUNNING","started_at":"2026-07-18T00:00:00Z","updated_at":"2026-07-18T00:10:00Z","completed_units":2,"total_units":10,"progress_percent":20,"stage":"  forecast   batch "}, reference_time=REFERENCE)
    assert diagnostics == []
    assert process["runtime"] == "10m"
    assert process["runtime_source"] == "TIMESTAMPS"
    assert process["progress_percent"] == 20
    assert process["estimated_remaining"] == "40m"
    assert process["eta_reason"] == "CALCULATED_FROM_THROUGHPUT"
    assert process["stage"] == "forecast batch"


def test_eta_is_unknown_without_throughput_evidence() -> None:
    process, diagnostics = normalize_process_progress({"name":"job","pid":42,"state":"RUNNING","updated_at":"2026-07-18T00:10:00Z","estimated_remaining":"about one hour"}, reference_time=REFERENCE)
    assert diagnostics == []
    assert process["estimated_remaining"] == "unknown"
    assert process["eta_reason"] == "INSUFFICIENT_EVIDENCE"


def test_stale_running_process_fails_closed() -> None:
    process, diagnostics = normalize_process_progress({"pid":42,"state":"RUNNING","updated_at":"2026-07-18T00:00:00Z"}, reference_time=REFERENCE)
    assert process["state"] == "BLOCKED"
    assert process["freshness"] == "STALE"
    assert "PROCESS_EVIDENCE_STALE" in diagnostics


def test_running_without_pid_and_progress_contradiction_fail_closed() -> None:
    process, diagnostics = normalize_process_progress({"state":"RUNNING","updated_at":"2026-07-18T00:10:00Z","completed_units":5,"total_units":10,"progress_percent":80}, reference_time=REFERENCE)
    assert process["state"] == "BLOCKED"
    assert "PROCESS_RUNNING_WITHOUT_PID" in diagnostics
    assert "PROCESS_PROGRESS_CONTRADICTION" in diagnostics


def test_completed_process_requires_completed_units_when_units_exist() -> None:
    process, diagnostics = normalize_process_progress({"state":"PASSED","completed_units":9,"total_units":10}, reference_time=REFERENCE)
    assert process["state"] == "BLOCKED"
    assert process["estimated_remaining"] == "unknown"
    assert "PROCESS_COMPLETION_CONTRADICTION" in diagnostics


def test_eta_above_bound_is_not_displayed() -> None:
    process, _ = normalize_process_progress({"pid":42,"state":"RUNNING","started_at":"2026-07-11T00:10:00Z","updated_at":"2026-07-18T00:10:00Z","completed_units":1,"total_units":100}, reference_time=REFERENCE)
    assert process["estimated_remaining"] == "unknown"
    assert process["eta_reason"] == "ESTIMATE_EXCEEDS_BOUND"


def test_dashboard_exposes_normalized_progress_model() -> None:
    progress = build_progress_dashboard(ROOT / "tests/fixtures/ui_obs1/progress_snapshot.json")
    assert progress["active_process"]["progress_percent"] == 43.75
    assert progress["active_process"]["progress_source"] == "REPORTED"
    assert progress["active_process"]["estimated_remaining"] == "unknown"
    assert progress["active_process"]["eta_reason"] == "INSUFFICIENT_EVIDENCE"
