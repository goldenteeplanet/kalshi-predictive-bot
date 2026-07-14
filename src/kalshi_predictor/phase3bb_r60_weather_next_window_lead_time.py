from __future__ import annotations

import json
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
    ProbeRunner,
    RemoteProbeResult,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import _mark_executable
from kalshi_predictor.phase3bb_r53_weather_current_window_cadence import (
    write_phase3bb_r53_weather_current_window_cadence_report,
)
from kalshi_predictor.phase3bb_r54_weather_missing_link_apply_deferral import (
    _float_or_none,
    _int_or_zero,
    _r53_compact,
    _write_probe_csv,
    _write_rows_csv,
)
from kalshi_predictor.phase3bb_r59_weather_catalog_refresh_r57_retry import (
    _probe_payloads_to_results,
    write_phase3bb_r59_weather_catalog_refresh_r57_retry_report,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R60_VERSION = "phase3bb_r60_weather_next_window_lead_time_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r60")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R53_OUTPUT_DIR = Path("reports/phase3bb_r53")
DEFAULT_R57_OUTPUT_DIR = Path("reports/phase3bb_r57")
DEFAULT_R59_OUTPUT_DIR = Path("reports/phase3bb_r59")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_REFRESH_TIMEOUT_SECONDS = 240
DEFAULT_R57_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Phase3BBR60WeatherNextWindowLeadTimeArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    lead_time_checks_csv_path: Path
    r53_pre_summary_csv_path: Path
    r59_summary_csv_path: Path
    probe_csv_path: Path
    scheduler_hook_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r60_weather_next_window_lead_time_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    r57_output_dir: Path = DEFAULT_R57_OUTPUT_DIR,
    r59_output_dir: Path = DEFAULT_R59_OUTPUT_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    expected_writer_pid: int | None = None,
    max_wait_seconds: int = 120,
    poll_interval_seconds: int = 10,
    min_minutes_before_target: int = 20,
    max_minutes_before_target: int = 90,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    catalog_limit: int = 100,
    catalog_max_pages: int = 3,
    parse_limit: int = 1500,
    series_ticker: str = "KXTEMPNYCH",
    max_records: int = 25,
    r12_limit: int = 2000,
    forecast_limit: int = 1,
    per_ticker_timeout_seconds: int = 25,
    refresh_timeout_seconds: int = DEFAULT_REFRESH_TIMEOUT_SECONDS,
    r57_timeout_seconds: int = DEFAULT_R57_TIMEOUT_SECONDS,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR60WeatherNextWindowLeadTimeArtifacts:
    payload = build_phase3bb_r60_weather_next_window_lead_time(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        r53_output_dir=r53_output_dir,
        r57_output_dir=r57_output_dir,
        r59_output_dir=r59_output_dir,
        settings=settings,
        command_args=command_args,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        expected_writer_pid=expected_writer_pid,
        max_wait_seconds=max_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        min_minutes_before_target=min_minutes_before_target,
        max_minutes_before_target=max_minutes_before_target,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        catalog_limit=catalog_limit,
        catalog_max_pages=catalog_max_pages,
        parse_limit=parse_limit,
        series_ticker=series_ticker,
        max_records=max_records,
        r12_limit=r12_limit,
        forecast_limit=forecast_limit,
        per_ticker_timeout_seconds=per_ticker_timeout_seconds,
        refresh_timeout_seconds=refresh_timeout_seconds,
        r57_timeout_seconds=r57_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_next_window_lead_time.md"
    json_path = output_dir / "weather_next_window_lead_time.json"
    lead_time_checks_csv_path = output_dir / "lead_time_checks.csv"
    r53_pre_summary_csv_path = output_dir / "r53_pre_summary.csv"
    r59_summary_csv_path = output_dir / "r59_summary.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    scheduler_hook_path = output_dir / "scheduler_hook_block.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(lead_time_checks_csv_path, payload["lead_time_checks"])
    _write_rows_csv(r53_pre_summary_csv_path, [payload.get("r53_pre_payload", {}).get("summary") or {}])
    _write_rows_csv(r59_summary_csv_path, [payload.get("r59_payload", {}).get("decision") or {}])
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    scheduler_hook_path.write_text(payload["scheduler_hook_block"], encoding="utf-8")
    _mark_executable(scheduler_hook_path)
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            lead_time_checks_csv_path,
            r53_pre_summary_csv_path,
            r59_summary_csv_path,
            probe_csv_path,
            scheduler_hook_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR60WeatherNextWindowLeadTimeArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        lead_time_checks_csv_path=lead_time_checks_csv_path,
        r53_pre_summary_csv_path=r53_pre_summary_csv_path,
        r59_summary_csv_path=r59_summary_csv_path,
        probe_csv_path=probe_csv_path,
        scheduler_hook_path=scheduler_hook_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r60_weather_next_window_lead_time(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    r57_output_dir: Path = DEFAULT_R57_OUTPUT_DIR,
    r59_output_dir: Path = DEFAULT_R59_OUTPUT_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    expected_writer_pid: int | None = None,
    max_wait_seconds: int = 120,
    poll_interval_seconds: int = 10,
    min_minutes_before_target: int = 20,
    max_minutes_before_target: int = 90,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    catalog_limit: int = 100,
    catalog_max_pages: int = 3,
    parse_limit: int = 1500,
    series_ticker: str = "KXTEMPNYCH",
    max_records: int = 25,
    r12_limit: int = 2000,
    forecast_limit: int = 1,
    per_ticker_timeout_seconds: int = 25,
    refresh_timeout_seconds: int = DEFAULT_REFRESH_TIMEOUT_SECONDS,
    r57_timeout_seconds: int = DEFAULT_R57_TIMEOUT_SECONDS,
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
        "command": "kalshi-bot phase3bb-r60-weather-next-window-lead-time-scheduler-repair",
        "argv": command_args or [],
    }
    runner = probe_runner or _run_ssh_probe

    r53_artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
        session,
        output_dir=r53_output_dir,
        reports_dir=reports_dir,
        settings=resolved,
        command_args=[
            "phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair",
            "--r60-pre-lead-time-check",
        ],
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        series_ticker=series_ticker,
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        min_minutes_before_target=min_minutes_before_target,
        limit=500,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=runner,
    )
    r53_pre_payload = _read_json(r53_artifacts.json_path)
    probe_results: list[RemoteProbeResult] = _probe_payloads_to_results(
        r53_pre_payload.get("remote_probe_results") or []
    )

    lead_gate = _lead_time_gate(
        r53_pre_payload,
        min_minutes_before_target=min_minutes_before_target,
        max_minutes_before_target=max_minutes_before_target,
    )
    r59_payload: dict[str, Any] = {}
    if lead_gate["allowed"]:
        r59_artifacts = write_phase3bb_r59_weather_catalog_refresh_r57_retry_report(
            session,
            output_dir=r59_output_dir,
            reports_dir=reports_dir,
            r53_output_dir=r53_output_dir,
            r57_output_dir=r57_output_dir,
            settings=resolved,
            command_args=["phase3bb-r59-weather-catalog-refresh-r57-retry", "--r60-lead-time-trigger"],
            ssh_target=ssh_target,
            identity_file=identity_file,
            app_path=app_path,
            env_path=env_path,
            db_path=db_path,
            expected_writer_pid=expected_writer_pid,
            max_wait_seconds=max_wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
            min_minutes_before_target=min_minutes_before_target,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            catalog_limit=catalog_limit,
            catalog_max_pages=catalog_max_pages,
            parse_limit=parse_limit,
            series_ticker=series_ticker,
            max_records=max_records,
            r12_limit=r12_limit,
            forecast_limit=forecast_limit,
            per_ticker_timeout_seconds=per_ticker_timeout_seconds,
            refresh_timeout_seconds=refresh_timeout_seconds,
            r57_timeout_seconds=r57_timeout_seconds,
            per_probe_timeout_seconds=per_probe_timeout_seconds,
            probe_runner=runner,
        )
        r59_payload = _read_json(r59_artifacts.json_path)
        probe_results.extend(_probe_payloads_to_results(r59_payload.get("remote_probe_results") or []))

    decision = _decision(lead_gate=lead_gate, r53_pre_payload=r53_pre_payload, r59_payload=r59_payload)
    scheduler_hook = _scheduler_hook_block(
        min_minutes_before_target=min_minutes_before_target,
        max_minutes_before_target=max_minutes_before_target,
        max_wait_seconds=max_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        refresh_timeout_seconds=refresh_timeout_seconds,
        r57_timeout_seconds=r57_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        series_ticker=series_ticker,
    )
    r59_safety = r59_payload.get("safety_flags") if isinstance(r59_payload.get("safety_flags"), dict) else {}
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": not bool(r59_payload),
        "weather_next_window_lead_time_scheduler_repair": True,
        "ssh_read_only_commands_executed": len(r53_pre_payload.get("remote_probe_results") or [])
        + _int_or_zero(r59_safety.get("ssh_read_only_commands_executed")),
        "ssh_write_capable_commands_executed": _int_or_zero(r59_safety.get("ssh_write_capable_commands_executed")),
        "runs_catalog_refresh": bool(r59_safety.get("runs_catalog_refresh")),
        "runs_market_legs_parse": bool(r59_safety.get("runs_market_legs_parse")),
        "runs_r53_after_refresh": bool(r59_safety.get("runs_r53_after_refresh")),
        "runs_r57_retry": bool(r59_safety.get("runs_r57_retry")),
        "installs_scheduler_hook": False,
        "starts_or_stops_services": False,
        "starts_or_stops_r5": False,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "thresholds_lowered": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R60-WEATHER-NEXT-WINDOW-LEAD-TIME-SCHEDULER-REPAIR",
        "phase_version": PHASE3BB_R60_VERSION,
        "mode": "PAPER_ONLY_WEATHER_NEXT_WINDOW_LEAD_TIME_REPAIR",
        "reports_dir": str(reports_dir),
        "parameters": {
            "expected_writer_pid": expected_writer_pid,
            "max_wait_seconds": max_wait_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "min_minutes_before_target": min_minutes_before_target,
            "max_minutes_before_target": max_minutes_before_target,
            "fresh_window_hours": fresh_window_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "catalog_limit": catalog_limit,
            "catalog_max_pages": catalog_max_pages,
            "parse_limit": parse_limit,
            "series_ticker": series_ticker,
            "forecast_limit": forecast_limit,
        },
        "r53_pre_payload": _r53_compact(r53_pre_payload),
        "lead_time_gate": lead_gate,
        "lead_time_checks": _lead_time_checks(lead_gate),
        "r59_payload": _r59_compact(r59_payload),
        "remote_probe_results": [_result_payload(result) for result in probe_results],
        "scheduler_hook_block": scheduler_hook,
        "decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _lead_time_gate(
    r53_payload: dict[str, Any],
    *,
    min_minutes_before_target: int,
    max_minutes_before_target: int,
) -> dict[str, Any]:
    decision = r53_payload.get("decision") if isinstance(r53_payload.get("decision"), dict) else {}
    summary = r53_payload.get("summary") if isinstance(r53_payload.get("summary"), dict) else {}
    status = decision.get("status")
    target = summary.get("selected_target_time")
    minutes = _float_or_none(summary.get("selected_minutes_until_target"))
    if status == "WEATHER_CURRENT_WINDOW_COMMAND_REGISTRY_INCOMPLETE":
        return _gate(False, "COMMAND_REGISTRY_MISSING", target, minutes, status)
    if status == "WEATHER_CURRENT_WINDOW_STATE_UNREADABLE":
        return _gate(False, "REMOTE_STATE_UNREADABLE", target, minutes, status)
    if not target:
        return _gate(True, "NO_SELECTED_WINDOW_REFRESH_CATALOG_FOR_DISCOVERY", target, minutes, status)
    if minutes is None:
        return _gate(False, "SELECTED_TARGET_MINUTES_UNKNOWN", target, minutes, status)
    if minutes < min_minutes_before_target:
        return _gate(False, "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY", target, minutes, status)
    if max_minutes_before_target > 0 and minutes > max_minutes_before_target:
        return _gate(False, "TARGET_WINDOW_TOO_EARLY_FOR_LEAD_TIME_BAND", target, minutes, status)
    return _gate(True, "TARGET_WINDOW_INSIDE_LEAD_TIME_BAND", target, minutes, status)


