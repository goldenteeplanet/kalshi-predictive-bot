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
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import ProbeRunner, _resolve_target, _result_payload, _run_ssh_probe
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import _mark_executable, _target_payload
from kalshi_predictor.phase3bb_r51_weather_ranking_path_repair import (
    write_phase3bb_r51_weather_ranking_path_repair_report,
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
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R55_VERSION = "phase3bb_r55_weather_ranking_path_retry_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r55")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R53_OUTPUT_DIR = Path("reports/phase3bb_r53")
DEFAULT_R51_OUTPUT_DIR = Path("reports/phase3bb_r51")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60
DEFAULT_REPAIR_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Phase3BBR55WeatherRankingPathRetryArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    wait_checks_csv_path: Path
    probe_csv_path: Path
    r51_summary_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r55_weather_ranking_path_retry_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    r51_output_dir: Path = DEFAULT_R51_OUTPUT_DIR,
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
    current_window_lookback_hours: int = 3,
    repair_timeout_seconds: int = DEFAULT_REPAIR_TIMEOUT_SECONDS,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR55WeatherRankingPathRetryArtifacts:
    payload = build_phase3bb_r55_weather_ranking_path_retry(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        r53_output_dir=r53_output_dir,
        r51_output_dir=r51_output_dir,
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
        current_window_lookback_hours=current_window_lookback_hours,
        repair_timeout_seconds=repair_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_ranking_path_retry.md"
    json_path = output_dir / "weather_ranking_path_retry.json"
    wait_checks_csv_path = output_dir / "writer_wait_checks.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    r51_summary_csv_path = output_dir / "r51_summary.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(wait_checks_csv_path, payload["writer_wait_checks"])
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(r51_summary_csv_path, [payload.get("r51_summary") or {}])
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
            probe_csv_path,
            r51_summary_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR55WeatherRankingPathRetryArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        wait_checks_csv_path=wait_checks_csv_path,
        probe_csv_path=probe_csv_path,
        r51_summary_csv_path=r51_summary_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r55_weather_ranking_path_retry(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    r53_output_dir: Path = DEFAULT_R53_OUTPUT_DIR,
    r51_output_dir: Path = DEFAULT_R51_OUTPUT_DIR,
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
    current_window_lookback_hours: int = 3,
    repair_timeout_seconds: int = DEFAULT_REPAIR_TIMEOUT_SECONDS,
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
        "command": "kalshi-bot phase3bb-r55-weather-ranking-path-retry",
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
    probe_results = list(writer_wait["probe_results"])
    r53_payload: dict[str, Any] = {}
    r51_payload: dict[str, Any] = {}
    if writer_wait["cleared"]:
        r53_artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
            session,
            output_dir=r53_output_dir,
            reports_dir=reports_dir,
            settings=resolved,
            command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--r55-gate"],
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
        r53_payload = _read_json(r53_artifacts.json_path)
        probe_results.extend(_probe_payloads_to_results(r53_payload.get("remote_probe_results") or []))
    gate = _r51_gate(r53_payload, writer_wait=writer_wait, min_minutes_before_target=min_minutes_before_target)
    if gate["allowed"]:
        r51_artifacts = write_phase3bb_r51_weather_ranking_path_repair_report(
            session,
            output_dir=r51_output_dir,
            reports_dir=reports_dir,
            settings=resolved,
            command_args=["phase3bb-r51-weather-ranking-path-repair", "--r55-retry"],
            ssh_target=ssh_target,
            identity_file=identity_file,
            app_path=app_path,
            env_path=env_path,
            db_path=db_path,
            current_window_lookback_hours=current_window_lookback_hours,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            run_repair=True,
            repair_timeout_seconds=repair_timeout_seconds,
            per_probe_timeout_seconds=per_probe_timeout_seconds,
            probe_runner=runner,
        )
        r51_payload = _read_json(r51_artifacts.json_path)
        probe_results.extend(_probe_payloads_to_results(r51_payload.get("remote_probe_results") or []))
    decision = _decision(writer_wait=writer_wait, gate=gate, r53_payload=r53_payload, r51_payload=r51_payload)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "weather_ranking_path_retry": True,
        "ssh_read_only_commands_executed": writer_wait["read_only_probe_count"]
        + (len((r53_payload.get("remote_probe_results") or [])) if r53_payload else 0)
        + (len((r51_payload.get("remote_probe_results") or [])) if r51_payload else 0),
        "ssh_write_capable_commands_executed": len(_r51_repair_probe_names(r51_payload)),
        "runs_weather_snapshot_capture": "weather_snapshot_capture" in _r51_repair_probe_names(r51_payload),
        "runs_weather_forecast": "weather_forecast_run" in _r51_repair_probe_names(r51_payload),
        "runs_weather_fast_lane": "weather_fast_lane_run" in _r51_repair_probe_names(r51_payload),
        "runs_missing_link_apply": False,
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
        "phase": "3BB-R55-WEATHER-RANKING-PATH-RETRY-AFTER-R5-WRITER-CLEARS",
        "phase_version": PHASE3BB_R55_VERSION,
        "mode": "PAPER_ONLY_WEATHER_RANKING_PATH_RETRY",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "parameters": {
            "expected_writer_pid": expected_writer_pid,
            "max_wait_seconds": max_wait_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "min_minutes_before_target": min_minutes_before_target,
            "fresh_window_hours": fresh_window_hours,
            "match_tolerance_hours": match_tolerance_hours,
            "current_window_lookback_hours": current_window_lookback_hours,
            "repair_timeout_seconds": repair_timeout_seconds,
        },
        "writer_wait": {key: value for key, value in writer_wait.items() if key != "probe_results"},
        "writer_wait_checks": writer_wait["checks"],
        "r53_gate_payload": _r53_compact(r53_payload),
        "r51_gate": gate,
        "r51_summary": _r51_summary(r51_payload),
        "r51_payload": r51_payload,
        "remote_probe_results": [_result_payload(result) for result in probe_results],
        "decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _r51_gate(
    r53_payload: dict[str, Any],
    *,
    writer_wait: dict[str, Any],
    min_minutes_before_target: int,
) -> dict[str, Any]:
    decision = (r53_payload.get("decision") if isinstance(r53_payload, dict) else {}) or {}
    summary = (r53_payload.get("summary") if isinstance(r53_payload, dict) else {}) or {}
    status = decision.get("status")
    if not writer_wait.get("cleared"):
        return {"allowed": False, "reason": "WRITER_DID_NOT_CLEAR"}
    if writer_wait.get("unexpected_writer"):
        return {"allowed": False, "reason": "UNEXPECTED_WRITER_PID_SEEN"}
    if not r53_payload:
        return {"allowed": False, "reason": "R53_NOT_RUN"}
    if decision.get("blocked_by_writer"):
        return {"allowed": False, "reason": "R53_STILL_BLOCKED_BY_WRITER"}
    if not summary.get("writer_safe_to_start_write"):
        return {"allowed": False, "reason": "R53_WRITER_NOT_SAFE"}
    if not summary.get("selected_target_time"):
        return {"allowed": False, "reason": "NO_SELECTED_LIVE_TARGET"}
    minutes = _float_or_none(summary.get("selected_minutes_until_target"))
    if minutes is None or minutes < min_minutes_before_target:
        return {"allowed": False, "reason": "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"}
    if _int_or_zero(summary.get("selected_window_market_rows")) <= 0:
        return {"allowed": False, "reason": "NO_SELECTED_WINDOW_ROWS"}
    if _int_or_zero(summary.get("selected_window_missing_link_rows")) > 0:
        return {"allowed": False, "reason": "MISSING_LINK_ROWS_STILL_OPEN"}
    if status not in {
        "WEATHER_CURRENT_WINDOW_SNAPSHOT_MISSING",
        "WEATHER_CURRENT_WINDOW_FEATURE_REFRESH_NEEDED",
        "WEATHER_CURRENT_WINDOW_RANKING_PATH_NEEDED",
        "WEATHER_CURRENT_WINDOW_EV_NOT_POSITIVE",
        "WEATHER_CURRENT_WINDOW_POSITIVE_EV_REFRESH_PAPER_GATE",
    }:
        return {"allowed": False, "reason": f"R53_STATUS_{status or 'UNKNOWN'}"}
    return {
        "allowed": True,
        "reason": "R53_LIVE_WINDOW_RANKING_PATH_GATE_OPEN",
        "selected_target_time": summary.get("selected_target_time"),
        "selected_window_market_rows": summary.get("selected_window_market_rows"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
        "r53_status": status,
    }


def _decision(
    *,
    writer_wait: dict[str, Any],
    gate: dict[str, Any],
    r53_payload: dict[str, Any],
    r51_payload: dict[str, Any],
) -> dict[str, Any]:
    r53_decision = (r53_payload.get("decision") or {}) if isinstance(r53_payload, dict) else {}
    r51_decision = (r51_payload.get("ranking_path_decision") or {}) if isinstance(r51_payload, dict) else {}
    r51_status = r51_decision.get("status")
    if not writer_wait.get("cleared"):
        status = "WAITING_FOR_WRITER_CLEAR"
        blocker = "ACTIVE_WRITER"
        reason = "Writer gate did not clear during the bounded R55 wait; R51 was not run."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R55 - Retry after R5 writer clears"
    elif not gate.get("allowed"):
        status = "R53_LIVE_WINDOW_GATE_CLOSED"
        blocker = gate.get("reason") or "R53_GATE_CLOSED"
        reason = f"R51 was not allowed because the current-window gate is closed: {blocker}."
        command = (
            "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
            "--output-dir reports/phase3bb_r53 --reports-dir reports"
        )
        next_step = "Phase 3BB-R53 - Recheck Current Weather Window"
    elif r51_status == "WEATHER_RANKING_PATH_REPAIRED":
        status = "WEATHER_RANKING_PATH_RETRY_COMPLETED"
        blocker = "EV_FAIR_VALUE_DIAGNOSTIC_NEXT"
        reason = "R51 ran after the writer cleared and produced current weather_v2 ranking rows."
        command = (
            "kalshi-bot phase3bb-r52-weather-ev-fair-value-diagnostic "
            "--output-dir reports/phase3bb_r52 --reports-dir reports"
        )
        next_step = "Phase 3BB-R52 - Weather EV / Fair-Value Diagnostic"
    elif r51_status == "WAIT_FOR_WRITER_CLEAR":
        status = "R51_REBLOCKED_BY_WRITER"
        blocker = "ACTIVE_WRITER"
        reason = "The writer was clear for R53 but became busy before R51 could run repair."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R55 - Retry after writer clears again"
    elif r51_status:
        status = "R51_COMPLETED_WITH_WEATHER_BLOCKER"
        blocker = r51_decision.get("first_weather_path_blocker") or r51_status
        reason = r51_decision.get("primary_reason") or "R51 completed but the weather path is still blocked."
        command = r51_decision.get("operator_next_command") or (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        next_step = r51_decision.get("next_codex_step") or "Phase 3BB-R51 - Continue weather ranking path repair"
    else:
        status = "R51_NOT_RUN_OR_UNREADABLE"
        blocker = "R51_PAYLOAD_MISSING"
        reason = "The R55 gate opened, but no readable R51 result was captured."
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        next_step = "Phase 3BB-R51 - Run Weather Ranking Path Repair"
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "primary_reason": reason,
        "writer_cleared": bool(writer_wait.get("cleared")),
        "r53_status": r53_decision.get("status"),
        "r51_status": r51_status,
        "r51_ranking_rows": _int_or_zero(r51_decision.get("ranking_rows")),
        "r51_forecast_rows": _int_or_zero(r51_decision.get("forecast_rows")),
        "r51_snapshot_rows": _int_or_zero(r51_decision.get("snapshot_rows")),
        "r51_live_or_future_rows": _int_or_zero(r51_decision.get("live_or_future_rows")),
        "operator_next_command": command,
        "next_codex_step": next_step,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _r51_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    decision = payload.get("ranking_path_decision") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "status": decision.get("status"),
        "first_weather_path_blocker": decision.get("first_weather_path_blocker"),
        "current_weather_rows": decision.get("current_weather_rows"),
        "live_or_future_rows": decision.get("live_or_future_rows"),
        "snapshot_rows": decision.get("snapshot_rows"),
        "forecast_rows": decision.get("forecast_rows"),
        "ranking_rows": decision.get("ranking_rows"),
        "repair_run_attempted": decision.get("repair_run_attempted"),
        "snapshot_capture_ok": decision.get("snapshot_capture_ok"),
        "forecast_run_ok": decision.get("forecast_run_ok"),
        "fast_lane_run_ok": decision.get("fast_lane_run_ok"),
    }


def _r51_repair_probe_names(payload: dict[str, Any]) -> list[str]:
    if not payload:
        return []
    names = []
    for result in payload.get("remote_probe_results") or []:
        name = result.get("name")
        if name in {"weather_snapshot_capture", "weather_forecast_run", "weather_fast_lane_run"}:
            names.append(name)
    return names


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
    gate = payload["r51_gate"]
    r53_summary = (payload.get("r53_gate_payload") or {}).get("summary") or {}
    r51 = payload.get("r51_summary") or {}
    lines = _metadata_lines(payload, "# Phase 3BB-R55 Weather Ranking Path Retry")
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
            f"- Writer attempts: `{wait['attempt_count']}`",
            f"- R53 selected target: `{r53_summary.get('selected_target_time')}`",
            f"- R53 minutes until target: `{r53_summary.get('selected_minutes_until_target')}`",
            f"- R53 missing links: `{r53_summary.get('selected_window_missing_link_rows')}`",
            f"- R51 gate allowed: `{gate.get('allowed')}`",
            f"- R51 status: `{r51.get('status')}`",
            f"- R51 ranking rows: `{r51.get('ranking_rows')}`",
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
            "R55 does not stop R5, start services, create paper trades, submit live/demo orders, or lower thresholds.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    lines = [
        "# Weather Ranking Path Retry",
        "",
        f"Status: `{decision['status']}`",
        f"First blocker: `{decision['first_hard_blocker']}`",
        "",
        "## Writer Wait",
        "",
        "| Attempt | Status | Safe | PID | Expected PID | Match |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for row in payload["writer_wait_checks"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("attempt")),
                    str(row.get("status") or ""),
                    str(row.get("safe_to_start_write")),
                    str(row.get("current_writer_pid") or ""),
                    str(row.get("expected_writer_pid") or ""),
                    str(row.get("expected_writer_match")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## R51 Summary",
            "",
            json.dumps(payload.get("r51_summary") or {}, indent=2, sort_keys=True),
            "",
            "## Guardrails",
            "",
            "- Paper-only.",
            "- No paper trades.",
            "- No live/demo orders.",
            "- No threshold lowering.",
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
