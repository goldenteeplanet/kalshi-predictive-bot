from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
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
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    UiApiProbe,
    UiApiResult,
    build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status,
    write_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_report,
)

DB_FINGERPRINT = "sha256:test-db"


def test_phase3bb_r32_verifies_dashboard_truth_and_scheduler_status(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_report(
            session,
            output_dir=reports_dir / "phase3bb_r32",
            reports_dir=reports_dir,
            scheduler_probe_runner=_fake_scheduler_runner(),
            ui_probe_runner=_fake_ui_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["verification_decision"]
    assert payload["phase"] == "3BB-R32-CLOUD-UI-DASHBOARD-TRUTH-SCHEDULER-STATUS"
    assert decision["status"] == "VERIFIED_DASHBOARD_TRUTH_AND_SCHEDULER_STATUS"
    assert decision["verification_passed"] is True
    assert decision["r18_status"] == "SYSTEMD_OWNS_R5"
    assert decision["duplicate_r5"] is False
    assert decision["service_owns_r5"] is True
    assert payload["ui_dashboard_truth_summaries"]["workspace_guard_api"][
        "database_fingerprint"
    ] == DB_FINGERPRINT
    assert all(row["passed"] for row in payload["verification_checks"])
    assert artifacts.manifest_path.exists()


def test_phase3bb_r32_blocks_stale_dashboard_snapshot(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status(
            session,
            output_dir=reports_dir / "phase3bb_r32",
            reports_dir=reports_dir,
            max_dashboard_age_seconds=60,
            scheduler_probe_runner=_fake_scheduler_runner(),
            ui_probe_runner=_fake_ui_runner(
                dashboard_generated_at=datetime.now(UTC) - timedelta(minutes=10)
            ),
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "BLOCKED_DASHBOARD_TRUTH_NOT_VERIFIED"
    assert decision["first_failed_check"] == "ui_dashboard_snapshot_current"


def test_phase3bb_r32_blocks_public_url_override(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status(
            session,
            output_dir=reports_dir / "phase3bb_r32",
            reports_dir=reports_dir,
            private_base_url="https://159.65.35.72",
            scheduler_probe_runner=_fake_scheduler_runner(),
            ui_probe_runner=_fake_ui_runner(),
        )

    decision = payload["verification_decision"]
    assert decision["status"] == "BLOCKED_PRIVATE_UI_SMOKE_NOT_VERIFIED"
    assert decision["first_failed_check"] == "private_base_url_is_tailscale_https"


def test_phase3bb_r32_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification" in (
        result.output
    )
    assert "--ui-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r32.db'}")
    return get_session_factory(engine)


def _write_context(reports_dir: Path, *, pid: int = 23133) -> None:
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
    r13_dir = reports_dir / "phase3bb_r13"
    r13_dir.mkdir(parents=True, exist_ok=True)
    (r13_dir / "cloud_scheduler_adoption.json").write_text(
        json.dumps(
            {
                "cloud_target": cloud_target,
                "adoption_decision": {
                    "recommendation": "ADOPT_EXISTING_R5",
                    "current_r5_pid": pid,
                    "guard_status": "RUNNING",
                    "guard_should_stop": False,
                    "duplicate_r5": False,
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
                    "current_r5_pid": pid,
                    "service_enabled": True,
                    "service_started": True,
                },
            }
        ),
        encoding="utf-8",
    )
    r31_dir = reports_dir / "phase3bb_r31"
    r31_dir.mkdir(parents=True, exist_ok=True)
    (r31_dir / "cloud_ui_private_access_operator_smoke_test.json").write_text(
        json.dumps(
            {
                "smoke_decision": {
                    "status": "VERIFIED_PRIVATE_ACCESS_OPERATOR_SMOKE_PASS",
                    "smoke_passed": True,
                    "private_base_url": "https://kalshi-bot-01.taile570d1.ts.net",
                    "r5_pid": pid,
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_scheduler_runner(*, pid: int = 23133):
    r5_status = {
        "pid": pid,
        "process": {
            "phase3bc_r5_process_running": True,
            "phase3bc_r5_pids": [pid],
            "status": "RUNNING",
        },
        "guard": {"status": "RUNNING", "should_stop": False},
        "latest_watch_state": "WAITING_FOR_EXECUTABLE_BOOK",
        "latest_summary": {"paper_ready_candidates": 0, "positive_ev_rows": 4},
    }
    writer = {
        "status": "WRITER_ACTIVE",
        "safe_to_start_write": False,
        "current_writer_pid": pid,
    }
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
        "r5_guard_dry_run": json.dumps({"after": r5_status, "status": "RUNNING"}),
        "db_writer_monitor": json.dumps(writer),
        "r5_processes": (
            f"{pid} /opt/kalshi-predictive-bot/.venv/bin/python "
            "-m kalshi_predictor.cli phase3bc-r5-crypto-freshness-watch\n"
        ),
        "r5_pid_file": f"{pid}\n",
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


def _fake_ui_runner(*, dashboard_generated_at: datetime | None = None):
    generated_at = dashboard_generated_at or datetime.now(UTC)

    def runner(probe: UiApiProbe, base_url: str) -> UiApiResult:
        if probe.name == "db_writer_api":
            body = {
                "ok": True,
                "read_only": True,
                "monitor": {
                    "status": "WRITER_ACTIVE",
                    "safe_to_start_write": False,
                    "current_writer_pid": 23133,
                },
            }
        elif probe.name == "workspace_guard_api":
            body = {
                "ok": True,
                "guard": {
                    "summary": {
                        "status": "PASS",
                        "missing_required_commands": 0,
                        "critical_findings": 0,
                        "database_fingerprint": DB_FINGERPRINT,
                        "git_commit": "unknown",
                    }
                },
            }
        else:
            body = {
                "schema_version": "phase-3t-dashboard-api-v1",
                "dashboard_snapshot_id": "snap-test",
                "generated_at": generated_at.isoformat(),
                "panel_as_of": generated_at.isoformat(),
                "effective_filters": {"execution_mode": "paper_shadow"},
                "source_watermarks": [
                    {
                        "source_id": "market_state",
                        "required": True,
                        "freshness_status": "FRESH",
                        "database_fingerprint": DB_FINGERPRINT,
                    }
                ],
            }
        return _ui_result(probe, base_url.rstrip("/") + probe.path, body)

    return runner


def _ui_result(probe: UiApiProbe, url: str, body: dict[str, object]) -> UiApiResult:
    encoded = json.dumps(body).encode("utf-8")
    from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
        _summarize_probe_json,
    )

    return UiApiResult(
        name=probe.name,
        path=probe.path,
        url=url,
        ok=True,
        status_code=200,
        content_type="application/json",
        duration_seconds=0.01,
        body_sha256=hashlib.sha256(encoded).hexdigest(),
        body_excerpt=encoded.decode("utf-8"),
        parsed_summary=_summarize_probe_json(probe.name, body),
        error="",
    )