def _gate(
    allowed: bool,
    reason: str,
    selected_target_time: Any,
    selected_minutes_until_target: float | None,
    r53_status: Any,
) -> dict[str, Any]:
    return {
        "allowed": allowed,
        "reason": reason,
        "selected_target_time": selected_target_time,
        "selected_minutes_until_target": selected_minutes_until_target,
        "r53_status": r53_status,
    }


def _decision(
    *,
    lead_gate: dict[str, Any],
    r53_pre_payload: dict[str, Any],
    r59_payload: dict[str, Any],
) -> dict[str, Any]:
    r53_decision = r53_pre_payload.get("decision") if isinstance(r53_pre_payload.get("decision"), dict) else {}
    r59_decision = r59_payload.get("decision") if isinstance(r59_payload.get("decision"), dict) else {}
    if not lead_gate.get("allowed"):
        reason = str(lead_gate.get("reason") or "LEAD_TIME_GATE_CLOSED")
        if reason == "COMMAND_REGISTRY_MISSING":
            command = "kalshi-bot phase3bb-r12-cloud-bootstrap-verification --output-dir reports/phase3bb_r12 --reports-dir reports"
            next_step = "Phase 3BB-R12 - Cloud Bootstrap Verification"
        else:
            command = (
                "kalshi-bot phase3bb-r60-weather-next-window-lead-time-scheduler-repair "
                "--output-dir reports/phase3bb_r60 --reports-dir reports"
            )
            next_step = "Phase 3BB-R60 - Retry on next scheduler cadence"
        return {
            "status": "LEAD_TIME_GATE_CLOSED",
            "first_hard_blocker": reason,
            "primary_reason": f"R60 skipped refresh/R57 because the lead-time gate is closed: {reason}.",
            "r53_status": r53_decision.get("status"),
            "selected_target_time": lead_gate.get("selected_target_time"),
            "selected_minutes_until_target": lead_gate.get("selected_minutes_until_target"),
            "r59_ran": False,
            "operator_next_command": command,
            "next_codex_step": next_step,
            "paper_trades_allowed": False,
            "live_demo_orders_allowed": False,
        }
    if not r59_payload:
        return {
            "status": "R59_NOT_CONFIRMED",
            "first_hard_blocker": "R59_PAYLOAD_MISSING",
            "primary_reason": "Lead-time gate opened but R59 payload was not captured.",
            "r53_status": r53_decision.get("status"),
            "selected_target_time": lead_gate.get("selected_target_time"),
            "selected_minutes_until_target": lead_gate.get("selected_minutes_until_target"),
            "r59_ran": False,
            "operator_next_command": (
                "kalshi-bot phase3bb-r59-weather-catalog-refresh-r57-retry "
                "--output-dir reports/phase3bb_r59 --reports-dir reports"
            ),
            "next_codex_step": "Phase 3BB-R59 - Retry lead-time refresh",
            "paper_trades_allowed": False,
            "live_demo_orders_allowed": False,
        }
    status = f"R59_{r59_decision.get('status') or 'COMPLETED'}"
    return {
        "status": status,
        "first_hard_blocker": r59_decision.get("first_hard_blocker") or "R59_COMPLETED",
        "primary_reason": r59_decision.get("primary_reason") or "R59 completed from the R60 lead-time trigger.",
        "r53_status": r53_decision.get("status"),
        "selected_target_time": lead_gate.get("selected_target_time"),
        "selected_minutes_until_target": lead_gate.get("selected_minutes_until_target"),
        "r59_status": r59_decision.get("status"),
        "r59_ran": True,
        "operator_next_command": r59_decision.get("operator_next_command")
        or "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports",
        "next_codex_step": r59_decision.get("next_codex_step") or "Follow R59 next action",
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _lead_time_checks(lead_gate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "check": "lead_time_gate_open",
            "passed": bool(lead_gate.get("allowed")),
            "detail": str(lead_gate.get("reason") or ""),
        },
        {
            "check": "selected_target_not_too_close",
            "passed": lead_gate.get("reason") != "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY",
            "detail": f"minutes_until_target={lead_gate.get('selected_minutes_until_target')}",
        },
        {
            "check": "selected_target_not_too_early",
            "passed": lead_gate.get("reason") != "TARGET_WINDOW_TOO_EARLY_FOR_LEAD_TIME_BAND",
            "detail": f"minutes_until_target={lead_gate.get('selected_minutes_until_target')}",
        },
    ]


