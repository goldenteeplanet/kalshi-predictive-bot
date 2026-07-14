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

PHASE3BB_R41_VERSION = "phase3bb_r41_writer_gate_normalization_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r41")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
DEFAULT_JOURNAL_LINES = 500


@dataclass(frozen=True)
class Phase3BBR41WriterGateNormalizationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    writer_gate_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r41_writer_gate_normalization_report(
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
) -> Phase3BBR41WriterGateNormalizationArtifacts:
    payload = build_phase3bb_r41_writer_gate_normalization(
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
    markdown_path = output_dir / "writer_gate_normalization.md"
    json_path = output_dir / "writer_gate_normalization.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "writer_gate_checks.csv"
    writer_gate_csv_path = output_dir / "writer_gate_skips.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["writer_gate_checks"])
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
            writer_gate_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR41WriterGateNormalizationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        writer_gate_csv_path=writer_gate_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r41_writer_gate_normalization(
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
        "command": "kalshi-bot phase3bb-r41-writer-gate-normalization",
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
        r5_service_name=r5_service_name,
        ui_service_name=ui_service_name,
        journal_lines=journal_lines,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    checks = _writer_gate_checks(parsed)
    decision = _decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "normalization_report_only": True,
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
        "phase": "3BB-R41-WRITER-GATE-NORMALIZATION",
        "phase_version": PHASE3BB_R41_VERSION,
        "mode": "PAPER_READ_ONLY_WRITER_GATE_NORMALIZATION",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "cloud_target": _target_payload(target),
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_writer_gate_state": parsed,
        "writer_gate_checks": checks,
        "writer_gate_decision": decision,
        "writer_gate_skips": parsed["writer_gate_skips"],
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
    r5_service_name: str,
    ui_service_name: str,
    journal_lines: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = _shell_quote(target.app_path)
    env = _shell_quote(target.env_path)
    service = _shell_quote(scheduler_service_name)
    timer = _shell_quote(scheduler_timer_name)
    r5_service = _shell_quote(r5_service_name)
    ui_service = _shell_quote(ui_service_name)
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
            "scheduler_journal",
            f"journalctl -u {service} -n {int(journal_lines)} --no-pager || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_fast_lane_help",
            f"cd {app} && .venv/bin/kalshi-bot phase3bb-r2-weather-fast-lane --help >/dev/null",
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    raw = _stdout(by_name.get("db_writer_monitor_raw"))
    strict_json_valid = False
    writer_payload: dict[str, Any] = {}
    parse_error = ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            strict_json_valid = True
            writer_payload = parsed
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
    journal = _stdout(by_name.get("scheduler_journal"))
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "db_writer_monitor_stdout_bytes": len(raw.encode("utf-8")),
        "db_writer_monitor_strict_json_valid": strict_json_valid,
        "db_writer_monitor_json_tool_ok": bool(
            by_name.get("db_writer_monitor_json_tool") and by_name["db_writer_monitor_json_tool"].ok
        ),
        "db_writer_monitor_parse_error": parse_error,
        "db_writer_monitor_payload": writer_payload,
        "writer_safe_to_start_write": bool(writer_payload.get("safe_to_start_write")),
        "writer_status": writer_payload.get("status") or "UNKNOWN",
        "writer_pid": writer_payload.get("current_writer_pid"),
        "writer_count": writer_payload.get("writer_count"),
        "holder_count": writer_payload.get("holder_count"),
        "scheduler_timer_active_state": _first_line(_stdout(by_name.get("scheduler_timer_active"))),
        "scheduler_service_active_state": _first_line(_stdout(by_name.get("scheduler_service_active"))),
        "r5_service_active_state": _first_line(_stdout(by_name.get("r5_service_active"))),
        "ui_service_active_state": _first_line(_stdout(by_name.get("ui_service_active"))),
        "weather_fast_lane_command_registered": bool(
            by_name.get("weather_fast_lane_help") and by_name["weather_fast_lane_help"].ok
        ),
        "writer_gate_skips": _parse_writer_gate_skips(journal),
        "writer_gate_skip_count": len(_parse_writer_gate_skips(journal)),
    }


def _writer_gate_checks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    return [
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
            f"timer={parsed.get('scheduler_timer_active_state')}.",
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
    if "db_writer_monitor_json_valid" in failed_names:
        status = "BLOCKED_INVALID_DB_WRITER_MONITOR_JSON"
        reason = "db-writer-monitor --json is still not strict JSON on the cloud host."
        next_step = "Phase 3BB-R41 - Sync CLI JSON Fix To Cloud"
        command = "python3 -m json.tool < <(kalshi-bot db-writer-monitor --json)"
    elif "writer_safe_to_start_write" in failed_names:
        status = "WAIT_FOR_ACTIVE_WRITER"
        reason = "The writer gate is parseable, but safe_to_start_write is false."
        next_step = "Phase 3BB-R41 - Wait For Writer Gate To Clear"
        command = "kalshi-bot db-writer-monitor --json"
    elif failed:
        status = "BLOCKED_WRITER_GATE_NORMALIZATION"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R41 - Resolve Writer Gate Runtime Dependency"
        command = "kalshi-bot phase3bb-r41-writer-gate-normalization --output-dir reports/phase3bb_r41 --reports-dir reports"
    else:
        status = "WRITER_GATE_NORMALIZED_WEATHER_FAST_LANE_UNBLOCKED"
        reason = "db-writer-monitor JSON is valid and safe_to_start_write=true; the next scheduler cycle can run weather fast-lane when its cadence is due."
        next_step = "Phase 3BB-R42 - Weather Fast-Lane Post-Unblock Verification"
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
    return {
        "status": status,
        "normalization_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "writer_gate_skip_count": parsed.get("writer_gate_skip_count"),
        "writer_status": parsed.get("writer_status"),
        "writer_safe_to_start_write": parsed.get("writer_safe_to_start_write"),
        "weather_fast_lane_unblocked": not failed,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _parse_writer_gate_skips(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if "Writer active; skip writer-gated job" in line:
            rows.append({"kind": "WRITER_ACTIVE_SKIP", "line": line[:500]})
        elif "db-writer-monitor JSON parse failed; skip writer-gated job" in line:
            rows.append({"kind": "WRITER_MONITOR_PARSE_SKIP", "line": line[:500]})
        elif "db-writer-monitor failed; skip writer-gated job" in line:
            rows.append({"kind": "WRITER_MONITOR_FAILED_SKIP", "line": line[:500]})
    return rows[-100:]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R41 Writer Gate Normalization")
    decision = payload["writer_gate_decision"]
    parsed = payload["parsed_writer_gate_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Normalization passed: `{decision['normalization_passed']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- db-writer-monitor strict JSON: `{parsed.get('db_writer_monitor_strict_json_valid')}`",
            f"- safe_to_start_write: `{parsed.get('writer_safe_to_start_write')}`",
            f"- Writer status: `{parsed.get('writer_status')}`",
            f"- Writer PID: `{parsed.get('writer_pid')}`",
            f"- Writer-gate skips in journal window: `{parsed.get('writer_gate_skip_count')}`",
            f"- Weather fast-lane registered: `{parsed.get('weather_fast_lane_command_registered')}`",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Weather fast-lane executed by this phase: `False`",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R41 Writer Gate Detail")
    decision = payload["writer_gate_decision"]
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
    for row in payload["writer_gate_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    lines.extend(["", "## Writer Gate Skips", ""])
    if payload["writer_gate_skips"]:
        for row in payload["writer_gate_skips"][-20:]:
            lines.append(f"- `{row['kind']}`: {row['line']}")
    else:
        lines.append("- None observed in the journal window.")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R41 Next Actions")
    decision = payload["writer_gate_decision"]
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
            "- Do not force weather fast-lane while safe_to_start_write is false.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R41 next safe operator command.",
            payload["writer_gate_decision"]["operator_next_command"],
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


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
