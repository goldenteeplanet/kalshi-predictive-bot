from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import ProbeRunner
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    DEFAULT_SERVICE_NAME,
    build_phase3bb_r18_cloud_scheduler_runtime_cutover,
)
from kalshi_predictor.phase3bb_r31_cloud_ui_private_access_operator_smoke_test import (
    _is_tailscale_https_url,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R32_VERSION = "phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r32")
VERIFIED_R31_STATUS = "VERIFIED_PRIVATE_ACCESS_OPERATOR_SMOKE_PASS"
SYSTEMD_OWNS_R5_STATUS = "SYSTEMD_OWNS_R5"
DEFAULT_UI_TIMEOUT_SECONDS = 90
DEFAULT_MAX_DASHBOARD_AGE_SECONDS = 300


@dataclass(frozen=True)
class UiApiProbe:
    name: str
    path: str
    timeout_seconds: int


@dataclass(frozen=True)
class UiApiResult:
    name: str
    path: str
    url: str
    ok: bool
    status_code: int | None
    content_type: str
    duration_seconds: float
    body_sha256: str
    body_excerpt: str
    parsed_summary: dict[str, Any]
    error: str


@dataclass(frozen=True)
class Phase3BBR32CloudUiDashboardTruthSchedulerStatusArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    ui_probe_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


UiApiProbeRunner = Callable[[UiApiProbe, str], UiApiResult]


