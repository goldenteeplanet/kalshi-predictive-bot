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
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _first_line,
    _mark_executable,
    _parse_report_stats,
    _stdout,
    _target_payload,
)
from kalshi_predictor.phase3bb_r47_weather_current_window_series_discovery import (
    WEATHER_CATALOG_JOB_ID,
    _runner_has_weather_feature_refresh,
    _weather_current_window_snapshot_command,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R48_VERSION = "phase3bb_r48_weather_feature_refresh_runtime_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r48")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_JOURNAL_LINES = 900

WEATHER_REPORT_PATHS = (
    "reports/phase3bb_r47/weather_current_window_series_discovery.json",
    "reports/phase3az_r12_weather/weather_activation_preview.json",
    "reports/phase3az_r12_weather/safe_to_link.csv",
    "reports/phase3az_r12_weather/safe_to_relink.csv",
    "reports/phase3bb_r2/weather_funnel.json",
    "reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json",
)


@dataclass(frozen=True)
class Phase3BBR48WeatherFeatureRefreshRuntimeVerificationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    feature_events_csv_path: Path
    feature_windows_csv_path: Path
    linkability_rows_csv_path: Path
    report_freshness_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r48_weather_feature_refresh_runtime_verification_report(
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
    current_window_lookback_hours: int = 3,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    journal_lines: int = DEFAULT_JOURNAL_LINES,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR48WeatherFeatureRefreshRuntimeVerificationArtifacts:
    payload = build_phase3bb_r48_weather_feature_refresh_runtime_verification(
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
        current_window_lookback_hours=current_window_lookback_hours,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        journal_lines=journal_lines,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_feature_refresh_runtime_verification.md"
    json_path = output_dir / "weather_feature_refresh_runtime_verification.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "runtime_checks.csv"
    feature_events_csv_path = output_dir / "scheduler_feature_events.csv"
    feature_windows_csv_path = output_dir / "weather_feature_windows.csv"
    linkability_rows_csv_path = output_dir / "linkability_rows.csv"
    report_freshness_csv_path = output_dir / "weather_report_freshness.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["runtime_checks"])
    _write_rows_csv(feature_events_csv_path, payload["scheduler_feature_events"])
    _write_rows_csv(feature_windows_csv_path, payload["weather_feature_windows"])
    _write_rows_csv(linkability_rows_csv_path, payload["linkability_rows"])
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
            feature_events_csv_path,
            feature_windows_csv_path,
            linkability_rows_csv_path,
            report_freshness_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR48WeatherFeatureRefreshRuntimeVerificationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        feature_events_csv_path=feature_events_csv_path,
        feature_windows_csv_path=feature_windows_csv_path,
        linkability_rows_csv_path=linkability_rows_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r48_weather_feature_refresh_runtime_verification(
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
    current_window_lookback_hours: int = 3,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
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
        "command": "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification",
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
        current_window_lookback_hours=current_window_lookback_hours,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        journal_lines=journal_lines,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    checks = _runtime_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "weather_feature_refresh_runtime_verification_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 0,
        "systemctl_start_stop_restart_executed": 0,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "scheduler_service_stopped": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "runs_weather_fast_lane": False,
        "runs_weather_forecast": False,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R48-WEATHER-FEATURE-REFRESH-RUNTIME-VERIFICATION",
        "phase_version": PHASE3BB_R48_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_FEATURE_REFRESH_RUNTIME_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_runtime_state": parsed,
        "runtime_checks": checks,
        "scheduler_feature_events": parsed.get("scheduler_feature_events") or [],
        "weather_feature_windows": parsed.get("feature_windows") or [],
        "linkability_rows": parsed.get("linkability_rows") or [],
        "weather_report_freshness": parsed.get("weather_report_freshness") or [],
        "runtime_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    scheduler_service_name: str,
    scheduler_timer_name: str,
    current_window_lookback_hours: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    journal_lines: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(scheduler_service_name)
    timer = shlex.quote(scheduler_timer_name)
    runner_path = shlex.quote(f"{target.app_path.rstrip('/')}/scripts/{RUNNER_SCRIPT_NAME}")
    report_list = " ".join(shlex.quote(path) for path in WEATHER_REPORT_PATHS)
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe(
            "scheduler_service_show",
            f"systemctl show {service} -p Result -p ActiveState -p SubState -p ExecMainStatus --no-pager || true",
            timeout_seconds,
        ),
        RemoteProbe("scheduler_timer_list", f"systemctl list-timers --all {timer} --no-pager || true", timeout_seconds),
        RemoteProbe("scheduler_journal", f"journalctl -u {service} -n {int(journal_lines)} --no-pager || true", timeout_seconds),
        RemoteProbe("scheduler_runner_script", f"test -r {runner_path} && sed -n '1,260p' {runner_path} || true", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe("r47_json", f"cd {app} && cat reports/phase3bb_r47/weather_current_window_series_discovery.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_activation_preview_json", f"cd {app} && cat reports/phase3az_r12_weather/weather_activation_preview.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("weather_funnel_json", f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe("r40_json", f"cd {app} && cat reports/phase3bb_r40/cloud_scheduler_runtime_monitor.json 2>/dev/null || true", timeout_seconds),
        RemoteProbe(
            "weather_current_window_snapshot",
            _weather_current_window_snapshot_command(
                target.db_path,
                current_window_lookback_hours=current_window_lookback_hours,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
            ),
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
            "command_registry",
            (
                f"cd {app} && for cmd in "
                "phase3bb-r48-weather-feature-refresh-runtime-verification "
                "phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
                "phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
                "phase3bb-r40-cloud-scheduler-runtime-monitor "
                "phase3bb-r45-weather-freshness-to-ranking-impact "
                "phase3az-r12-weather-activation-preview "
                "phase3az-r12-weather-missing-link-apply "
                "phase3bb-r2-weather-fast-lane ingest-weather build-weather-features; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    writer = _json_from_probe(by_name.get("db_writer_monitor_raw"))
    r47_payload = _json_from_probe(by_name.get("r47_json"))
    preview_payload = _json_from_probe(by_name.get("weather_activation_preview_json"))
    funnel_payload = _json_from_probe(by_name.get("weather_funnel_json"))
    r40_payload = _json_from_probe(by_name.get("r40_json"))
    snapshot = _json_from_probe(by_name.get("weather_current_window_snapshot"))
    if not isinstance(writer, dict):
        writer = {}
    if not isinstance(r47_payload, dict):
        r47_payload = {}
    if not isinstance(preview_payload, dict):
        preview_payload = {}
    if not isinstance(funnel_payload, dict):
        funnel_payload = {}
    if not isinstance(r40_payload, dict):
        r40_payload = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    runner_script = _stdout(by_name.get("scheduler_runner_script"))
    journal = _stdout(by_name.get("scheduler_journal"))
    job_events = _parse_scheduler_job_runs(journal)
    feature_sequence = _latest_feature_refresh_sequence(job_events)
    preview_summary = preview_payload.get("summary") if isinstance(preview_payload.get("summary"), dict) else {}
    funnel_summary = funnel_payload.get("summary") if isinstance(funnel_payload.get("summary"), dict) else {}
    snapshot_summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    r47_decision = r47_payload.get("linkability_decision") if isinstance(r47_payload.get("linkability_decision"), dict) else {}
    r40_parsed = r40_payload.get("parsed_runtime_state") if isinstance(r40_payload.get("parsed_runtime_state"), dict) else {}
    feature_windows = snapshot.get("feature_windows") if isinstance(snapshot.get("feature_windows"), list) else []
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "scheduler_timer_active_state": _first_line(_stdout(by_name.get("scheduler_timer_active"))),
        "scheduler_service_active_state": _first_line(_stdout(by_name.get("scheduler_service_active"))),
        "scheduler_service_show": _parse_systemd_show(_stdout(by_name.get("scheduler_service_show"))),
        "scheduler_timer_next": _extract_timer_next(_stdout(by_name.get("scheduler_timer_list"))),
        "scheduler_timer_last": _extract_timer_last(_stdout(by_name.get("scheduler_timer_list"))),
        "runner_has_weather_catalog_hook": WEATHER_CATALOG_JOB_ID in runner_script,
        "runner_has_feature_refresh": _runner_has_weather_feature_refresh(runner_script),
        "scheduler_job_events": job_events,
        "scheduler_feature_events": [row for row in job_events if str(row.get("event", "")).startswith("WEATHER_")],
        "feature_refresh_sequence": feature_sequence,
        "feature_refresh_runtime_observed": feature_sequence.get("status") == "FEATURE_REFRESH_THEN_PREVIEW_VERIFIED",
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "r47_json_available": bool(r47_payload),
        "r47_status": r47_decision.get("status"),
        "r47_runner_repaired_after": r47_decision.get("runner_repaired_after"),
        "weather_activation_preview_json_ok": bool(preview_payload),
        "weather_activation_preview_summary": preview_summary,
        "weather_activation_rows_safe_to_link": preview_summary.get("rows_safe_to_link"),
        "weather_activation_rows_safe_to_relink": preview_summary.get("rows_safe_to_relink"),
        "weather_funnel_json_ok": bool(funnel_payload),
        "weather_funnel_status": funnel_payload.get("status"),
        "weather_funnel_summary": funnel_summary,
        "r40_json_available": bool(r40_payload),
        "r40_weather_source_ingest_event_count": r40_parsed.get("weather_source_ingest_event_count"),
        "r40_weather_feature_build_event_count": r40_parsed.get("weather_feature_build_event_count"),
        "weather_current_window_snapshot_ok": bool(snapshot.get("ok")),
        "weather_current_window_error": snapshot.get("error"),
        "weather_current_window_summary": snapshot_summary,
        "current_weather_series": snapshot.get("current_weather_series") or [],
        "linkability_rows": snapshot.get("linkability_rows") or [],
        "feature_windows": feature_windows,
        "weather_report_freshness": _parse_report_stats(_stdout(by_name.get("weather_report_stats"))),
        "command_registry_ok": "COMMAND_REGISTRY_OK" in _stdout(by_name.get("command_registry")),
        "failed_probe_names": [result.name for result in results if not result.ok],
    }


def _latest_feature_refresh_sequence(job_events: list[dict[str, Any]]) -> dict[str, Any]:
    hook_indexes = [
        index
        for index, row in enumerate(job_events)
        if row.get("event") == "JOB_STARTED" and row.get("job_id") == WEATHER_CATALOG_JOB_ID
    ]
    if not hook_indexes:
        return {"status": "NO_WEATHER_CATALOG_RUNTIME_SEEN", "events_seen": []}
    start = hook_indexes[-1]
    following = job_events[start:]
    events = [str(row.get("event") or "") for row in following]
    required = [
        "WEATHER_CATALOG_SYNCED",
        "WEATHER_CATALOG_PARSED",
        "WEATHER_SOURCE_INGESTED",
        "WEATHER_FEATURES_BUILT",
        "WEATHER_CATALOG_PREVIEW_WRITTEN",
    ]
    missing = [event for event in required if event not in events]
    status = "FEATURE_REFRESH_THEN_PREVIEW_VERIFIED" if not missing else "FEATURE_REFRESH_SEQUENCE_INCOMPLETE"
    return {
        "status": status,
        "missing_events": missing,
        "events_seen": events,
        "started_index": start,
    }


def _runtime_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    summary = parsed.get("weather_current_window_summary") or {}
    service_show = parsed.get("scheduler_service_show") or {}
    return [
        _check("remote_probes_completed", not parsed.get("failed_probe_names"), f"failed={','.join(parsed.get('failed_probe_names') or []) or 'none'}."),
        _check("scheduler_timer_active", parsed.get("scheduler_timer_active_state") == "active", f"timer={parsed.get('scheduler_timer_active_state')}."),
        _check(
            "scheduler_service_state_valid",
            parsed.get("scheduler_service_active_state") in {"active", "activating", "inactive"},
            f"service={parsed.get('scheduler_service_active_state')} result={service_show.get('Result')}.",
        ),
        _check("runner_feature_refresh_installed", bool(parsed.get("runner_has_feature_refresh")), "Runner includes ingest-weather and build-weather-features before R12 preview."),
        _check("current_window_snapshot_readable", bool(parsed.get("weather_current_window_snapshot_ok")), f"error={parsed.get('weather_current_window_error')}."),
        _check("current_weather_rows_seen", int(summary.get("current_weather_market_rows") or 0) > 0, f"current_rows={summary.get('current_weather_market_rows')}."),
        _check("r12_preview_available", bool(parsed.get("weather_activation_preview_json_ok")), "R12 weather activation preview JSON exists and parses."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "R48/R47/R45/R40/R12/R2/weather feature commands are registered on the cloud host."),
    ]


def _decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    summary = parsed.get("weather_current_window_summary") or {}
    preview = parsed.get("weather_activation_preview_summary") or {}
    funnel = parsed.get("weather_funnel_summary") or {}
    current_rows = int(summary.get("current_weather_market_rows") or 0)
    missing_links = int(summary.get("missing_current_weather_link_rows") or 0)
    stale_features = int(summary.get("fresh_feature_window_missing_rows") or 0)
    ready_rows = int(summary.get("ready_for_r12_safe_link_preview_rows") or 0)
    rows_safe_to_link = int(preview.get("rows_safe_to_link") or 0)
    rows_safe_to_relink = int(preview.get("rows_safe_to_relink") or 0)
    current_weather_rows = _int_or_none(funnel.get("current_weather_rows"))
    ranking_rows = _int_or_none(funnel.get("ranking_rows"))
    runtime_observed = bool(parsed.get("feature_refresh_runtime_observed"))
    fresh_features_present = current_rows > 0 and stale_features == 0 and ready_rows > 0

    if failed:
        status = "BLOCKED_WEATHER_FEATURE_REFRESH_RUNTIME_VERIFICATION"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R48 - Resolve Runtime Verification Preconditions"
        command = (
            "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification "
            "--output-dir reports/phase3bb_r48 --reports-dir reports"
        )
        first_blocker = failed[0]["check"].upper()
    elif not runtime_observed and not fresh_features_present:
        status = "WAIT_FOR_NEXT_SCHEDULER_CYCLE"
        reason = "R47 is installed, but no repaired scheduler cycle with fresh New York features is visible yet."
        next_step = "Phase 3BB-R48 - Recheck After Next Scheduler Cycle"
        command = (
            "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification "
            "--output-dir reports/phase3bb_r48 --reports-dir reports"
        )
        first_blocker = "FEATURE_REFRESH_RUNTIME_NOT_OBSERVED"
    elif stale_features > 0:
        status = "WEATHER_FEATURE_REFRESH_RUNTIME_STILL_STALE"
        reason = "The repaired scheduler cycle was observed, but current weather rows still lack fresh feature windows."
        next_step = "Phase 3BB-R48 - Inspect Weather Feature Refresh Runtime"
        command = (
            "kalshi-bot phase3bb-r48-weather-feature-refresh-runtime-verification "
            "--output-dir reports/phase3bb_r48 --reports-dir reports"
        )
        first_blocker = "FRESH_FEATURE_WINDOW_MISSING"
    elif rows_safe_to_link > 0 or rows_safe_to_relink > 0 or ready_rows > 0:
        status = "WEATHER_FEATURE_REFRESH_RUNTIME_VERIFIED_LINK_GATE_READY"
        reason = "Fresh current-window weather features exist; the next safe step is the writer-gated missing-link apply."
        next_step = "Phase 3BB-R49 - Weather Missing Link Apply After Feature Refresh"
        command = (
            "kalshi-bot phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
            "--output-dir reports/phase3bb_r49 --reports-dir reports"
        )
        first_blocker = "SAFE_LINK_WRITE_GATE_READY"
    elif missing_links == 0 and (current_weather_rows or 0) > 0 and (ranking_rows or 0) > 0:
        status = "WEATHER_FEATURE_REFRESH_RUNTIME_VERIFIED_RANKING_PRESENT"
        reason = "Weather links, current rows, and rankings are present; refresh the unified paper gate next."
        next_step = "Phase 3BB-R8 - Unified Paper Gate Across Categories"
        command = (
            "kalshi-bot phase3bb-r8-unified-paper-gate "
            "--output-dir reports/phase3bb_r8 --reports-dir reports"
        )
        first_blocker = "PAPER_GATE_REFRESH_NEEDED"
    else:
        status = "WEATHER_FEATURE_REFRESH_RUNTIME_VERIFIED_NEEDS_RANKING_RECHECK"
        reason = "Fresh weather features are present, but ranking/paper-gate impact is not yet confirmed."
        next_step = "Phase 3BB-R45 - Weather Freshness To Ranking Impact Review"
        command = (
            "kalshi-bot phase3bb-r45-weather-freshness-to-ranking-impact "
            "--output-dir reports/phase3bb_r45 --reports-dir reports"
        )
        first_blocker = "RANKING_IMPACT_UNKNOWN"

    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "first_weather_blocker": first_blocker,
        "feature_refresh_runtime_observed": runtime_observed,
        "current_weather_market_rows": current_rows,
        "missing_current_weather_link_rows": missing_links,
        "fresh_feature_window_missing_rows": stale_features,
        "ready_for_r12_safe_link_preview_rows": ready_rows,
        "rows_safe_to_link": rows_safe_to_link,
        "rows_safe_to_relink": rows_safe_to_relink,
        "current_weather_rows": current_weather_rows,
        "ranking_rows": ranking_rows,
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R48 Weather Feature Refresh Runtime Verification")
    decision = payload["runtime_decision"]
    parsed = payload["parsed_runtime_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- First weather blocker: `{decision['first_weather_blocker']}`",
            f"- Scheduler timer: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Scheduler service: `{parsed.get('scheduler_service_active_state')}`",
            f"- Feature refresh runtime observed: `{decision['feature_refresh_runtime_observed']}`",
            f"- Current weather rows: `{decision['current_weather_market_rows']}`",
            f"- Missing current weather links: `{decision['missing_current_weather_link_rows']}`",
            f"- Fresh feature window missing rows: `{decision['fresh_feature_window_missing_rows']}`",
            f"- R12 rows_safe_to_link: `{decision['rows_safe_to_link']}`",
            f"- R12 rows_safe_to_relink: `{decision['rows_safe_to_relink']}`",
            f"- R2 current weather rows: `{decision['current_weather_rows']}`",
            f"- R2 ranking rows: `{decision['ranking_rows']}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler service/timer changes by this phase: `0`",
            "- Refresh jobs run directly by this phase: `0`",
            "- Remote DB writes by this phase: `0`",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R48 Runtime Detail")
    decision = payload["runtime_decision"]
    parsed = payload["parsed_runtime_state"]
    sequence = parsed.get("feature_refresh_sequence") or {}
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Primary reason: {decision['primary_reason']}",
            f"- Feature sequence: `{sequence.get('status')}`",
            f"- Missing sequence events: `{sequence.get('missing_events')}`",
            "",
            "## Checks",
            "",
            "| Check | Passed | Detail |",
            "|---|---:|---|",
        ]
    )
    for row in payload["runtime_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Feature Events", "", "| Event | Job | Count | Line |", "|---|---|---:|---|"])
    for row in payload["scheduler_feature_events"][-80:]:
        lines.append(
            f"| `{row.get('event')}` | `{row.get('job_id', '')}` | `{row.get('count', '')}` | {row.get('line', '')} |"
        )
    lines.extend(["", "## Feature Windows", "", "| Location | Rows | Max Generated | Age Hours |", "|---|---:|---|---:|"])
    for row in payload["weather_feature_windows"]:
        lines.append(
            "| {location_key} | {feature_rows_sampled} | {max_generated_at} | {max_generated_age_hours} |".format(
                **{**{"location_key": "", "feature_rows_sampled": "", "max_generated_at": "", "max_generated_age_hours": ""}, **row}
            )
        )
    lines.extend(["", "## Current Linkability Sample", "", "| Ticker | Location | Target Time | Link | Blocker |", "|---|---|---|---:|---|"])
    for row in payload["linkability_rows"][:40]:
        lines.append(
            "| {ticker} | {location_key} | {target_time} | {has_weather_link} | {blocker} |".format(
                **{**{"ticker": "", "location_key": "", "target_time": "", "has_weather_link": "", "blocker": ""}, **row}
            )
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["runtime_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R48 Next Actions")
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- First weather blocker: `{decision['first_weather_blocker']}`",
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
            "- Do not run weather forecasts until links are written or R48/R45 says the gate is ready.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            payload["runtime_decision"]["operator_next_command"],
            "",
        ]
    )


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


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _extract_timer_next(text: str) -> str:
    for line in text.splitlines():
        if "kalshi-multicategory-refresh-scheduler.timer" in line and not line.startswith("NEXT"):
            parts = line.split()
            return " ".join(parts[:3]) if len(parts) >= 3 and parts[0] != "-" else "-"
    return ""


def _extract_timer_last(text: str) -> str:
    for line in text.splitlines():
        if "kalshi-multicategory-refresh-scheduler.timer" in line and not line.startswith("NEXT"):
            parts = line.split()
            return " ".join(parts[4:7]) if len(parts) >= 7 else ""
    return ""


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}
