from __future__ import annotations

import json
import subprocess
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
from kalshi_predictor.phase3bb_r39_cloud_auto_login_admin_bootstrap import (
    build_phase3bb_r39_cloud_auto_login_admin_bootstrap,
    write_phase3bb_r39_cloud_auto_login_admin_bootstrap_report,
)


def test_phase3bb_r39_writes_auto_login_and_admin_bootstrap(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r39_cloud_auto_login_admin_bootstrap_report(
            session,
            output_dir=reports_dir / "phase3bb_r39",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["bootstrap_decision"]
    ssh_script = artifacts.ssh_config_handoff_path.read_text(encoding="utf-8")
    root_script = artifacts.root_bootstrap_path.read_text(encoding="utf-8")
    assert payload["phase"] == "3BB-R39-CLOUD-AUTO-LOGIN-ADMIN-BOOTSTRAP"
    assert decision["status"] == "AUTO_LOGIN_ADMIN_BOOTSTRAP_READY"
    assert decision["handoff_ready"] is True
    assert decision["ssh_batch_login_ok"] is True
    assert decision["sudo_noninteractive_ok"] is False
    assert decision["admin_helper_present"] is False
    assert "Host kalshi-cloud" in ssh_script
    assert "BatchMode yes" in ssh_script
    assert "scp \"$ROOT_HELPER_LOCAL\" kalshi-cloud" in ssh_script
    assert "NOPASSWD: $HELPER" in root_script
    assert "sudo -n \"$HELPER\"" in root_script
    assert "systemctl enable \"${TIMER}\"" in root_script
    assert "systemctl start" not in root_script
    assert payload["safety_flags"]["ssh_config_modified_by_codex"] is False
    assert payload["safety_flags"]["root_bootstrap_executed_by_codex"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r39_blocks_when_ssh_key_login_fails(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)
    runner = _fake_probe_runner({"ssh_batch_login": ("", False, 255, "permission denied")})

    with session_factory() as session:
        payload = build_phase3bb_r39_cloud_auto_login_admin_bootstrap(
            session,
            output_dir=reports_dir / "phase3bb_r39",
            reports_dir=reports_dir,
            probe_runner=runner,
        )

    decision = payload["bootstrap_decision"]
    assert decision["status"] == "BLOCKED_AUTO_LOGIN_ADMIN_BOOTSTRAP"
    assert decision["first_failed_check"] == "ssh_batch_login_current_target_ok"


def test_phase3bb_r39_ssh_handoff_defaults_to_dry_run(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r39_cloud_auto_login_admin_bootstrap_report(
            session,
            output_dir=reports_dir / "phase3bb_r39",
            reports_dir=reports_dir,
            probe_runner=_fake_probe_runner(),
        )

    result = subprocess.run(
        ["bash", str(artifacts.ssh_config_handoff_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "dry-run" in result.stdout
    assert "add Host kalshi-cloud" in result.stdout


def test_phase3bb_r39_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r39-cloud-auto-login-admin-bootstrap", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r39-cloud-auto-login-admin-bootstrap" in result.output
    assert "--per-probe-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r39.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path) -> None:
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
    r38_dir = reports_dir / "phase3bb_r38"
    r38_dir.mkdir(parents=True, exist_ok=True)
    (r38_dir / "cloud_scheduler_install_repair_handoff.json").write_text(
        json.dumps({"repair_decision": {"status": "REPAIR_HANDOFF_READY_NO_START"}}),
        encoding="utf-8",
    )


def _fake_probe_runner(
    overrides: dict[str, tuple[str, bool, int | None, str]] | None = None,
):
    outputs = {
        "ssh_batch_login": ("kalshi-bot-01\nkalshi\n", True, 0, ""),
        "sudo_noninteractive": ("SUDO_N_BLOCKED\n", True, 0, ""),
        "admin_helper": ("HELPER_MISSING\n", True, 0, ""),
    }
    outputs.update(overrides or {})

    def run(probe: RemoteProbe, _target: CloudBootstrapTarget) -> RemoteProbeResult:
        stdout, ok, exit_code, stderr = outputs[probe.name]
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.01,
        )

    return run
