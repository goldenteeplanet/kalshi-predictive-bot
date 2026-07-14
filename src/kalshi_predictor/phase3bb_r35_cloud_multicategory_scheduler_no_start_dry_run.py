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
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BB_R35_VERSION = "phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r35")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R34_MAX_AGE_MINUTES = 60
READY_R34_STATUS = "READY_FOR_NO_START_SCHEDULER_DRY_RUN"

FORBIDDEN_DRAFT_FRAGMENTS = (
    "accelerate-learning",
    "autopilot-once",
    "autopilot-run",
    "cancel-order",
    "create-paper-trade",
    "demo-order",
    "live-order",
    "paper-trade-create",
    "place-order",
    "replace-order",
    "submit-order",
    "systemctl enable",
    "systemctl restart",
    "systemctl start",
    "tailscale funnel",
)


@dataclass(frozen=True)
class Phase3BBR35CloudMulticategorySchedulerNoStartDryRunArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    checks_csv_path: Path
    job_plan_csv_path: Path
    service_draft_path: Path
    timer_draft_path: Path
    runner_draft_path: Path
    no_start_dry_run_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    r34_max_age_minutes: int = DEFAULT_R34_MAX_AGE_MINUTES,
) -> Phase3BBR35CloudMulticategorySchedulerNoStartDryRunArtifacts:
    payload = build_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        r34_max_age_minutes=r34_max_age_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_multicategory_scheduler_no_start_dry_run.md"
    json_path = output_dir / "cloud_multicategory_scheduler_no_start_dry_run.json"
    checks_csv_path = output_dir / "scheduler_dry_run_checks.csv"
    job_plan_csv_path = output_dir / "scheduler_job_plan.csv"
    service_draft_path = output_dir / "kalshi-multicategory-refresh-scheduler.service.draft"
    timer_draft_path = output_dir / "kalshi-multicategory-refresh-scheduler.timer.draft"
    runner_draft_path = output_dir / "kalshi-multicategory-refresh-runner.sh.draft"
    no_start_dry_run_path = output_dir / "no_start_dry_run.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_rows_csv(checks_csv_path, payload["dry_run_checks"])
    _write_rows_csv(job_plan_csv_path, payload["scheduler_job_plan"])
    service_draft_path.write_text(payload["service_draft"], encoding="utf-8")
    timer_draft_path.write_text(payload["timer_draft"], encoding="utf-8")
    runner_draft_path.write_text(payload["runner_draft"], encoding="utf-8")
    _mark_executable(runner_draft_path)
    no_start_dry_run_path.write_text(_render_no_start_dry_run(payload), encoding="utf-8")
    _mark_executable(no_start_dry_run_path)
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            checks_csv_path,
            job_plan_csv_path,
            service_draft_path,
            timer_draft_path,
            runner_draft_path,
            no_start_dry_run_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR35CloudMulticategorySchedulerNoStartDryRunArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        checks_csv_path=checks_csv_path,
        job_plan_csv_path=job_plan_csv_path,
        service_draft_path=service_draft_path,
        timer_draft_path=timer_draft_path,
        runner_draft_path=runner_draft_path,
        no_start_dry_run_path=no_start_dry_run_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r35_cloud_multicategory_scheduler_no_start_dry_run(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    r34_max_age_minutes: int = DEFAULT_R34_MAX_AGE_MINUTES,
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
        "command": "kalshi-bot phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run",
        "argv": command_args or [],
    }
    r34_path = reports_dir / "phase3bb_r34" / "cloud_multicategory_refresh_scheduler_review.json"
    r34 = _read_json(r34_path)
    r34_age_seconds = _artifact_age_seconds(r34, now)
    jobs = _normalise_jobs(r34.get("refresh_jobs") or [])
    service_draft = _render_service_draft()
    timer_draft = _render_timer_draft()
    runner_draft = _render_runner_draft(jobs)
    checks = _dry_run_checks(
        r34=r34,
        jobs=jobs,
        service_draft=service_draft,
        timer_draft=timer_draft,
        runner_draft=runner_draft,
        r34_age_seconds=r34_age_seconds,
        r34_max_age_minutes=r34_max_age_minutes,
    )
    decision = _decision(r34, jobs, checks)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_start": True,
        "no_install": True,
        "scheduler_no_start_dry_run_only": True,
        "installs_systemd_services": False,
        "writes_service_files_to_system": False,
        "starts_scheduler": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "remote_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
        "systemctl_mutating_commands_executed": 0,
        "tailscale_mutating_commands_executed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "thresholds_lowered": False,
    }
    return {
        **metadata,
        "phase": "3BB-R35-CLOUD-MULTICATEGORY-SCHEDULER-NO-START-DRY-RUN",
        "phase_version": PHASE3BB_R35_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_NO_START_DRY_RUN",
        "reports_dir": str(reports_dir),
        "r34_artifact_path": str(r34_path),
        "r34_age_seconds": r34_age_seconds,
        "r34_max_age_minutes": r34_max_age_minutes,
        "r34_scheduler_decision": r34.get("scheduler_decision") or {},
        "scheduler_job_plan": jobs,
        "service_draft": service_draft,
        "timer_draft": timer_draft,
        "runner_draft": runner_draft,
        "dry_run_checks": checks,
        "dry_run_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _normalise_jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = []
    for row in sorted(rows, key=lambda item: int(item.get("priority") or 999)):
        command = str(row.get("command") or "").strip()
        jobs.append(
            {
                "job_id": str(row.get("job_id") or "unknown"),
                "category": str(row.get("category") or "unknown"),
                "cadence_minutes": int(row.get("cadence_minutes") or 0),
                "priority": int(row.get("priority") or 999),
                "command": command,
                "remote_command": _remote_command(command),
                "writer_capable": _boolish(row.get("writer_capable")),
                "requires_db_writer_gate": _boolish(row.get("requires_db_writer_gate")),
                "max_runtime_seconds": int(row.get("max_runtime_seconds") or 90),
                "enabled_in_draft": _boolish(row.get("enabled_in_draft"), default=True),
                "purpose": str(row.get("purpose") or ""),
            }
        )
    return jobs


def _dry_run_checks(
    *,
    r34: dict[str, Any],
    jobs: list[dict[str, Any]],
    service_draft: str,
    timer_draft: str,
    runner_draft: str,
    r34_age_seconds: float | None,
    r34_max_age_minutes: int,
) -> list[dict[str, Any]]:
    r34_decision = r34.get("scheduler_decision") or {}
    max_age_seconds = max(1, r34_max_age_minutes) * 60
    draft_text = "\n".join([service_draft, timer_draft, runner_draft])
    forbidden_hits = sorted(
        {fragment for fragment in FORBIDDEN_DRAFT_FRAGMENTS if fragment in draft_text.lower()}
    )
    writer_jobs_without_gate = [
        row["job_id"]
        for row in jobs
        if row.get("writer_capable") and not row.get("requires_db_writer_gate")
    ]
    job_commands = [str(row.get("command") or "") for row in jobs]
    duplicate_r5_start_jobs = [
        row["job_id"] for row in jobs if "phase3bc-r5-unattended-start" in row["command"]
    ]
    return [
        _check("r34_artifact_present", bool(r34), "R34 scheduler review artifact exists."),
        _check(
            "r34_recent_enough",
            r34_age_seconds is not None and r34_age_seconds <= max_age_seconds,
            f"R34 artifact age is {r34_age_seconds} seconds.",
        ),
        _check(
            "r34_ready_for_no_start_dry_run",
            r34_decision.get("status") == READY_R34_STATUS
            and bool(r34_decision.get("review_passed")),
            f"R34 status is {r34_decision.get('status')}.",
        ),
        _check("job_plan_present", bool(jobs), f"Job count is {len(jobs)}."),
        _check(
            "writer_jobs_have_db_writer_gate",
            not writer_jobs_without_gate,
            (
                "missing_gate="
                f"{','.join(writer_jobs_without_gate) if writer_jobs_without_gate else 'none'}."
            ),
        ),
        _check(
            "no_duplicate_r5_start_job",
            not duplicate_r5_start_jobs,
            (
                "duplicate_start_jobs="
                f"{','.join(duplicate_r5_start_jobs) if duplicate_r5_start_jobs else 'none'}."
            ),
        ),
        _check(
            "no_forbidden_trade_or_service_fragments",
            not forbidden_hits,
            f"forbidden={','.join(forbidden_hits) if forbidden_hits else 'none'}.",
        ),
        _check(
            "runner_has_writer_gate",
            "db-writer-monitor --json" in runner_draft
            and "Writer active; skip writer-gated job" in runner_draft,
            "Runner draft checks db-writer-monitor before writer-capable jobs.",
        ),
        _check(
            "runner_handles_midrun_writer_busy",
            "Writer became active during" in runner_draft
            and "Status: BUSY_WRITER|Database is busy" in runner_draft,
            "Runner draft treats mid-run writer-busy output as a clean retry skip.",
        ),
        _check(
            "systemd_drafts_are_no_install_artifacts",
            "WantedBy=multi-user.target" in timer_draft
            and "ExecStart=" in service_draft
            and "systemctl" not in draft_text.lower(),
            "Service/timer drafts exist locally and contain no systemctl mutation.",
        ),
        _check(
            "scheduled_commands_are_kalshi_bot_only",
            all(command.startswith("kalshi-bot ") for command in job_commands),
            "Every job command starts with kalshi-bot.",
        ),
    ]


def _decision(
    r34: dict[str, Any],
    jobs: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r34_decision = r34.get("scheduler_decision") or {}
    if failed:
        status = "BLOCKED_SCHEDULER_NO_START_DRY_RUN"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R35 - Resolve Scheduler No-Start Dry Run"
        command = (
            "kalshi-bot phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run "
            "--output-dir reports/phase3bb_r35 --reports-dir reports"
        )
    else:
        status = "READY_FOR_OPERATOR_APPROVED_SCHEDULER_INSTALL_HANDOFF"
        reason = (
            "No-start dry run passed; local-only systemd timer/service/runner drafts are "
            "ready for an operator-approved install handoff."
        )
        next_step = "Phase 3BB-R36 - Operator-Approved Cloud Scheduler Install Handoff"
        command = (
            "Review reports/phase3bb_r35/*draft and then run R36 to create the "
            "approval-gated install handoff."
        )
    return {
        "status": status,
        "dry_run_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "job_count": len(jobs),
        "writer_gated_job_count": sum(1 for row in jobs if row.get("writer_capable")),
        "r34_status": r34_decision.get("status"),
        "r5_pid": r34_decision.get("r5_pid"),
        "watch_state": r34_decision.get("watch_state"),
        "paper_ready_candidates": r34_decision.get("paper_ready_candidates"),
        "primary_reason": reason,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _render_service_draft() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Kalshi paper-only multi-category refresh scheduler",
            "After=network-online.target kalshi-r5-watcher.service",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            "User=kalshi",
            "WorkingDirectory=/opt/kalshi-predictive-bot",
            "EnvironmentFile=/etc/kalshi-bot/kalshi-bot.env",
            "ExecStart=/opt/kalshi-predictive-bot/scripts/"
            "kalshi-multicategory-refresh-runner.sh",
            "TimeoutStartSec=900",
            "",
        ]
    )


def _render_timer_draft() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Kalshi paper-only multi-category refresh scheduler timer",
            "",
            "[Timer]",
            "OnBootSec=10min",
            "OnUnitActiveSec=15min",
            "RandomizedDelaySec=60",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _render_runner_draft(jobs: list[dict[str, Any]]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "cd /opt/kalshi-predictive-bot",
        "LOCK_FILE=/tmp/kalshi-multicategory-refresh-scheduler.lock",
        "KALSHI_BOT=${KALSHI_BOT:-.venv/bin/kalshi-bot}",
        "exec 9>\"${LOCK_FILE}\"",
        "if ! flock -n 9; then",
        "  echo '[phase3bb-r35] scheduler already running; exiting cleanly'",
        "  exit 0",
        "fi",
        "",
        "writer_clear() {",
        "  local monitor_output",
        "  if ! monitor_output=$(\"${KALSHI_BOT}\" db-writer-monitor --json 2>/dev/null); then",
        "    echo '[phase3bb-r35] db-writer-monitor failed; skip writer-gated job' >&2",
        "    return 1",
        "  fi",
        "  MONITOR_OUTPUT=\"${monitor_output}\" python3 -c '",
        "import json, os, re, sys",
        "text = os.environ.get(\"MONITOR_OUTPUT\", \"\")",
        "text = re.sub(r\"[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]\", \"\", text)",
        "decoder = json.JSONDecoder()",
        "for idx, char in enumerate(text):",
        "    if char != \"{\":",
        "        continue",
        "    try:",
        "        data, _end = decoder.raw_decode(text[idx:])",
        "    except json.JSONDecodeError:",
        "        continue",
        "    raise SystemExit(0 if data.get(\"safe_to_start_write\") else 1)",
        "print(\"[phase3bb-r35] db-writer-monitor JSON parse failed; skip writer-gated job\", file=sys.stderr)",
        "raise SystemExit(1)'",
        "}",
        "",
        "run_job() {",
        "  local job_id=\"$1\"",
        "  local writer_capable=\"$2\"",
        "  shift 2",
        "  if [[ \"${writer_capable}\" == \"true\" ]] && ! writer_clear; then",
        "    echo \"[phase3bb-r35] Writer active; skip writer-gated job ${job_id}\"",
        "    return 0",
        "  fi",
        "  echo \"[phase3bb-r35] running ${job_id}\"",
        "  local output status",
        "  set +e",
        "  output=$(\"$@\" 2>&1)",
        "  status=$?",
        "  set -e",
        "  if [[ -n \"${output}\" ]]; then",
        "    printf '%s\\n' \"${output}\"",
        "  fi",
        "  if [[ \"${status}\" -ne 0 ]]; then",
        "    if [[ \"${writer_capable}\" == \"true\" ]] && printf '%s\\n' \"${output}\" | grep -Eq 'Status: BUSY_WRITER|Database is busy|safe_to_start_write[^A-Za-z0-9_:-]*false'; then",
        "      echo \"[phase3bb-r35] Writer became active during ${job_id}; clean skip for retry\"",
        "      return 0",
        "    fi",
        "    return \"${status}\"",
        "  fi",
        "}",
    ]
    for row in jobs:
        command_parts = shlex.split(row["remote_command"])
        quoted = " ".join(shlex.quote(part) for part in command_parts)
        writer_capable = "true" if row["writer_capable"] else "false"
        lines.extend(
            [
                "",
                f"# cadence_minutes={row['cadence_minutes']} category={row['category']}",
                f"run_job {shlex.quote(row['job_id'])} {writer_capable} {quoted}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _render_no_start_dry_run(payload: dict[str, Any]) -> str:
    decision = payload["dry_run_decision"]
    checks = payload["dry_run_checks"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "echo '[phase3bb-r35] no-start dry run only'",
        "echo '[phase3bb-r35] no install, no systemctl, no refresh jobs'",
        f"echo '[phase3bb-r35] decision: {decision['status']}'",
    ]
    for row in checks:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"echo '[phase3bb-r35] {marker} {row['check']}'")
    lines.extend(
        [
            "",
            "echo '[phase3bb-r35] generated local draft files are ready for review'",
            "",
        ]
    )
    return "\n".join(lines)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R35 Scheduler No-Start Dry Run")
    decision = payload["dry_run_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Dry run passed: `{decision['dry_run_passed']}`",
            f"- Jobs in runner draft: `{decision['job_count']}`",
            f"- Writer-gated jobs: `{decision['writer_gated_job_count']}`",
            f"- R34 status: `{decision['r34_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- Watch state: `{decision['watch_state']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- This phase writes local draft artifacts only.",
            "- It does not install, enable, start, or restart scheduler services.",
            "- It does not run refresh jobs.",
            "- It does not start or stop R5.",
            "- It does not create paper trades or live/demo orders.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R35 Scheduler Dry Run Detail")
    decision = payload["dry_run_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["dry_run_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Job Plan", ""])
    for row in payload["scheduler_job_plan"]:
        gate = "writer-gated" if row["writer_capable"] else "read-only/report"
        lines.append(
            f"- `{row['job_id']}` ({row['category']}, every "
            f"{row['cadence_minutes']}m, {gate}): `{row['command']}`"
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["dry_run_decision"]["operator_next_command"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"printf '%s\\n' {_shell_quote(command)}",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R35 Next Actions")
    decision = payload["dry_run_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Command/action: `{decision['operator_next_command']}`",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not install the scheduler until an approval-gated handoff exists.",
            "- Do not start/restart scheduler services manually from this phase.",
            "- Do not start a duplicate R5 watcher.",
            "- Do not create paper trades from this dry run.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _remote_command(command: str) -> str:
    parts = shlex.split(command)
    if parts and parts[0] == "kalshi-bot":
        parts[0] = ".venv/bin/kalshi-bot"
    return " ".join(shlex.quote(part) for part in parts)


def _artifact_age_seconds(payload: dict[str, Any], now: Any) -> float | None:
    parsed = parse_datetime(payload.get("generated_at"))
    if parsed is None:
        return None
    return max(0.0, round((now - parsed).total_seconds(), 3))


def _boolish(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
