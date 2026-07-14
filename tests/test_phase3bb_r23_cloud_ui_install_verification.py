from __future__ import annotations

import json
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
from kalshi_predictor.phase3bb_r23_cloud_ui_install_verification import (
    build_phase3bb_r23_cloud_ui_install_verification,
    write_phase3bb_r23_cloud_ui_install_verification_report,
)


def test_phase3bb_r23_verifies_ui_enable_no_start(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r23_cloud_ui_install_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r23",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["verification_decision"]

    assert payload["phase"] == "3BB-R23-CLOUD-UI-INSTALL-VERIFICATION"
    assert decision["status"] == "VERIFIED_UI_ENABLE_NO_START_HANDOFF"
    assert decision["verification_passed"] is True
    assert decision["ui_service_loaded"] is True
    assert decision["ui_service_enabled"] is True
    assert decision["ui_service_started"] is False
    assert decision["ui_port_listening"] is False
    assert payload["safety_flags"]["starts_ui_service"] is False
    assert payload["safety_flags"]["systemctl_mutating_commands_executed"] == 0
    assert all(row["passed"] for row in payload["verification_checks"])
    assert artifacts.manifest_path.exists()


def test_phase3bb_r23_blocks_if_ui_started_unexpectedly(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r23_cloud_ui_install_verification(
            session,
            output_dir=reports_dir / "phase3bb_r23",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(ui_active=True),
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "BLOCKED_UI_INSTALL_VERIFICATION"
    assert decision["verification_passed"] is False
    assert decision["first_failed_check"] == "ui_service_not_started"


def test_phase3bb_r23_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r23-cloud-ui-install-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r23-cloud-ui-install-verification" in result.output
    assert "--ui-service-name" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r23.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
    r17_dir = reports_dir / "phase3bb_r17"
    r17_dir.mkdir(parents=True, exist_ok=True)
    (r17_dir / "cloud_service_install_verification.json").write_text(
        json.dumps(
            {
                "verification_decision": {
                    "status": "VERIFIED_ENABLE_NO_START_HANDOFF",
                    "verification_passed": True,
                    "current_r5_pid": 23133,
                    "service_enabled": True,
                    "service_started": True,
                }
            }
        ),
        encoding="utf-8",
    )

    r22_dir = reports_dir / "phase3bb_r22"
    r22_dir.mkdir(parents=True, exist_ok=True)
    (r22_dir / "cloud_ui_install_handoff.json").write_text(
        json.dumps(
            {
                "handoff_decision": {
                    "status": "HANDOFF_READY_UI_INSTALL_ENABLE_NO_START",
                    "handoff_ready": True,
                    "r5_pid": 23133,
                    "ui_service_name": "kalshi-ui.service",
                    "codex_executed_start": False,
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_runner(*, ui_active: bool = False):
    def runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        del target
        outputs = _r18_outputs() | _ui_outputs(ui_active=ui_active)
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


def _r18_outputs() -> dict[str, str]:
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
    return {
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
        "r5_guard_dry_run": json.dumps({"after": {"guard": r5_status["guard"]}}),
        "db_writer_monitor": json.dumps({"status": "CLEAR", "safe_to_start_write": True}),
        "r5_processes": (
            f"{pid} /opt/kalshi-predictive-bot/.venv/bin/python "
            "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch\n"
        ),
        "r5_pid_file": f"{pid}\n",
    }


def _ui_outputs(*, ui_active: bool) -> dict[str, str]:
    return {
        "ui_systemd_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                f"ActiveState={'active' if ui_active else 'inactive'}",
                f"SubState={'running' if ui_active else 'dead'}",
                "FragmentPath=/etc/systemd/system/kalshi-ui.service",
                f"ExecMainPID={4444 if ui_active else 0}",
            ]
        ),
        "ui_systemd_enabled": "enabled\n",
        "ui_systemd_active": "active\n" if ui_active else "inactive\n",
        "ui_processes": "4444 kalshi-bot ui --host 127.0.0.1 --port 8080\n"
        if ui_active
        else "",
        "ui_listeners": "LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*\n" if ui_active else "",
        "nginx_state": "nginx_missing\n",
        "local_ui_http": "HTTP_OK\n" if ui_active else "HTTP_NOT_READY\n",
    }
