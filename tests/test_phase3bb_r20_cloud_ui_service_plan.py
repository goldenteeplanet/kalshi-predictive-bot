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
from kalshi_predictor.phase3bb_r20_cloud_ui_service_plan import (
    build_phase3bb_r20_cloud_ui_service_plan,
    write_phase3bb_r20_cloud_ui_service_plan_report,
)


def test_phase3bb_r20_writes_draft_only_ui_service_plan(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r20_cloud_ui_service_plan_report(
            session,
            output_dir=reports_dir / "phase3bb_r20",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(r5_systemd_owned=True),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    service_draft = artifacts.service_draft_path.read_text(encoding="utf-8")
    nginx_draft = artifacts.nginx_draft_path.read_text(encoding="utf-8")

    assert payload["phase"] == "3BB-R20-CLOUD-UI-SERVICE-PLAN"
    assert payload["ui_service_plan"]["status"] == "DRAFT_READY_FOR_REVIEW"
    assert payload["ui_service_plan"]["install_allowed_now"] is False
    assert payload["ui_service_plan"]["start_allowed_now"] is False
    assert payload["ui_service_plan"]["expose_public_allowed_now"] is False
    assert payload["safety_flags"]["no_service_install"] is True
    assert payload["safety_flags"]["starts_ui_service"] is False
    assert "kalshi-bot ui --host 127.0.0.1 --port 8080" in service_draft
    assert "Environment=UI_READ_ONLY=true" in service_draft
    assert "Environment=EXECUTION_ENABLED=false" in service_draft
    assert "Environment=EXECUTION_DRY_RUN=true" in service_draft
    assert "proxy_pass http://127.0.0.1:8080;" in nginx_draft
    assert artifacts.manifest_path.exists()


def test_phase3bb_r20_blocks_when_r5_is_not_systemd_owned(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r20_cloud_ui_service_plan(
            session,
            output_dir=reports_dir / "phase3bb_r20",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(r5_systemd_owned=False),
        )

    assert payload["ui_service_plan"]["status"] == "BLOCKED_R5_NOT_SYSTEMD_OWNED"
    assert payload["ui_service_plan"]["ready_for_review"] is False
    assert payload["ui_service_plan"]["install_allowed_now"] is False


def test_phase3bb_r20_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r20-cloud-ui-service-plan", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r20-cloud-ui-service-plan" in result.output
    assert "--ui-service-name" in result.output
    assert "--ui-port" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r20.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
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
                    "app_path": "/opt/kalshi-predictive-bot",
                    "env_path": "/etc/kalshi-bot/kalshi-bot.env",
                    "db_path": "/var/lib/kalshi-bot/kalshi_phase1.db",
                    "reports_path": "/opt/kalshi-predictive-bot/reports",
                },
            }
        ),
        encoding="utf-8",
    )
    r13_dir = reports_dir / "phase3bb_r13"
    r13_dir.mkdir(parents=True, exist_ok=True)
    (r13_dir / "cloud_scheduler_adoption.json").write_text(
        json.dumps(
            {
                "cloud_target": cloud_target,
                "adoption_decision": {
                    "recommendation": "ADOPT_EXISTING_R5",
                    "current_r5_pid": 10573,
                    "guard_status": "RUNNING",
                    "guard_should_stop": False,
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
                    "current_r5_pid": 10573,
                    "service_enabled": True,
                    "service_started": False,
                },
            }
        ),
        encoding="utf-8",
    )


def _fake_runner(*, r5_systemd_owned: bool):
    r5_pid = 16798 if r5_systemd_owned else 10573
    r5_service_active = r5_systemd_owned

    def runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        if probe.name in {
            "systemd_unit",
            "systemd_enabled",
            "systemd_active",
            "r5_status",
            "r5_guard_dry_run",
            "db_writer_monitor",
            "r5_processes",
            "r5_pid_file",
        }:
            outputs = _r18_outputs(pid=r5_pid, service_active=r5_service_active)
        else:
            outputs = _ui_outputs()
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


def _r18_outputs(*, pid: int, service_active: bool) -> dict[str, str]:
    service_pid = pid if service_active else 0
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
    writer = {
        "status": "CLEAR" if service_active else "WRITER_ACTIVE",
        "safe_to_start_write": service_active,
        "current_writer_pid": None if service_active else pid,
    }
    active_state = "active" if service_active else "inactive"
    process_line = (
        f"{pid} /opt/kalshi-predictive-bot/.venv/bin/python "
        "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch\n"
    )
    return {
        "systemd_unit": "\n".join(
            [
                "LoadState=loaded",
                "UnitFileState=enabled",
                f"ActiveState={active_state}",
                "SubState=running" if service_active else "SubState=dead",
                "FragmentPath=/etc/systemd/system/kalshi-r5-watcher.service",
                f"ExecMainPID={service_pid}",
            ]
        ),
        "systemd_enabled": "enabled\n",
        "systemd_active": f"{active_state}\n",
        "r5_status": json.dumps(r5_status),
        "r5_guard_dry_run": json.dumps({"after": {"guard": r5_status["guard"]}}),
        "db_writer_monitor": json.dumps(writer),
        "r5_processes": process_line,
        "r5_pid_file": f"{pid}\n",
    }


def _ui_outputs() -> dict[str, str]:
    return {
        "ui_systemd_unit": "\n".join(
            [
                "LoadState=not-found",
                "UnitFileState=disabled",
                "ActiveState=inactive",
                "SubState=dead",
                "FragmentPath=",
                "ExecMainPID=0",
            ]
        ),
        "ui_systemd_enabled": "disabled\n",
        "ui_systemd_active": "inactive\n",
        "ui_processes": "",
        "ui_listeners": "",
        "nginx_state": "nginx_missing\n",
        "local_ui_http": "HTTP_NOT_READY\n",
    }
