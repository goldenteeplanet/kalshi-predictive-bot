from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3bb_r1
from kalshi_predictor.cli import app


def _running_r5_status(*, should_stop: bool = False) -> dict[str, object]:
    return {
        "process": {"status": "RUNNING"},
        "guard": {
            "status": "OVERRUNNING" if should_stop else "RUNNING",
            "should_stop": should_stop,
        },
    }


def test_active_non_r5_writer_blocks_writer_jobs() -> None:
    action = phase3bb_r1.choose_scheduler_action(
        writer={
            "current_writer_pid": 1234,
            "current_writer_command": "kalshi-bot snapshot --status open",
        },
        r5_status=_running_r5_status(),
        weather={"ranking_job_due": True},
        artifact_statuses={},
        pending_writer_capable_jobs=[],
    )

    assert action["action"] == "BLOCKED_BY_ACTIVE_WRITER"
    assert action["command"] == "kalshi-bot db-writer-monitor --json"
    assert action["clearly_wait"] is True


def test_overrun_r5_writer_uses_guarded_stop_path() -> None:
    action = phase3bb_r1.choose_scheduler_action(
        writer={
            "current_writer_pid": 10041,
            "current_writer_command": "kalshi-bot phase3bc-r5-unattended-start",
        },
        r5_status=_running_r5_status(should_stop=True),
        weather={"ranking_job_due": True},
        artifact_statuses={},
        pending_writer_capable_jobs=[],
    )

    assert action["action"] == "STOP_OVERRUN_R5"
    assert "phase3bc-r5-unattended-guard" in action["command"]
    assert "--stop-overrun" in action["command"]


def test_running_r5_never_gets_duplicate_start_recommendation() -> None:
    action = phase3bb_r1.choose_scheduler_action(
        writer={"current_writer_pid": None},
        r5_status=_running_r5_status(),
        weather={"ranking_job_due": True},
        artifact_statuses={},
        pending_writer_capable_jobs=[],
    )
    checks = phase3bb_r1.command_checks_for_scheduler(
        action["command"],
        r5_running=True,
    )

    assert action["action"] == "RUN_WEATHER_RANKING"
    assert checks["duplicate_r5_start_risk"] is False
    assert "phase3bc-r5-unattended-start" not in action["command"]


def test_stopped_r5_starts_exactly_one_guarded_watcher() -> None:
    action = phase3bb_r1.choose_scheduler_action(
        writer={"current_writer_pid": None},
        r5_status={"process": {"status": "STOPPED"}, "guard": {"status": "STOPPED"}},
        weather={"ranking_job_due": True},
        artifact_statuses={},
        pending_writer_capable_jobs=[],
    )
    checks = phase3bb_r1.command_checks_for_scheduler(
        action["command"],
        r5_running=False,
    )

    assert action["action"] == "START_R5"
    assert checks["r5_start_commands"] == ["phase3bc-r5-unattended-start"]
    assert checks["duplicate_r5_start_risk"] is False


def test_settlement_health_can_be_selected_after_higher_priority_lanes() -> None:
    action = phase3bb_r1.choose_scheduler_action(
        writer={"current_writer_pid": None},
        r5_status=_running_r5_status(),
        weather={"ranking_job_due": False},
        artifact_statuses={
            "paper_ready_truth": {"freshness": "CURRENT"},
            "weather_paper_gate": {"exists": True, "freshness": "CURRENT"},
            "settlement_health": {"freshness": "MISSING"},
            "category_backlog": {"freshness": "CURRENT"},
        },
        pending_writer_capable_jobs=[],
    )

    assert action["action"] == "RUN_SETTLEMENT_HEALTH"
    assert "phase3an-settlement-health-confirm" in action["command"]


def test_scheduler_command_checks_reject_missing_and_trade_commands() -> None:
    safe = phase3bb_r1.command_checks_for_scheduler(
        "kalshi-bot db-writer-monitor --json\n"
        "kalshi-bot phase3ba-r2-weather-ranking-activation "
        "--output-dir reports/phase3ba_r2 --reports-dir reports --limit 100",
        r5_running=True,
    )
    unsafe = phase3bb_r1.command_checks_for_scheduler(
        "kalshi-bot imaginary-command\nkalshi-bot place-order --ticker X",
        r5_running=False,
    )

    assert safe["all_recommended_commands_registered"] is True
    assert safe["contains_forbidden_trade_command"] is False
    assert unsafe["unregistered_commands"] == ["imaginary-command", "place-order"]
    assert unsafe["contains_forbidden_trade_command"] is True


def test_phase3bb_r1_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r1-operator-scheduler", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r1-operator-scheduler" in result.output
