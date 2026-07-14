from __future__ import annotations

import json
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
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _mark_executable,
    _target_payload,
)
from kalshi_predictor.phase3bb_r53_weather_current_window_cadence import (
    write_phase3bb_r53_weather_current_window_cadence_report,
)
from kalshi_predictor.phase3bb_r54_weather_missing_link_apply_deferral import (
    _float_or_none,
    _int_or_zero,
    _r53_compact,
    _wait_for_writer_clear,
    _write_probe_csv,
    _write_rows_csv,
)
from kalshi_predictor.phase3bb_r57_weather_selected_window_pipeline import (
    write_phase3bb_r57_weather_selected_window_pipeline_report,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R59_VERSION = "phase3bb_r59_weather_catalog_refresh_r57_retry_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r59")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R53_OUTPUT_DIR = Path("reports/phase3bb_r53")
DEFAULT_R57_OUTPUT_DIR = Path("reports/phase3bb_r57")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_REFRESH_TIMEOUT_SECONDS = 240
DEFAULT_R57_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class Phase3BBR59WeatherCatalogRefreshR57RetryArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    wait_checks_csv_path: Path
    refresh_steps_csv_path: Path
    r53_summary_csv_path: Path
    r57_summary_csv_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r59_weather_catalog_refresh_r57_retry_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    r57_output_dir: Path = DEFAULT_R57_OUTPUT_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    expected_writer_pid: int | None = None,
    max_wait_seconds: int = 420,
    poll_interval_seconds: int = 15,
    min_minutes_before_target: int = 10,
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
) -> Phase3BBR59WeatherCatalogRefreshR57RetryArtifacts:
    payload = build_phase3bb_r59_weather_catalog_refresh_r57_retry(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        r53_output_dir=r53_output_dir,
        r57_output_dir=r57_output_dir,
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
    markdown_path = output_dir / "weather_catalog_refresh_r57_retry.md"
    json_path = output_dir / "weather_catalog_refresh_r57_retry.json"
    wait_checks_csv_path = output_dir / "writer_wait_checks.csv"
    refresh_steps_csv_path = output_dir / "refresh_steps.csv"
    r53_summary_csv_path = output_dir / "r53_summary.csv"
    r57_summary_csv_path = output_dir / "r57_summary.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(wait_checks_csv_path, payload["writer_wait_checks"])
    _write_rows_csv(refresh_steps_csv_path, payload["refresh_steps"])
    _write_rows_csv(r53_summary_csv_path, [payload.get("r53_payload", {}).get("summary") or {}])
    _write_rows_csv(r57_summary_csv_path, [payload.get("r57_payload", {}).get("decision") or {}])
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            wait_checks_csv_path,
            refresh_steps_csv_path,
            r53_summary_csv_path,
            r57_summary_csv_path,
            probe_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR59WeatherCatalogRefreshR57RetryArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        wait_checks_csv_path=wait_checks_csv_path,
        refresh_steps_csv_path=refresh_steps_csv_path,
        r53_summary_csv_path=r53_summary_csv_path,
        r57_summary_csv_path=r57_summary_csv_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r59_weather_catalog_refresh_r57_retry(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    r57_output_dir: Path = DEFAULT_R57_OUTPUT_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    expected_writer_pid: int | None = None,
    max_wait_seconds: int = 420,
    poll_interval_seconds: int = 15,
    min_minutes_before_target: int = 10,
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
        "command": "kalshi-bot phase3bb-r59-weather-catalog-refresh-r57-retry",
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
    runner = probe_runner or _run_ssh_probe
    writer_wait = _wait_for_writer_clear(
        target,
        runner=runner,
        expected_writer_pid=expected_writer_pid,
        max_wait_seconds=max_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=per_probe_timeout_seconds,
    )
    probe_results: list[RemoteProbeResult] = list(writer_wait["probe_results"])
    refresh_result: RemoteProbeResult | None = None
    r53_payload: dict[str, Any] = {}
    r57_payload: dict[str, Any] = {}

    if writer_wait["cleared"] and not writer_wait.get("unexpected_writer"):
        refresh_result = runner(
            _catalog_refresh_probe(
                target,
                series_ticker=series_ticker,
                catalog_limit=catalog_limit,
                catalog_max_pages=catalog_max_pages,
                parse_limit=parse_limit,
                timeout_seconds=refresh_timeout_seconds,
            ),
            target,
        )
        probe_results.append(refresh_result)

    if refresh_result is not None and refresh_result.ok:
        r53_artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
            session,
            output_dir=r53_output_dir,
            reports_dir=reports_dir,
            settings=resolved,
            command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--r59-post-catalog-refresh"],
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
        r53_payload = _read_json(r53_artifacts.json_path)
        probe_results.extend(_probe_payloads_to_results(r53_payload.get("remote_probe_results") or []))

    r57_gate = _r57_gate(r53_payload, writer_wait=writer_wait, refresh_result=refresh_result, min_minutes_before_target=min_minutes_before_target)
    if r57_gate["allowed"]:
        r57_artifacts = write_phase3bb_r57_weather_selected_window_pipeline_report(
            session,
            output_dir=r57_output_dir,
            reports_dir=reports_dir,
            r53_output_dir=r53_output_dir,
            settings=resolved,
            command_args=["phase3bb-r57-weather-selected-window-pipeline-speed-repair", "--r59-retry"],
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
            max_records=max_records,
            limit=r12_limit,
            forecast_limit=forecast_limit,
            per_ticker_timeout_seconds=per_ticker_timeout_seconds,
            pipeline_timeout_seconds=r57_timeout_seconds,
            per_probe_timeout_seconds=per_probe_timeout_seconds,
            probe_runner=runner,
        )
        r57_payload = _read_json(r57_artifacts.json_path)
        probe_results.extend(_probe_payloads_to_results(r57_payload.get("remote_probe_results") or []))

    decision = _decision(
        writer_wait=writer_wait,
        refresh_result=refresh_result,
        r57_gate=r57_gate,
        r53_payload=r53_payload,
        r57_payload=r57_payload,
    )
    r57_safety = r57_payload.get("safety_flags") if isinstance(r57_payload.get("safety_flags"), dict) else {}
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "weather_catalog_refresh_r57_retry": True,
        "ssh_read_only_commands_executed": writer_wait["read_only_probe_count"]
        + len((r53_payload.get("remote_probe_results") or []) if r53_payload else [])
        + _int_or_zero(r57_safety.get("ssh_read_only_commands_executed")),
        "ssh_write_capable_commands_executed": (1 if refresh_result is not None else 0)
        + _int_or_zero(r57_safety.get("ssh_write_capable_commands_executed")),
        "runs_catalog_refresh": refresh_result is not None,
        "runs_market_legs_parse": refresh_result is not None,
        "runs_r53_after_refresh": bool(r53_payload),
        "runs_r57_retry": bool(r57_payload),
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "starts_or_stops_services": False,
        "starts_or_stops_r5": False,
        "thresholds_lowered": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R59-WEATHER-CURRENT-CATALOG-REFRESH-AFTER-WRITER-CLEARS-R57-RETRY",
        "phase_version": PHASE3BB_R59_VERSION,
        "mode": "PAPER_ONLY_WRITER_GATED_WEATHER_CATALOG_REFRESH_R57_RETRY",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "parameters": {
            "expected_writer_pid": expected_writer_pid,
            "max_wait_seconds": max_wait_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "min_minutes_before_target": min_minutes_before_target,
            "fresh_window_hours": fresh_window_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "catalog_limit": catalog_limit,
            "catalog_max_pages": catalog_max_pages,
            "parse_limit": parse_limit,
            "series_ticker": series_ticker,
            "forecast_limit": forecast_limit,
        },
        "writer_wait": {key: value for key, value in writer_wait.items() if key != "probe_results"},
        "writer_wait_checks": writer_wait["checks"],
        "refresh_steps": _refresh_steps(refresh_result),
        "catalog_refresh_probe": _result_payload(refresh_result) if refresh_result is not None else {},
        "r53_payload": _r53_compact(r53_payload),
        "r57_gate": r57_gate,
        "r57_payload": _r57_compact(r57_payload),
        "remote_probe_results": [_result_payload(result) for result in probe_results],
        "decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _catalog_refresh_probe(
    target: CloudBootstrapTarget,
    *,
    series_ticker: str,
    catalog_limit: int,
    catalog_max_pages: int,
    parse_limit: int,
    timeout_seconds: int,
) -> RemoteProbe:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    command = (
        f"cd {app} && set -a && . {env} && set +a && "
        f"timeout {int(timeout_seconds)} .venv/bin/kalshi-bot sync-markets "
        f"--status open --limit {int(catalog_limit)} --max-pages {int(catalog_max_pages)} "
        f"--series-ticker {shlex.quote(series_ticker)} && "
        f"timeout {int(timeout_seconds)} .venv/bin/kalshi-bot market-legs-parse "
        f"--refresh --limit {int(parse_limit)}"
    )
    return RemoteProbe("weather_catalog_refresh_parse", command, timeout_seconds * 2 + 30)


def _r57_gate(
    r53_payload: dict[str, Any],
    *,
    writer_wait: dict[str, Any],
    refresh_result: RemoteProbeResult | None,
    min_minutes_before_target: int,
) -> dict[str, Any]:
    if not writer_wait.get("cleared"):
        return {"allowed": False, "reason": "WRITER_DID_NOT_CLEAR"}
    if writer_wait.get("unexpected_writer"):
        return {"allowed": False, "reason": "UNEXPECTED_WRITER_PID_SEEN"}
    if refresh_result is None:
        return {"allowed": False, "reason": "CATALOG_REFRESH_NOT_RUN"}
    if not refresh_result.ok:
        return {"allowed": False, "reason": "CATALOG_REFRESH_FAILED"}
    if not r53_payload:
        return {"allowed": False, "reason": "R53_NOT_RUN"}
    summary = r53_payload.get("summary") if isinstance(r53_payload.get("summary"), dict) else {}
    if not summary.get("selected_target_time"):
        return {"allowed": False, "reason": "NO_FUTURE_SELECTED_WEATHER_WINDOW"}
    minutes = _float_or_none(summary.get("selected_minutes_until_target"))
    if minutes is None or minutes < min_minutes_before_target:
        return {"allowed": False, "reason": "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"}
    if _int_or_zero(summary.get("selected_window_market_rows")) <= 0:
        return {"allowed": False, "reason": "NO_SELECTED_WINDOW_MARKET_ROWS"}
    return {
        "allowed": True,
        "reason": "FUTURE_SELECTED_WEATHER_WINDOW_FOUND",
        "selected_target_time": summary.get("selected_target_time"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
        "selected_window_market_rows": summary.get("selected_window_market_rows"),
    }


def _decision(
    *,
    writer_wait: dict[str, Any],
    refresh_result: RemoteProbeResult | None,
    r57_gate: dict[str, Any],
    r53_payload: dict[str, Any],
    r57_payload: dict[str, Any],
) -> dict[str, Any]:
    r53_decision = r53_payload.get("decision") if isinstance(r53_payload.get("decision"), dict) else {}
    r53_summary = r53_payload.get("summary") if isinstance(r53_payload.get("summary"), dict) else {}
    r57_decision = r57_payload.get("decision") if isinstance(r57_payload.get("decision"), dict) else {}
    if not writer_wait.get("cleared"):
        status = "WAITING_FOR_WRITER_CLEAR"
        blocker = "ACTIVE_WRITER"
        reason = "Writer gate did not clear; no catalog refresh or R57 retry was run."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R59 - Retry after R5 writer clears"
    elif writer_wait.get("unexpected_writer"):
        status = "BLOCKED_BY_UNEXPECTED_WRITER"
        blocker = "UNEXPECTED_WRITER_PID"
        reason = "A writer other than the expected PID was seen; R59 did not run write-capable work."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Inspect active writer before weather refresh"
    elif refresh_result is None:
        status = "CATALOG_REFRESH_NOT_RUN"
        blocker = "REFRESH_SKIPPED"
        reason = "Writer cleared state was not sufficient to start the catalog refresh."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R59 - Retry refresh"
    elif not refresh_result.ok:
        status = "CATALOG_REFRESH_FAILED"
        blocker = "SYNC_OR_PARSE_FAILED"
        reason = "The targeted KXTEMPNYCH sync/parse command failed."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Inspect R59 catalog refresh stderr"
    elif not r57_gate.get("allowed"):
        status = "NO_R57_RETRY_GATE_CLOSED"
        blocker = r57_gate.get("reason") or "R57_GATE_CLOSED"
        reason = f"Patched R57 was not run because the post-refresh R53 gate is closed: {blocker}."
        command = (
            "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
            "--output-dir reports/phase3bb_r53 --reports-dir reports"
        )
        next_step = "Wait for next current weather window or rerun targeted catalog refresh"
    elif not r57_payload:
        status = "R57_RETRY_NOT_CONFIRMED"
        blocker = "R57_PAYLOAD_MISSING"
        reason = "R59 gate opened but no R57 payload was captured."
        command = (
            "kalshi-bot phase3bb-r57-weather-selected-window-pipeline-speed-repair "
            "--output-dir reports/phase3bb_r57 --reports-dir reports"
        )
        next_step = "Phase 3BB-R57 - Retry selected-window pipeline"
    else:
        status = f"R57_{r57_decision.get('status') or 'COMPLETED'}"
        blocker = r57_decision.get("first_hard_blocker") or "R57_COMPLETED"
        reason = r57_decision.get("primary_reason") or "Patched R57 completed after targeted catalog refresh."
        command = r57_decision.get("operator_next_command") or (
            "kalshi-bot phase3bb-r8-unified-paper-gate --output-dir reports/phase3bb_r8 --reports-dir reports"
        )
        next_step = r57_decision.get("next_codex_step") or "Follow R57 next action"
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "primary_reason": reason,
        "writer_cleared": bool(writer_wait.get("cleared")),
        "catalog_refresh_ok": bool(refresh_result and refresh_result.ok),
        "r53_status": r53_decision.get("status"),
        "r53_selected_target_time": r53_summary.get("selected_target_time"),
        "r53_selected_window_market_rows": r53_summary.get("selected_window_market_rows"),
        "r57_status": r57_decision.get("status"),
        "r57_first_hard_blocker": r57_decision.get("first_hard_blocker"),
        "operator_next_command": command,
        "next_codex_step": next_step,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _refresh_steps(result: RemoteProbeResult | None) -> list[dict[str, Any]]:
    if result is None:
        return []
    return [
        {
            "name": result.name,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": round(result.duration_seconds, 3),
        }
    ]


def _r57_compact(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        "generated_at": payload.get("generated_at"),
        "decision": payload.get("decision"),
        "pipeline_gate": payload.get("pipeline_gate"),
        "final_gate_summary": payload.get("final_gate_summary"),
        "r53_final_payload": payload.get("r53_final_payload"),
    }


def _probe_payloads_to_results(rows: list[dict[str, Any]]) -> list[Any]:
    class _PayloadResult:
        def __init__(self, row: dict[str, Any]) -> None:
            self.name = row.get("name") or ""
            self.command = row.get("command") or ""
            self.ok = bool(row.get("ok"))
            self.exit_code = int(row.get("exit_code") or 0)
            self.stdout = row.get("stdout_excerpt") or ""
            self.stderr = row.get("stderr_excerpt") or ""
            self.duration_seconds = float(row.get("duration_seconds") or 0)
            self.timed_out = bool(row.get("timed_out"))

    return [_PayloadResult(row) for row in rows]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    r53_summary = (payload.get("r53_payload") or {}).get("summary") or {}
    lines = _metadata_lines(payload, "# Phase 3BB-R59 Weather Catalog Refresh + R57 Retry")
    lines.extend(
        [
            f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
            f"- Order submission/cancel/replace: `{payload['order_submission_cancel_replace']}`",
            f"- Paper trade creation: `{payload['paper_trade_creation']}`",
            f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- First hard blocker: `{decision['first_hard_blocker']}`",
            f"- Writer cleared: `{decision['writer_cleared']}`",
            f"- Catalog refresh ok: `{decision['catalog_refresh_ok']}`",
            f"- R53 selected target: `{r53_summary.get('selected_target_time')}`",
            f"- R53 selected rows: `{r53_summary.get('selected_window_market_rows')}`",
            f"- R57 status: `{decision.get('r57_status')}`",
            "",
            "## Why",
            "",
            decision["primary_reason"],
            "",
            "## Next",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "R59 does not stop R5, start services, create paper trades, submit live/demo orders, or lower thresholds.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Weather Catalog Refresh + Patched R57 Retry",
        "",
        f"Status: `{payload['decision']['status']}`",
        f"First blocker: `{payload['decision']['first_hard_blocker']}`",
        "",
        "## Gate",
        "",
        "```json",
        json.dumps(payload.get("r57_gate") or {}, indent=2, sort_keys=True),
        "```",
        "",
        "## Guardrails",
        "",
        "- Waits for writer gate before sync/parse.",
        "- Runs targeted KXTEMPNYCH catalog refresh only.",
        "- Runs patched R57 only when R53 finds a future selected window.",
        "- No paper trades or live/demo orders.",
    ]
    return "\n".join(lines) + "\n"


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
            decision["operator_next_command"],
            "```",
            "",
            "Do not create paper trades or live/demo orders from this phase.",
        ]
    ) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + payload["decision"]["operator_next_command"] + "\n"