def _scheduler_hook_block(
    *,
    min_minutes_before_target: int,
    max_minutes_before_target: int,
    max_wait_seconds: int,
    poll_interval_seconds: int,
    refresh_timeout_seconds: int,
    r57_timeout_seconds: int,
    per_probe_timeout_seconds: int,
    series_ticker: str,
) -> str:
    return "\n".join(
        [
            "# cadence_minutes=10 category=weather-lead-time",
            (
                "run_job weather_next_window_lead_time true "
                ".venv/bin/kalshi-bot phase3bb-r60-weather-next-window-lead-time-scheduler-repair "
                "--output-dir reports/phase3bb_r60 --reports-dir reports "
                f"--series-ticker {series_ticker} "
                f"--max-wait-seconds {int(max_wait_seconds)} "
                f"--poll-interval-seconds {int(poll_interval_seconds)} "
                f"--min-minutes-before-target {int(min_minutes_before_target)} "
                f"--max-minutes-before-target {int(max_minutes_before_target)} "
                f"--refresh-timeout-seconds {int(refresh_timeout_seconds)} "
                f"--r57-timeout-seconds {int(r57_timeout_seconds)} "
                f"--per-probe-timeout-seconds {int(per_probe_timeout_seconds)}"
            ),
            "",
        ]
    )


