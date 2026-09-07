from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r28_cloud_ui_private_access_operator_review import (
    build_phase3bb_r28_cloud_ui_private_access_operator_review,
    write_phase3bb_r28_cloud_ui_private_access_operator_review_report,
)


def test_phase3bb_r28_operator_review_ready_no_install(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r28_cloud_ui_private_access_operator_review_report(
            session,
            output_dir=reports_dir / "phase3bb_r28",
            reports_dir=reports_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["operator_review_decision"]
    assert payload["phase"] == "3BB-R28-CLOUD-UI-PRIVATE-ACCESS-OPERATOR-REVIEW"
    assert decision["status"] == "PRIVATE_ACCESS_OPERATOR_REVIEW_READY_NO_INSTALL"
    assert decision["selected_option"] == "PRIVATE_VPN_OR_TAILSCALE"
    assert decision["review_ready"] is True
    assert decision["install_allowed_now"] is False
    assert decision["private_access_install_allowed_now"] is False
    assert decision["public_https_allowed_now"] is False
    assert payload["safety_flags"]["no_private_access_install"] is True
    assert payload["safety_flags"]["no_nginx_install"] is True
    assert payload["safety_flags"]["no_firewall_change"] is True
    assert payload["safety_flags"]["systemctl_commands_executed"] == 0
    assert payload["safety_flags"]["ssh_commands_executed"] == 0
    assert artifacts.manifest_path.exists()


def test_phase3bb_r28_can_override_to_ssh_tunnel(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r28_cloud_ui_private_access_operator_review(
            session,
            output_dir=reports_dir / "phase3bb_r28",
            reports_dir=reports_dir,
            selected_access="ssh_tunnel",
        )

    decision = payload["operator_review_decision"]
    assert decision["status"] == "PRIVATE_ACCESS_OPERATOR_REVIEW_READY_NO_INSTALL"
    assert decision["selected_option"] == "SSH_TUNNEL_ONLY"
    assert payload["selected_private_access_plan"]["status"] == "APPROVED_NOW"


def test_phase3bb_r28_blocks_rejected_public_option(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, selected_option="OPEN_PUBLIC_NO_AUTH")

    with session_factory() as session:
        payload = build_phase3bb_r28_cloud_ui_private_access_operator_review(
            session,
            output_dir=reports_dir / "phase3bb_r28",
            reports_dir=reports_dir,
        )

    decision = payload["operator_review_decision"]
    assert decision["status"] == "BLOCKED_PRIVATE_ACCESS_OPERATOR_REVIEW"
    assert decision["review_ready"] is False
    assert decision["install_allowed_now"] is False
    assert decision["public_https_allowed_now"] is False
    assert decision["first_failed_check"] == "selected_option_safe"


def test_phase3bb_r28_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r28-cloud-ui-private-access-operator-review", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r28-cloud-ui-private-access-operator-review" in result.output
    assert "--selected-access" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r28.db'}")
    return get_session_factory(engine)


def _write_context(
    reports_dir: Path,
    *,
    selected_option: str = "PRIVATE_VPN_OR_TAILSCALE",
) -> None:
    (reports_dir / "phase3bb_r24").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json").write_text(
        json.dumps(
            {
                "verification_decision": {
                    "status": "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
                    "r5_pid": 23133,
                }
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r25").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r25" / "cloud_ui_operator_smoke_test.json").write_text(
        json.dumps(
            {
                "smoke_decision": {
                    "status": "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS",
                    "r5_pid": 23133,
                }
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r26").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r26" / "cloud_ui_access_control_decision.json").write_text(
        json.dumps(
            {
                "access_control_decision": {
                    "status": "SSH_TUNNEL_APPROVED_PUBLIC_HTTPS_BLOCKED",
                    "ssh_tunnel_allowed_now": True,
                    "public_https_allowed_now": False,
                    "r5_pid": 23133,
                }
            }
        ),
        encoding="utf-8",
    )
    options = [
        {
            "option": "SSH_TUNNEL_ONLY",
            "status": "APPROVED_NOW",
            "risk": "LOW",
            "auth_boundary": "Local SSH key plus remote localhost binding.",
            "public_exposure": "NONE",
            "operator_work": "Run an SSH tunnel from the approved operator device.",
            "notes": "Already validated by R24/R25.",
        },
        {
            "option": "PRIVATE_VPN_OR_TAILSCALE",
            "status": "APPROVED_DRAFT_REQUIRES_OPERATOR_INSTALL",
            "risk": "LOW_MEDIUM",
            "auth_boundary": "Private network identity plus device approval.",
            "public_exposure": "NONE",
            "operator_work": "Install and approve private-network client/server enrollment.",
            "notes": "Selected long-term private access path.",
        },
        {
            "option": "OPEN_PUBLIC_NO_AUTH",
            "status": "REJECTED",
            "risk": "CRITICAL",
            "auth_boundary": "None.",
            "public_exposure": "PUBLIC",
            "operator_work": "Do not run.",
            "notes": "Rejected because it exposes the dashboard without auth.",
        },
    ]
    selected = next(option for option in options if option["option"] == selected_option)
    (reports_dir / "phase3bb_r27").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r27" / "cloud_ui_private_access_auth_draft.json").write_text(
        json.dumps(
            {
                "private_access_options": options,
                "selected_private_access_plan": selected,
                "private_access_decision": {
                    "status": "PRIVATE_ACCESS_DRAFT_READY_NO_INSTALL",
                    "selected_option": selected["option"],
                    "selected_private_access_plan": selected,
                    "ssh_tunnel_allowed_now": True,
                    "public_https_allowed_now": False,
                    "r5_pid": 23133,
                },
            }
        ),
        encoding="utf-8",
    )
