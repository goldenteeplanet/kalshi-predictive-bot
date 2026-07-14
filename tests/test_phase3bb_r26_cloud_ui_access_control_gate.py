from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r26_cloud_ui_access_control_gate import (
    build_phase3bb_r26_cloud_ui_access_control_gate,
    write_phase3bb_r26_cloud_ui_access_control_gate_report,
)


def test_phase3bb_r26_keeps_current_ui_tunnel_only(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, slow_route_seconds=37.5)

    with session_factory() as session:
        artifacts = write_phase3bb_r26_cloud_ui_access_control_gate_report(
            session,
            output_dir=reports_dir / "phase3bb_r26",
            reports_dir=reports_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["access_control_decision"]
    assert payload["phase"] == "3BB-R26-CLOUD-UI-ACCESS-CONTROL-GATE"
    assert decision["status"] == "SSH_TUNNEL_APPROVED_PUBLIC_HTTPS_BLOCKED"
    assert decision["ssh_tunnel_allowed_now"] is True
    assert decision["public_https_allowed_now"] is False
    assert decision["public_https_review_ready"] is False
    assert decision["requires_operator_domain"] is True
    assert decision["requires_operator_ip_cidr"] is True
    assert decision["requires_auth_mode"] is True
    assert decision["slow_route_count"] == 1
    assert payload["safety_flags"]["no_nginx_install"] is True
    assert payload["safety_flags"]["no_firewall_change"] is True
    assert artifacts.manifest_path.exists()


def test_phase3bb_r26_public_https_review_ready_only_with_all_gates(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, slow_route_seconds=4.0)

    with session_factory() as session:
        payload = build_phase3bb_r26_cloud_ui_access_control_gate(
            session,
            output_dir=reports_dir / "phase3bb_r26",
            reports_dir=reports_dir,
            public_domain="ui.example.com",
            operator_ip_cidr="203.0.113.4/32",
            auth_mode="basic_auth",
            max_public_route_seconds=10,
        )

    decision = payload["access_control_decision"]
    assert decision["status"] == "PUBLIC_HTTPS_REVIEW_READY_NO_INSTALL"
    assert decision["public_https_allowed_now"] is False
    assert decision["public_https_review_ready"] is True
    assert decision["install_or_firewall_allowed_now"] is False
    assert payload["exposure_options"][2]["status"] == "REVIEW_READY"


def test_phase3bb_r26_blocks_when_r25_failed(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, r25_status="BLOCKED_CLOUD_UI_OPERATOR_SMOKE_TEST")

    with session_factory() as session:
        payload = build_phase3bb_r26_cloud_ui_access_control_gate(
            session,
            output_dir=reports_dir / "phase3bb_r26",
            reports_dir=reports_dir,
            public_domain="ui.example.com",
            operator_ip_cidr="203.0.113.4/32",
            auth_mode="basic_auth",
        )

    decision = payload["access_control_decision"]
    assert decision["status"] == "BLOCKED_UI_NOT_READY_FOR_ACCESS_DECISION"
    assert decision["ssh_tunnel_allowed_now"] is False
    assert decision["first_failed_check"] == "r25_smoke_passed"


def test_phase3bb_r26_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r26-cloud-ui-access-control-gate", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r26-cloud-ui-access-control-gate" in result.output
    assert "--public-domain" in result.output
    assert "--auth-mode" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r26.db'}")
    return get_session_factory(engine)


def _write_context(
    reports_dir: Path,
    *,
    r25_status: str = "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS",
    slow_route_seconds: float = 4.0,
) -> None:
    (reports_dir / "phase3bb_r20").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r20" / "cloud_ui_service_plan.json").write_text(
        json.dumps(
            {
                "ui_service_plan": {
                    "expose_public_allowed_now": False,
                    "r18_status": "SYSTEMD_OWNS_R5",
                    "r5_pid": 23133,
                }
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r21").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r21" / "cloud_ui_install_review.json").write_text(
        json.dumps({"install_review_decision": {"public_exposure_allowed_now": False}}),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r24").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json").write_text(
        json.dumps(
            {
                "verification_decision": {
                    "status": "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
                    "public_http_listening": False,
                    "public_https_listening": False,
                    "r5_pid": 23133,
                },
                "parsed_ui_state": {
                    "listener_text": "LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*",
                },
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "phase3bb_r25").mkdir(parents=True, exist_ok=True)
    (reports_dir / "phase3bb_r25" / "cloud_ui_operator_smoke_test.json").write_text(
        json.dumps(
            {
                "smoke_decision": {
                    "status": r25_status,
                    "r5_pid": 23133,
                },
                "local_ui_smoke_results": [
                    {
                        "name": "today_workspace",
                        "path": "/",
                        "status_code": 200,
                        "duration_seconds": 0.5,
                    },
                    {
                        "name": "dashboard_snapshot_api",
                        "path": "/api/dashboard/v1/snapshots/current",
                        "status_code": 200,
                        "duration_seconds": slow_route_seconds,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
