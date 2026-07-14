from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r29_cloud_ui_private_access_install_handoff import (
    build_phase3bb_r29_cloud_ui_private_access_install_handoff,
    write_phase3bb_r29_cloud_ui_private_access_install_handoff_report,
)


def test_phase3bb_r29_writes_private_access_handoff_bundle(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r29_cloud_ui_private_access_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r29",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    script = artifacts.operator_handoff_script_path.read_text(encoding="utf-8")
    decision = payload["private_access_handoff_decision"]

    assert payload["phase"] == "3BB-R29-CLOUD-UI-PRIVATE-ACCESS-INSTALL-HANDOFF"
    assert decision["status"] == "HANDOFF_READY_PRIVATE_ACCESS_INSTALL_DRY_RUN"
    assert decision["handoff_ready"] is True
    assert decision["selected_option"] == "PRIVATE_VPN_OR_TAILSCALE"
    assert decision["codex_executed_private_access_install"] is False
    assert decision["public_https_allowed_now"] is False
    assert payload["safety_flags"]["tailscale_commands_executed_by_codex"] == 0
    assert payload["safety_flags"]["no_nginx_install"] is True
    assert payload["safety_flags"]["no_firewall_change"] is True
    assert all(row["passed"] for row in payload["private_access_handoff_checks"])
    assert "tailscale serve --bg http://127.0.0.1:8080" in script
    assert "tailscale funnel" not in script
    assert "ufw allow" not in script
    assert "systemctl start kalshi-ui.service" not in script
    assert "PHASE3BB_R29_EXECUTE" in script
    assert artifacts.manifest_path.exists()


def test_phase3bb_r29_blocks_without_operator_approval(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r29_cloud_ui_private_access_install_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r29",
            reports_dir=reports_dir,
            operator_approved=False,
        )

    decision = payload["private_access_handoff_decision"]
    assert decision["status"] == "BLOCKED_PRIVATE_ACCESS_INSTALL_HANDOFF"
    assert decision["handoff_ready"] is False
    assert decision["first_failed_check"] == "operator_approved_flag_present"
    assert decision["codex_executed_private_access_install"] is False


def test_phase3bb_r29_blocks_public_selected_option(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, selected_option="OPEN_PUBLIC_NO_AUTH")

    with session_factory() as session:
        payload = build_phase3bb_r29_cloud_ui_private_access_install_handoff(
            session,
            output_dir=reports_dir / "phase3bb_r29",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    decision = payload["private_access_handoff_decision"]
    assert decision["status"] == "BLOCKED_PRIVATE_ACCESS_INSTALL_HANDOFF"
    assert decision["handoff_ready"] is False
    assert decision["first_failed_check"] == "selected_private_vpn_plan"


def test_phase3bb_r29_handoff_script_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r29_cloud_ui_private_access_install_handoff_report(
            session,
            output_dir=reports_dir / "phase3bb_r29",
            reports_dir=reports_dir,
            operator_approved=True,
        )

    result = subprocess.run(
        ["bash", str(artifacts.operator_handoff_script_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "dry-run command list" in result.stdout
    assert "no private access install command executed" in result.stdout


def test_phase3bb_r29_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r29-cloud-ui-private-access-install-handoff", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r29-cloud-ui-private-access-install-handoff" in result.output
    assert "--operator-approved" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r29.db'}")
    return get_session_factory(engine)


def _write_context(
    reports_dir: Path,
    *,
    selected_option: str = "PRIVATE_VPN_OR_TAILSCALE",
) -> None:
    generated_at = datetime.now(UTC).isoformat()
    private_plan = {
        "option": "PRIVATE_VPN_OR_TAILSCALE",
        "status": "RECOMMENDED_DRAFT",
        "risk": "LOW_TO_MEDIUM",
        "auth_boundary": "Private network membership plus device identity",
        "public_exposure": "NONE",
        "operator_work": "Install private network client after approval.",
        "notes": "Best always-on private path.",
    }
    public_plan = {
        "option": "OPEN_PUBLIC_NO_AUTH",
        "status": "REJECTED",
        "risk": "HIGH",
        "auth_boundary": "None.",
        "public_exposure": "PUBLIC",
        "operator_work": "Do not run.",
        "notes": "Rejected.",
    }
    selected_plan = private_plan if selected_option == private_plan["option"] else public_plan

    r20_dir = reports_dir / "phase3bb_r20"
    r20_dir.mkdir(parents=True, exist_ok=True)
    ui_plan = {
        "status": "DRAFT_READY_FOR_REVIEW",
        "r18_status": "SYSTEMD_OWNS_R5",
        "r5_pid": 23133,
        "ssh_target": "kalshi@203.0.113.10",
        "identity_file": "/home/james/.ssh/id_ed25519_do",
        "remote_app_path": "/opt/kalshi-predictive-bot",
        "remote_env_path": "/etc/kalshi-bot/kalshi-bot.env",
        "ui_bind_host": "127.0.0.1",
        "ui_bind_port": 8080,
        "ui_service_name": "kalshi-ui.service",
        "ssh_tunnel_command": (
            "ssh -i '/home/james/.ssh/id_ed25519_do' "
            "-L 8081:127.0.0.1:8080 'kalshi@203.0.113.10'"
        ),
    }
    (r20_dir / "cloud_ui_service_plan.json").write_text(
        json.dumps({"generated_at": generated_at, "ui_service_plan": ui_plan}),
        encoding="utf-8",
    )

    (reports_dir / "phase3bb_r24").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "verification_decision": {
                    "status": "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
                    "r5_pid": 23133,
                    "ssh_tunnel_command": ui_plan["ssh_tunnel_command"],
                },
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r26").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r26" / "cloud_ui_access_control_decision.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "access_control_decision": {
                    "status": "SSH_TUNNEL_APPROVED_PUBLIC_HTTPS_BLOCKED",
                    "ssh_tunnel_allowed_now": True,
                    "public_https_allowed_now": False,
                    "r5_pid": 23133,
                },
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r27").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r27" / "cloud_ui_private_access_auth_draft.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "selected_private_access_plan": selected_plan,
                "private_access_decision": {
                    "status": "PRIVATE_ACCESS_DRAFT_READY_NO_INSTALL",
                    "selected_option": selected_plan["option"],
                    "selected_private_access_plan": selected_plan,
                    "public_https_allowed_now": False,
                    "r5_pid": 23133,
                },
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r28").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r28" / "cloud_ui_private_access_operator_review.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "selected_private_access_plan": selected_plan,
                "operator_review_decision": {
                    "status": "PRIVATE_ACCESS_OPERATOR_REVIEW_READY_NO_INSTALL",
                    "selected_option": selected_plan["option"],
                    "selected_option_status": selected_plan["status"],
                    "failed_check_count": 0,
                    "public_https_allowed_now": False,
                    "r5_pid": 23133,
                },
            }
        ),
        encoding="utf-8",
    )
