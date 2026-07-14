from __future__ import annotations

import base64
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
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R43_VERSION = "phase3bb_r43_weather_catalog_scheduler_hook_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r43")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
HOOK_JOB_ID = "weather_current_catalog_refresh"
WEATHER_FAST_LANE_JOB_ID = "weather_fast_lane"

HOOK_BLOCK = "\n".join(
    [
        "# cadence_minutes=30 category=weather-catalog",
        (
            "run_job weather_current_catalog_refresh true bash -lc "
            "'set -euo pipefail; "
            ".venv/bin/kalshi-bot sync-markets --status open --limit 100 "
            "--max-pages 3 --series-ticker KXTEMPNYCH; "
            ".venv/bin/kalshi-bot market-legs-parse --refresh --limit 1500; "
            ".venv/bin/kalshi-bot ingest-weather --location-key new_york; "
            ".venv/bin/kalshi-bot build-weather-features --location-key new_york; "
            ".venv/bin/kalshi-bot phase3az-r12-weather-activation-preview "
            "--output-dir reports/phase3az_r12_weather --limit 2000 "
            "--fresh-window-hours 24 --match-tolerance-hours 3'"
        ),
    ]
)


@dataclass(frozen=True)
class Phase3BBR43WeatherCatalogSchedulerHookArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    runner_draft_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r43_weather_catalog_scheduler_hook_report(
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
    apply: bool = False,
    backup_first: bool = False,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR43WeatherCatalogSchedulerHookArtifacts:
    payload = build_phase3bb_r43_weather_catalog_scheduler_hook(
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
        apply=apply,
        backup_first=backup_first,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "weather_catalog_scheduler_hook.md"
    json_path = output_dir / "weather_catalog_scheduler_hook.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "hook_checks.csv"
    runner_draft_path = output_dir / f"{RUNNER_SCRIPT_NAME}.phase3bb_r43.draft"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["hook_checks"])
    runner_draft_path.write_text(payload["patched_runner_script"], encoding="utf-8")
    _mark_executable(runner_draft_path)
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
            runner_draft_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR43WeatherCatalogSchedulerHookArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        runner_draft_path=runner_draft_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r43_weather_catalog_scheduler_hook(
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
    apply: bool = False,
    backup_first: bool = False,
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
        "command": "kalshi-bot phase3bb-r43-weather-catalog-scheduler-hook",
        "argv": command_args or [],
    }
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    r42 = _read_json(reports_dir / "phase3bb_r42" / "weather_fast_lane_post_unblock.json")
    target = _resolve_target(
        r11_context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    runner = probe_runner or _run_ssh_probe
    probes = _build_remote_probes(target, timeout_seconds=per_probe_timeout_seconds)
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results, r42=r42, target=target)
    patched_runner = _patched_runner(parsed.get("runner_script") or "")
    parsed["patched_runner_has_hook"] = HOOK_JOB_ID in patched_runner
    parsed["runner_patch_required"] = bool(patched_runner and patched_runner != parsed.get("runner_script"))
    checks = _hook_checks(parsed=parsed, apply=apply, backup_first=backup_first)
    install_result: dict[str, Any] = {"attempted": False, "ok": False, "exit_code": None, "stdout": "", "stderr": ""}
    verify_after = parsed.get("runner_hook_present", False)
    if apply and _can_apply(checks, parsed):
        install_probe = _build_install_probe(
            target,
            patched_runner=patched_runner,
            backup_first=backup_first,
            timeout_seconds=per_probe_timeout_seconds,
        )
        install_probe_result = runner(install_probe, target)
        results.append(install_probe_result)
        install_result = {
            "attempted": True,
            "ok": bool(install_probe_result.ok),
            "exit_code": install_probe_result.exit_code,
            "stdout": install_probe_result.stdout[-2000:],
            "stderr": install_probe_result.stderr[-2000:],
        }
        verify_probe = RemoteProbe(
            "runner_script_after_apply",
            (
                f"test -x {shlex.quote(_runner_path(target))} && "
                f"sed -n '1,260p' {shlex.quote(_runner_path(target))}"
            ),
            per_probe_timeout_seconds,
        )
        verify_result = runner(verify_probe, target)
        results.append(verify_result)
        verify_after = HOOK_JOB_ID in (verify_result.stdout or "")
    decision = _decision(
        checks=checks,
        parsed=parsed,
        apply=apply,
        backup_first=backup_first,
        install_result=install_result,
        verify_after=bool(verify_after),
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": not apply,
        "scheduler_hook_phase": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 1 if install_result["attempted"] else 0,
        "systemctl_mutating_commands_executed": 0,
        "scheduler_runner_written_to_system": bool(install_result["attempted"] and install_result["ok"]),
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
        "phase": "3BB-R43-WEATHER-CATALOG-SCHEDULER-HOOK",
        "phase_version": PHASE3BB_R43_VERSION,
        "mode": "PAPER_READ_ONLY_WEATHER_CATALOG_SCHEDULER_HOOK",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "apply_requested": apply,
        "backup_first": backup_first,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_hook_state": parsed,
        "hook_checks": checks,
        "install_result": install_result,
        "hook_decision": decision,
        "current_runner_script": parsed.get("runner_script") or "",
        "patched_runner_script": patched_runner,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(target: CloudBootstrapTarget, *, timeout_seconds: int) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    runner_path = shlex.quote(_runner_path(target))
    writer_cmd = (
        f"cd {app} && set -a && . {env} && set +a && "
        ".venv/bin/kalshi-bot db-writer-monitor --json"
    )
    registry_commands = (
        "sync-markets",
        "market-legs-parse",
        "phase3az-r12-weather-activation-preview",
        "phase3bb-r2-weather-fast-lane",
    )
    registry_loop = " ".join(shlex.quote(command) for command in registry_commands)
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
        RemoteProbe("db_writer_monitor_json_tool", f"{writer_cmd} | python3 -m json.tool >/dev/null", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {SCHEDULER_TIMER_NAME} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {SCHEDULER_SERVICE_NAME} || true", timeout_seconds),
        RemoteProbe("scheduler_runner_script", f"test -x {runner_path} && sed -n '1,260p' {runner_path}", timeout_seconds),
        RemoteProbe(
            "command_registry",
            (
                f"cd {app} && for cmd in {registry_loop}; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "weather_funnel_json",
            f"cd {app} && cat reports/phase3bb_r2/weather_funnel.json 2>/dev/null || true",
            timeout_seconds,
        ),
    ]


def _build_install_probe(
    target: CloudBootstrapTarget,
    *,
    patched_runner: str,
    backup_first: bool,
    timeout_seconds: int,
) -> RemoteProbe:
    encoded = base64.b64encode(patched_runner.encode("utf-8")).decode("ascii")
    runner_path = _runner_path(target)
    backup_literal = "True" if backup_first else "False"
    command = f"""python3 - <<'PY'
import base64
import os
import shutil
import time
from pathlib import Path

runner = Path({runner_path!r})
tmp = Path('/tmp/phase3bb_r43_weather_catalog_runner.sh')
data = base64.b64decode({encoded!r})
tmp.write_bytes(data)
tmp.chmod(0o755)
if runner.exists():
    st = runner.stat()
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except PermissionError:
        pass
    if {backup_literal}:
        backup = runner.with_name(runner.name + '.phase3bb-r43.' + time.strftime('%Y%m%d%H%M%S') + '.bak')
        shutil.copy2(runner, backup)
    else:
        backup = None
else:
    runner.parent.mkdir(parents=True, exist_ok=True)
    backup = None
os.replace(tmp, runner)
runner.chmod(0o755)
print('INSTALLED_R43_WEATHER_CATALOG_HOOK')
print(f'runner={{runner}}')
print(f'backup={{backup or ""}}')
PY"""
    return RemoteProbe("install_runner_hook", command, timeout_seconds)


def _parse_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    r42: dict[str, Any],
    target: CloudBootstrapTarget,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    raw_writer = _stdout(by_name.get("db_writer_monitor_raw"))
    writer_payload: dict[str, Any] = {}
    writer_json_valid = False
    writer_parse_error = ""
    try:
        parsed_writer = json.loads(raw_writer)
        if isinstance(parsed_writer, dict):
            writer_payload = parsed_writer
            writer_json_valid = True
    except json.JSONDecodeError as exc:
        writer_parse_error = str(exc)
    runner_script = _stdout(by_name.get("scheduler_runner_script"))
    weather_funnel = _json_from_probe(by_name.get("weather_funnel_json"))
    r42_decision = r42.get("post_unblock_decision") or {}
    r42_summary = r42.get("weather_fast_lane_summary") or {}
    weather_summary = weather_funnel.get("summary") if isinstance(weather_funnel, dict) else {}
    if not isinstance(weather_summary, dict):
        weather_summary = {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "runner_path": _runner_path(target),
        "runner_script": runner_script,
        "runner_exists": bool(runner_script.strip()),
        "runner_hook_present": HOOK_JOB_ID in runner_script,
        "runner_weather_fast_lane_present": WEATHER_FAST_LANE_JOB_ID in runner_script,
        "runner_hook_duplicate_count": runner_script.count(HOOK_JOB_ID),
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
        "command_registry_ok": bool(by_name.get("command_registry") and by_name["command_registry"].ok),
        "r42_status": r42_decision.get("status"),
        "r42_first_hard_blocker": r42_decision.get("first_hard_blocker") or r42_summary.get("first_hard_blocker"),
        "weather_funnel_status": weather_funnel.get("status") if isinstance(weather_funnel, dict) else None,
        "current_weather_rows": weather_summary.get("current_weather_rows"),
    }


def _patched_runner(runner_script: str) -> str:
    if not runner_script.strip():
        return ""
    if HOOK_JOB_ID in runner_script:
        return runner_script
    marker = "# cadence_minutes=30 category=weather\nrun_job weather_fast_lane true"
    index = runner_script.find(marker)
    if index < 0:
        return runner_script
    return runner_script[:index] + HOOK_BLOCK + "\n\n" + runner_script[index:]


def _hook_checks(
    *,
    parsed: dict[str, Any],
    apply: bool,
    backup_first: bool,
) -> list[dict[str, Any]]:
    service_state = parsed.get("scheduler_service_active_state")
    return [
        _check("runner_script_found", bool(parsed.get("runner_exists")), f"runner={parsed.get('runner_path')}."),
        _check(
            "runner_has_weather_fast_lane_anchor",
            bool(parsed.get("runner_weather_fast_lane_present")),
            f"weather_fast_lane_present={parsed.get('runner_weather_fast_lane_present')}.",
        ),
        _check(
            "runner_hook_not_duplicated",
            int(parsed.get("runner_hook_duplicate_count") or 0) <= 1,
            f"hook_count={parsed.get('runner_hook_duplicate_count')}.",
        ),
        _check(
            "patched_runner_has_hook",
            bool(parsed.get("patched_runner_has_hook")),
            f"patch_required={parsed.get('runner_patch_required')}.",
        ),
        _check(
            "command_registry_ok",
            bool(parsed.get("command_registry_ok")),
            "sync-markets, market-legs-parse, R12 preview, and weather fast-lane are registered.",
        ),
        _check(
            "db_writer_monitor_json_valid",
            bool(parsed.get("db_writer_monitor_strict_json_valid"))
            and bool(parsed.get("db_writer_monitor_json_tool_ok")),
            parsed.get("db_writer_monitor_parse_error") or "db-writer-monitor --json parses cleanly.",
        ),
        _check(
            "scheduler_service_not_running_for_apply",
            (not apply) or service_state not in {"active", "activating"},
            f"scheduler_service={service_state}.",
        ),
        _check(
            "backup_first_for_apply",
            (not apply) or backup_first,
            f"apply={apply} backup_first={backup_first}.",
        ),
    ]


def _decision(
    *,
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
    apply: bool,
    backup_first: bool,
    install_result: dict[str, Any],
    verify_after: bool,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if failed:
        status = "BLOCKED_WEATHER_CATALOG_HOOK"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R43 - Resolve Weather Catalog Hook Preconditions"
        command = "kalshi-bot phase3bb-r43-weather-catalog-scheduler-hook --output-dir reports/phase3bb_r43 --reports-dir reports"
    elif parsed.get("runner_hook_present") and not apply:
        status = "WEATHER_CATALOG_HOOK_ALREADY_INSTALLED"
        reason = "The scheduler runner already contains weather_current_catalog_refresh."
        next_step = "Phase 3BB-R44 - Weather Catalog Hook Runtime Verification"
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
    elif not apply:
        status = "READY_TO_INSTALL_WEATHER_CATALOG_HOOK"
        reason = "Patched runner is ready; rerun R43 with --apply --backup-first to install it."
        next_step = "Phase 3BB-R43 - Apply Weather Catalog Hook With Backup"
        command = (
            "kalshi-bot phase3bb-r43-weather-catalog-scheduler-hook "
            "--output-dir reports/phase3bb_r43 --reports-dir reports --apply --backup-first"
        )
    elif install_result.get("ok") and verify_after:
        status = "WEATHER_CATALOG_HOOK_INSTALLED"
        reason = "The cloud scheduler runner was backed up and updated with weather_current_catalog_refresh."
        next_step = "Phase 3BB-R44 - Weather Catalog Hook Runtime Verification"
        command = "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor --output-dir reports/phase3bb_r40 --reports-dir reports"
    else:
        status = "BLOCKED_WEATHER_CATALOG_HOOK_INSTALL"
        reason = "The install command did not verify the weather catalog hook in the cloud runner."
        next_step = "Phase 3BB-R43 - Inspect Weather Catalog Hook Install Failure"
        command = "kalshi-bot phase3bb-r43-weather-catalog-scheduler-hook --output-dir reports/phase3bb_r43 --reports-dir reports"
    return {
        "status": status,
        "hook_ready_or_installed": status in {
            "WEATHER_CATALOG_HOOK_ALREADY_INSTALLED",
            "READY_TO_INSTALL_WEATHER_CATALOG_HOOK",
            "WEATHER_CATALOG_HOOK_INSTALLED",
        },
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "apply_requested": apply,
        "backup_first": backup_first,
        "runner_patch_required": parsed.get("runner_patch_required"),
        "runner_hook_present_before": parsed.get("runner_hook_present"),
        "runner_hook_present_after": verify_after,
        "install_attempted": install_result.get("attempted"),
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _can_apply(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> bool:
    return all(row["passed"] for row in checks) and bool(parsed.get("runner_patch_required"))


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R43 Weather Current Catalog Scheduler Hook")
    decision = payload["hook_decision"]
    parsed = payload["parsed_hook_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Apply requested: `{decision['apply_requested']}`",
            f"- Backup first: `{decision['backup_first']}`",
            f"- Runner hook before: `{decision['runner_hook_present_before']}`",
            f"- Runner hook after: `{decision['runner_hook_present_after']}`",
            f"- Patch required: `{decision['runner_patch_required']}`",
            f"- Scheduler timer: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Scheduler service: `{parsed.get('scheduler_service_active_state')}`",
            f"- Writer status: `{parsed.get('writer_status')}`",
            f"- R42 blocker: `{parsed.get('r42_first_hard_blocker')}`",
            "",
            "## Hook",
            "",
            "The hook runs before weather_fast_lane under the existing writer gate:",
            "",
            "```bash",
            HOOK_BLOCK,
            "```",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler timer/service start or stop by this phase: `0`",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R43 Hook Detail")
    lines.extend(["", "## Checks", "", "| Check | Passed | Detail |", "|---|---:|---|"])
    for row in payload["hook_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    install = payload["install_result"]
    lines.extend(
        [
            "",
            "## Install Result",
            "",
            f"- Attempted: `{install.get('attempted')}`",
            f"- OK: `{install.get('ok')}`",
            f"- Exit code: `{install.get('exit_code')}`",
            f"- Stdout: `{install.get('stdout')}`",
            f"- Stderr: `{install.get('stderr')}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R43 Next Actions")
    decision = payload["hook_decision"]
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
            "- Do not restart the scheduler service manually.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "# Phase 3BB-R43 next safe operator command.\n"
        f"{payload['hook_decision']['operator_next_command']}\n"
    )


def _runner_path(target: CloudBootstrapTarget) -> str:
    return f"{target.app_path.rstrip('/')}/scripts/{RUNNER_SCRIPT_NAME}"


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


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
