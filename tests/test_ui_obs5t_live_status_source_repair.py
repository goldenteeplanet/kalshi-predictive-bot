from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.live_status_collector import _timer_next_run, collect_live_snapshot
from kalshi_predictor.ui.progress import build_progress_dashboard


def _runner(command: list[str], _timeout: int) -> str:
    if command[:2] == ["kalshi-bot", "db-writer-monitor"]:
        return "Current writer PID: none\nSafe to start another write job: yes\n"
    if command[:2] == ["kalshi-bot", "db-locks"]:
        return (
            "Database lock diagnostics: OPEN_READERS\n"
            "Safe to start another write job: yes\n"
            "Open DB holders:\n"
            "- pid 42 (reader/unknown) kalshi-bot ui --host 127.0.0.1\n"
        )
    unit = command[2]
    if unit == "kalshi-r5-bounded.service":
        return (
            "ActiveState=active\nSubState=running\nExecMainPID=77\nResult=success\n"
            "MemoryCurrent=104857600\nMemoryPeak=125829120\n"
            "ExecMainStartTimestamp=2026-07-19T22:00:00+00:00\n"
        )
    if unit == "kalshi-r5-bounded.timer":
        return (
            "ActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
            "NextElapseUSecRealtime=Sun 2026-07-19 23:30:00 UTC\n"
        )
    if unit == "kalshi-r5-watcher.service":
        return "ActiveState=inactive\nUnitFileState=disabled\n"
    if unit == "kalshi-ui-status-collector.timer":
        return (
            "ActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
            "NextElapseUSecRealtime=Mon 2026-07-20 00:15:30 UTC\n"
            "LastTriggerUSec=Mon 2026-07-20 00:15:00 UTC\n"
        )
    raise AssertionError(command)


def test_read_only_holder_is_not_classified_as_writer_blocker(tmp_path: Path) -> None:
    snapshot = collect_live_snapshot(
        runner=_runner,
        backup_root=tmp_path,
        reports_root=tmp_path,
    )
    assert snapshot["writer"] == {
        "state": "PASSED",
        "safe_to_start_write": True,
        "lock_status": "READERS_PRESENT",
        "readers_present": True,
        "current_writer_pid": None,
        "pid": None,
    }


def test_backup_storage_uses_exact_configured_root(monkeypatch, tmp_path: Path) -> None:
    seen: list[Path] = []
    usage = type("Usage", (), {"total": 1000, "used": 100, "free": 900})()

    def disk_usage(path: Path):
        seen.append(Path(path))
        return usage

    monkeypatch.setattr("kalshi_predictor.ui.live_status_collector.shutil.disk_usage", disk_usage)
    snapshot = collect_live_snapshot(
        runner=_runner,
        backup_root=tmp_path / "kalshi-backup-02",
        reports_root=tmp_path,
    )
    assert snapshot["storage"]["backup"]["path"] == str(tmp_path / "kalshi-backup-02")
    assert tmp_path / "kalshi-backup-02" in seen


def test_scheduler_exact_runtime_memory_and_timer_sources(tmp_path: Path) -> None:
    snapshot = collect_live_snapshot(
        runner=_runner,
        backup_root=tmp_path,
        reports_root=tmp_path,
        poll_interval_seconds=30,
    )
    scheduler = snapshot["scheduler"]
    assert scheduler["memory_current_bytes"] == 104857600
    assert scheduler["memory_peak_bytes"] == 125829120
    assert scheduler["runtime_seconds"] is not None
    assert scheduler["next_run"] == "Sun 2026-07-19 23:30:00 UTC"
    assert snapshot["collector"]["poll_interval_seconds"] == 30


def test_dashboard_advertises_collector_publish_cadence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KALSHI_CERTIFICATION_REPORTS_ROOT", str(tmp_path))
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-19T22:58:08Z",
                "execution_enabled": False,
                "collector": {"poll_interval_seconds": 30},
            }
        ),
        encoding="utf-8",
    )
    dashboard = build_progress_dashboard(path)
    assert dashboard["polling"]["interval_seconds"] == 30


def test_timer_next_run_is_exact_when_systemd_reports_calendar_elapse() -> None:
    result = _timer_next_run(
        {
            "ActiveState": "active",
            "SubState": "waiting",
            "NextElapseUSecRealtime": "Mon 2026-07-20 00:15:30 UTC",
        },
        service_running=True,
    )
    assert result == {
        "value": "Mon 2026-07-20 00:15:30 UTC",
        "state": "EXACT",
        "basis": "SYSTEMD_NEXT_ELAPSE_REALTIME",
    }


def test_on_unit_active_timer_is_explicitly_pending_during_self_observation() -> None:
    result = _timer_next_run(
        {"ActiveState": "active", "SubState": "waiting", "NextElapseUSecRealtime": ""},
        service_running=True,
    )
    assert result["value"] is None
    assert result["state"] == "PENDING_SERVICE_EXIT"
    assert result["basis"] == "ON_UNIT_ACTIVE_SEC_SCHEDULES_AFTER_COLLECTOR_EXIT"


def test_inactive_bounded_timer_is_truthful_not_a_missing_source_failure() -> None:
    result = _timer_next_run(
        {
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "enabled",
            "NextElapseUSecRealtime": "",
        },
        service_running=False,
    )
    assert result == {
        "value": None,
        "state": "INACTIVE_NO_SCHEDULE",
        "basis": "SYSTEMD_TIMER_INACTIVE",
    }


def test_scheduler_and_collector_timer_sources_are_separate(tmp_path: Path) -> None:
    snapshot = collect_live_snapshot(
        runner=_runner,
        backup_root=tmp_path,
        reports_root=tmp_path,
    )
    assert snapshot["scheduler"]["timer"] == "kalshi-r5-bounded.timer"
    assert snapshot["collector"]["timer"]["name"] == "kalshi-ui-status-collector.timer"
    assert snapshot["collector"]["timer"]["next_run_state"] == "EXACT"
