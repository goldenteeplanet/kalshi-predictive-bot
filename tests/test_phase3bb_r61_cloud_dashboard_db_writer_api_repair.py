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
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    UiApiProbe,
    UiApiResult,
)
from kalshi_predictor.phase3bb_r61_cloud_dashboard_db_writer_api_repair import (
    build_phase3bb_r61_cloud_dashboard_db_writer_api_repair,
    write_phase3bb_r61_cloud_dashboard_db_writer_api_repair_report,
)


def test_phase3bb_r61_reports_inactive_ui_backend(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r61_cloud_dashboard_db_writer_api_repair_report(
            session,
            output_dir=reports_dir / "phase3bb_r61",
            reports_dir=reports_dir,
            probe_runner=_fake_remote_runner(ui_active=False),
            ui_probe_runner=_fake_ui_runner(ok=False, status_code=502),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["repair_decision"]
    assert payload["phase"] == "3BB-R61-CLOUD-DASHBOARD-DB-WRITER-API-REACHABILITY-REPAIR"
    assert decision["status"] == "BLOCKED_UI_BACKEND_INACTIVE"
    assert decision["first_failed_check"] == "ui_service_active"
    assert "phase3bb-r24-cloud-ui-start-tunnel-verification" in decision["operator_next_command"]
    assert payload["safety_flags"]["starts_ui_service"] is False
    assert payload["safety_flags"]["starts_scheduler"] is False
    assert artifacts.scheduler_no_start_handoff_path.exists()
    assert artifacts.manifest_path.exists()


def test_phase3bb_r61_opens_r60_no_start_handoff_when_truth_clean(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir, r32_ready=True, r33_ready=True, r34_ready=True)

    with session_factory() as session:
        payload = build_phase3bb_r61_cloud_dashboard_db_writer_api_repair(
            session,
            output_dir=reports_dir / "phase3bb_r61",
            reports_dir=reports_dir,
            probe_runner=_fake_remote_runner(ui_active=True),
            ui_probe_runner=_fake_ui_runner(ok=True, status_code=200),
        )

    decision = payload["repair_decision"]
    assert decision["status"] == "READY_FOR_R60_SCHEDULER_NO_START_HANDOFF"
    assert decision["repair_passed"] is True
    assert decision["operator_next_command"].startswith(
        "kalshi-bot phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run"
    )
    assert payload["parsed_remote_state"]["r60_registered_on_cloud"] is True


def test_phase3bb_r61_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r61-cloud-dashboard-db-writer-api-reachability-repair", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r61-cloud-dashboard-db-writer-api-reachability-repair" in result.output
    assert "--private-base-url" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r61.db'}")
    return get_session_factory(engine)


def _write_context(
    reports_dir: Path,
    *,
    r32_ready: bool = False,
    r33_ready: bool = False,
    r34_ready: bool = False,
) -> None:
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
    r31_dir = reports_dir / "phase3bb_r31"
    r31_dir.mkdir(parents=True, exist_ok=True)
    (r31_dir / "cloud_ui_private_access_operator_smoke_test.json").write_text(
        json.dumps(
            {
                "smoke_decision": {
                    "status": "VERIFIED_PRIVATE_ACCESS_OPERATOR_SMOKE_PASS",
                    "private_base_url": "https://kalshi-bot-01.taile570d1.ts.net",
                }
            }
        ),
        encoding="utf-8",
    )
    r32_dir = reports_dir / "phase3bb_r32"
    r32_dir.mkdir(parents=True, exist_ok=True)
    (r32_dir / "cloud_ui_dashboard_truth_scheduler_status.json").write_text(
        json.dumps(
            {
                "private_base_url": "https://kalshi-bot-01.taile570d1.ts.net",
                "verification_decision": {
                    "status": "VERIFIED_DASHBOARD_TRUTH_AND_SCHEDULER_STATUS"
                    if r32_ready
                    else "BLOCKED_DASHBOARD_TRUTH_NOT_VERIFIED",
                },
            }
        ),
        encoding="utf-8",
    )
    r33_dir = reports_dir / "phase3bb_r33"
    r33_dir.mkdir(parents=True, exist_ok=True)
    (r33_dir / "cloud_paper_only_operations_readiness.json").write_text(
        json.dumps(
            {
                "readiness_decision": {
                    "status": "PAPER_ONLY_MONITORING_READY"
                    if r33_ready
                    else "BLOCKED_PAPER_ONLY_OPERATIONS_READINESS",
                }
            }
        ),
        encoding="utf-8",
    )
    r34_dir = reports_dir / "phase3bb_r34"
    r34_dir.mkdir(parents=True, exist_ok=True)
    (r34_dir / "cloud_multicategory_refresh_scheduler_review.json").write_text(
        json.dumps(
            {
                "scheduler_decision": {
                    "status": "READY_FOR_NO_START_SCHEDULER_DRY_RUN"
                    if r34_ready
                    else "BLOCKED_SCHEDULER_REVIEW",
                }
            }
        ),
        encoding="utf-8",
    )


def _fake_remote_runner(*, ui_active: bool):
    def run(probe: RemoteProbe, target: CloudBootstrapTarget) -> RemoteProbeResult:
        del target
        stdout = ""
        exit_code = 0
        if probe.name == "ui_systemd_state":
            stdout = "\n".join(
                [
                    "Id=kalshi-ui.service",
                    "LoadState=loaded",
                    "UnitFileState=enabled",
                    f"ActiveState={'active' if ui_active else 'inactive'}",
                    f"SubState={'running' if ui_active else 'dead'}",
                    f"ExecMainPID={2468 if ui_active else 0}",
                ]
            )
        elif probe.name == "ui_local_listener":
            stdout = (
                "LISTEN 0 2048 127.0.0.1:8080 0.0.0.0:* users:((\"uvicorn\",pid=2468,fd=7))"
                if ui_active
                else ""
            )
        elif probe.name == "ui_loopback_db_writer_api":
            if ui_active:
                stdout = 'HTTP/1.1 200 OK\ncontent-type: application/json\n\n{"ok": true}'
            else:
                exit_code = 7
                stdout = ""
        elif probe.name == "tailscale_serve_status":
            stdout = "https://kalshi-bot-01.taile570d1.ts.net/\n|-- proxy http://127.0.0.1:8080"
        elif probe.name == "remote_r60_registry":
            stdout = "R60_REGISTERED"
        elif probe.name == "remote_r32_r33_r34_registry":
            stdout = "R32_R33_R34_REGISTERED"
        return RemoteProbeResult(
            name=probe.name,
            command=probe.command,
            ok=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout,
            stderr="" if exit_code == 0 else "curl failed",
            duration_seconds=0.01,
        )

    return run


def _fake_ui_runner(*, ok: bool, status_code: int):
    def run(probe: UiApiProbe, base_url: str) -> UiApiResult:
        return UiApiResult(
            name=probe.name,
            path=probe.path,
            url=base_url.rstrip("/") + probe.path,
            ok=ok,
            status_code=status_code,
            content_type="application/json" if ok else "",
            duration_seconds=0.01,
            body_sha256="sha256:test",
            body_excerpt="{}" if ok else "",
            parsed_summary={"ok": True} if ok else {},
            error="" if ok else f"HTTP Error {status_code}: Bad Gateway",
        )

    return run