def _r59_compact(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        "generated_at": payload.get("generated_at"),
        "decision": payload.get("decision"),
        "r57_gate": payload.get("r57_gate"),
        "r53_payload": payload.get("r53_payload"),
        "r57_payload": payload.get("r57_payload"),
        "catalog_refresh_probe": payload.get("catalog_refresh_probe"),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    lead_gate = payload["lead_time_gate"]
    lines = [
        *_metadata_lines(payload, "# Phase 3BB-R60 Weather Next-Window Lead-Time Scheduler Repair"),
        "",
        "## Result",
        "",
        f"- Status: `{decision['status']}`",
        f"- First hard blocker: `{decision['first_hard_blocker']}`",
        f"- Selected target: `{lead_gate.get('selected_target_time')}`",
        f"- Minutes until target: `{lead_gate.get('selected_minutes_until_target')}`",
        f"- Lead-time gate: `{lead_gate.get('reason')}`",
        f"- R59 ran: `{decision.get('r59_ran')}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "",
        "## Why",
        "",
        str(decision["primary_reason"]),
        "",
        "## Next",
        "",
        "```bash",
        str(decision["operator_next_command"]),
        "```",
        "",
        "R60 does not install scheduler files, stop/start R5, create paper trades, submit live/demo orders, or lower thresholds.",
    ]
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    lead_gate = payload["lead_time_gate"]
    r53_summary = payload.get("r53_pre_payload", {}).get("summary") or {}
    r59_decision = payload.get("r59_payload", {}).get("decision") or {}
    lines = [
        "# Weather Next-Window Lead-Time Scheduler Repair",
        "",
        "## Lead-Time Gate",
        "",
        f"- Allowed: `{lead_gate.get('allowed')}`",
        f"- Reason: `{lead_gate.get('reason')}`",
        f"- Selected target: `{lead_gate.get('selected_target_time')}`",
        f"- Minutes until target: `{lead_gate.get('selected_minutes_until_target')}`",
        f"- Pre-R53 status: `{r53_summary.get('selected_target_time')}` / `{decision.get('r53_status')}`",
        "",
        "## R59 Result",
        "",
        f"- R59 ran: `{decision.get('r59_ran')}`",
        f"- R59 status: `{r59_decision.get('status')}`",
        f"- R59 blocker: `{r59_decision.get('first_hard_blocker')}`",
        "",
        "## Scheduler Hook Draft",
        "",
        "```bash",
        payload["scheduler_hook_block"].rstrip(),
        "```",
        "",
        "## Guardrails",
        "",
        "- No paper trades.",
        "- No live/demo exchange orders.",
        "- No threshold lowering.",
        "- No service install/start/stop from this command.",
    ]
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = str(payload["decision"]["operator_next_command"]).strip()
    if "\n" in command:
        return "#!/usr/bin/env bash\nset -euo pipefail\n" + command + "\n"
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + command + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    return "\n".join(
        [
            "# Next Actions",
            "",
            f"Status: `{decision['status']}`",
            f"First hard blocker: `{decision['first_hard_blocker']}`",
            "",
            "```bash",
            str(decision["operator_next_command"]),
            "```",
            "",
            "Scheduler hook draft is available at `scheduler_hook_block.sh`; review/install through the scheduler handoff flow, not by ad hoc service edits.",
            "",
            "Do not create paper trades or live/demo orders from this phase.",
        ]
    ) + "\n"
