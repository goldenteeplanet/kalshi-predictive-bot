from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r11_codex_cloud_bridge import (
    build_phase3bb_r11_codex_cloud_bridge,
    write_phase3bb_r11_codex_cloud_bridge_report,
)


def test_phase3bb_r11_writes_no_deploy_bridge_artifacts(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r10_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r11_codex_cloud_bridge_report(
            session,
            output_dir=reports_dir / "phase3bb_r11",
            reports_dir=reports_dir,
            cloud_host="203.0.113.10",
            ssh_alias="kalshi-cloud-test",
            identity_file="~/.ssh/id_ed25519_do",
        )

    assert artifacts.executive_summary_path.exists()
    assert artifacts.bridge_markdown_path.exists()
    assert artifacts.operator_commands_path.exists()
    assert artifacts.smoke_test_path.exists()
    assert artifacts.context_json_path.exists()
    assert artifacts.next_actions_path.exists()
    assert artifacts.readme_for_codex_path.exists()
    assert artifacts.manifest_path.exists()

    payload = json.loads(artifacts.context_json_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "3BB-R11-CODEX-CLOUD-BRIDGE"
    assert payload["safety_flags"]["no_deploy"] is True
    assert payload["safety_flags"]["remote_commands_executed"] == 0
    assert payload["safety_flags"]["secrets_printed"] is False
    assert payload["safety_flags"]["starts_r5_watcher"] is False
    assert payload["r10_decision_summary"]["status"] == "NEED_ALWAYS_ON_SCHEDULER"

    smoke_text = artifacts.smoke_test_path.read_text(encoding="utf-8")
    assert "phase3ba-status" in smoke_text
    assert "id_ed25519_do" in smoke_text
    assert "cat $ENV_PATH" not in smoke_text
    assert "phase3bc-r5-unattended-start" not in smoke_text


def test_phase3bb_r11_default_payload_uses_placeholder_host(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)

    with session_factory() as session:
        payload = build_phase3bb_r11_codex_cloud_bridge(
            session,
            output_dir=tmp_path / "reports" / "phase3bb_r11",
            reports_dir=tmp_path / "reports",
        )

    assert payload["ssh_profile"]["host"] == "YOUR_DROPLET_IP"
    assert payload["ssh_profile"]["placeholder_host"] is True
    assert payload["bridge_commands"]["mirror_reports_only"].startswith("rsync -avz")
    assert "--exclude '*.env'" in payload["bridge_commands"]["mirror_reports_only"]
    assert payload["live_or_demo_execution"] is False
    assert payload["paper_trade_creation"] is False


def test_phase3bb_r11_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r11-codex-cloud-bridge", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r11-codex-cloud-bridge" in result.output
    assert "--cloud-host" in result.output
    assert "--ssh-alias" in result.output
    assert "--identity-file" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r11.db'}")
    return get_session_factory(engine)


def _write_r10_context(reports_dir: Path) -> None:
    phase_dir = reports_dir / "phase3bb_r10"
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / "cloud_readiness_decision.json").write_text(
        json.dumps(
            {
                "decision": {
                    "status": "NEED_ALWAYS_ON_SCHEDULER",
                    "buy_compute_now": True,
                    "recommendation": "small VPS + systemd",
                    "recommended_architecture": "small VPS + systemd",
                },
                "cost_plan": {
                    "monthly_budget_usd": 24,
                    "budget_ceiling_usd": 48,
                    "spec": "2 vCPU / 4 GB RAM / 80 GB SSD",
                },
                "deployment_plan": {"deploy_now": False},
            }
        ),
        encoding="utf-8",
    )
