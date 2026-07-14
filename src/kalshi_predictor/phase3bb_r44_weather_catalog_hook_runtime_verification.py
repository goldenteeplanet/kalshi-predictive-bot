from __future__ import annotations

import csv
import json
import re
import shlex
from dataclasses import dataclass
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
    RUNNER_SCRIPT_NAME,
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.phase3bb_r40_cloud_scheduler_runtime_monitor import (
    _parse_scheduler_job_runs,
    _runner_hook_before_fast_lane,
)
from kalshi_predictor.phase3bb_r43_weather_catalog_scheduler_hook import (
    HOOK_JOB_ID,
    WEATHER_FAST_LANE_JOB_ID,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R44_VERSION = "phase3bb_r44_weather_catalog_hook_runtime_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r44")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
DEFAULT_JOURNAL_LINES = 900

WEATHER_CATALOG_REPORT_PATHS = (
    "reports/phase3az_r12_weather/weather_activation_preview.json",
    "reports/phase3az_r12_weather/weather_activation_preview.md",
    "reports/phase3az_r12_weather/weather_activation_candidates.csv",
    "reports/phase3az_r12_weather/safe_to_link.csv",
    "reports/phase3az_r12_weather/safe_to_relink.csv",
    "reports/phase3bb_r2/weather_funnel.json",
    "reports/phase3bb_r2/weather_fast_lane.md",
    "reports/phase3bb_r2/weather_candidates.csv",
)


