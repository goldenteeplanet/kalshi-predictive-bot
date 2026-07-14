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
    build_phase3bb_r12_cloud_bootstrap_verification,
    write_phase3bb_r12_cloud_bootstrap_verification_report,
)


def test_phase3bb_r12_writes_ready_bootstrap_artifacts(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r11_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r12_cloud_bootstrap_verification_report(
            session,
            output_dir=reports_dir / "phase3bb_r12",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(),
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.json_path.exists()
    assert artifacts.probe_csv_path.exists()
    assert artifacts.next_actions_path.exists()
    assert artifacts.manifest_path.exists()

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "3BB-R12-CLOUD-BOOTSTRAP-VERIFICATION"
    assert payload["bootstrap_decision"]["status"] == "READY_FOR_OPERATOR_SCHEDULER"
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["safety_flags"]["secrets_printed"] is False
    assert payload["safety_flags"]["remote_db_writes_performed"] == 0
    assert payload["cloud_target"]["identity_file"] == "~/.ssh/id_ed25519_do"
    assert "phase3bb-r1-operator-scheduler" in artifacts.next_actions_path.read_text(
        encoding="utf-8"
    )


def test_phase3bb_r12_blocks_unsafe_remote_env(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r11_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r12_cloud_bootstrap_verification(
            session,
            output_dir=reports_dir / "phase3bb_r12",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(
                env_flags={
                    "kalshi_env_present": True,
                    "kalshi_env_class": "review",
                    "database_url_present": True,
                    "database_backend_class": "sqlite",
                    "danger_truthy_flags": ["LIVE_TRADING_ENABLED"],
                    "paper_read_only_pass": False,
                }
            ),
        )

    assert payload["bootstrap_decision"]["status"] == "UNSAFE_ENV_BLOCKED"
    assert payload["bootstrap_decision"]["ready_for_scheduler"] is False
    assert payload["parsed_remote_state"]["env_flags"]["danger_truthy_flags"] == [
        "LIVE_TRADING_ENABLED"
    ]


def test_phase3bb_r12_parses_wrapped_writer_output(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r11_context(reports_dir)
    wrapped_writer = """
{
  "long_job_status": {
    "recommended_next_action": "No heartbeat exists yet. Run link-remediate with
--heartbeat-dir reports/phase3au.",
    "status": "STALE"
  },
  "safe_to_start_write": true,
  "status": "CLEAR",
  "current_writer_pid": null
}
"""

    with session_factory() as session:
        payload = build_phase3bb_r12_cloud_bootstrap_verification(
            session,
            output_dir=reports_dir / "phase3bb_r12",
            reports_dir=reports_dir,
            probe_runner=_fake_runner(writer_output=wrapped_writer),
        )

    assert payload["parsed_remote_state"]["writer_safe_to_start_write"] is True
    assert payload["parsed_remote_state"]["writer_status"] == "CLEAR"
    assert payload["bootstrap_decision"]["status"] == "READY_FOR_OPERATOR_SCHEDULER"


def test_phase3bb_r12_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r12-cloud-bootstrap-verification", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r12-cloud-bootstrap-verification" in result.output
    assert "--ssh-target" in result.output
    assert "--identity-file" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r12.db'}")
    return get_session_factory(engine)


def _write_r11_context(reports_dir: Path) -> None:
    phase_dir = reports_dir / "phase3bb_r11"
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "codex_cloud_context.json").write_text(
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


def _fake_runner(
    *,
    env_flags: dict[str, object] | None = None,
    writer: dict[str, object] | None = None,
    writer_output: str | None = None,
):
    env_payload = env_flags or {
        "kalshi_env_present": True,
        "kalshi_env_class": "safe",
        "database_url_present": True,
        "database_backend_class": "sqlite",
        "danger_truthy_flags": [],
        "paper_read_only_pass": True,
    }
    writer_payload = writer or {"status": "NO_ACTIVE_WRITER", "safe_to_start_write": True}
    outputs = {
        "ssh_identity": "kalshi-bot-01\nkalshi\n",
        "os_python": "Linux x86_64\nUbuntu 24.04 LTS\n/usr/bin/python3\nPython 3.12.3\n",
        "repo": "/opt/kalshi-predictive-bot\nabc123\nREPO_OK\n",
        "venv_cli": "Python 3.12.3\nVENV_CLI_OK\n",
        "env_flags": json.dumps(env_payload),
        "db_path": "DB_PATH_OK\n",
        "reports_path": "REPORTS_PATH_OK\n",
        "db_writer_monitor": writer_output or json.dumps(writer_payload),
        "r5_status": json.dumps(
            {
                "process": {
                    "phase3bc_r5_process_running": False,
                    "phase3bc_r5_pids": [],
                },
                "guard": {"status": "STOPPED"},
            }
        ),
        "phase3ba_status": json.dumps(
            {"summary": {"safe": True}, "next_operator_command": "WAIT"}
        ),
        "command_registry": "COMMAND_REGISTRY_OK\n",
    }

    def runner(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=True,
            exit_code=0,
            stdout=outputs[probe.name],
            stderr="",
            duration_seconds=0.01,
        )

    return runner
