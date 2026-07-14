from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r27_cloud_ui_private_access_auth_draft import (
    build_phase3bb_r27_cloud_ui_private_access_auth_draft,
    write_phase3bb_r27_cloud_ui_private_access_auth_draft_report,
)


def test_phase3bb_r27_drafts_private_vpn_without_install(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r27_cloud_ui_private_access_auth_draft_report(
            session,
            output_dir=reports_dir / "phase3bb_r27",
            reports_dir=reports_dir,
            preferred_access="private_vpn",
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["private_access_decision"]
    assert payload["phase"] == "3BB-R27-CLOUD-UI-PRIVATE-ACCESS-AUTH-DRAFT"
    assert decision["status"] == "PRIVATE_ACCESS_DRAFT_READY_NO_INSTALL"
    assert decision["selected_option"] == "PRIVATE_VPN_OR_TAILSCALE"
    assert decision["ssh_tunnel_allowed_now"] is True
    assert decision["public_https_allowed_now"] is False
    assert decision["install_allowed_now"] is False
    assert payload["safety_flags"]["no_nginx_install"] is True
    assert payload["safety_flags"]["no_firewall_change"] is True
    assert artifacts.manifest_path.exists()


def test_phase3bb_r27_can_select_ssh_tunnel(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r27_cloud_ui_private_access_auth_draft(
            session,
            output_dir=reports_dir / "phase3bb_r27",
            reports_dir=reports_dir,
            preferred_access="ssh_tunnel",
        )

    decision = payload["private_access_decision"]
    assert decision["status"] == "PRIVATE_ACCESS_DRAFT_READY_NO_INSTALL"
    assert decision["selected_option"] == "SSH_TUNNEL_ONLY"
    assert decision["selected_private_access_plan"]["status"] == "APPROVED_NOW"


def test_phase3bb_r27_blocks_when_r26_not_tunnel_ready(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, r26_ssh_allowed=False)

    with session_factory() as session:
        payload = build_phase3bb_r27_cloud_ui_private_access_auth_draft(
            session,
            output_dir=reports_dir / "phase3bb_r27",
            reports_dir=reports_dir,
        )

    decision = payload["private_access_decision"]
    assert decision["status"] == "BLOCKED_PRIVATE_ACCESS_DRAFT"
    assert decision["ssh_tunnel_allowed_now"] is False
    assert decision["first_failed_check"] == "r26_ssh_tunnel_allowed"


def test_phase3bb_r27_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r27-cloud-ui-private-access-auth-draft", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r27-cloud-ui-private-access-auth-draft" in result.output
    assert "--preferred-access" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r27.db'}")
    return get_session_factory(engine)


def _write_context(
    reports_dir: Path,
    *,
    r26_ssh_allowed: bool = True,
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
                    "ssh_tunnel_allowed_now": r26_ssh_allowed,
                    "public_https_allowed_now": False,
                    "r5_pid": 23133,
                    "slow_route_count": 3,
                }
            }
        ),
        encoding="utf-8",
    )