@dataclass(frozen=True)
class Phase3BBR44WeatherCatalogHookRuntimeVerificationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    job_events_csv_path: Path
    report_freshness_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r44_weather_catalog_hook_runtime_verification_report(
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
    journal_lines: int = DEFAULT_JOURNAL_LINES,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR44WeatherCatalogHookRuntimeVerificationArtifacts:
    payload = build_phase3bb_r44_weather_catalog_hook_runtime_verification(
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
        journal_lines=journal_lines,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_catalog_hook_runtime_verification.md"
    json_path = output_dir / "weather_catalog_hook_runtime_verification.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "hook_runtime_checks.csv"
    job_events_csv_path = output_dir / "scheduler_job_events.csv"
    report_freshness_csv_path = output_dir / "weather_catalog_report_freshness.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["hook_runtime_checks"])
    _write_rows_csv(job_events_csv_path, payload["scheduler_job_events"])
    _write_rows_csv(report_freshness_csv_path, payload["weather_catalog_report_freshness"])
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
            job_events_csv_path,
            report_freshness_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR44WeatherCatalogHookRuntimeVerificationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        job_events_csv_path=job_events_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r44_weather_catalog_hook_runtime_verification(
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
        "command": "kalshi-bot phase3bb-r44-weather-catalog-hook-runtime-verification",
        "argv": command_args or [],
    }
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
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        journal_lines=journal_lines,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    local_r40_payload = _read_json(reports_dir / "phase3bb_r40" / "cloud_scheduler_runtime_monitor.json")
    parsed = _parse_probe_outputs(results, local_r40_payload=local_r40_payload)
    checks = _runtime_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "runtime_verification_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "systemctl_mutating_commands_executed": 0,
        "scheduler_files_written_to_system": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "runs_weather_fast_lane": False,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R44-WEATHER-CATALOG-HOOK-RUNTIME-VERIFICATION",
        "phase_version": PHASE3BB_R44_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_CATALOG_HOOK_RUNTIME_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_hook_runtime_state": parsed,
        "scheduler_job_events": parsed["scheduler_job_events"],
        "weather_catalog_report_freshness": parsed["weather_catalog_report_freshness"],
        "hook_runtime_checks": checks,
        "hook_runtime_decision": decision,
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
    scheduler_service_name: str,
    scheduler_timer_name: str,
    journal_lines: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(scheduler_service_name)
    timer = shlex.quote(scheduler_timer_name)
    runner_path = shlex.quote(f"{target.app_path.rstrip('/')}/scripts/{RUNNER_SCRIPT_NAME}")
    report_list = " ".join(shlex.quote(path) for path in WEATHER_CATALOG_REPORT_PATHS)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe("scheduler_timer_list", f"systemctl list-timers --all {timer} --no-pager || true", timeout_seconds),
        RemoteProbe("scheduler_runner_script", f"test -r {runner_path} && sed -n '1,260p' {runner_path} || true", timeout_seconds),
        RemoteProbe("scheduler_journal", f"journalctl -u {service} -n {int(journal_lines)} --no-pager || true", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe(
            "weather_catalog_report_stats",
            (
                f"cd {app} && for p in {report_list}; do "
                "if [ -e \"$p\" ]; then stat -c '%n|%Y|%s' \"$p\"; "
                "else echo \"$p|MISSING|0\"; fi; done"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_activation_preview_json",
            f"cd {app} && cat reports/phase3az_r12_weather/weather_activation_preview.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_funnel_json",
            f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r40_json",
            f"cd {app} && cat reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "phase3bb-r40-cloud-scheduler-runtime-monitor "
                "phase3bb-r44-weather-catalog-hook-runtime-verification "
                "phase3az-r12-weather-activation-preview "
                "phase3bb-r2-weather-fast-lane; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    local_r40_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    runner_script = _stdout(by_name.get("scheduler_runner_script"))
    journal = _stdout(by_name.get("scheduler_journal"))
    job_events = _parse_scheduler_job_runs(journal)
    sequence = _latest_weather_catalog_sequence(job_events)
    remote_r40_payload = _json_from_probe(by_name.get("r40_json"))
    r40_payload = local_r40_payload if local_r40_payload else remote_r40_payload
    r40_parsed = r40_payload.get("parsed_runtime_state") if isinstance(r40_payload, dict) else {}
    if not isinstance(r40_parsed, dict):
        r40_parsed = {}
    preview_payload = _json_from_probe(by_name.get("weather_activation_preview_json"))
    funnel_payload = _json_from_probe(by_name.get("weather_funnel_json"))
    writer = _json_from_probe(by_name.get("db_writer_monitor_raw"))
    if not isinstance(writer, dict):
        writer = {}
    preview_summary = preview_payload.get("summary") if isinstance(preview_payload, dict) else {}
    if not isinstance(preview_summary, dict):
        preview_summary = {}
    funnel_summary = funnel_payload.get("summary") if isinstance(funnel_payload, dict) else {}
    if not isinstance(funnel_summary, dict):
        funnel_summary = {}
    report_freshness = _parse_report_stats(_stdout(by_name.get("weather_catalog_report_stats")))
    present_reports = [row for row in report_freshness if row["status"] == "PRESENT"]
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "scheduler_timer_active_state": _first_line(_stdout(by_name.get("scheduler_timer_active"))),
        "scheduler_service_active_state": _first_line(_stdout(by_name.get("scheduler_service_active"))),
        "scheduler_timer_next": _extract_timer_next(_stdout(by_name.get("scheduler_timer_list"))),
        "runner_hook_present": HOOK_JOB_ID in runner_script,
        "runner_weather_fast_lane_present": WEATHER_FAST_LANE_JOB_ID in runner_script,
        "runner_hook_before_fast_lane": _runner_hook_before_fast_lane(runner_script),
        "scheduler_job_events": job_events,
        "weather_catalog_hook_run_count": sum(
            1 for row in job_events if row.get("event") == "JOB_STARTED" and row.get("job_id") == HOOK_JOB_ID
        ),
        "weather_fast_lane_run_count": sum(
            1 for row in job_events if row.get("event") == "JOB_STARTED" and row.get("job_id") == WEATHER_FAST_LANE_JOB_ID
        ),
        "weather_catalog_sequence": sequence,
        "weather_catalog_report_freshness": report_freshness,
        "weather_catalog_present_report_count": len(present_reports),
        "weather_activation_preview_json_ok": bool(preview_payload),
        "weather_activation_preview_summary": preview_summary,
        "weather_activation_rows_safe_to_link": preview_summary.get("rows_safe_to_link"),
        "weather_activation_rows_safe_to_relink": preview_summary.get("rows_safe_to_relink"),
        "weather_funnel_json_ok": bool(funnel_payload),
        "weather_funnel_status": funnel_payload.get("status") if isinstance(funnel_payload, dict) else None,
        "weather_funnel_summary": funnel_summary,
        "r40_json_available": bool(r40_payload),
        "r40_source": "LOCAL_REPORTS_DIR" if local_r40_payload else "REMOTE_REPORTS_DIR",
        "r40_scheduler_job_runs_available": bool(
            isinstance(r40_payload, dict) and isinstance(r40_payload.get("scheduler_job_runs"), list)
        ),
        "r40_weather_catalog_hook_job_run_count": r40_parsed.get("weather_catalog_hook_job_run_count"),
        "r40_weather_fast_lane_job_run_count": r40_parsed.get("weather_fast_lane_job_run_count"),
        "r40_weather_catalog_runtime_order_ok": r40_parsed.get("weather_catalog_runtime_order_ok"),
        "command_registry_ok": bool(by_name.get("command_registry") and by_name["command_registry"].ok),
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "scheduler_failed_log_count": len(re.findall(r"\bfailed\b|Failed with result", journal, flags=re.IGNORECASE)),
        "scheduler_traceback_count": len(re.findall(r"\bTraceback \(most recent call last\):", journal)),
    }


def _latest_weather_catalog_sequence(job_events: list[dict[str, Any]]) -> dict[str, Any]:
    hook_indexes = [
        index
        for index, row in enumerate(job_events)
        if row.get("event") == "JOB_STARTED" and row.get("job_id") == HOOK_JOB_ID
    ]
    fast_lane_indexes = [
        index
        for index, row in enumerate(job_events)
        if row.get("event") == "JOB_STARTED" and row.get("job_id") == WEATHER_FAST_LANE_JOB_ID
    ]
    if not hook_indexes:
        return {
            "status": "NO_WEATHER_CATALOG_HOOK_RUN",
            "hook_index": None,
            "fast_lane_index_after_hook": None,
            "sync_after_hook": False,
            "parse_after_hook": False,
            "preview_after_hook": False,
            "fast_lane_after_hook": False,
            "weather_funnel_after_fast_lane": False,
        }
    hook_index = max(hook_indexes)
    fast_after = [index for index in fast_lane_indexes if index > hook_index]
    fast_index = min(fast_after) if fast_after else None

    def _has_event(event: str, *, after: int, before: int | None = None) -> bool:
        for index, row in enumerate(job_events):
            if index <= after:
                continue
            if before is not None and index >= before:
                continue
            if row.get("event") == event:
                return True
        return False

    sync_after_hook = _has_event("WEATHER_CATALOG_SYNCED", after=hook_index, before=fast_index)
    parse_after_hook = _has_event("WEATHER_CATALOG_PARSED", after=hook_index, before=fast_index)
    preview_after_hook = _has_event("WEATHER_CATALOG_PREVIEW_WRITTEN", after=hook_index, before=fast_index)
    funnel_after_fast_lane = False
    if fast_index is not None:
        funnel_after_fast_lane = _has_event("WEATHER_FAST_LANE_WRITTEN", after=fast_index)
    complete = bool(sync_after_hook and parse_after_hook and preview_after_hook and fast_index is not None)
    return {
        "status": "CATALOG_THEN_FAST_LANE_VERIFIED" if complete else "CATALOG_SEQUENCE_INCOMPLETE",
        "hook_index": hook_index,
        "fast_lane_index_after_hook": fast_index,
        "sync_after_hook": sync_after_hook,
        "parse_after_hook": parse_after_hook,
        "preview_after_hook": preview_after_hook,
        "fast_lane_after_hook": fast_index is not None,
        "weather_funnel_after_fast_lane": funnel_after_fast_lane,
    }


def _runtime_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    sequence = parsed.get("weather_catalog_sequence") or {}
    return [
        _check("scheduler_timer_active", parsed.get("scheduler_timer_active_state") == "active", f"timer={parsed.get('scheduler_timer_active_state')}."),
        _check("scheduler_service_state_valid", parsed.get("scheduler_service_active_state") in {"active", "activating", "inactive"}, f"service={parsed.get('scheduler_service_active_state')}."),
        _check("runner_has_weather_catalog_hook", bool(parsed.get("runner_hook_present")), f"hook_present={parsed.get('runner_hook_present')}."),
        _check("runner_hook_before_weather_fast_lane", bool(parsed.get("runner_hook_before_fast_lane")), f"hook_before_fast_lane={parsed.get('runner_hook_before_fast_lane')}."),
        _check("weather_catalog_hook_run_seen", int(parsed.get("weather_catalog_hook_run_count") or 0) > 0, f"runs={parsed.get('weather_catalog_hook_run_count')}."),
        _check("weather_catalog_sync_seen", bool(sequence.get("sync_after_hook")), f"sequence={sequence.get('status')}."),
        _check("weather_catalog_parse_seen", bool(sequence.get("parse_after_hook")), f"sequence={sequence.get('status')}."),
        _check("weather_catalog_preview_seen", bool(sequence.get("preview_after_hook")), f"sequence={sequence.get('status')}."),
        _check("weather_fast_lane_after_catalog", bool(sequence.get("fast_lane_after_hook")), f"sequence={sequence.get('status')}."),
        _check("weather_fast_lane_funnel_written", bool(sequence.get("weather_funnel_after_fast_lane")), f"sequence={sequence.get('status')}."),
        _check("weather_activation_preview_json_ok", bool(parsed.get("weather_activation_preview_json_ok")), "R12 preview JSON exists and parses."),
        _check("weather_funnel_json_ok", bool(parsed.get("weather_funnel_json_ok")), "Weather fast-lane funnel JSON exists and parses."),
        _check("weather_reports_present", int(parsed.get("weather_catalog_present_report_count") or 0) >= 4, f"present={parsed.get('weather_catalog_present_report_count')}."),
        _check("r40_understands_scheduler_job_runs", bool(parsed.get("r40_scheduler_job_runs_available")), "R40 JSON exposes scheduler_job_runs."),
        _check(
            "r40_understands_weather_catalog_hook",
            int(parsed.get("r40_weather_catalog_hook_job_run_count") or 0) > 0
            and bool(parsed.get("r40_weather_catalog_runtime_order_ok")),
            (
                f"r40_catalog_runs={parsed.get('r40_weather_catalog_hook_job_run_count')} "
                f"r40_order_ok={parsed.get('r40_weather_catalog_runtime_order_ok')}."
            ),
        ),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "R40, R44, R12 preview, and weather fast-lane help are registered."),
        _check("scheduler_no_tracebacks", int(parsed.get("scheduler_traceback_count") or 0) == 0, f"tracebacks={parsed.get('scheduler_traceback_count')}."),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if failed:
        if failed[0]["check"].startswith("r40_"):
            status = "WEATHER_CATALOG_RUNTIME_VERIFIED_R40_REFRESH_NEEDED"
            next_step = "Phase 3BB-R44 - Refresh R40 Then Re-verify Weather Catalog Hook"
            command = (
                "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor "
                "--output-dir reports/phase3bb_r40 --reports-dir reports"
            )
            reason = f"Runtime hook evidence exists, but R40 still needs refresh: {failed[0]['check']}."
        else:
            status = "BLOCKED_WEATHER_CATALOG_HOOK_RUNTIME_VERIFICATION"
            next_step = "Phase 3BB-R44 - Recheck Weather Catalog Hook After Next Scheduler Cycle"
            command = (
                "kalshi-bot phase3bb-r44-weather-catalog-hook-runtime-verification "
                "--output-dir reports/phase3bb_r44 --reports-dir reports"
            )
            reason = f"First failing check: {failed[0]['check']}."
    else:
        status = "WEATHER_CATALOG_HOOK_RUNTIME_VERIFIED"
        next_step = "Phase 3BB-R45 - Weather Freshness To Ranking Impact Review"
        command = (
            "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor "
            "--output-dir reports/phase3bb_r40 --reports-dir reports"
        )
        reason = "The scheduler ran weather_current_catalog_refresh, wrote R12 preview artifacts, then ran weather_fast_lane; R40 also recognizes the hook."
    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "weather_catalog_hook_run_count": parsed.get("weather_catalog_hook_run_count"),
        "weather_fast_lane_run_count": parsed.get("weather_fast_lane_run_count"),
        "weather_catalog_sequence": (parsed.get("weather_catalog_sequence") or {}).get("status"),
        "weather_activation_rows_safe_to_link": parsed.get("weather_activation_rows_safe_to_link"),
        "weather_activation_rows_safe_to_relink": parsed.get("weather_activation_rows_safe_to_relink"),
        "weather_funnel_status": parsed.get("weather_funnel_status"),
        "r40_weather_catalog_hook_job_run_count": parsed.get("r40_weather_catalog_hook_job_run_count"),
        "r40_weather_catalog_runtime_order_ok": parsed.get("r40_weather_catalog_runtime_order_ok"),
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R44 Weather Catalog Hook Runtime Verification")
    decision = payload["hook_runtime_decision"]
    parsed = payload["parsed_hook_runtime_state"]
    sequence = parsed.get("weather_catalog_sequence") or {}
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Scheduler timer: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Scheduler service: `{parsed.get('scheduler_service_active_state')}`",
            f"- Timer next: `{parsed.get('scheduler_timer_next')}`",
            f"- Runner hook present: `{parsed.get('runner_hook_present')}`",
            f"- Runner hook before weather fast-lane: `{parsed.get('runner_hook_before_fast_lane')}`",
            f"- Runtime sequence: `{sequence.get('status')}`",
            f"- Catalog hook runs in journal: `{decision['weather_catalog_hook_run_count']}`",
            f"- Weather fast-lane runs in journal: `{decision['weather_fast_lane_run_count']}`",
            f"- R12 rows_safe_to_link: `{decision['weather_activation_rows_safe_to_link']}`",
            f"- R12 rows_safe_to_relink: `{decision['weather_activation_rows_safe_to_relink']}`",
            f"- Weather funnel status: `{decision['weather_funnel_status']}`",
            f"- R40 catalog hook runs: `{decision['r40_weather_catalog_hook_job_run_count']}`",
            f"- R40 catalog-before-fast-lane: `{decision['r40_weather_catalog_runtime_order_ok']}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler service/timer changes by this phase: `0`",
            "- Refresh jobs run by this phase: `0`",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R44 Runtime Detail")
    decision = payload["hook_runtime_decision"]
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
    for row in payload["hook_runtime_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Scheduler Job Events", "", "| Event | Job | Count | Line |", "|---|---|---:|---|"])
    for row in payload["scheduler_job_events"][-60:]:
        lines.append(
            f"| `{row.get('event')}` | `{row.get('job_id', '')}` | `{row.get('count', '')}` | {row.get('line', '')} |"
        )
    lines.extend(["", "## Weather Report Freshness", "", "| Path | Status | Mtime | Size |", "|---|---|---:|---:|"])
    for row in payload["weather_catalog_report_freshness"]:
        lines.append(
            f"| `{row['path']}` | `{row['status']}` | `{row['mtime_epoch']}` | `{row['size_bytes']}` |"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["hook_runtime_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R44 Next Actions")
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
            "- Do not manually run weather refresh jobs while the scheduler is active.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R44 next safe operator command.",
            payload["hook_runtime_decision"]["operator_next_command"],
            "",
        ]
    )


def _parse_report_stats(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        path, mtime, size = parts
        rows.append(
            {
                "path": path,
                "status": "MISSING" if mtime == "MISSING" else "PRESENT",
                "mtime_epoch": "" if mtime == "MISSING" else mtime,
                "size_bytes": size,
            }
        )
    return rows


def _extract_timer_next(text: str) -> str:
    for line in text.splitlines():
        if "kalshi-multicategory-refresh-scheduler.timer" in line and not line.startswith("NEXT"):
            parts = line.split()
            return " ".join(parts[:3]) if len(parts) >= 3 and parts[0] != "-" else "-"
    return ""


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["name", "ok", "exit_code", "duration_seconds", "timed_out", "stdout_excerpt", "stderr_excerpt"]
    _write_rows_csv(path, [{name: row.get(name) for name in fieldnames} for row in rows], fieldnames=fieldnames)


def _write_rows_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
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
