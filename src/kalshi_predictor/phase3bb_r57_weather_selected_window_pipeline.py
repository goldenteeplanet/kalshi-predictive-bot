from __future__ import annotations

import csv
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
    _r12_apply_probe,
    _r53_compact,
    _wait_for_writer_clear,
    _write_probe_csv,
    _write_rows_csv,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R57_VERSION = "phase3bb_r57_weather_selected_window_pipeline_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r57")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R53_OUTPUT_DIR = Path("reports/phase3bb_r53")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_PIPELINE_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class Phase3BBR57WeatherSelectedWindowPipelineArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    wait_checks_csv_path: Path
    pipeline_steps_csv_path: Path
    selected_tickers_csv_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r57_weather_selected_window_pipeline_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    expected_writer_pid: int | None = None,
    max_wait_seconds: int = 300,
    poll_interval_seconds: int = 30,
    min_minutes_before_target: int = 10,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    max_records: int = 25,
    limit: int = 2000,
    forecast_limit: int = 1,
    per_ticker_timeout_seconds: int = 30,
    pipeline_timeout_seconds: int = DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR57WeatherSelectedWindowPipelineArtifacts:
    payload = build_phase3bb_r57_weather_selected_window_pipeline(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        r53_output_dir=r53_output_dir,
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
        max_records=max_records,
        limit=limit,
        forecast_limit=forecast_limit,
        per_ticker_timeout_seconds=per_ticker_timeout_seconds,
        pipeline_timeout_seconds=pipeline_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "selected_window_weather_pipeline.md"
    json_path = output_dir / "selected_window_weather_pipeline.json"
    wait_checks_csv_path = output_dir / "writer_wait_checks.csv"
    pipeline_steps_csv_path = output_dir / "pipeline_steps.csv"
    selected_tickers_csv_path = output_dir / "selected_window_tickers.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(wait_checks_csv_path, payload["writer_wait_checks"])
    _write_rows_csv(pipeline_steps_csv_path, payload["pipeline_steps"])
    _write_rows_csv(selected_tickers_csv_path, payload["selected_window_tickers"])
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
            pipeline_steps_csv_path,
            selected_tickers_csv_path,
            probe_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR57WeatherSelectedWindowPipelineArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        wait_checks_csv_path=wait_checks_csv_path,
        pipeline_steps_csv_path=pipeline_steps_csv_path,
        selected_tickers_csv_path=selected_tickers_csv_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r57_weather_selected_window_pipeline(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    expected_writer_pid: int | None = None,
    max_wait_seconds: int = 300,
    poll_interval_seconds: int = 30,
    min_minutes_before_target: int = 10,
    fresh_window_hours: int = 24,
    match_tolerance_hours: int = 3,
    max_records: int = 25,
    limit: int = 2000,
    forecast_limit: int = 1,
    per_ticker_timeout_seconds: int = 30,
    pipeline_timeout_seconds: int = DEFAULT_PIPELINE_TIMEOUT_SECONDS,
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
        "command": "kalshi-bot phase3bb-r57-weather-selected-window-pipeline-speed-repair",
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
    pipeline_results: list[RemoteProbeResult] = []
    r53_initial: dict[str, Any] = {}
    r53_after_apply: dict[str, Any] = {}
    r53_final: dict[str, Any] = {}
    r12_apply_payload: dict[str, Any] = {}
    apply_result: RemoteProbeResult | None = None

    if writer_wait["cleared"]:
        r53_initial = _run_r53(
            session,
            output_dir=r53_output_dir,
            reports_dir=reports_dir,
            settings=resolved,
            command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--r57-initial"],
            ssh_target=ssh_target,
            identity_file=identity_file,
            app_path=app_path,
            env_path=env_path,
            db_path=db_path,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            min_minutes_before_target=min_minutes_before_target,
            per_probe_timeout_seconds=per_probe_timeout_seconds,
            runner=runner,
        )
        probe_results.extend(_probe_payloads_to_results(r53_initial.get("remote_probe_results") or []))

    active_r53 = r53_initial
    link_gate = _link_apply_gate(
        active_r53,
        writer_wait=writer_wait,
        min_minutes_before_target=min_minutes_before_target,
    )
    if link_gate["allowed"]:
        apply_probe = _r12_apply_probe(
            target,
            limit=limit,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            max_records=max_records,
            timeout_seconds=pipeline_timeout_seconds,
        )
        apply_probe = RemoteProbe(
            apply_probe.name,
            apply_probe.command.replace("reports/phase3az_r12_weather_r54", "reports/phase3az_r12_weather_r57"),
            apply_probe.timeout_seconds,
        )
        apply_result = runner(apply_probe, target)
        pipeline_results.append(apply_result)
        probe_results.append(apply_result)
        r12_apply_payload = _json_from_probe(apply_result)
        if apply_result.ok:
            r53_after_apply = _run_r53(
                session,
                output_dir=r53_output_dir,
                reports_dir=reports_dir,
                settings=resolved,
                command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--r57-post-link-apply"],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                fresh_window_hours=fresh_window_hours,
                match_tolerance_hours=match_tolerance_hours,
                min_minutes_before_target=min_minutes_before_target,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
                runner=runner,
            )
            active_r53 = r53_after_apply
            probe_results.extend(_probe_payloads_to_results(r53_after_apply.get("remote_probe_results") or []))

    pipeline_gate = _pipeline_gate(
        active_r53,
        writer_wait=writer_wait,
        link_gate=link_gate,
        apply_result=apply_result,
        min_minutes_before_target=min_minutes_before_target,
    )
    final_gate_summary: dict[str, Any] = {}
    if pipeline_gate["allowed"]:
        for probe in _pipeline_probes(
            target,
            selected_target_time=active_r53.get("summary", {}).get("selected_target_time"),
            selected_tickers=_selected_ticker_values(active_r53),
            forecast_limit=forecast_limit,
            per_ticker_timeout_seconds=per_ticker_timeout_seconds,
            pipeline_timeout_seconds=pipeline_timeout_seconds,
        ):
            result = runner(probe, target)
            pipeline_results.append(result)
            probe_results.append(result)
            if not result.ok:
                break
        final_gate_summary = _json_from_named_probe(pipeline_results, "paper_gate_summary")
        r53_final = _run_r53(
            session,
            output_dir=r53_output_dir,
            reports_dir=reports_dir,
            settings=resolved,
            command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--r57-final"],
            ssh_target=ssh_target,
            identity_file=identity_file,
            app_path=app_path,
            env_path=env_path,
            db_path=db_path,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            min_minutes_before_target=min_minutes_before_target,
            per_probe_timeout_seconds=per_probe_timeout_seconds,
            runner=runner,
        )
        probe_results.extend(_probe_payloads_to_results(r53_final.get("remote_probe_results") or []))

    decision = _decision(
        writer_wait=writer_wait,
        link_gate=link_gate,
        pipeline_gate=pipeline_gate,
        pipeline_results=pipeline_results,
        active_r53=active_r53,
        r53_final=r53_final,
        r12_apply_payload=r12_apply_payload,
        final_gate_summary=final_gate_summary,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "weather_selected_window_pipeline_speed_repair": True,
        "ssh_read_only_commands_executed": writer_wait["read_only_probe_count"]
        + len((r53_initial.get("remote_probe_results") or []) if r53_initial else [])
        + len((r53_after_apply.get("remote_probe_results") or []) if r53_after_apply else [])
        + len((r53_final.get("remote_probe_results") or []) if r53_final else []),
        "ssh_write_capable_commands_executed": len(pipeline_results),
        "runs_missing_link_apply": apply_result is not None,
        "runs_weather_feature_refresh": _pipeline_probe_ok_or_seen(pipeline_results, "weather_feature_refresh"),
        "runs_weather_snapshot_capture": _pipeline_probe_ok_or_seen(pipeline_results, "weather_snapshot_capture"),
        "runs_weather_per_ticker_forecast": _pipeline_probe_ok_or_seen(pipeline_results, "weather_per_ticker_forecast"),
        "runs_weather_fast_lane": _pipeline_probe_ok_or_seen(pipeline_results, "weather_fast_lane_run"),
        "runs_unified_paper_gate": _pipeline_probe_ok_or_seen(pipeline_results, "unified_paper_gate_run"),
        "runs_ba_r5_truth": _pipeline_probe_ok_or_seen(pipeline_results, "ba_r5_truth_run"),
        "uses_broad_weather_forecast": False,
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
        "phase": "3BB-R57-WEATHER-SELECTED-WINDOW-PIPELINE-SPEED-REPAIR",
        "phase_version": PHASE3BB_R57_VERSION,
        "mode": "PAPER_ONLY_SELECTED_WINDOW_WEATHER_PIPELINE",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "parameters": {
            "expected_writer_pid": expected_writer_pid,
            "max_wait_seconds": max_wait_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "min_minutes_before_target": min_minutes_before_target,
            "fresh_window_hours": fresh_window_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "max_records": max_records,
            "limit": limit,
            "forecast_limit": forecast_limit,
            "per_ticker_timeout_seconds": per_ticker_timeout_seconds,
            "pipeline_timeout_seconds": pipeline_timeout_seconds,
        },
        "writer_wait": {key: value for key, value in writer_wait.items() if key != "probe_results"},
        "writer_wait_checks": writer_wait["checks"],
        "r53_initial_payload": _r53_compact(r53_initial),
        "r53_after_apply_payload": _r53_compact(r53_after_apply),
        "r53_final_payload": _r53_compact(r53_final),
        "r12_apply_gate": link_gate,
        "r12_apply_summary": r12_apply_payload.get("summary") if isinstance(r12_apply_payload, dict) else {},
        "pipeline_gate": pipeline_gate,
        "pipeline_steps": _pipeline_steps(pipeline_results),
        "selected_window_tickers": _selected_ticker_rows(r53_final or active_r53),
        "final_gate_summary": final_gate_summary,
        "remote_probe_results": [_result_payload(result) for result in probe_results],
        "decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _run_r53(
    session: Session,
    *,
    output_dir: Path,
    reports_dir: Path,
    settings: Settings,
    command_args: list[str],
    ssh_target: str | None,
    identity_file: str | None,
    app_path: str | None,
    env_path: str | None,
    db_path: str | None,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    min_minutes_before_target: int,
    per_probe_timeout_seconds: int,
    runner: ProbeRunner,
) -> dict[str, Any]:
    artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
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
        fresh_window_hours=fresh_window_hours,
        match_tolerance_hours=match_tolerance_hours,
        min_minutes_before_target=min_minutes_before_target,
        limit=500,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=runner,
    )
    return _read_json(artifacts.json_path)


def _link_apply_gate(
    r53_payload: dict[str, Any],
    *,
    writer_wait: dict[str, Any],
    min_minutes_before_target: int,
) -> dict[str, Any]:
    decision = (r53_payload.get("decision") if isinstance(r53_payload, dict) else {}) or {}
    summary = (r53_payload.get("summary") if isinstance(r53_payload, dict) else {}) or {}
    if not writer_wait.get("cleared"):
        return {"allowed": False, "reason": "WRITER_DID_NOT_CLEAR"}
    if writer_wait.get("unexpected_writer"):
        return {"allowed": False, "reason": "UNEXPECTED_WRITER_PID_SEEN"}
    if decision.get("status") != "WEATHER_CURRENT_WINDOW_LINK_APPLY_NEEDED":
        return {"allowed": False, "reason": f"R53_STATUS_{decision.get('status') or 'UNKNOWN'}"}
    minutes = _float_or_none(summary.get("selected_minutes_until_target"))
    if minutes is None or minutes < min_minutes_before_target:
        return {"allowed": False, "reason": "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"}
    if _int_or_zero(summary.get("selected_window_missing_link_rows")) <= 0:
        return {"allowed": False, "reason": "NO_MISSING_LINK_ROWS"}
    return {
        "allowed": True,
        "reason": "R57_SAFE_MISSING_LINK_APPLY",
        "selected_target_time": summary.get("selected_target_time"),
        "selected_window_missing_link_rows": summary.get("selected_window_missing_link_rows"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
    }


def _pipeline_gate(
    r53_payload: dict[str, Any],
    *,
    writer_wait: dict[str, Any],
    link_gate: dict[str, Any],
    apply_result: RemoteProbeResult | None,
    min_minutes_before_target: int,
) -> dict[str, Any]:
    decision = (r53_payload.get("decision") if isinstance(r53_payload, dict) else {}) or {}
    summary = (r53_payload.get("summary") if isinstance(r53_payload, dict) else {}) or {}
    if not writer_wait.get("cleared"):
        return {"allowed": False, "reason": "WRITER_DID_NOT_CLEAR"}
    if writer_wait.get("unexpected_writer"):
        return {"allowed": False, "reason": "UNEXPECTED_WRITER_PID_SEEN"}
    if link_gate.get("allowed") and (apply_result is None or not apply_result.ok):
        return {"allowed": False, "reason": "SAFE_LINK_APPLY_FAILED"}
    if not summary.get("selected_target_time"):
        return {"allowed": False, "reason": "NO_SELECTED_LIVE_TARGET"}
    minutes = _float_or_none(summary.get("selected_minutes_until_target"))
    if minutes is None or minutes < min_minutes_before_target:
        return {"allowed": False, "reason": "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"}
    if _int_or_zero(summary.get("selected_window_market_rows")) <= 0:
        return {"allowed": False, "reason": "NO_SELECTED_WINDOW_ROWS"}
    if _int_or_zero(summary.get("selected_window_missing_link_rows")) > 0:
        return {"allowed": False, "reason": "MISSING_LINK_ROWS_STILL_OPEN"}
    if _int_or_zero(summary.get("selected_window_stale_link_rows")) > 0:
        return {"allowed": False, "reason": "STALE_LINK_ROWS_STILL_OPEN"}
    return {
        "allowed": True,
        "reason": "SELECTED_WINDOW_PIPELINE_GATE_OPEN",
        "r53_status": decision.get("status"),
        "selected_target_time": summary.get("selected_target_time"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
        "selected_window_market_rows": summary.get("selected_window_market_rows"),
    }


def _pipeline_probes(
    target: CloudBootstrapTarget,
    *,
    selected_target_time: str | None,
    selected_tickers: list[str],
    forecast_limit: int,
    per_ticker_timeout_seconds: int,
    pipeline_timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    prefix = f"cd {app} && set -a && . {env} && set +a && "
    timeout_value = int(pipeline_timeout_seconds)
    return [
        RemoteProbe(
            "weather_feature_refresh",
            prefix
            + f"timeout {timeout_value} .venv/bin/kalshi-bot ingest-weather --location-key new_york && "
            + f"timeout {timeout_value} .venv/bin/kalshi-bot build-weather-features --location-key new_york",
            timeout_value * 2 + 20,
        ),
        RemoteProbe(
            "weather_snapshot_capture",
            prefix
            + f"timeout {timeout_value} .venv/bin/kalshi-bot snapshot "
            "--status open --limit 100 --max-pages 3 --series-ticker KXTEMPNYCH --include-orderbook",
            timeout_value + 20,
        ),
        RemoteProbe(
            "weather_per_ticker_forecast",
            prefix
            + _per_ticker_forecast_shell(
                target.db_path,
                selected_target_time=selected_target_time,
                selected_tickers=selected_tickers,
                forecast_limit=forecast_limit,
                per_ticker_timeout_seconds=per_ticker_timeout_seconds,
            ),
            (per_ticker_timeout_seconds + 15) * 12,
        ),
        RemoteProbe(
            "weather_fast_lane_run",
            prefix
            + f"timeout {timeout_value} .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane "
            "--output-dir reports/phase3bb_r2 --reports-dir reports",
            timeout_value + 20,
        ),
        RemoteProbe(
            "unified_paper_gate_run",
            prefix
            + f"timeout {timeout_value} .venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate "
            "--output-dir reports/phase3bb_r8 --reports-dir reports",
            timeout_value + 20,
        ),
        RemoteProbe(
            "ba_r5_truth_run",
            prefix
            + f"timeout {timeout_value} .venv/bin/kalshi-bot phase3ba-r5-paper-ready-truth "
            "--output-dir reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120",
            timeout_value + 20,
        ),
        RemoteProbe("paper_gate_summary", f"cd {app} && " + _paper_gate_summary_shell(), 60),
    ]


def _per_ticker_forecast_shell(
    db_path: str,
    *,
    selected_target_time: str | None,
    selected_tickers: list[str],
    forecast_limit: int,
    per_ticker_timeout_seconds: int,
) -> str:
    ticker_literal = json.dumps([ticker for ticker in selected_tickers if ticker])
    script = f"""
python3 - <<'PY' >/tmp/phase3bb_r57_weather_tickers.txt
import sqlite3
from datetime import datetime, timezone

db_path = {db_path!r}
selected_target_time = {selected_target_time!r}
selected_tickers = {ticker_literal}
now = datetime.now(timezone.utc)

def parse_dt(value):
    if value is None:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def same_target(value):
    selected = parse_dt(selected_target_time)
    parsed = parse_dt(value)
    if selected is None or parsed is None:
        return True
    return abs((parsed - selected).total_seconds()) <= 3600

conn = sqlite3.connect(f"file:{{db_path}}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
if not selected_tickers:
    print("PHASE3BB_R57_SELECTED_TICKERS=0")
else:
    placeholders = ",".join("?" for _ in selected_tickers)
    rows = [dict(row) for row in conn.execute(
        f'''
        select ticker, close_time, expected_expiration_time, expiration_time, settlement_ts, status
        from markets
        where ticker in ({{placeholders}})
          and (status is null or lower(status) not in ('closed','settled','expired','inactive'))
        order by ticker asc
        ''',
        selected_tickers,
    ).fetchall()]
    for row in rows:
        target = None
        for key in ("close_time", "expected_expiration_time", "expiration_time", "settlement_ts"):
            target = parse_dt(row.get(key))
            if target is not None:
                break
        if target is not None and target >= now and same_target(target):
            print(row["ticker"])
    print(f"PHASE3BB_R57_SELECTED_TICKERS={{len(selected_tickers)}}")
PY
count=0
while IFS= read -r ticker; do
  [ -n "$ticker" ] || continue
  case "$ticker" in PHASE3BB_R57_SELECTED_TICKERS=*) echo "$ticker"; continue ;; esac
  count=$((count + 1))
  echo "PHASE3BB_R57_FORECAST_TICKER=$ticker"
  timeout {int(per_ticker_timeout_seconds)} .venv/bin/kalshi-bot forecast --model weather_v2 --ticker "$ticker" --limit {int(forecast_limit)}
done </tmp/phase3bb_r57_weather_tickers.txt
echo "PHASE3BB_R57_FORECASTED_TICKERS=$count"
"""
    return script.strip()


def _selected_ticker_values(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("window_rows") if isinstance(payload, dict) else []
    tickers: list[str] = []
    for row in rows or []:
        ticker = str(row.get("ticker") or "").strip()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def _paper_gate_summary_shell() -> str:
    return r"""
python3 - <<'PY'
import csv
import json
from pathlib import Path

rows = []
path = Path("reports/phase3bb_r8/paper_gate_rows.csv")
if path.exists():
    rows = list(csv.DictReader(path.open(newline="")))
weather = [row for row in rows if row.get("category") == "weather"]
blockers = {}
for row in weather:
    blocker = row.get("first_blocker") or "UNKNOWN"
    blockers[blocker] = blockers.get(blocker, 0) + 1
positive = [row for row in weather if row.get("positive_ev") == "True"]
ready = [row for row in weather if row.get("paper_ready") == "True"]
print(json.dumps({
    "rows": len(rows),
    "weather_rows": len(weather),
    "weather_positive_ev_rows": len(positive),
    "weather_paper_ready_rows": len(ready),
    "weather_blockers": blockers,
    "first_weather_positive_ticker": positive[0].get("ticker") if positive else None,
    "first_weather_positive_blocker": positive[0].get("first_blocker") if positive else None,
    "first_weather_ready_ticker": ready[0].get("ticker") if ready else None,
}, sort_keys=True))
PY
""".strip()


def _decision(
    *,
    writer_wait: dict[str, Any],
    link_gate: dict[str, Any],
    pipeline_gate: dict[str, Any],
    pipeline_results: list[RemoteProbeResult],
    active_r53: dict[str, Any],
    r53_final: dict[str, Any],
    r12_apply_payload: dict[str, Any],
    final_gate_summary: dict[str, Any],
) -> dict[str, Any]:
    final_summary = ((r53_final or active_r53).get("summary") or {}) if isinstance(r53_final or active_r53, dict) else {}
    first_failed = next((result for result in pipeline_results if not result.ok), None)
    weather_ready = _int_or_zero(final_gate_summary.get("weather_paper_ready_rows"))
    weather_positive = _int_or_zero(final_gate_summary.get("weather_positive_ev_rows"))
    if not writer_wait.get("cleared"):
        status = "WAITING_FOR_WRITER_CLEAR"
        blocker = "ACTIVE_WRITER"
        reason = "Writer gate did not clear; selected-window pipeline did not run."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R57 - Retry after writer clears"
    elif link_gate.get("allowed") and not r12_apply_payload:
        status = "R12_APPLY_NOT_CONFIRMED"
        blocker = "MISSING_LINK_APPLY_NOT_CONFIRMED"
        reason = "R53 required safe links, but no R12 apply payload was captured."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R57 - Inspect link apply"
    elif not pipeline_gate.get("allowed"):
        status = "SELECTED_WINDOW_GATE_CLOSED"
        blocker = pipeline_gate.get("reason") or "PIPELINE_GATE_CLOSED"
        reason = f"Pipeline did not run because the selected-window gate is closed: {blocker}."
        command = (
            "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
            "--output-dir reports/phase3bb_r53 --reports-dir reports"
        )
        next_step = "Phase 3BB-R53 - Recheck current weather window"
    elif first_failed is not None:
        status = "PIPELINE_STEP_FAILED"
        blocker = first_failed.name
        reason = f"Selected-window pipeline stopped at failed step {first_failed.name}."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R57 - Inspect failed pipeline step"
    elif weather_ready > 0:
        status = "PAPER_READY_GATE_OPEN"
        blocker = "PAPER_ONLY_OPERATOR_REVIEW_NEXT"
        reason = "Unified paper gate reports weather paper-ready rows. Do not create trades automatically."
        command = (
            "kalshi-bot phase3bb-r8-unified-paper-gate "
            "--output-dir reports/phase3bb_r8 --reports-dir reports"
        )
        next_step = "Paper-only operator review / risk preflight"
    elif weather_positive > 0:
        status = "WEATHER_POSITIVE_EV_NOT_PAPER_READY"
        blocker = final_gate_summary.get("first_weather_positive_blocker") or "PAPER_GATE_STILL_CLOSED"
        reason = "Weather has positive-EV rows, but the paper gate is still closed."
        command = (
            "kalshi-bot phase3bb-r8-unified-paper-gate "
            "--output-dir reports/phase3bb_r8 --reports-dir reports"
        )
        next_step = "Phase 3BB-R58 - Weather risk/snapshot final blocker review"
    elif _int_or_zero(final_summary.get("selected_window_ranking_rows")) > 0:
        status = "WEATHER_RANKED_EV_NOT_POSITIVE"
        blocker = "EV_NOT_POSITIVE"
        reason = "Selected weather window was ranked, but no paper-positive EV survived the gate."
        command = (
            "kalshi-bot phase3bb-r52-weather-ev-fair-value-diagnostic "
            "--output-dir reports/phase3bb_r52 --reports-dir reports"
        )
        next_step = "Phase 3BB-R52 - Weather EV / Fair-Value Diagnostic"
    else:
        status = "PIPELINE_COMPLETED_STILL_BLOCKED"
        blocker = (final_summary.get("first_window_blocker_counts") or {}) or "NO_RANKING_ROWS"
        reason = "Selected-window pipeline completed, but final R53/R8 still shows no ranked paper-ready rows."
        command = (
            "kalshi-bot phase3bb-r57-weather-selected-window-pipeline-speed-repair "
            "--output-dir reports/phase3bb_r57 --reports-dir reports"
        )
        next_step = "Phase 3BB-R57 - Retry selected-window pipeline"
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "primary_reason": reason,
        "operator_next_command": command,
        "next_codex_step": next_step,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
        "weather_positive_ev_rows": weather_positive,
        "weather_paper_ready_rows": weather_ready,
        "final_selected_window_ranking_rows": _int_or_zero(final_summary.get("selected_window_ranking_rows")),
        "final_selected_window_forecast_rows": _int_or_zero(final_summary.get("selected_window_forecast_rows")),
    }


def _pipeline_steps(results: list[RemoteProbeResult]) -> list[dict[str, Any]]:
    return [
        {
            "step": index,
            "name": result.name,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_seconds": round(result.duration_seconds, 3),
        }
        for index, result in enumerate(results, start=1)
    ]


def _selected_ticker_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("window_rows") if isinstance(payload, dict) else []
    selected = []
    for row in rows or []:
        selected.append(
            {
                "ticker": row.get("ticker"),
                "target_time": row.get("target_time"),
                "has_link": row.get("has_link"),
                "has_snapshot": row.get("has_snapshot"),
                "has_current_forecast": row.get("has_current_forecast"),
                "has_current_ranking": row.get("has_current_ranking"),
                "first_window_blocker": row.get("first_window_blocker"),
            }
        )
    return selected


def _json_from_named_probe(results: list[RemoteProbeResult], name: str) -> dict[str, Any]:
    for result in results:
        if result.name == name:
            parsed = _json_from_probe(result)
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _pipeline_probe_ok_or_seen(results: list[RemoteProbeResult], name: str) -> bool:
    return any(result.name == name for result in results)


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
    wait = payload["writer_wait"]
    gate = payload["pipeline_gate"]
    r53 = (payload.get("r53_final_payload") or payload.get("r53_after_apply_payload") or payload.get("r53_initial_payload") or {})
    summary = r53.get("summary") or {}
    lines = _metadata_lines(payload, "# Phase 3BB-R57 Selected-Window Weather Pipeline")
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
            f"- Writer cleared: `{wait['cleared']}`",
            f"- Pipeline gate allowed: `{gate.get('allowed')}`",
            f"- Selected target: `{summary.get('selected_target_time')}`",
            f"- Minutes until target: `{summary.get('selected_minutes_until_target')}`",
            f"- Selected window rows: `{summary.get('selected_window_market_rows')}`",
            f"- Forecast rows: `{decision.get('final_selected_window_forecast_rows')}`",
            f"- Ranking rows: `{decision.get('final_selected_window_ranking_rows')}`",
            f"- Weather positive-EV rows: `{decision.get('weather_positive_ev_rows')}`",
            f"- Weather paper-ready rows: `{decision.get('weather_paper_ready_rows')}`",
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
            "R57 does not stop R5, start services, create paper trades, submit live/demo orders, or lower thresholds.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Selected-Window Weather Pipeline",
        "",
        f"Status: `{payload['decision']['status']}`",
        f"First blocker: `{payload['decision']['first_hard_blocker']}`",
        "",
        "## Pipeline Steps",
        "",
        "| Step | Name | OK | Exit | Timed out |",
        "|---:|---|---:|---:|---:|",
    ]
    for row in payload["pipeline_steps"]:
        lines.append(
            f"| {row['step']} | {row['name']} | {row['ok']} | {row['exit_code']} | {row['timed_out']} |"
        )
    lines.extend(
        [
            "",
            "## Final Gate Summary",
            "",
            json.dumps(payload.get("final_gate_summary") or {}, indent=2, sort_keys=True),
            "",
            "## Guardrails",
            "",
            "- Paper-only.",
            "- No paper trades.",
            "- No live/demo orders.",
            "- No threshold lowering.",
            "- Per-ticker weather forecasts only; no broad weather forecast.",
        ]
    )
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
            "Guardrails:",
            "- Do not stop R5 from this phase.",
            "- Do not create paper trades unless a downstream paper-ready gate opens.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not lower EV, confidence, liquidity, spread, settlement, or risk thresholds.",
        ]
    ) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "#!/usr/bin/env bash\nset -euo pipefail\n" + payload["decision"]["operator_next_command"] + "\n"
