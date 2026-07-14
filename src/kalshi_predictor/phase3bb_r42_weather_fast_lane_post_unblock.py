from __future__ import annotations

import csv
import json
import re
import shlex
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
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _json_from_probe,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.phase3bb_r40_cloud_scheduler_runtime_monitor import (
    DEFAULT_R5_SERVICE_NAME,
    DEFAULT_UI_SERVICE_NAME,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R42_VERSION = "phase3bb_r42_weather_fast_lane_post_unblock_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r42")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
DEFAULT_JOURNAL_LINES = 700

WEATHER_REPORT_PATHS = (
    "reports/phase3bb_r2/weather_funnel.json",
    "reports/phase3bb_r2/weather_fast_lane.md",
    "reports/phase3bb_r2/weather_candidates.csv",
    "reports/weather_opportunities.md",
    "reports/phase3ba_r2/weather_ranking_activation.json",
    "reports/phase3ba_r3/weather_paper_gate.json",
    "reports/phase3ba_r5/paper_ready_truth.json",
)


@dataclass(frozen=True)
class Phase3BBR42WeatherFastLanePostUnblockArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    events_csv_path: Path
    report_freshness_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r42_weather_fast_lane_post_unblock_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    scheduler_service_name: str = SCHEDULER_SERVICE_NAME,
    scheduler_timer_name: str = SCHEDULER_TIMER_NAME,
    r5_service_name: str = DEFAULT_R5_SERVICE_NAME,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    journal_lines: int = DEFAULT_JOURNAL_LINES,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR42WeatherFastLanePostUnblockArtifacts:
    payload = build_phase3bb_r42_weather_fast_lane_post_unblock(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        r5_service_name=r5_service_name,
        ui_service_name=ui_service_name,
        journal_lines=journal_lines,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_fast_lane_post_unblock.md"
    json_path = output_dir / "weather_fast_lane_post_unblock.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "verification_checks.csv"
    events_csv_path = output_dir / "weather_fast_lane_events.csv"
    report_freshness_csv_path = output_dir / "weather_report_freshness.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["verification_checks"])
    _write_rows_csv(events_csv_path, payload["weather_fast_lane_events"])
    _write_rows_csv(report_freshness_csv_path, payload["weather_report_freshness"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            probe_csv_path,
            checks_csv_path,
            events_csv_path,
            report_freshness_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR42WeatherFastLanePostUnblockArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        events_csv_path=events_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r42_weather_fast_lane_post_unblock(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    scheduler_service_name: str = SCHEDULER_SERVICE_NAME,
    scheduler_timer_name: str = SCHEDULER_TIMER_NAME,
    r5_service_name: str = DEFAULT_R5_SERVICE_NAME,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    journal_lines: int = DEFAULT_JOURNAL_LINES,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
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
        "command": "kalshi-bot phase3bb-r42-weather-fast-lane-post-unblock-verification",
        "argv": command_args or [],
    }
    r41_payload = _read_json(reports_dir / "phase3bb_r41" / "writer_gate_normalization.json")
    r41_generated_at = str(r41_payload.get("generated_at") or "")
    r41_epoch = _epoch_from_iso(r41_generated_at)
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    target = _resolve_target(
        r11_context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    probes = _build_remote_probes(
        target,
        r41_epoch=r41_epoch,
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        r5_service_name=r5_service_name,
        ui_service_name=ui_service_name,
        journal_lines=journal_lines,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results, r41_payload=r41_payload, r41_epoch=r41_epoch)
    checks = _verification_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "post_unblock_verification_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "systemctl_mutating_commands_executed": 0,
        "scheduler_files_written_to_system": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_weather_fast_lane": False,
        "runs_refresh_jobs": False,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R42-WEATHER-FAST-LANE-POST-UNBLOCK",
        "phase_version": PHASE3BB_R42_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_FAST_LANE_POST_UNBLOCK_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "r41_context_available": bool(r41_payload),
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "r5_service_name": r5_service_name,
        "ui_service_name": ui_service_name,
        "r41_generated_at": r41_generated_at,
        "r41_epoch": r41_epoch,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_post_unblock_state": parsed,
        "verification_checks": checks,
        "post_unblock_decision": decision,
        "weather_fast_lane_events": parsed["weather_fast_lane_events"],
        "weather_report_freshness": parsed["weather_report_freshness"],
        "weather_fast_lane_summary": parsed["weather_fast_lane_summary"],
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    r41_epoch: int | None,
    scheduler_service_name: str,
    scheduler_timer_name: str,
    r5_service_name: str,
    ui_service_name: str,
    journal_lines: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(scheduler_service_name)
    timer = shlex.quote(scheduler_timer_name)
    r5_service = shlex.quote(r5_service_name)
    ui_service = shlex.quote(ui_service_name)
    since = f"--since @{int(r41_epoch)} " if r41_epoch else ""
    report_list = " ".join(shlex.quote(path) for path in WEATHER_REPORT_PATHS)
    writer_cmd = (
        f"cd {app} && set -a && . {env} && set +a && "
        ".venv/bin/kalshi-bot db-writer-monitor --json"
    )
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe(
            "db_writer_monitor_json_tool",
            f"{writer_cmd} | python3 -m json.tool >/dev/null",
            timeout_seconds,
        ),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe("r5_service_active", f"systemctl is-active {r5_service} || true", timeout_seconds),
        RemoteProbe("ui_service_active", f"systemctl is-active {ui_service} || true", timeout_seconds),
        RemoteProbe(
            "scheduler_timer_list",
            f"systemctl list-timers --all {timer} --no-pager || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_journal_post_unblock",
            f"journalctl -u {service} {since}-n {int(journal_lines)} --no-pager || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_report_stats",
            (
                f"cd {app} && for p in {report_list}; do "
                "if [ -e \"$p\" ]; then stat -c '%n|%Y|%s' \"$p\"; "
                "else echo \"$p|MISSING|0\"; fi; done"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_funnel_json",
            f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_ranking_activation_json",
            f"cd {app} && cat reports/phase3ba_r2/weather_ranking_activation.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_paper_gate_json",
            f"cd {app} && cat reports/phase3ba_r3/weather_paper_gate.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_fast_lane_help",
            f"cd {app} && .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --help >/dev/null",
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    r41_payload: dict[str, Any],
    r41_epoch: int | None,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    raw_writer = _stdout(by_name.get("db_writer_monitor_raw"))
    writer_payload: dict[str, Any] = {}
    writer_parse_error = ""
    writer_json_valid = False
    try:
        parsed_writer = json.loads(raw_writer)
        if isinstance(parsed_writer, dict):
            writer_payload = parsed_writer
            writer_json_valid = True
    except json.JSONDecodeError as exc:
        writer_parse_error = str(exc)
    journal = _stdout(by_name.get("scheduler_journal_post_unblock"))
    events = _parse_weather_fast_lane_events(journal)
    report_freshness = _parse_report_stats(_stdout(by_name.get("weather_report_stats")), r41_epoch=r41_epoch)
    weather_funnel = _json_from_probe(by_name.get("weather_funnel_json"))
    ranking_activation = _json_from_probe(by_name.get("weather_ranking_activation_json"))
    paper_gate = _json_from_probe(by_name.get("weather_paper_gate_json"))
    summary = _weather_summary(weather_funnel, ranking_activation, paper_gate)
    latest_weather_event = events[-1] if events else {}
    refreshed_report_paths = [
        row["path"]
        for row in report_freshness
        if row["status"] == "PRESENT" and bool(row.get("refreshed_after_r41"))
    ]
    run_events = [row for row in events if row["kind"] == "WEATHER_FAST_LANE_RUN"]
    skip_events = [row for row in events if row["kind"].endswith("_SKIP")]
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "r41_status": ((r41_payload.get("writer_gate_decision") or {}).get("status")),
        "r41_unblocked": (r41_payload.get("writer_gate_decision") or {}).get("weather_fast_lane_unblocked") is True,
        "r41_generated_at": r41_payload.get("generated_at"),
        "r41_epoch": r41_epoch,
        "db_writer_monitor_stdout_bytes": len(raw_writer.encode("utf-8")),
        "db_writer_monitor_strict_json_valid": writer_json_valid,
        "db_writer_monitor_json_tool_ok": bool(
            by_name.get("db_writer_monitor_json_tool") and by_name["db_writer_monitor_json_tool"].ok
        ),
        "db_writer_monitor_parse_error": writer_parse_error,
        "db_writer_monitor_payload": writer_payload,
        "writer_safe_to_start_write": bool(writer_payload.get("safe_to_start_write")),
        "writer_status": writer_payload.get("status") or "UNKNOWN",
        "writer_pid": writer_payload.get("current_writer_pid"),
        "scheduler_timer_active_state": _first_line(_stdout(by_name.get("scheduler_timer_active"))),
        "scheduler_service_active_state": _first_line(_stdout(by_name.get("scheduler_service_active"))),
        "scheduler_timer_next": _extract_timer_next(_stdout(by_name.get("scheduler_timer_list"))),
        "scheduler_timer_last": _extract_timer_last(_stdout(by_name.get("scheduler_timer_list"))),
        "r5_service_active_state": _first_line(_stdout(by_name.get("r5_service_active"))),
        "ui_service_active_state": _first_line(_stdout(by_name.get("ui_service_active"))),
        "weather_fast_lane_command_registered": bool(
            by_name.get("weather_fast_lane_help") and by_name["weather_fast_lane_help"].ok
        ),
        "weather_fast_lane_events": events,
        "weather_fast_lane_run_count": len(run_events),
        "weather_fast_lane_skip_count": len(skip_events),
        "latest_weather_fast_lane_event": latest_weather_event,
        "weather_report_freshness": report_freshness,
        "weather_reports_refreshed_after_r41": refreshed_report_paths,
        "weather_funnel_report_refreshed_after_r41": "reports/phase3bb_r2/weather_funnel.json"
        in refreshed_report_paths,
        "weather_funnel_json_loaded": bool(weather_funnel),
        "weather_ranking_activation_json_loaded": bool(ranking_activation),
        "weather_paper_gate_json_loaded": bool(paper_gate),
        "weather_fast_lane_summary": summary,
    }


def _verification_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check(
            "r41_unblocked",
            bool(parsed.get("r41_unblocked")),
            f"R41 status={parsed.get('r41_status')} generated_at={parsed.get('r41_generated_at')}.",
        ),
        _check(
            "db_writer_monitor_json_valid",
            bool(parsed.get("db_writer_monitor_strict_json_valid"))
            and bool(parsed.get("db_writer_monitor_json_tool_ok")),
            parsed.get("db_writer_monitor_parse_error") or "db-writer-monitor --json parses cleanly.",
        ),
        _check(
            "writer_safe_to_start_write",
            bool(parsed.get("writer_safe_to_start_write")),
            f"writer_status={parsed.get('writer_status')} writer_pid={parsed.get('writer_pid')}.",
        ),
        _check(
            "scheduler_timer_active",
            parsed.get("scheduler_timer_active_state") == "active",
            f"timer={parsed.get('scheduler_timer_active_state')} next={parsed.get('scheduler_timer_next')}.",
        ),
        _check(
            "r5_service_active",
            parsed.get("r5_service_active_state") == "active",
            f"r5_service={parsed.get('r5_service_active_state')}.",
        ),
        _check(
            "ui_service_active",
            parsed.get("ui_service_active_state") == "active",
            f"ui_service={parsed.get('ui_service_active_state')}.",
        ),
        _check(
            "weather_fast_lane_command_registered",
            bool(parsed.get("weather_fast_lane_command_registered")),
            "phase3bb-r2-weather-fast-lane help is registered.",
        ),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    failed_names = {row["check"] for row in failed}
    latest_event = parsed.get("latest_weather_fast_lane_event") or {}
    latest_kind = str(latest_event.get("kind") or "")
    run_count = int(parsed.get("weather_fast_lane_run_count") or 0)
    skip_count = int(parsed.get("weather_fast_lane_skip_count") or 0)
    report_refreshed = bool(parsed.get("weather_funnel_report_refreshed_after_r41"))
    weather_summary = parsed.get("weather_fast_lane_summary") or {}
    paper_ready_rows = int(weather_summary.get("paper_ready_rows") or 0)
    first_blocker = str(weather_summary.get("first_hard_blocker") or "")
    if "r41_unblocked" in failed_names:
        status = "BLOCKED_R41_NOT_UNBLOCKED"
        reason = "R41 has not produced a clean writer-gate unblock yet."
        next_step = "Phase 3BB-R41 - Writer Gate Normalization / Weather Fast-Lane Unblock"
        command = "kalshi-bot phase3bb-r41-writer-gate-normalization --output-dir reports/phase3bb_r41 --reports-dir reports"
    elif "db_writer_monitor_json_valid" in failed_names:
        status = "BLOCKED_INVALID_DB_WRITER_MONITOR_JSON"
        reason = "db-writer-monitor --json is not strict JSON in the post-unblock probe."
        next_step = "Phase 3BB-R41 - Sync CLI JSON Fix To Cloud"
        command = "kalshi-bot phase3bb-r41-writer-gate-normalization --output-dir reports/phase3bb_r41 --reports-dir reports"
    elif "writer_safe_to_start_write" in failed_names:
        status = "WAIT_FOR_ACTIVE_WRITER"
        reason = "The writer gate is parseable, but safe_to_start_write is false."
        next_step = "Phase 3BB-R42 - Wait For Writer Gate To Clear"
        command = "kalshi-bot db-writer-monitor --json"
    elif failed:
        status = "BLOCKED_POST_UNBLOCK_RUNTIME_DEPENDENCY"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R42 - Resolve Post-Unblock Runtime Dependency"
        command = "kalshi-bot phase3bb-r42-weather-fast-lane-post-unblock-verification --output-dir reports/phase3bb_r42 --reports-dir reports"
    elif skip_count and latest_kind.endswith("_SKIP"):
        status = "BLOCKED_WEATHER_FAST_LANE_STILL_SKIPPING"
        reason = f"Weather fast-lane still skipped after R41; latest skip kind={latest_kind}."
        next_step = "Phase 3BB-R41 - Recheck Writer Gate Normalization"
        command = "kalshi-bot phase3bb-r41-writer-gate-normalization --output-dir reports/phase3bb_r41 --reports-dir reports"
    elif not run_count and not report_refreshed:
        status = "WAITING_FOR_NEXT_WEATHER_FAST_LANE_CYCLE"
        reason = "The gate is open, but no post-R41 scheduler weather_fast_lane cycle is visible yet."
        next_step = "Phase 3BB-R42 - Rerun After Next Scheduler Tick"
        command = "kalshi-bot phase3bb-r42-weather-fast-lane-post-unblock-verification --output-dir reports/phase3bb_r42 --reports-dir reports"
    elif run_count and not report_refreshed:
        status = "BLOCKED_WEATHER_FAST_LANE_ARTIFACT_NOT_REFRESHED"
        reason = "A post-R41 weather_fast_lane run is visible, but weather_funnel.json was not refreshed."
        next_step = "Phase 3BB-R42 - Inspect Weather Fast-Lane Runtime Output"
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
    elif paper_ready_rows > 0:
        status = "WEATHER_FAST_LANE_VERIFIED_PAPER_GATE_OPEN"
        reason = f"Weather fast-lane refreshed after R41 and reports paper_ready_rows={paper_ready_rows}."
        next_step = "Phase 3BB-R43 - Paper-Only Weather Operator Review"
        command = "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
    else:
        status = "WEATHER_FAST_LANE_POST_UNBLOCK_VERIFIED"
        reason = (
            "Weather fast-lane refreshed after R41; no paper-ready row opened. "
            f"Current weather blocker: {first_blocker or 'UNKNOWN'}."
        )
        next_step = "Phase 3BB-R43 - Weather Current Catalog Refresh Scheduler Hook"
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
    return {
        "status": status,
        "verification_passed": status.startswith("WEATHER_FAST_LANE_POST_UNBLOCK_VERIFIED")
        or status == "WEATHER_FAST_LANE_VERIFIED_PAPER_GATE_OPEN",
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "weather_fast_lane_run_count": run_count,
        "weather_fast_lane_skip_count": skip_count,
        "weather_funnel_report_refreshed_after_r41": report_refreshed,
        "weather_status": weather_summary.get("status"),
        "current_weather_rows": weather_summary.get("current_weather_rows"),
        "ranking_rows": weather_summary.get("ranking_rows"),
        "positive_ev_rows": weather_summary.get("positive_ev_rows"),
        "paper_ready_rows": paper_ready_rows,
        "first_hard_blocker": first_blocker,
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _weather_summary(
    weather_funnel: dict[str, Any],
    ranking_activation: dict[str, Any],
    paper_gate: dict[str, Any],
) -> dict[str, Any]:
    summary = weather_funnel.get("summary") if isinstance(weather_funnel, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    gate_summary = paper_gate.get("summary") if isinstance(paper_gate, dict) else {}
    if not isinstance(gate_summary, dict):
        gate_summary = {}
    ranking_summary = ranking_activation.get("summary") if isinstance(ranking_activation, dict) else {}
    if not isinstance(ranking_summary, dict):
        ranking_summary = {}
    return {
        "status": weather_funnel.get("status"),
        "generated_at": weather_funnel.get("generated_at"),
        "current_weather_rows": _first_number(
            summary,
            gate_summary,
            ranking_summary,
            keys=("current_weather_rows", "current_rows", "total_current_weather_links"),
        ),
        "verified_link_rows": _first_number(summary, gate_summary, keys=("verified_link_rows", "verified_rows")),
        "fresh_source_rows": _first_number(summary, gate_summary, keys=("fresh_source_rows", "source_rows")),
        "forecast_rows": _first_number(summary, gate_summary, ranking_summary, keys=("forecast_rows", "links_with_forecasts")),
        "ranking_rows": _first_number(summary, gate_summary, ranking_summary, keys=("ranking_rows", "ranked_rows")),
        "positive_ev_rows": _first_number(summary, gate_summary, keys=("positive_ev_rows",)),
        "paper_ready_rows": _first_number(summary, gate_summary, keys=("paper_ready_rows",)),
        "first_hard_blocker": summary.get("first_hard_blocker")
        or gate_summary.get("first_hard_blocker")
        or weather_funnel.get("status"),
        "next_action": weather_funnel.get("next_action") or {},
    }


def _parse_weather_fast_lane_events(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        kind = ""
        if "running weather_fast_lane" in line:
            kind = "WEATHER_FAST_LANE_RUN"
        elif "Writer active; skip writer-gated job weather_fast_lane" in line:
            kind = "WRITER_ACTIVE_SKIP"
        elif "db-writer-monitor JSON parse failed; skip writer-gated job" in line:
            kind = "WRITER_MONITOR_PARSE_SKIP"
        elif "db-writer-monitor failed; skip writer-gated job" in line:
            kind = "WRITER_MONITOR_FAILED_SKIP"
        elif "Wrote JSON: reports/phase3bb_r2/weather_funnel.json" in line:
            kind = "WEATHER_FAST_LANE_JSON_WRITTEN"
        elif "Phase 3BB-R2" in line and "Weather" in line:
            kind = "WEATHER_FAST_LANE_OUTPUT"
        if kind:
            rows.append({"kind": kind, "line": line[:700]})
    return rows[-150:]


def _parse_report_stats(text: str, *, r41_epoch: int | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        path, mtime, size = parts
        mtime_epoch = None if mtime == "MISSING" else _to_int(mtime)
        rows.append(
            {
                "path": path,
                "status": "MISSING" if mtime == "MISSING" else "PRESENT",
                "mtime_epoch": "" if mtime_epoch is None else mtime_epoch,
                "size_bytes": size,
                "refreshed_after_r41": bool(
                    r41_epoch is not None and mtime_epoch is not None and mtime_epoch >= r41_epoch
                ),
            }
        )
    return rows


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R42 Weather Fast-Lane Post-Unblock Verification")
    decision = payload["post_unblock_decision"]
    parsed = payload["parsed_post_unblock_state"]
    summary = payload["weather_fast_lane_summary"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- R41 generated at: `{payload.get('r41_generated_at')}`",
            f"- db-writer-monitor strict JSON: `{parsed.get('db_writer_monitor_strict_json_valid')}`",
            f"- safe_to_start_write: `{parsed.get('writer_safe_to_start_write')}`",
            f"- Writer status: `{parsed.get('writer_status')}`",
            f"- Weather run count after R41: `{decision['weather_fast_lane_run_count']}`",
            f"- Weather skip count after R41: `{decision['weather_fast_lane_skip_count']}`",
            f"- Weather funnel refreshed after R41: `{decision['weather_funnel_report_refreshed_after_r41']}`",
            "",
            "## Weather Funnel",
            "",
            f"- Weather status: `{summary.get('status')}`",
            f"- Current weather rows: `{summary.get('current_weather_rows')}`",
            f"- Ranking rows: `{summary.get('ranking_rows')}`",
            f"- Positive EV rows: `{summary.get('positive_ev_rows')}`",
            f"- Paper-ready rows: `{summary.get('paper_ready_rows')}`",
            f"- First blocker: `{summary.get('first_hard_blocker')}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Weather fast-lane executed by this phase: `False`",
            "- Scheduler/R5/UI starts or stops by this phase: `0`",
            "",
            "## Next",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R42 Weather Fast-Lane Detail")
    decision = payload["post_unblock_decision"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Primary reason: {decision['primary_reason']}",
            "",
            "## Checks",
            "",
            "| Check | Passed | Detail |",
            "|---|---:|---|",
        ]
    )
    for row in payload["verification_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Weather Events After R41", ""])
    if payload["weather_fast_lane_events"]:
        for row in payload["weather_fast_lane_events"][-30:]:
            lines.append(f"- `{row['kind']}`: {row['line']}")
    else:
        lines.append("- No weather fast-lane run/skip lines observed after the R41 timestamp.")
    lines.extend(["", "## Weather Report Freshness", "", "| Path | Status | Mtime | Refreshed After R41 |", "|---|---|---:|---:|"])
    for row in payload["weather_report_freshness"]:
        lines.append(
            f"| `{row['path']}` | `{row['status']}` | `{row['mtime_epoch']}` | `{row['refreshed_after_r41']}` |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R42 Next Actions")
    decision = payload["post_unblock_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not start duplicate R5 watchers.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not manually force weather fast-lane unless a later approved phase asks for it.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "# Phase 3BB-R42 next safe operator command.\n"
        f"{payload['post_unblock_decision']['operator_next_command']}\n"
    )


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def _stdout(result: RemoteProbeResult | None) -> str:
    if result is None:
        return ""
    return result.stdout or ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _extract_timer_next(text: str) -> str:
    for line in text.splitlines():
        if SCHEDULER_TIMER_NAME in line or "kalshi-multicategory-refresh-scheduler.timer" in line:
            return line.strip()[:500]
    return ""


def _extract_timer_last(text: str) -> str:
    for line in text.splitlines():
        if SCHEDULER_TIMER_NAME in line or "kalshi-multicategory-refresh-scheduler.timer" in line:
            match = re.search(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{4}-\d{2}-\d{2}.*", line)
            return match.group(0)[:500] if match else line.strip()[:500]
    return ""


def _epoch_from_iso(value: str) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _to_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _first_number(*sources: dict[str, Any], keys: tuple[str, ...]) -> int:
    for source in sources:
        for key in keys:
            value = source.get(key)
            number = _to_int(value)
            if number is not None:
                return number
    return 0
