from __future__ import annotations

from pathlib import Path

from kalshi_predictor.r5_recovery9 import certify_preview, parse_unit

SERVICE = Path("deploy/systemd/kalshi-r5-bounded.service.preview")
TIMER = Path("deploy/systemd/kalshi-r5-bounded.timer.preview")


def test_preview_is_inert_bounded_and_fail_closed() -> None:
    report = certify_preview(SERVICE, TIMER)
    assert report["status"] == "PASSED_LOCAL_PREVIEW"
    assert all(report["gates"].values())
    assert report["deployment_requires_new_approval"] is True
    assert report["rollback_plan"]["commands_executable"] is False
    assert report["rollback_plan"]["legacy_32_cycle_restart_allowed"] is False


def test_timer_is_completion_relative_and_does_not_replay_missed_runs() -> None:
    timer = parse_unit(TIMER)["Timer"]
    assert timer["OnUnitInactiveSec"] == ["15min"]
    assert timer["Persistent"] == ["false"]
    assert "OnCalendar" not in timer


def test_service_runs_exactly_one_cycle_without_restart() -> None:
    service = parse_unit(SERVICE)["Service"]
    command = service["ExecStart"][0]
    assert "--cycles 1" in command
    assert "--interval-minutes 0" in command
    assert service["Restart"] == ["no"]


def test_preview_files_cannot_be_discovered_or_enabled_by_systemd() -> None:
    assert SERVICE.suffix == ".preview"
    assert TIMER.suffix == ".preview"
    assert "Install" not in parse_unit(SERVICE)
    assert "Install" not in parse_unit(TIMER)


def test_report_is_deterministic() -> None:
    assert certify_preview(SERVICE, TIMER) == certify_preview(SERVICE, TIMER)
