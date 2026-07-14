from __future__ import annotations

import csv
import json
import re
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
    _loose_db_writer_state,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    RUNNER_SCRIPT_NAME,
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R40_VERSION = "phase3bb_r40_cloud_scheduler_runtime_monitor_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r40")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
DEFAULT_JOURNAL_LINES = 500
DEFAULT_UI_SERVICE_NAME = "kalshi-ui.service"
DEFAULT_R5_SERVICE_NAME = "kalshi-r5-watcher.service"

REPORT_PATHS = (
    "reports/phase3bb_r33/cloud_paper_only_operations_readiness.json",
    "reports/phase3bb_r8/unified_paper_gate.md",
    "reports/phase3bb_r8/paper_gate_rows.csv",
    "reports/phase3bb_r2/weather_fast_lane.md",
    "reports/phase3az_r12_weather/weather_activation_preview.json",
    "reports/phase3az_r12_weather/weather_activation_preview.md",
    "reports/phase3bb_r53/weather_current_window_cadence.json",
    "reports/phase3bb_r53/weather_current_window_cadence.md",
    "reports/phase3bb_r54/weather_missing_link_apply_deferral.json",
    "reports/phase3bb_r54/weather_missing_link_apply_deferral.md",
    "reports/phase3bb_r55/weather_ranking_path_retry.json",
    "reports/phase3bb_r55/weather_ranking_path_retry.md",
    "reports/phase3bb_r3/free_source_inventory.md",
    "reports/phase3bb_r4/economic_parser_backfill.md",
    "reports/phase3bb_r6/sports_provenance_repair.md",
    "reports/phase3bb_r7/news_event_discovery.md",
    "reports/market_coverage/market_coverage_doctor.md",
    "reports/phase3bc_r5/phase3bc_r5_status.json",
)


