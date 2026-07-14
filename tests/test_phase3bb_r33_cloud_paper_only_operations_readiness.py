from __future__ import annotations

import hashlib
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
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    UiApiProbe,
    UiApiResult,
    _summarize_probe_json,
)
from kalshi_predictor.phase3bb_r33_cloud_paper_only_operations_readiness import (
    build_phase3bb_r33_cloud_paper_only_operations_readiness,
    write_phase3bb_r33_cloud_paper_only_operations_readiness_report,
)

DB_FINGERPRINT = "sha256:r33-test-db"


def test_phase3bb_r33_reports_monitoring_ready_without_candidates(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        artifacts = write_phase3bb_r33_cloud_paper_only_operations_readiness_report(
            session,
            output_dir=reports_dir / "phase3bb_r33",
            reports_dir=reports_dir,
            scheduler_probe_runner=_fake_scheduler_runner(
                paper_ready_candidates=0,
                positive_ev_rows=0,
            ),
            ui_probe_runner=_fake_ui_runner(),
        )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    decision = payload["readiness_decision"]
    warnings = {row["warning"] for row in payload["readiness_warnings"]}
    assert payload["phase"] == "3BB-R33-CLOUD-PAPER-ONLY-OPERATIONS-READINESS"
    assert decision["status"] == "PAPER_ONLY_MONITORING_READY"
    assert decision["readiness_passed"] is True
    assert decision["paper_ready_candidates"] == 0
    assert decision["positive_ev_rows"] == 0
    assert decision["paper_gate_state"] == "MONITORING_NO_TRADE"
    assert payload["operations_snapshot"]["paper_ready_candidates_present"] is True
    assert "NO_CURRENT_POSITIVE_EV" in warnings
    assert "NO_PAPER_READY_CANDIDATES" in warnings
    assert payload["safety_flags"]["creates_paper_trades"] is False
    assert payload["safety_flags"]["places_exchange_orders"] is False
    assert artifacts.manifest_path.exists()


def test_phase3bb_r33_reports_operator_review_when_candidates_exist(
    tmp_path: Path,
) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r33_cloud_paper_only_operations_readiness(
            session,
            output_dir=reports_dir / "phase3bb_r33",
            reports_dir=reports_dir,
            scheduler_probe_runner=_fake_scheduler_runner(
                paper_ready_candidates=2,
                positive_ev_rows=3,
            ),
            ui_probe_runner=_fake_ui_runner(),
        )

    decision = payload["readiness_decision"]
    assert decision["status"] == "PAPER_ONLY_OPERATOR_REVIEW_READY"
    assert decision["paper_ready_candidates"] == 2
    assert decision["next_codex_step"] == "Phase 3BB-R34 - Paper-Only Candidate Operator Review"
    assert payload["paper_trade_creation"] is False
    assert payload["order_submission"] is False


def test_phase3bb_r33_blocks_when_paper_ready_count_missing(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r33_cloud_paper_only_operations_readiness(
            session,
            output_dir=reports_dir / "phase3bb_r33",
            reports_dir=reports_dir,
            scheduler_probe_runner=_fake_scheduler_runner(
                paper_ready_candidates=None,
                positive_ev_rows=4,
            ),
            ui_probe_runner=_fake_ui_runner(),
        )

    decision = payload["readiness_decision"]
    assert decision["status"] == "BLOCKED_PAPER_ONLY_OPERATIONS_READINESS"
    assert decision["first_failed_check"] == "paper_ready_count_explicit"
    assert payload["operations_snapshot"]["paper_ready_candidates"] == 0
    assert payload["operations_snapshot"]["paper_ready_candidates_present"] is False


def test_phase3bb_r33_blocks_when_r32_is_not_verified(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    reports_dir = tmp_path / "reports"
    _write_context(reports_dir)

    with session_factory() as session:
        payload = build_phase3bb_r33_cloud_paper_only_operations_readiness(
            session,
            output_dir=reports_dir / "phase3bb_r33",
            reports_dir=reports_dir,
            private_base_url="https://159.65.35.72",
            scheduler_probe_runner=_fake_scheduler_runner(
                paper_ready_candidates=0,
                positive_ev_rows=0,
            ),
            ui_probe_runner=_fake_ui_runner(),
        )

    decision = payload["readiness_decision"]
    assert decision["status"] == "BLOCKED_PAPER_ONLY_OPERATIONS_READINESS"
    assert decision["first_failed_check"] == "r32_dashboard_scheduler_verified"
    assert payload["r32_dashboard_scheduler_status"]["verification_decision"][
        "status"
    ] == "BLOCKED_PRIVATE_UI_SMOKE_NOT_VERIFIED"


def test_phase3bb_r33_cli_help_registered() -> None:
    result = CliRunner().invoke(
        app,
        ["phase3bb-r33-cloud-paper-only-operations-readiness", "--help"],
    )

    assert result.exit_code == 0
    assert "phase3bb-r33-cloud-paper-only-operations-readiness" in result.output
    assert "--ui-timeout-seconds" in result.output


def _session_factory(tmp_path: Path):
    get_settings.cache_clear()
    engine = init_db(f"sqlite:///{tmp_path / 'phase3bb_r33.db'}")
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


def _fake_scheduler_runner(
    *,
    pid: int = 23133,
    paper_ready_candidates: int | None,
    positive_ev_rows: int | None,
):
    latest_summary: dict[str, int] = {}
    if paper_ready_candidates is not None:
        latest_summary["paper_ready_candidates"] = paper_ready_candidates
    if positive_ev_rows is not None:
        latest_summary["positive_ev_rows"] = positive_ev_rows
    r5_status = {
        "pid": pid,
        "process": {
            "phase3bc_r5_process_running": True,
            "phase3bc_r5_pids": [pid],
            "status": "RUNNING",
        },
        "guard": {"status": "RUNNING", "should_stop": False},
        "latest_watch_state": "WAITING_FOR_POSITIVE_EV",
        "latest_summary": latest_summary,
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


def _fake_ui_runner():
    generated_at = datetime.now(UTC)

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
                "dashboard_snapshot_id": "snap-r33-test",
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