def write_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    scheduler_probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
) -> Phase3BBR32CloudUiDashboardTruthSchedulerStatusArtifacts:
    payload = build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        private_base_url=private_base_url,
        ui_timeout_seconds=ui_timeout_seconds,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        scheduler_probe_runner=scheduler_probe_runner,
        ui_probe_runner=ui_probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_dashboard_truth_scheduler_status.md"
    json_path = output_dir / "cloud_ui_dashboard_truth_scheduler_status.json"
    ui_probe_csv_path = output_dir / "ui_dashboard_truth_probe_results.csv"
    checks_csv_path = output_dir / "dashboard_truth_scheduler_checks.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_ui_probe_csv(ui_probe_csv_path, payload["ui_dashboard_truth_probe_results"])
    _write_checks_csv(checks_csv_path, payload["verification_checks"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            ui_probe_csv_path,
            checks_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR32CloudUiDashboardTruthSchedulerStatusArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        ui_probe_csv_path=ui_probe_csv_path,
        checks_csv_path=checks_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    scheduler_probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": (
            "kalshi-bot "
            "phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification"
        ),
        "argv": command_args or [],
    }
    r31_path = reports_dir / "phase3bb_r31" / "cloud_ui_private_access_operator_smoke_test.json"
    r31 = _read_json(r31_path)
    resolved_url = (private_base_url or _r31_private_base_url(r31)).rstrip("/")
    r18 = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
        session,
        output_dir=output_dir / "r18_scheduler_status",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r18-cloud-scheduler-runtime-cutover"],
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=scheduler_probe_runner,
    )
    ui_probes = _dashboard_truth_probes(timeout_seconds=ui_timeout_seconds)
    ui_runner = ui_probe_runner or _run_ui_api_probe
    ui_results = [ui_runner(probe, resolved_url) for probe in ui_probes] if resolved_url else []
    ui_payloads = [_ui_result_payload(result) for result in ui_results]
    summaries = {result.name: result.parsed_summary for result in ui_results}
    checks = _verification_checks(
        r31=r31,
        r18=r18,
        private_base_url=resolved_url,
        ui_results=ui_results,
        summaries=summaries,
        now=now,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
    )
    decision = _decision(checks, r31=r31, r18=r18, private_base_url=resolved_url)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "private_tailnet_dashboard_truth_probe": True,
        "ui_http_get_requests": len(ui_results),
        "remote_scheduler_read_only_commands": len(r18.get("remote_probe_results") or []),
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
        "systemctl_mutating_commands_executed": 0,
        "tailscale_mutating_commands_executed": 0,
        "service_files_written_to_system": False,
        "starts_ui_service": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "nginx_or_firewall_changed": False,
        "public_exposure_changed": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
    }
    return {
        **metadata,
        "phase": "3BB-R32-CLOUD-UI-DASHBOARD-TRUTH-SCHEDULER-STATUS",
        "phase_version": PHASE3BB_R32_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_DASHBOARD_TRUTH_SCHEDULER_STATUS",
        "reports_dir": str(reports_dir),
        "r31_artifact_path": str(r31_path),
        "r31_context_available": bool(r31),
        "private_base_url": resolved_url,
        "r18_scheduler_status": r18,
        "ui_dashboard_truth_probe_results": ui_payloads,
        "ui_dashboard_truth_summaries": summaries,
        "verification_checks": checks,
        "verification_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _dashboard_truth_probes(*, timeout_seconds: int) -> list[UiApiProbe]:
    return [
        UiApiProbe("db_writer_api", "/api/db-writer-monitor", timeout_seconds),
        UiApiProbe("workspace_guard_api", "/api/workspace-guard", timeout_seconds),
        UiApiProbe(
            "dashboard_snapshot_api",
            "/api/dashboard/v1/snapshots/current",
            timeout_seconds,
        ),
    ]


def _run_ui_api_probe(probe: UiApiProbe, base_url: str) -> UiApiResult:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", probe.path.lstrip("/"))
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "kalshi-bot-phase3bb-r32-dashboard-truth/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=probe.timeout_seconds) as response:  # noqa: S310 - verified private Tailscale URL only.
            body = response.read()
            return _ui_result(
                probe,
                url=url,
                status_code=response.status,
                content_type=response.headers.get("Content-Type", ""),
                body=body,
                error="",
                duration_seconds=round(time.monotonic() - started, 3),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return _ui_result(
            probe,
            url=url,
            status_code=exc.code,
            content_type=exc.headers.get("Content-Type", "") if exc.headers else "",
            body=body,
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 3),
        )
    except Exception as exc:  # noqa: BLE001 - report must capture private UI probe failures.
        return _ui_result(
            probe,
            url=url,
            status_code=None,
            content_type="",
            body=b"",
            error=str(exc),
            duration_seconds=round(time.monotonic() - started, 3),
        )


def _ui_result(
    probe: UiApiProbe,
    *,
    url: str,
    status_code: int | None,
    content_type: str,
    body: bytes,
    error: str,
    duration_seconds: float,
) -> UiApiResult:
    decoded = body.decode("utf-8", errors="replace")
    parsed = _json_from_body(decoded)
    return UiApiResult(
        name=probe.name,
        path=probe.path,
        url=url,
        ok=status_code is not None and 200 <= status_code < 400 and not error,
        status_code=status_code,
        content_type=content_type,
        duration_seconds=duration_seconds,
        body_sha256=hashlib.sha256(body).hexdigest(),
        body_excerpt=decoded[:2048],
        parsed_summary=_summarize_probe_json(probe.name, parsed),
        error=error,
    )


def _verification_checks(
    *,
    r31: dict[str, Any],
    r18: dict[str, Any],
    private_base_url: str,
    ui_results: list[UiApiResult],
    summaries: dict[str, dict[str, Any]],
    now: datetime,
    max_dashboard_age_seconds: int,
) -> list[dict[str, Any]]:
    r31_decision = r31.get("smoke_decision") or {}
    r18_decision = r18.get("runtime_cutover_decision") or {}
    result_by_name = {result.name: result for result in ui_results}
    writer = summaries.get("db_writer_api") or {}
    guard = summaries.get("workspace_guard_api") or {}
    dashboard = summaries.get("dashboard_snapshot_api") or {}
    dashboard_age = _age_seconds(dashboard.get("generated_at"), now)
    guard_fingerprint = guard.get("database_fingerprint")
    dashboard_fingerprints = dashboard.get("database_fingerprints") or []
    r5_pid = r18_decision.get("current_r5_pid")
    writer_pid = writer.get("current_writer_pid")
    writer_clear_or_matches_r5 = writer_pid is None or writer_pid == r5_pid
    return [
        _check("r31_artifact_present", bool(r31), "R31 smoke artifact exists."),
        _check(
            "r31_private_smoke_passed",
            r31_decision.get("status") == VERIFIED_R31_STATUS,
            f"R31 status is {r31_decision.get('status')}.",
        ),
        _check(
            "private_base_url_is_tailscale_https",
            _is_tailscale_https_url(private_base_url),
            f"Private base URL is {private_base_url or 'missing'}.",
        ),
        _check(
            "scheduler_systemd_owns_single_r5",
            r18_decision.get("status") == SYSTEMD_OWNS_R5_STATUS
            and not bool(r18_decision.get("duplicate_r5"))
            and bool(r18_decision.get("service_owns_r5")),
            (
                f"R18 status={r18_decision.get('status')}; "
                f"duplicate_r5={r18_decision.get('duplicate_r5')}; "
                f"service_owns_r5={r18_decision.get('service_owns_r5')}."
            ),
        ),
        _check(
            "r5_guard_running_not_overrun",
            r18_decision.get("guard_status") == "RUNNING"
            and not bool(r18_decision.get("guard_should_stop")),
            (
                f"guard_status={r18_decision.get('guard_status')}; "
                f"guard_should_stop={r18_decision.get('guard_should_stop')}."
            ),
        ),
        _probe_check(result_by_name, "db_writer_api"),
        _check(
            "ui_db_writer_api_read_only_no_conflict",
            bool(writer.get("ok"))
            and bool(writer.get("read_only"))
            and writer_clear_or_matches_r5,
            (
                f"ok={writer.get('ok')}; read_only={writer.get('read_only')}; "
                f"status={writer.get('writer_status')}; "
                f"current_writer_pid={writer_pid}; r5_pid={r5_pid}."
            ),
        ),
        _probe_check(result_by_name, "workspace_guard_api"),
        _check(
            "ui_workspace_guard_passed",
            bool(guard.get("ok"))
            and guard.get("summary_status") == "PASS"
            and int(guard.get("missing_required_commands") or 0) == 0
            and int(guard.get("critical_findings") or 0) == 0,
            (
                f"summary_status={guard.get('summary_status')}; "
                f"missing_required_commands={guard.get('missing_required_commands')}; "
                f"critical_findings={guard.get('critical_findings')}."
            ),
        ),
        _probe_check(result_by_name, "dashboard_snapshot_api"),
        _check(
            "ui_dashboard_snapshot_current",
            bool(dashboard.get("dashboard_snapshot_id"))
            and dashboard_age is not None
            and dashboard_age <= max_dashboard_age_seconds,
            (
                f"snapshot_id={dashboard.get('dashboard_snapshot_id')}; "
                f"generated_at={dashboard.get('generated_at')}; age={dashboard_age}."
            ),
        ),
        _check(
            "ui_dashboard_has_required_watermarks",
            int(dashboard.get("required_watermark_count") or 0) > 0
            and int(dashboard.get("watermark_count") or 0) > 0,
            (
                f"watermark_count={dashboard.get('watermark_count')}; "
                f"required={dashboard.get('required_watermark_count')}."
            ),
        ),
        _check(
            "dashboard_workspace_db_fingerprint_matches",
            bool(guard_fingerprint)
            and guard_fingerprint in dashboard_fingerprints,
            (
                f"workspace_guard={guard_fingerprint}; "
                f"dashboard={','.join(dashboard_fingerprints)}."
            ),
        ),
    ]


def _decision(
    checks: list[dict[str, Any]],
    *,
    r31: dict[str, Any],
    r18: dict[str, Any],
    private_base_url: str,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r31_decision = r31.get("smoke_decision") or {}
    r18_decision = r18.get("runtime_cutover_decision") or {}
    if failed:
        first = failed[0]["check"]
        if first.startswith("scheduler") or first.startswith("r5"):
            status = "BLOCKED_SCHEDULER_STATUS_NOT_VERIFIED"
            next_step = "Phase 3BB-R18 - Resolve Scheduler Runtime Status"
            command = (
                "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
                "--output-dir reports/phase3bb_r18 --reports-dir reports"
            )
        elif first.startswith("r31") or first.startswith("private_base_url"):
            status = "BLOCKED_PRIVATE_UI_SMOKE_NOT_VERIFIED"
            next_step = "Phase 3BB-R31 - Resolve Private UI Smoke"
            command = (
                "kalshi-bot phase3bb-r31-cloud-ui-private-access-operator-smoke-test "
                "--output-dir reports/phase3bb_r31 --reports-dir reports"
            )
        else:
            status = "BLOCKED_DASHBOARD_TRUTH_NOT_VERIFIED"
            next_step = "Phase 3BB-R32 - Resolve Dashboard Truth Verification"
            command = (
                "kalshi-bot phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-"
                "verification --output-dir reports/phase3bb_r32 --reports-dir reports"
            )
        reason = f"First failing check: {first}."
    else:
        status = "VERIFIED_DASHBOARD_TRUTH_AND_SCHEDULER_STATUS"
        next_step = "Phase 3BB-R33 - Cloud Paper-Only Operations Readiness Monitor"
        command = f"Open {private_base_url} and review System, Opportunities, and Reports."
        reason = (
            "The private UI dashboard truth APIs agree with workspace guard state, "
            "and systemd owns exactly one guarded R5 watcher."
        )
    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "private_base_url": private_base_url,
        "r31_status": r31_decision.get("status"),
        "r18_status": r18_decision.get("status"),
        "r5_pid": r18_decision.get("current_r5_pid"),
        "duplicate_r5": bool(r18_decision.get("duplicate_r5")),
        "service_owns_r5": bool(r18_decision.get("service_owns_r5")),
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _probe_check(results: dict[str, UiApiResult], name: str) -> dict[str, Any]:
    result = results.get(name)
    return _check(
        f"ui_{name}_reachable",
        bool(result and result.ok and result.status_code == 200),
        (
            "missing"
            if result is None
            else (
                f"status={result.status_code}; duration={result.duration_seconds}; "
                f"error={result.error or 'none'}."
            )
        ),
    )


def _summarize_probe_json(name: str, parsed: dict[str, Any]) -> dict[str, Any]:
    if name == "db_writer_api":
        monitor = parsed.get("monitor") if isinstance(parsed, dict) else {}
        return {
            "ok": parsed.get("ok") if isinstance(parsed, dict) else False,
            "read_only": parsed.get("read_only") if isinstance(parsed, dict) else False,
            "writer_status": (monitor or {}).get("status"),
            "safe_to_start_write": (monitor or {}).get("safe_to_start_write"),
            "current_writer_pid": (monitor or {}).get("current_writer_pid"),
        }
    if name == "workspace_guard_api":
        guard = parsed.get("guard") if isinstance(parsed, dict) else {}
        summary = (guard or {}).get("summary") if isinstance(guard, dict) else {}
        return {
            "ok": parsed.get("ok") if isinstance(parsed, dict) else False,
            "summary_status": (summary or {}).get("status"),
            "missing_required_commands": (summary or {}).get("missing_required_commands"),
            "critical_findings": (summary or {}).get("critical_findings"),
            "database_fingerprint": (summary or {}).get("database_fingerprint"),
            "git_commit": (summary or {}).get("git_commit"),
        }
    if name == "dashboard_snapshot_api":
        watermarks = parsed.get("source_watermarks") if isinstance(parsed, dict) else []
        effective_filters = parsed.get("effective_filters") if isinstance(parsed, dict) else {}
        fingerprints = sorted(
            {
                str(row.get("database_fingerprint"))
                for row in watermarks or []
                if isinstance(row, dict) and row.get("database_fingerprint")
            }
        )
        return {
            "schema_version": parsed.get("schema_version") if isinstance(parsed, dict) else None,
            "dashboard_snapshot_id": (
                parsed.get("dashboard_snapshot_id") if isinstance(parsed, dict) else None
            ),
            "generated_at": parsed.get("generated_at") if isinstance(parsed, dict) else None,
            "panel_as_of": parsed.get("panel_as_of") if isinstance(parsed, dict) else None,
            "effective_execution_mode": (effective_filters or {}).get("execution_mode"),
            "watermark_count": len(watermarks or []),
            "required_watermark_count": sum(
                1 for row in watermarks or [] if isinstance(row, dict) and row.get("required")
            ),
            "stale_watermark_count": sum(
                1
                for row in watermarks or []
                if isinstance(row, dict) and row.get("freshness_status") == "STALE"
            ),
            "database_fingerprints": fingerprints,
        }
    return {"json_available": bool(parsed)}


def _json_from_body(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _r31_private_base_url(r31: dict[str, Any]) -> str:
    decision = r31.get("smoke_decision") or {}
    return str(decision.get("private_base_url") or "")


def _age_seconds(value: Any, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return round(max(0.0, (now - parsed.astimezone(UTC)).total_seconds()), 3)


def _ui_result_payload(result: UiApiResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "path": result.path,
        "url": result.url,
        "ok": result.ok,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "duration_seconds": result.duration_seconds,
        "body_sha256": result.body_sha256,
        "body_excerpt": result.body_excerpt,
        "parsed_summary": result.parsed_summary,
        "error": result.error,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R32 Dashboard Truth Scheduler Status")
    decision = payload["verification_decision"]
    summaries = payload["ui_dashboard_truth_summaries"]
    dashboard = summaries.get("dashboard_snapshot_api") or {}
    writer = summaries.get("db_writer_api") or {}
    guard = summaries.get("workspace_guard_api") or {}
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Private base URL: `{decision['private_base_url']}`",
            f"- R18 scheduler status: `{decision['r18_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- Duplicate R5: `{decision['duplicate_r5']}`",
            f"- Service owns R5: `{decision['service_owns_r5']}`",
            f"- Writer status: `{writer.get('writer_status')}`",
            f"- Workspace guard: `{guard.get('summary_status')}`",
            f"- Dashboard snapshot: `{dashboard.get('dashboard_snapshot_id')}`",
            f"- Dashboard generated at: `{dashboard.get('generated_at')}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- Used private Tailscale UI/API reads only.",
            "- Ran R18 read-only scheduler probes only.",
            "- Did not start/stop UI, R5, Tailscale, nginx, or firewall services.",
            "- Did not create paper/live/demo trades.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R32 Verification Detail")
    decision = payload["verification_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["verification_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## UI API Summaries", ""])
    for name, summary in payload["ui_dashboard_truth_summaries"].items():
        lines.extend(["", f"### {name}", "", "```json"])
        lines.append(json.dumps(summary, indent=2, sort_keys=True))
        lines.append("```")
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["verification_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"printf '%s\\n' {_shell_quote(decision['operator_next_command'])}",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R32 Next Actions")
    decision = payload["verification_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Command/action: `{decision['operator_next_command']}`",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly.",
            "- Do not enable Tailscale Funnel.",
            "- Do not stop or duplicate R5.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_ui_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "name",
        "path",
        "url",
        "ok",
        "status_code",
        "content_type",
        "duration_seconds",
        "body_sha256",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["check", "passed", "detail"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
