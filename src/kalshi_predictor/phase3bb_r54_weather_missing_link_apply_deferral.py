from __future__ import annotations

import csv
import json
import shlex
import time
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
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R54_VERSION = "phase3bb_r54_weather_missing_link_apply_deferral_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r54")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R53_OUTPUT_DIR = Path("reports/phase3bb_r53")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Phase3BBR54WeatherMissingLinkApplyDeferralArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    wait_checks_csv_path: Path
    probe_csv_path: Path
    apply_summary_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r54_weather_missing_link_apply_deferral_report(
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
    apply_timeout_seconds: int = 180,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR54WeatherMissingLinkApplyDeferralArtifacts:
    payload = build_phase3bb_r54_weather_missing_link_apply_deferral(
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
        apply_timeout_seconds=apply_timeout_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_missing_link_apply_deferral.md"
    json_path = output_dir / "weather_missing_link_apply_deferral.json"
    wait_checks_csv_path = output_dir / "writer_wait_checks.csv"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    apply_summary_csv_path = output_dir / "r12_apply_summary.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(wait_checks_csv_path, payload["writer_wait_checks"])
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(apply_summary_csv_path, [payload.get("r12_apply_summary") or {}])
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
            apply_summary_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR54WeatherMissingLinkApplyDeferralArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        wait_checks_csv_path=wait_checks_csv_path,
        probe_csv_path=probe_csv_path,
        apply_summary_csv_path=apply_summary_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r54_weather_missing_link_apply_deferral(
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
    apply_timeout_seconds: int = 180,
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
        "command": "kalshi-bot phase3bb-r54-weather-missing-link-apply-deferral",
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
    r12_apply_payload: dict[str, Any] = {}
    post_r53_payload: dict[str, Any] = {}
    apply_result: RemoteProbeResult | None = None

    if writer_wait["cleared"]:
        r53_artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
            session,
            output_dir=r53_output_dir,
            reports_dir=reports_dir,
            settings=resolved,
            command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair"],
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

    gate = _apply_gate(r53_payload, writer_wait=writer_wait, min_minutes_before_target=min_minutes_before_target)
    if gate["allowed"]:
        apply_probe = _r12_apply_probe(
            target,
            limit=limit,
            fresh_window_hours=fresh_window_hours,
            match_tolerance_hours=match_tolerance_hours,
            max_records=max_records,
            timeout_seconds=apply_timeout_seconds,
        )
        apply_result = runner(apply_probe, target)
        probe_results.append(apply_result)
        r12_apply_payload = _json_from_probe(apply_result)
        if apply_result.ok:
            post_r53_artifacts = write_phase3bb_r53_weather_current_window_cadence_report(
                session,
                output_dir=r53_output_dir,
                reports_dir=reports_dir,
                settings=resolved,
                command_args=["phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair", "--post-r12-apply"],
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
            post_r53_payload = _read_json(post_r53_artifacts.json_path)
    decision = _decision(
        writer_wait=writer_wait,
        gate=gate,
        r53_payload=r53_payload,
        r12_apply_payload=r12_apply_payload,
        apply_result=apply_result,
        post_r53_payload=post_r53_payload,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "weather_missing_link_apply_deferral": True,
        "ssh_read_only_commands_executed": writer_wait["read_only_probe_count"]
        + (len((r53_payload.get("remote_probe_results") or [])) if r53_payload else 0)
        + (len((post_r53_payload.get("remote_probe_results") or [])) if post_r53_payload else 0),
        "ssh_write_capable_commands_executed": 1 if apply_result is not None else 0,
        "remote_db_write_capable_apply_executed": apply_result is not None,
        "runs_missing_link_apply": apply_result is not None,
        "runs_weather_forecast": False,
        "runs_weather_fast_lane": False,
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
        "phase": "3BB-R54-WEATHER-MISSING-LINK-APPLY-DEFERRAL",
        "phase_version": PHASE3BB_R54_VERSION,
        "mode": "PAPER_ONLY_WEATHER_MISSING_LINK_APPLY_DEFERRAL",
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
        },
        "writer_wait": {key: value for key, value in writer_wait.items() if key != "probe_results"},
        "writer_wait_checks": writer_wait["checks"],
        "r53_gate_payload": _r53_compact(r53_payload),
        "r53_post_apply_payload": _r53_compact(post_r53_payload),
        "r12_apply_gate": gate,
        "r12_apply_summary": r12_apply_payload.get("summary") if isinstance(r12_apply_payload, dict) else {},
        "r12_apply_status": r12_apply_payload.get("status") if isinstance(r12_apply_payload, dict) else None,
        "r12_apply_payload": r12_apply_payload,
        "remote_probe_results": [_result_payload(result) for result in probe_results],
        "decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _wait_for_writer_clear(
    target: CloudBootstrapTarget,
    *,
    runner: ProbeRunner,
    expected_writer_pid: int | None,
    max_wait_seconds: int,
    poll_interval_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    results: list[RemoteProbeResult] = []
    max_checks = max(1, int(max(max_wait_seconds, 0) / max(poll_interval_seconds, 1)) + 1)
    cleared = False
    unexpected_writer = False
    final_writer: dict[str, Any] = {}
    for index in range(max_checks):
        probe = _writer_probe(target, name=f"writer_gate_check_{index + 1}", timeout_seconds=timeout_seconds)
        result = runner(probe, target)
        results.append(result)
        writer = _json_from_probe(result)
        if not isinstance(writer, dict):
            writer = {}
        writer_pid = writer.get("current_writer_pid")
        safe = bool(writer.get("safe_to_start_write"))
        final_writer = writer
        expected_match = expected_writer_pid is None or writer_pid in (None, expected_writer_pid)
        if writer_pid is not None and not expected_match:
            unexpected_writer = True
        checks.append(
            {
                "attempt": index + 1,
                "status": writer.get("status") or "UNKNOWN",
                "safe_to_start_write": safe,
                "current_writer_pid": writer_pid,
                "expected_writer_pid": expected_writer_pid,
                "expected_writer_match": expected_match,
                "elapsed_seconds": writer.get("current_writer_elapsed_seconds"),
            }
        )
        if safe:
            cleared = True
            break
        if poll_interval_seconds > 0 and index < max_checks - 1:
            time.sleep(poll_interval_seconds)
    return {
        "cleared": cleared,
        "unexpected_writer": unexpected_writer,
        "final_writer": final_writer,
        "checks": checks,
        "probe_results": results,
        "read_only_probe_count": len(results),
        "attempt_count": len(checks),
    }


def _writer_probe(target: CloudBootstrapTarget, *, name: str, timeout_seconds: int) -> RemoteProbe:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    command = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    return RemoteProbe(name, command, timeout_seconds)


def _r12_apply_probe(
    target: CloudBootstrapTarget,
    *,
    limit: int,
    fresh_window_hours: int,
    match_tolerance_hours: int,
    max_records: int,
    timeout_seconds: int,
) -> RemoteProbe:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    command = (
        f"cd {app} && set -a && . {env} && set +a && "
        ".venv/bin/kalshi-bot phase3az-r12-weather-missing-link-apply "
        "--output-dir reports/phase3az_r12_weather_r54 "
        f"--limit {int(limit)} "
        f"--fresh-window-hours {int(fresh_window_hours)} "
        f"--match-tolerance-hours {int(match_tolerance_hours)} "
        f"--max-records {int(max_records)} "
        "--apply --backup-first && "
        "cat reports/phase3az_r12_weather_r54/weather_missing_link_apply.json"
    )
    return RemoteProbe("r12_missing_link_apply", command, timeout_seconds)


def _apply_gate(
    r53_payload: dict[str, Any],
    *,
    writer_wait: dict[str, Any],
    min_minutes_before_target: int,
) -> dict[str, Any]:
    decision = r53_payload.get("decision") if isinstance(r53_payload, dict) else {}
    summary = r53_payload.get("summary") if isinstance(r53_payload, dict) else {}
    if not writer_wait.get("cleared"):
        return {"allowed": False, "reason": "WRITER_DID_NOT_CLEAR"}
    if writer_wait.get("unexpected_writer"):
        return {"allowed": False, "reason": "UNEXPECTED_WRITER_PID_SEEN"}
    if not r53_payload:
        return {"allowed": False, "reason": "R53_NOT_RUN"}
    if decision.get("status") != "WEATHER_CURRENT_WINDOW_LINK_APPLY_NEEDED":
        return {"allowed": False, "reason": f"R53_STATUS_{decision.get('status') or 'UNKNOWN'}"}
    if decision.get("blocked_by_writer"):
        return {"allowed": False, "reason": "R53_STILL_BLOCKED_BY_WRITER"}
    if not summary.get("writer_safe_to_start_write"):
        return {"allowed": False, "reason": "R53_WRITER_NOT_SAFE"}
    if not summary.get("selected_target_time"):
        return {"allowed": False, "reason": "NO_SELECTED_LIVE_TARGET"}
    minutes = _float_or_none(summary.get("selected_minutes_until_target"))
    if minutes is None or minutes < min_minutes_before_target:
        return {"allowed": False, "reason": "TARGET_WINDOW_TOO_CLOSE_TO_EXPIRY"}
    if _int_or_zero(summary.get("selected_window_missing_link_rows")) <= 0:
        return {"allowed": False, "reason": "NO_MISSING_LINK_ROWS"}
    return {
        "allowed": True,
        "reason": "R53_LIVE_WINDOW_MISSING_LINK_GATE_OPEN",
        "selected_target_time": summary.get("selected_target_time"),
        "selected_window_missing_link_rows": summary.get("selected_window_missing_link_rows"),
        "selected_minutes_until_target": summary.get("selected_minutes_until_target"),
    }


def _decision(
    *,
    writer_wait: dict[str, Any],
    gate: dict[str, Any],
    r53_payload: dict[str, Any],
    r12_apply_payload: dict[str, Any],
    apply_result: RemoteProbeResult | None,
    post_r53_payload: dict[str, Any],
) -> dict[str, Any]:
    apply_summary = (
        r12_apply_payload.get("summary") if isinstance(r12_apply_payload, dict) else {}
    ) or {}
    post_summary = (
        post_r53_payload.get("summary") if isinstance(post_r53_payload, dict) else {}
    ) or {}
    links_written = _int_or_zero(apply_summary.get("link_rows_written"))
    post_missing = _int_or_zero(post_summary.get("selected_window_missing_link_rows"))
    if not writer_wait.get("cleared"):
        status = "WAITING_FOR_WRITER_CLEAR"
        blocker = "ACTIVE_WRITER"
        reason = "Writer gate did not clear within the bounded R54 wait window."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R54 - Retry after R5 writer clears"
    elif not gate.get("allowed"):
        status = "R53_LIVE_WINDOW_GATE_CLOSED"
        blocker = gate.get("reason") or "R53_GATE_CLOSED"
        reason = f"R12 apply was not allowed: {blocker}."
        command = (
            "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
            "--output-dir reports/phase3bb_r53 --reports-dir reports"
        )
        next_step = "Phase 3BB-R53 - Recheck current weather window"
    elif apply_result is None:
        status = "R12_APPLY_NOT_ATTEMPTED"
        blocker = "APPLY_RESULT_MISSING"
        reason = "R54 gate opened but the apply probe was not attempted."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R54 - Repair apply orchestration"
    elif not apply_result.ok:
        status = "R12_APPLY_FAILED"
        blocker = "R12_APPLY_COMMAND_FAILED"
        reason = "The R12 missing-link apply command returned a non-zero exit code."
        command = "kalshi-bot db-writer-monitor --json"
        next_step = "Phase 3BB-R54 - Inspect R12 apply failure"
    elif links_written > 0 and post_missing == 0:
        status = "WEATHER_MISSING_LINK_APPLY_COMPLETED"
        blocker = "RANKING_PATH_NEXT"
        reason = "R12 wrote missing weather links and post-apply R53 confirms the selected window link gap closed."
        command = (
            "kalshi-bot phase3bb-r51-weather-ranking-path-repair "
            "--output-dir reports/phase3bb_r51 --reports-dir reports"
        )
        next_step = "Phase 3BB-R51 - Weather Ranking Path Repair"
    elif links_written > 0:
        status = "WEATHER_MISSING_LINK_APPLY_PARTIAL"
        blocker = "POST_APPLY_R53_STILL_HAS_MISSING_LINKS"
        reason = "R12 wrote links, but the post-apply R53 gate still reports missing selected-window links."
        command = (
            "kalshi-bot phase3bb-r53-weather-current-window-cadence-preview-narrowing-repair "
            "--output-dir reports/phase3bb_r53 --reports-dir reports"
        )
        next_step = "Phase 3BB-R53 - Recheck current weather window"
    else:
        status = "R12_APPLY_NO_ROWS_WRITTEN"
        blocker = r12_apply_payload.get("status") or "NO_ROWS_WRITTEN"
        reason = "R12 apply completed but wrote no weather links."
        command = (
            "kalshi-bot phase3az-r12-weather-activation-preview "
            "--output-dir reports/phase3az_r12_weather --limit 2000 --fresh-window-hours 24 --match-tolerance-hours 3"
        )
        next_step = "Phase 3AZ-R12 - Inspect preview rows"
    return {
        "status": status,
        "first_hard_blocker": blocker,
        "primary_reason": reason,
        "writer_cleared": bool(writer_wait.get("cleared")),
        "r53_status": (r53_payload.get("decision") or {}).get("status") if r53_payload else None,
        "r12_apply_status": r12_apply_payload.get("status") if isinstance(r12_apply_payload, dict) else None,
        "link_rows_written": links_written,
        "post_apply_missing_links": post_missing if post_r53_payload else None,
        "operator_next_command": command,
        "next_codex_step": next_step,
        "paper_trades_allowed": False,
        "live_demo_orders_allowed": False,
    }


def _r53_compact(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        "generated_at": payload.get("generated_at"),
        "decision": payload.get("decision"),
        "summary": payload.get("summary"),
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _render_executive_summary(payload: dict[str, Any]) -> str:
    decision = payload["decision"]
    wait = payload["writer_wait"]
    gate = payload["r12_apply_gate"]
    apply_summary = payload.get("r12_apply_summary") or {}
    r53_summary = (payload.get("r53_gate_payload") or {}).get("summary") or {}
    lines = _metadata_lines(payload, "# Phase 3BB-R54 Weather Missing-Link Apply Deferral")
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
            f"- R53 missing links: `{r53_summary.get('selected_window_missing_link_rows')}`",
            f"- Apply gate allowed: `{gate.get('allowed')}`",
            f"- R12 apply status: `{payload.get('r12_apply_status')}`",
            f"- Link rows written: `{apply_summary.get('link_rows_written')}`",
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
            "R54 does not stop R5, start services, create paper trades, submit live/demo orders, or lower thresholds.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    checks = payload["writer_wait_checks"]
    decision = payload["decision"]
    lines = [
        "# Weather Missing-Link Apply Deferral",
        "",
        f"Status: `{decision['status']}`",
        f"First blocker: `{decision['first_hard_blocker']}`",
        "",
        "| Attempt | Status | Safe | PID | Expected PID | Match |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for row in checks:
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
    lines.extend(["", "## Guardrails", "", "- Paper-only.", "- No paper trades.", "- No live/demo orders.", "- No threshold lowering."])
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


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row}) if rows else ["status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_probe_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = ["name", "ok", "exit_code", "duration_seconds", "timed_out", "stdout_excerpt", "stderr_excerpt", "command"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(result)
