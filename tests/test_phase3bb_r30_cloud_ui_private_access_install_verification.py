from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    RemoteProbe,
    RemoteProbeResult,
)
from kalshi_predictor.phase3bb_r30_cloud_ui_private_access_install_verification import (
    build_phase3bb_r30_cloud_ui_private_access_install_verification,
    write_phase3bb_r30_cloud_ui_private_access_install_verification_report,
)


def test_phase3bb_r30_verifies_private_access_ready(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r30_cloud_ui_private_access_install_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r30",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["verification_decision"]

    assert payload["phase"] == "3BB-R30-CLOUD-UI-PRIVATE-ACCESS-INSTALL-VERIFICATION"
    assert decision["status"] == "VERIFIED_PRIVATE_ACCESS_UI_READY"
    assert decision["verification_passed"] is True
    assert decision["tailscale_installed"] is True
    assert decision["tailscaled_active"] is True
    assert decision["tailscale_authenticated"] is True
    assert decision["tailnet_ipv4"] == "100.64.1.2"
    assert decision["tailscale_serve_configured"] is True
    assert decision["tailscale_funnel_enabled"] is False
    assert payload["safety_flags"]["tailscale_mutating_commands_executed"] == 0
    assert payload["safety_flags"]["systemctl_mutating_commands_executed"] == 0
    assert all(row["passed"] for row in payload["verification_checks"])
    assert artifacts.manifest_path.exists()


def test_phase3bb_r30_blocks_when_tailscale_login_missing(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r30_cloud_ui_private_access_install_verification(
            session,
            output_dir=reports_dir / "phase3bb_r30",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(authenticated=False),
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "PRIVATE_ACCESS_INSTALL_NOT_VERIFIED"
    assert decision["verification_passed"] is False
    assert decision["first_failed_check"] == "tailscale_authenticated"
    assert "PHASE3BB_R29_EXECUTE" in decision["operator_next_command"]


def test_phase3bb_r30_blocks_when_funnel_enabled(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r30_cloud_ui_private_access_install_verification(
            session,
            output_dir=reports_dir / "phase3bb_r30",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(funnel_enabled=True),
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "PRIVATE_ACCESS_INSTALL_NOT_VERIFIED"
    assert decision["verification_passed"] is False
    assert decision["first_failed_check"] == "tailscale_funnel_not_enabled"


def test_phase3bb_r30_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r30-cloud-ui-private-access-install-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r30-cloud-ui-private-access-install-verification" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r30.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    generated_at = datetime.now(UTC).isoformat()
    cloud_target = {
        "ssh_target": "kalshi@203.0.113.10",
        "identity_file": "~/.ssh/id_ed25519_do",
        "app_path": "/opt/kalshi-predictive-bot",
        "env_path": "/etc/kalshi-bot/kalshi-bot.env",
        "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
        "reports_path": "/opt/kalshi-predictive-bot/reports",
    }
    r11_dir = reports_dir / "phase3bb_r11"
    r11_dir.mkdir(parents=True, exist_ok=True)
    (r11_dir / "codex_cloud_context.json").write_text(
        json.dumps(
            {
                "ssh_profile": {
                    "host": "203.0.113.10",
                    "user": "kalshi",
                    "identity_file": "~/.ssh/id_ed25519_do",
                },
                "remote_paths": {
                    "app_path": cloud_target["app_path"],
                    "env_path": cloud_target["env_path"],
                    "db_path": cloud_target["db_path"],
                    "reports_path": cloud_target["reports_path"],
                },
            }
        ),
        encoding="utf-8",
    )
    r17_dir = reports_dir / "phase3bb_r17"
    r17_dir.mkdir(parents=True, exist_ok=True)
    (r17_dir / "cloud_service_install_verification.json").write_text(
        json.dumps(
            {
                "cloud_target": cloud_target,
                "verification_decision": {
                    "status": "VERIFIED_ENABLE_NO_START_HANDOFF",
                    "verification_passed": True,
                    "current_r5_pid": 23133,
                    "service_enabled": True,
                    "service_started": True,
                },
            }
        ),
        encoding="utf-8",
    )
    r29_dir = reports_dir / "phase3bb_r29"
    r29_dir.mkdir(parents=True, exist_ok=True)
    (r29_dir / "cloud_ui_private_access_install_handoff.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "private_access_handoff_decision": {
                    "status": "HANDOFF_READY_PRIVATE_ACCESS_INSTALL_DRY_RUN",
                    "handoff_ready": True,
                    "r5_pid": 23133,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_runner(*, authenticated: bool = True, funnel_enabled: bool = False):
    pid = 23133
    r5_status = {
        "pid": pid,
        "process": {
            "phase3bc_r5_process_running": True,
            "phase3bc_r5_pids": [pid],
            "status": "RUNNING",
        },
        "guard": {"status": "RUNNING", "should_stop": False},
        "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
        "latest_summary": {"paper_ready_candidates": 0, "positive_ev_rows": 3},
    }
    guard = {
        "before": r5_status,
        "after": r5_status,
        "action": {"requested_stop_overrun": False, "terminated_pid": None},
        "status": "RUNNING",
    }
    writer = {"status": "CLEAR", "safe_to_start_write": True, "current_writer_pid": None}
    serve_status = (
        "https://kalshi-bot-01.tailnet.example\n"
        "|-- / proxy http://127.0.0.1:8080\n"
    )
    if funnel_enabled:
        serve_status += "Funnel on\n"
    tailscale_status = (
        {
            "BackendState": "Running",
            "Self": {"Online": True},
            "User": {"LoginName": "operator@example.com"},
        }
        if authenticated
        else {"BackendState": "NeedsLogin", "Self": {"Online": False}}
    )
    outputs = {
        "systemd_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                "ActiveState=active",
                "SubState=running",
                "FragmentPath=/etc/systemd/system/kalshi-r5-watcher.service",
                f"ExecMainPID={pid}",
            ]
        ),
        "systemd_enabled": "enabled\n",
        "systemd_active": "active\n",
        "r5_status": json.dumps(r5_status),
        "r5_guard_dry_run": json.dumps(guard),
        "db_writer_monitor": json.dumps(writer),
        "r5_processes": (
            f"{pid} /opt/kalshi-predictive-bot/.venv/bin/python "
            "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch\n"
        ),
        "r5_pid_file": f"{pid}\n",
        "tailscale_binary": "/usr/bin/tailscale\n",
        "tailscaled_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                "ActiveState=active",
                "SubState=running",
                "ExecMainPID=4242",
            ]
        ),
        "tailscale_status": json.dumps(tailscale_status),
        "tailscale_ip": "100.64.1.2\n",
        "tailscale_serve_status": serve_status,
        "tailscale_local_backend_probe": "HTTP_OK\n",
        "ui_systemd_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                "ActiveState=active",
                "SubState=running",
                "FragmentPath=/etc/systemd/system/kalshi-ui.service",
                "ExecMainPID=28862",
            ]
        ),
        "ui_systemd_enabled": "enabled\n",
        "ui_systemd_active": "active\n",
        "ui_processes": "28862 kalshi-bot ui --host 127.0.0.1 --port 8080\n",
        "ui_listeners": (
            'LISTEN 0 2048 127.0.0.1:8080 0.0.0.0:* users:(("kalshi-bot",pid=28862,fd=9))\n'
            "LISTEN 0 4096 100.64.1.2:443 0.0.0.0:*\n"
            "LISTEN 0 4096 [fd7a:115c:a1e0::9801:7fbb]:443 [::]:*\n"
        ),
        "nginx_state": "nginx_missing\n",
        "local_ui_http": "HTTP_OK\n",
    }

    def runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        del target
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=True,
            exit_code=0,
            stdout=outputs.get(probe.name, ""),
            stderr="",
            duration_seconds=0.01,
        )

    return runner
