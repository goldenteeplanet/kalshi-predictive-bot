from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kalshi_predictor.cli import app
from kalshi_predictor.config import get_settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase3bb_r21_cloud_ui_install_review import (
    build_phase3bb_r21_cloud_ui_install_review,
    write_phase3bb_r21_cloud_ui_install_review_report,
)


def test_phase3bb_r21_writes_no_start_install_review(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r20_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r21_cloud_ui_install_review_report(
            session,
            output_dir=reports_dir / "phase3bb_r21",
            reports_dir=reports_dir,
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    dry_run = artifacts.no_start_dry_run_path.read_text(encoding="utf-8")

    assert payload["phase"] == "3BB-R21-CLOUD-UI-INSTALL-REVIEW-NO-START"
    decision = payload["install_review_decision"]
    assert decision["status"] == "READY_FOR_OPERATOR_UI_INSTALL_REVIEW_NO_START"
    assert decision["install_allowed_now"] is False
    assert decision["start_allowed_now"] is False
    assert decision["public_exposure_allowed_now"] is False
    assert payload["safety_flags"]["no_service_install"] is True
    assert payload["safety_flags"]["starts_ui_service"] is False
    assert all(row["passed"] for row in payload["review_checks"])
    assert "systemctl" not in dry_run
    assert "scp " not in dry_run
    assert "ufw " not in dry_run
    assert artifacts.manifest_path.exists()


def test_phase3bb_r21_blocks_unsafe_public_ui_bind(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_r20_context(reports_dir, host="0.0.0.0")

    with session_factory() as session:
        payload = build_phase3bb_r21_cloud_ui_install_review(
            session,
            output_dir=reports_dir / "phase3bb_r21",
            reports_dir=reports_dir,
        )

    decision = payload["install_review_decision"]
    failed = [row["check"] for row in payload["review_checks"] if not row["passed"]]
    assert decision["status"] == "BLOCKED_UI_INSTALL_REVIEW"
    assert "service_is_localhost_only" in failed
    assert decision["install_allowed_now"] is False
    assert decision["next_codex_step"] == "Phase 3BB-R20 - Refresh Cloud UI Service Plan"


def test_phase3bb_r21_cli_help_registered() -> None:
    result = CliRunner().invoke(app, ["phase3bb-r21-cloud-ui-install-review", "--help"])

    assert result.exit_code == 0
    assert "phase3bb-r21-cloud-ui-install-review" in result.output
    assert "--r20-max-age-minutes" in result.output
    assert "--ui-service-name" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r21.db'}")
    return get_session_factory(engine)


def _write_r20_context(reports_dir: Path, *, host: str = "127.0.0.1") -> None:
    r20_dir = reports_dir / "phase3bb_r20"
    r20_dir.mkdir(parents=True, exist_ok=True)
    service_text = _service_draft(host=host)
    nginx_text = _nginx_draft()
    (r20_dir / "kalshi-ui.service.draft").write_text(service_text, encoding="utf-8")
    (r20_dir / "kalshi-ui.nginx.draft").write_text(nginx_text, encoding="utf-8")
    (r20_dir / "ui_install_review_checklist.md").write_text(
        "# Phase 3BB-R20 UI Install Review Checklist\n\n## R20 Is Draft Only\n",
        encoding="utf-8",
    )
    (r20_dir / "cloud_ui_service_plan.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "ui_service_plan": {
                    "status": "DRAFT_READY_FOR_REVIEW",
                    "ready_for_review": True,
                    "install_allowed_now": False,
                    "start_allowed_now": False,
                    "expose_public_allowed_now": False,
                    "ui_service_name": "kalshi-ui.service",
                    "r18_status": "SYSTEMD_OWNS_R5",
                    "r5_pid": 16798,
                    "ssh_tunnel_command": (
                        "ssh -i '~/.ssh/id_ed25519_do' "
                        "-L 8080:127.0.0.1:8080 'kalshi@203.0.113.10'"
                    ),
                },
                "parsed_ui_state": {
                    "ui_duplicate_process": False,
                    "service_started": False,
                    "local_ui_http_ok": False,
                },
            }
        ),
        encoding="utf-8",
    )


def _service_draft(*, host: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Kalshi Bot operator UI (paper/read-only)",
            "After=network-online.target kalshi-r5-watcher.service",
            "Requires=kalshi-r5-watcher.service",
            "ConditionPathExists=/etc/kalshi-bot/kalshi-bot.env",
            "ConditionPathExists=/var/lib/kalshi-bot/kalshi_phase1.db",
            "",
            "[Service]",
            "Type=simple",
            "User=kalshi",
            "WorkingDirectory=/opt/kalshi-predictive-bot",
            "EnvironmentFile=/etc/kalshi-bot/kalshi-bot.env",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=UI_READ_ONLY=true",
            "Environment=EXECUTION_ENABLED=false",
            "Environment=EXECUTION_DRY_RUN=true",
            "Environment=EXECUTION_KILL_SWITCH=true",
            (
                "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot "
                f"ui --host {host} --port 8080"
            ),
            "Restart=always",
            "ProtectSystem=full",
            "ReadWritePaths=/opt/kalshi-predictive-bot/reports /var/lib/kalshi-bot",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _nginx_draft() -> str:
    return "\n".join(
        [
            "# Draft only. Do not install until reviewed in a later phase.",
            "server {",
            "    listen 80;",
            "    location / {",
            "        proxy_pass http://127.0.0.1:8080;",
            "    }",
            "}",
            "",
        ]
    )
