from __future__ import annotations

from typer.testing import CliRunner

from kalshi_predictor import phase3ba_certification as cert
from kalshi_predictor.cli import app


def _status_truth() -> dict[str, object]:
    return {
        "summary": {
            "app_safe": True,
            "active_writer": False,
            "active_writer_pid": None,
            "r5_running": True,
            "crypto_paper_ready": False,
            "crypto_first_blocker": "ZERO_VISIBLE_DEPTH",
            "weather_paper_ready": False,
            "weather_first_blocker": "EV_NOT_POSITIVE",
            "paper_ready_rows": 0,
            "positive_ev_rows": 2,
            "true_first_blocker": "EV_NOT_POSITIVE",
            "phase3ap_is_stale": True,
            "what_codex_should_build_next": "weather / FINISH_WEATHER_ACTIVATION",
            "composite_rows_parked": 29900,
        },
        "writer": {"current_writer_pid": None},
        "r5_status": {"process": {"status": "RUNNING", "phase3bc_r5_pids": [123]}},
        "dashboard_truth": {
            "summary": "0 paper-ready rows",
            "metrics": {"paper_ready_rows": 0, "positive_ev_rows": 2},
        },
        "category_backlog": {
            "immediate_work": {
                "category": "weather",
                "stage": "FINISH_WEATHER_ACTIVATION",
            }
        },
        "command_checks": {
            "all_recommended_commands_registered": True,
            "contains_forbidden_trade_command": False,
        },
        "composite_parking": {
            "parking_status": "PARKED_OUTSIDE_SINGLE_MARKET_LINK_REMEDIATION",
            "exact_component_evidence_rows": 0,
        },
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "thresholds_lowered": False,
        "safety_flags": {"recommended_command_contains_forbidden_trade_command": False},
    }


def test_phase3ba_certification_passes_guarded_paper_only_truth() -> None:
    tests = {"status": "PASSED", "returncode": 0}
    checks = cert._certification_checks(status=_status_truth(), tests=tests)

    assert all(checks.values())


def test_phase3ba_certification_blocks_unsafe_writer() -> None:
    status = _status_truth()
    status["writer"] = {
        "current_writer_pid": 456,
        "current_writer_command": "kalshi-bot sync-markets --status open",
    }

    checks = cert._certification_checks(status=status, tests={"status": "PASSED"})

    assert checks["no_active_unsafe_writer"] is False


def test_phase3ba_certification_requires_registered_safe_commands() -> None:
    status = _status_truth()
    status["command_checks"] = {
        "all_recommended_commands_registered": False,
        "contains_forbidden_trade_command": True,
    }

    checks = cert._certification_checks(status=status, tests={"status": "PASSED"})

    assert checks["recommended_commands_registered"] is False
    assert checks["no_forbidden_trade_commands_recommended"] is False


def test_phase3ba_certification_cli_help_exposes_command() -> None:
    result = CliRunner().invoke(app, ["phase3ba-paper-certification", "--help"])

    assert result.exit_code == 0
    assert "phase3ba-paper-certification" in result.output