@dataclass(frozen=True)
class Phase3BBR40CloudSchedulerRuntimeMonitorArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    cycle_csv_path: Path
    job_csv_path: Path
    report_freshness_csv_path: Path
    writer_gate_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r40_cloud_scheduler_runtime_monitor_report(
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
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    r5_service_name: str = DEFAULT_R5_SERVICE_NAME,
    journal_lines: int = DEFAULT_JOURNAL_LINES,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR40CloudSchedulerRuntimeMonitorArtifacts:
    payload = build_phase3bb_r40_cloud_scheduler_runtime_monitor(
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
        ui_service_name=ui_service_name,
        r5_service_name=r5_service_name,
        journal_lines=journal_lines,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_scheduler_runtime_monitor.md"
    json_path = output_dir / "cloud_scheduler_runtime_monitor.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "runtime_checks.csv"
    cycle_csv_path = output_dir / "scheduler_cycle_rows.csv"
    job_csv_path = output_dir / "scheduler_job_runs.csv"
    report_freshness_csv_path = output_dir / "latest_report_freshness.csv"
    writer_gate_csv_path = output_dir / "writer_gate_skips.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["runtime_checks"])
    _write_rows_csv(cycle_csv_path, payload["scheduler_cycles"])
    _write_rows_csv(job_csv_path, payload["scheduler_job_runs"])
    _write_rows_csv(report_freshness_csv_path, payload["latest_reports"])
    _write_rows_csv(writer_gate_csv_path, payload["writer_gate_skips"])
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
            cycle_csv_path,
            job_csv_path,
            report_freshness_csv_path,
            writer_gate_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR40CloudSchedulerRuntimeMonitorArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        cycle_csv_path=cycle_csv_path,
        job_csv_path=job_csv_path,
        report_freshness_csv_path=report_freshness_csv_path,
        writer_gate_csv_path=writer_gate_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r40_cloud_scheduler_runtime_monitor(
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
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    r5_service_name: str = DEFAULT_R5_SERVICE_NAME,
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
        "command": "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor",
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
        ui_service_name=ui_service_name,
        r5_service_name=r5_service_name,
        journal_lines=journal_lines,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    checks = _runtime_checks(parsed)
    decision = _runtime_decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "runtime_monitor_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "systemctl_mutating_commands_executed": 0,
        "tailscale_mutating_commands_executed": 0,
        "scheduler_files_written_to_system": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
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
        "phase": "3BB-R40-CLOUD-SCHEDULER-RUNTIME-MONITOR",
        "phase_version": PHASE3BB_R40_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_RUNTIME_MONITOR",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "ui_service_name": ui_service_name,
        "r5_service_name": r5_service_name,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_runtime_state": parsed,
        "scheduler_cycles": parsed["scheduler_cycles"],
        "scheduler_job_runs": parsed["scheduler_job_runs"],
        "writer_gate_skips": parsed["writer_gate_skips"],
        "latest_reports": parsed["latest_reports"],
        "runtime_checks": checks,
        "runtime_decision": decision,
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
    ui_service_name: str,
    r5_service_name: str,
    journal_lines: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = _shell_quote(target.app_path)
    service = _shell_quote(scheduler_service_name)
    timer = _shell_quote(scheduler_timer_name)
    ui_service = _shell_quote(ui_service_name)
    r5_service = _shell_quote(r5_service_name)
    report_list = " ".join(_shell_quote(path) for path in REPORT_PATHS)
    runner_script_path = _shell_quote(f"{target.app_path.rstrip('/')}/scripts/{RUNNER_SCRIPT_NAME}")
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_timer_enabled", f"systemctl is-enabled {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe("scheduler_timer_list", f"systemctl list-timers --all {timer} --no-pager || true", timeout_seconds),
        RemoteProbe(
            "scheduler_service_systemd",
            f"systemctl show {service} --property=LoadState,ActiveState,SubState,ExecMainPID,Result,NRestarts || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_timer_systemd",
            f"systemctl show {timer} --property=LoadState,ActiveState,SubState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_journal",
            f"journalctl -u {service} -n {int(journal_lines)} --no-pager || true",
            timeout_seconds,
        ),
        RemoteProbe("scheduler_runner_script", f"test -r {runner_script_path} && sed -n '1,260p' {runner_script_path} || true", timeout_seconds),
        RemoteProbe("ui_service_active", f"systemctl is-active {ui_service} || true", timeout_seconds),
        RemoteProbe("r5_service_active", f"systemctl is-active {r5_service} || true", timeout_seconds),
        RemoteProbe(
            "r5_service_systemd",
            f"systemctl show {r5_service} --property=LoadState,ActiveState,SubState,ExecMainPID,Result,NRestarts || true",
            timeout_seconds,
        ),
        RemoteProbe("tailscale_serve_status", "tailscale serve status 2>/dev/null || true", timeout_seconds),
        RemoteProbe(
            "ui_local_http",
            "curl -fsS -m 8 -o /dev/null -w '%{http_code} %{content_type}' http://127.0.0.1:8080/ || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_status_json",
            f"cd {app} && cat reports/phase3bc_r5/phase3bc_r5_status.json 2>/dev/null || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_processes",
            "pgrep -af 'phase3bc-r5-crypto-freshness-watch' || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "db_writer_monitor",
            (
                f"cd {app} && set -a && . {_shell_quote(target.env_path)} && set +a && "
                ".venv/bin/kalshi-bot db-writer-monitor --json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "latest_reports",
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
                "phase3bb-r33-cloud-paper-only-operations-readiness "
                "phase3bb-r8-unified-paper-gate "
                "phase3bb-r2-weather-fast-lane "
                "phase3az-r12-weather-activation-preview "
                "phase3bb-r44-weather-catalog-hook-runtime-verification "
                "phase3bb-r46-cloud-scheduler-weather-writer-gate-repair "
                "phase3bb-r47-weather-current-window-series-discovery-linkability-repair "
                "phase3bb-r48-weather-feature-refresh-runtime-verification "
                "phase3bb-r49-weather-missing-link-apply-after-feature-refresh "
                "phase3bb-r50-weather-post-link-ranking-fast-lane-recheck "
                "phase3bb-r51-weather-ranking-path-repair "
                "phase3bb-r52-weather-ev-fair-value-diagnostic "
                "phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
                "phase3bb-r54-weather-missing-link-apply-deferral "
                "phase3bb-r55-weather-ranking-path-retry "
                "phase3bb-r57-weather-selected-window-pipeline-speed-repair "
                "phase3bb-r58-weather-selected-window-forecast-feature-alignment-repair "
                "phase3bb-r59-weather-catalog-refresh-r57-retry "
                "phase3bb-r60-weather-next-window-lead-time-scheduler-repair "
                "phase3bb-r3-free-source-inventory "
                "phase3bc-r5-status; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    r5_status = _json_from_probe(by_name.get("r5_status_json"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not writer:
        writer = _loose_db_writer_state(by_name.get("db_writer_monitor"))
    service_systemd = _parse_systemd_show(_stdout(by_name.get("scheduler_service_systemd")))
    timer_systemd = _parse_systemd_show(_stdout(by_name.get("scheduler_timer_systemd")))
    r5_service_systemd = _parse_systemd_show(_stdout(by_name.get("r5_service_systemd")))
    timer_active = _first_line(_stdout(by_name.get("scheduler_timer_active")))
    service_active = _first_line(_stdout(by_name.get("scheduler_service_active")))
    timer_enabled = _first_line(_stdout(by_name.get("scheduler_timer_enabled")))
    ui_service_active = _first_line(_stdout(by_name.get("ui_service_active")))
    r5_service_active = _first_line(_stdout(by_name.get("r5_service_active")))
    ui_http = _first_line(_stdout(by_name.get("ui_local_http")))
    tailscale = _stdout(by_name.get("tailscale_serve_status"))
    journal_text = _stdout(by_name.get("scheduler_journal"))
    runner_text = _stdout(by_name.get("scheduler_runner_script"))
    r5_process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    r5_guard = r5_status.get("guard") if isinstance(r5_status, dict) else {}
    latest_summary = r5_status.get("latest_summary") if isinstance(r5_status, dict) else {}
    r5_pids = [_to_int(pid) for pid in (r5_process or {}).get("phase3bc_r5_pids") or []]
    r5_pids = [pid for pid in r5_pids if pid is not None]
    r5_service_pid = _to_int(r5_service_systemd.get("ExecMainPID"))
    r5_systemd_running = (r5_service_active or r5_service_systemd.get("ActiveState")) == "active" and bool(
        r5_service_pid
    )
    if r5_systemd_running and r5_service_pid:
        r5_pids = [r5_service_pid]
    elif not r5_pids:
        r5_pids = _pids_from_process_output(_stdout(by_name.get("r5_processes")))
    scheduler_cycles = _parse_scheduler_cycles(journal_text)
    scheduler_job_runs = _parse_scheduler_job_runs(journal_text)
    writer_gate_skips = _parse_writer_gate_skips(journal_text)
    latest_reports = _parse_latest_reports(_stdout(by_name.get("latest_reports")))
    traceback_count = len(re.findall(r"\bTraceback \(most recent call last\):", journal_text))
    failed_count = len(re.findall(r"\bfailed\b|Failed with result", journal_text, flags=re.IGNORECASE))
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "scheduler_timer_active_state": timer_active or timer_systemd.get("ActiveState"),
        "scheduler_timer_enabled_state": timer_enabled or timer_systemd.get("UnitFileState"),
        "scheduler_service_active_state": service_active or service_systemd.get("ActiveState"),
        "scheduler_service_result": service_systemd.get("Result"),
        "scheduler_timer_next": _extract_timer_next(_stdout(by_name.get("scheduler_timer_list"))),
        "scheduler_timer_last": _extract_timer_last(_stdout(by_name.get("scheduler_timer_list"))),
        "scheduler_service_systemd": service_systemd,
        "scheduler_timer_systemd": timer_systemd,
        "scheduler_cycles": scheduler_cycles,
        "scheduler_job_runs": scheduler_job_runs,
        "scheduler_cycle_started_count": sum(1 for row in scheduler_cycles if row["event"] == "STARTED"),
        "scheduler_cycle_finished_count": sum(
            1 for row in scheduler_cycles if row["event"] in {"FINISHED", "CRYPTO_STATUS_COMPLETE"}
        ),
        "scheduler_traceback_count": traceback_count,
        "scheduler_failed_log_count": failed_count,
        "scheduler_runner_has_weather_catalog_hook": "weather_current_catalog_refresh" in runner_text,
        "scheduler_runner_weather_catalog_before_fast_lane": _runner_hook_before_fast_lane(runner_text),
        "weather_catalog_hook_job_run_count": sum(
            1
            for row in scheduler_job_runs
            if row.get("event") == "JOB_STARTED" and row.get("job_id") == "weather_current_catalog_refresh"
        ),
        "weather_fast_lane_job_run_count": sum(
            1 for row in scheduler_job_runs if row.get("event") == "JOB_STARTED" and row.get("job_id") == "weather_fast_lane"
        ),
        "weather_catalog_sync_event_count": sum(1 for row in scheduler_job_runs if row.get("event") == "WEATHER_CATALOG_SYNCED"),
        "weather_catalog_parse_event_count": sum(1 for row in scheduler_job_runs if row.get("event") == "WEATHER_CATALOG_PARSED"),
        "weather_catalog_preview_event_count": sum(1 for row in scheduler_job_runs if row.get("event") == "WEATHER_CATALOG_PREVIEW_WRITTEN"),
        "weather_source_ingest_event_count": sum(1 for row in scheduler_job_runs if row.get("event") == "WEATHER_SOURCE_INGESTED"),
        "weather_feature_build_event_count": sum(1 for row in scheduler_job_runs if row.get("event") == "WEATHER_FEATURES_BUILT"),
        "weather_fast_lane_complete_event_count": sum(1 for row in scheduler_job_runs if row.get("event") == "WEATHER_FAST_LANE_WRITTEN"),
        "weather_catalog_runtime_order_ok": _job_started_before(
            scheduler_job_runs,
            before_job="weather_current_catalog_refresh",
            after_job="weather_fast_lane",
        ),
        "writer_gate_skips": writer_gate_skips,
        "writer_gate_skip_count": len(writer_gate_skips),
        "ui_service_active_state": ui_service_active,
        "ui_local_http": ui_http,
        "ui_local_http_ok": ui_http.startswith("200"),
        "tailscale_serve_status": tailscale.strip(),
        "tailscale_private_url": _extract_tailscale_url(tailscale),
        "tailscale_private_access_ok": "tailnet only" in tailscale.lower()
        and "127.0.0.1:8080" in tailscale,
        "r5_status": r5_status,
        "r5_service_active_state": r5_service_active or r5_service_systemd.get("ActiveState"),
        "r5_service_systemd": r5_service_systemd,
        "r5_service_pid": r5_service_pid,
        "r5_systemd_running": r5_systemd_running,
        "r5_running": r5_systemd_running
        or bool((r5_process or {}).get("phase3bc_r5_process_running"))
        or bool(r5_pids),
        "r5_pids": r5_pids,
        "duplicate_r5": len(r5_pids) > 1,
        "r5_pid": _to_int(r5_status.get("pid")) if isinstance(r5_status, dict) else (r5_pids[0] if r5_pids else None),
        "r5_guard_status": (r5_guard or {}).get("status"),
        "r5_guard_should_stop": bool((r5_guard or {}).get("should_stop")),
        "r5_watch_state": r5_status.get("latest_watch_state") if isinstance(r5_status, dict) else None,
        "positive_ev_rows": (latest_summary or {}).get("positive_ev_rows"),
        "paper_ready_candidates": (latest_summary or {}).get("paper_ready_candidates"),
        "db_writer_monitor": writer,
        "writer_status": writer.get("status") if isinstance(writer, dict) else "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if isinstance(writer, dict) else False,
        "latest_reports": latest_reports,
        "missing_report_count": sum(1 for row in latest_reports if row["status"] == "MISSING"),
        "command_registry_ok": bool(by_name.get("command_registry") and by_name["command_registry"].ok),
    }


def _runtime_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check("scheduler_timer_active", parsed.get("scheduler_timer_active_state") == "active", f"Timer state is {parsed.get('scheduler_timer_active_state')}."),
        _check("scheduler_timer_enabled", parsed.get("scheduler_timer_enabled_state") == "enabled", f"Timer enabled state is {parsed.get('scheduler_timer_enabled_state')}."),
        _check("scheduler_service_runtime_state_valid", parsed.get("scheduler_service_active_state") in {"active", "activating", "inactive"}, f"Service state is {parsed.get('scheduler_service_active_state')}."),
        _check("scheduler_cycles_seen", parsed.get("scheduler_cycle_finished_count", 0) > 0 or bool(parsed.get("scheduler_timer_last")), f"Finished cycles in journal window: {parsed.get('scheduler_cycle_finished_count')}; timer last={parsed.get('scheduler_timer_last')}."),
        _check("scheduler_service_not_failed", parsed.get("scheduler_service_result") in {"", "success", None}, f"Service result is {parsed.get('scheduler_service_result')}."),
        _check("weather_catalog_hook_in_runner", bool(parsed.get("scheduler_runner_has_weather_catalog_hook")), f"weather_current_catalog_refresh present={parsed.get('scheduler_runner_has_weather_catalog_hook')}."),
        _check("weather_catalog_hook_before_fast_lane", bool(parsed.get("scheduler_runner_weather_catalog_before_fast_lane")), f"hook_before_fast_lane={parsed.get('scheduler_runner_weather_catalog_before_fast_lane')}."),
        _check("r5_running_single", bool(parsed.get("r5_running")) and not parsed.get("duplicate_r5"), f"R5 PIDs: {parsed.get('r5_pids')}; service={parsed.get('r5_service_active_state')}."),
        _check("r5_guard_not_overrunning", (bool(parsed.get("r5_systemd_running")) or parsed.get("r5_guard_status") == "RUNNING") and parsed.get("r5_guard_should_stop") is False, f"guard={parsed.get('r5_guard_status')} should_stop={parsed.get('r5_guard_should_stop')} service={parsed.get('r5_service_active_state')}."),
        _check("ui_service_active", parsed.get("ui_service_active_state") == "active", f"UI service state is {parsed.get('ui_service_active_state')}."),
        _check("ui_local_http_ok", bool(parsed.get("ui_local_http_ok")), f"Local UI probe: {parsed.get('ui_local_http')}."),
        _check("tailscale_private_access_ok", bool(parsed.get("tailscale_private_access_ok")), f"Tailscale URL: {parsed.get('tailscale_private_url')}."),
        _check("command_registry_ok", bool(parsed.get("command_registry_ok")), "R40 CLI help is registered on the cloud host."),
    ]


def _runtime_decision(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    writer_skips = int(parsed.get("writer_gate_skip_count") or 0)
    paper_ready = int(parsed.get("paper_ready_candidates") or 0)
    if failed:
        status = "BLOCKED_CLOUD_RUNTIME_MONITOR"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R40 - Resolve Cloud Runtime Monitor Failure"
    elif writer_skips > 0 or int(parsed.get("scheduler_traceback_count") or 0) > 0:
        status = "OVERNIGHT_READY_WITH_WRITER_GATE_WARNINGS"
        reason = (
            "Scheduler, R5, UI, and private access are running, but writer-gated "
            f"weather work skipped {writer_skips} time(s) and "
            f"{parsed.get('scheduler_traceback_count')} old/new traceback marker(s) exist in the journal window."
        )
        next_step = "Phase 3BB-R41 - Writer Gate Normalization / Weather Fast-Lane Unblock"
    else:
        status = "OVERNIGHT_READY"
        reason = "Scheduler, R5, UI, and private access are healthy in the current runtime window."
        next_step = "Phase 3BB-R41 - Morning Scheduler Cycle Review"
    return {
        "status": status,
        "runtime_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "overnight_safe_to_leave_running": not failed,
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "will_continue_scheduler_cycles": not failed and parsed.get("scheduler_timer_active_state") == "active",
        "will_continue_r5_watch": bool(parsed.get("r5_running")) and not parsed.get("duplicate_r5"),
        "paper_ready_candidates": paper_ready,
        "positive_ev_rows": parsed.get("positive_ev_rows"),
        "writer_gate_skip_count": writer_skips,
        "scheduler_traceback_count": parsed.get("scheduler_traceback_count"),
        "weather_catalog_hook_job_run_count": parsed.get("weather_catalog_hook_job_run_count"),
        "weather_fast_lane_job_run_count": parsed.get("weather_fast_lane_job_run_count"),
        "weather_catalog_runtime_order_ok": parsed.get("weather_catalog_runtime_order_ok"),
        "timer_next": parsed.get("scheduler_timer_next"),
        "operator_next_command": (
            "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor "
            "--output-dir reports/phase3bb_r40 --reports-dir reports"
        ),
        "next_codex_step": next_step,
    }


def _parse_scheduler_cycles(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "Starting kalshi-multicategory-refresh-scheduler.service" in line:
            rows.append({"event": "STARTED", "line": line[:500]})
        elif "running operations_readiness_monitor" in line:
            rows.append({"event": "STARTED", "line": line[:500]})
        elif "Finished kalshi-multicategory-refresh-scheduler.service" in line:
            rows.append({"event": "FINISHED", "line": line[:500]})
        elif "Deactivated successfully" in line:
            rows.append({"event": "DEACTIVATED", "line": line[:500]})
        elif "Wrote Markdown: reports/phase3bc_r5/phase3bc_r5_status.md" in line:
            rows.append({"event": "CRYPTO_STATUS_COMPLETE", "line": line[:500]})
    return rows[-50:]


def _parse_scheduler_job_runs(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.search(r"\[phase3bb-r35\]\s+running\s+([A-Za-z0-9_:-]+)", line)
        if match:
            rows.append({"event": "JOB_STARTED", "job_id": match.group(1), "line": line[:500]})
        elif re.search(r"\bSynced\s+\d+\s+markets\b", line):
            count_match = re.search(r"\bSynced\s+(\d+)\s+markets\b", line)
            rows.append(
                {
                    "event": "WEATHER_CATALOG_SYNCED",
                    "job_id": "weather_current_catalog_refresh",
                    "count": count_match.group(1) if count_match else "",
                    "line": line[:500],
                }
            )
        elif "Market leg parse summary" in line:
            rows.append({"event": "WEATHER_CATALOG_PARSED", "job_id": "weather_current_catalog_refresh", "line": line[:500]})
        elif re.search(r"\bInserted\s+\d+\s+weather forecast row\(s\)", line):
            count_match = re.search(r"\bInserted\s+(\d+)\s+weather forecast row\(s\)", line)
            rows.append(
                {
                    "event": "WEATHER_SOURCE_INGESTED",
                    "job_id": "weather_current_catalog_refresh",
                    "count": count_match.group(1) if count_match else "",
                    "line": line[:500],
                }
            )
        elif re.search(r"\bProcessed\s+\d+\s+weather forecast row\(s\)", line):
            count_match = re.search(r"\bProcessed\s+(\d+)\s+weather forecast row\(s\)", line)
            rows.append(
                {
                    "event": "WEATHER_FEATURES_BUILT",
                    "job_id": "weather_current_catalog_refresh",
                    "count": count_match.group(1) if count_match else "",
                    "line": line[:500],
                }
            )
        elif "Wrote JSON: reports/phase3az_r12_weather/weather_activation_preview.json" in line:
            rows.append({"event": "WEATHER_CATALOG_PREVIEW_WRITTEN", "job_id": "weather_current_catalog_refresh", "line": line[:500]})
        elif "Wrote JSON: reports/phase3bb_r2/weather_funnel.json" in line:
            rows.append({"event": "WEATHER_FAST_LANE_WRITTEN", "job_id": "weather_fast_lane", "line": line[:500]})
    return rows[-200:]


def _runner_hook_before_fast_lane(runner_text: str) -> bool:
    hook_index = runner_text.find("weather_current_catalog_refresh")
    fast_lane_index = runner_text.find("weather_fast_lane")
    return hook_index >= 0 and fast_lane_index >= 0 and hook_index < fast_lane_index


def _job_started_before(rows: list[dict[str, Any]], *, before_job: str, after_job: str) -> bool:
    before_indexes = [
        index
        for index, row in enumerate(rows)
        if row.get("event") == "JOB_STARTED" and row.get("job_id") == before_job
    ]
    after_indexes = [
        index
        for index, row in enumerate(rows)
        if row.get("event") == "JOB_STARTED" and row.get("job_id") == after_job
    ]
    return bool(before_indexes and after_indexes and min(before_indexes) < max(after_indexes))


def _parse_writer_gate_skips(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "Writer active; skip writer-gated job" in line:
            rows.append({"kind": "WRITER_ACTIVE_SKIP", "line": line[:500]})
        elif "db-writer-monitor JSON parse failed; skip writer-gated job" in line:
            rows.append({"kind": "WRITER_MONITOR_PARSE_SKIP", "line": line[:500]})
        elif "db-writer-monitor failed; skip writer-gated job" in line:
            rows.append({"kind": "WRITER_MONITOR_FAILED_SKIP", "line": line[:500]})
    return rows[-100:]


def _parse_latest_reports(text: str) -> list[dict[str, Any]]:
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


def _extract_timer_last(text: str) -> str:
    for line in text.splitlines():
        if "kalshi-multicategory-refresh-scheduler.timer" in line and not line.startswith("NEXT"):
            parts = line.split()
            return " ".join(parts[4:7]) if len(parts) >= 7 else ""
    return ""


def _extract_tailscale_url(text: str) -> str:
    match = re.search(r"https://\S+\.ts\.net", text)
    return match.group(0) if match else ""


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _pids_from_process_output(text: str) -> list[int]:
    pids: list[int] = []
    for line in text.splitlines():
        if "pgrep -af" in line or "ssh " in line:
            continue
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        pid = _to_int(parts[0])
        if pid is not None:
            pids.append(pid)
    return pids


def _to_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


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


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R40 Cloud Scheduler Runtime Monitor")
    decision = payload["runtime_decision"]
    parsed = payload["parsed_runtime_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Overnight safe to leave running: `{decision['overnight_safe_to_leave_running']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Timer active state: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Timer next: `{parsed.get('scheduler_timer_next')}`",
            f"- Service active state: `{parsed.get('scheduler_service_active_state')}`",
            f"- Finished scheduler cycles in window: `{parsed.get('scheduler_cycle_finished_count')}`",
            f"- Weather catalog hook runs in window: `{decision.get('weather_catalog_hook_job_run_count')}`",
            f"- Weather fast-lane runs in window: `{decision.get('weather_fast_lane_job_run_count')}`",
            f"- Weather catalog before fast-lane: `{decision.get('weather_catalog_runtime_order_ok')}`",
            f"- Writer-gate skips in window: `{decision['writer_gate_skip_count']}`",
            f"- Scheduler tracebacks in window: `{decision['scheduler_traceback_count']}`",
            "",
            "## R5",
            "",
            f"- Running: `{parsed.get('r5_running')}`",
            f"- PID(s): `{parsed.get('r5_pids')}`",
            f"- Guard: `{parsed.get('r5_guard_status')}`",
            f"- Watch state: `{parsed.get('r5_watch_state')}`",
            f"- Positive EV rows: `{decision['positive_ev_rows']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            "",
            "## UI / Private Access",
            "",
            f"- UI service: `{parsed.get('ui_service_active_state')}`",
            f"- Local UI HTTP: `{parsed.get('ui_local_http')}`",
            f"- Tailscale URL: `{parsed.get('tailscale_private_url')}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler starts/stops performed by this monitor: `0`",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R40 Runtime Detail")
    decision = payload["runtime_decision"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Runtime passed: `{decision['runtime_passed']}`",
            f"- Primary reason: {decision['primary_reason']}",
            "",
            "## Checks",
            "",
            "| Check | Passed | Detail |",
            "|---|---:|---|",
        ]
    )
    for row in payload["runtime_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Latest Reports", "", "| Path | Status | Mtime | Size |", "|---|---|---:|---:|"])
    for row in payload["latest_reports"]:
        lines.append(
            f"| `{row['path']}` | `{row['status']}` | `{row['mtime_epoch']}` | `{row['size_bytes']}` |"
        )
    lines.extend(["", "## Scheduler Jobs", "", "| Event | Job | Count | Line |", "|---|---|---:|---|"])
    for row in payload["scheduler_job_runs"][-40:]:
        lines.append(
            f"| `{row.get('event')}` | `{row.get('job_id', '')}` | `{row.get('count', '')}` | {row.get('line', '')} |"
        )
    lines.extend(["", "## Writer Gate Skips", ""])
    if payload["writer_gate_skips"]:
        for row in payload["writer_gate_skips"][-20:]:
            lines.append(f"- `{row['kind']}`: {row['line']}")
    else:
        lines.append("- None observed in the journal window.")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R40 Next Actions")
    decision = payload["runtime_decision"]
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
            "- Do not disable the scheduler timer unless an approved stop handoff says to.",
        ]
    )
    if decision["overnight_safe_to_leave_running"]:
        lines.extend(
            [
                "",
                "## Overnight Note",
                "",
                "- It is safe to leave the scheduler and R5 watcher running in paper/read-only mode.",
                "- The bot will keep refreshing reports/status, but it will not create paper or live trades.",
            ]
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R40 next safe operator command.",
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


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
