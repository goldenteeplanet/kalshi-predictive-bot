from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.phase3ba_status import build_phase3ba_status
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_json,
    _write_manifest,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R1_VERSION = "phase3bb_r1_operator_scheduler_v1"

ACTION_ORDER = (
    "WAIT",
    "START_R5",
    "STOP_OVERRUN_R5",
    "RUN_WEATHER_RANKING",
    "RUN_DASHBOARD_TRUTH",
    "RUN_SETTLEMENT_HEALTH",
    "RUN_CATEGORY_GAP",
    "BLOCKED_BY_ACTIVE_WRITER",
)

REGISTERED_SCHEDULER_COMMANDS = {
    "db-writer-monitor",
    "market-coverage-doctor",
    "phase3an-settlement-health-confirm",
    "phase3ba-r2-weather-ranking-activation",
    "phase3ba-status",
    "phase3bb-r1-operator-scheduler",
    "phase3bb-r2-weather-fast-lane",
    "phase3bb-r3-free-source-inventory",
    "phase3bb-r4-economic-parser-backfill",
    "phase3bb-r6-sports-provenance-repair",
    "phase3bb-r7-news-event-discovery",
    "phase3bb-r8-unified-paper-gate",
    "phase3bb-r9-learning-acceleration",
    "phase3bb-r10-cloud-readiness-decision",
    "phase3bb-r11-codex-cloud-bridge",
    "phase3bb-r12-cloud-bootstrap-verification",
    "phase3bb-r13-cloud-scheduler-adoption",
    "phase3bb-r14-cloud-service-plan",
    "phase3bb-r15-cloud-service-install-review",
    "phase3bb-r16-cloud-service-install-handoff",
    "phase3bc-r5-unattended-guard",
    "phase3bc-r5-unattended-start",
}

R5_START_COMMANDS = {"phase3bc-r5-unattended-start"}

FORBIDDEN_COMMAND_FRAGMENTS = (
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
)

ACTION_COMMANDS = {
    "WAIT": "kalshi-bot db-writer-monitor --json",
    "BLOCKED_BY_ACTIVE_WRITER": "kalshi-bot db-writer-monitor --json",
    "START_R5": "kalshi-bot phase3bc-r5-unattended-start --output-dir reports/phase3bc_r5",
    "STOP_OVERRUN_R5": (
        "kalshi-bot phase3bc-r5-unattended-guard --output-dir reports/phase3bc_r5 "
        "--stop-overrun"
    ),
    "RUN_WEATHER_RANKING": (
        "kalshi-bot db-writer-monitor --json\n"
        "kalshi-bot phase3bb-r2-weather-fast-lane --output-dir "
        "reports/phase3bb_r2 --reports-dir reports --limit 100"
    ),
    "RUN_DASHBOARD_TRUTH": (
        "kalshi-bot phase3ba-status --output-dir reports/phase3ba_status "
        "--reports-dir reports"
    ),
    "RUN_SETTLEMENT_HEALTH": (
        "kalshi-bot phase3an-settlement-health-confirm --output-dir "
        "reports/phase3an_settlement_health --reports-dir reports --max-records 5"
    ),
    "RUN_CATEGORY_GAP": (
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage"
    ),
}


@dataclass(frozen=True)
class Phase3BBR1OperatorSchedulerArtifacts:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    operator_next_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r1_operator_scheduler_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r1"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BBR1OperatorSchedulerArtifacts:
    payload = build_phase3bb_r1_operator_scheduler(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "operator_scheduler.json"
    operator_next_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(json_path, payload)
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    operator_next_command_path.write_text(_render_operator_script(payload), encoding="utf-8")
    try:
        operator_next_command_path.chmod(operator_next_command_path.stat().st_mode | 0o111)
    except OSError:
        pass
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [executive_summary_path, json_path, operator_next_command_path, next_actions_path],
    )
    return Phase3BBR1OperatorSchedulerArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        operator_next_command_path=operator_next_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r1_operator_scheduler(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bb_r1"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=utc_now().isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r1-operator-scheduler",
        "argv": command_args or [],
    }
    status = build_phase3ba_status(
        session,
        output_dir=reports_dir / "phase3ba_status",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r1-operator-scheduler", "embedded-phase3ba-status"],
    )
    writer = db_writer_monitor(settings=resolved)
    r5_status = status.get("r5_status") or {}
    artifact_statuses = {
        **(status.get("artifact_statuses") or {}),
        **_scheduler_artifact_statuses(reports_dir),
    }
    dashboard_truth = status.get("dashboard_truth") or {}
    weather = _weather_readiness(reports_dir, status)
    stale_artifacts = _stale_artifacts(artifact_statuses)
    pending_jobs = _pending_writer_capable_jobs(
        writer=writer,
        r5_status=r5_status,
        weather=weather,
        artifact_statuses=artifact_statuses,
    )
    next_action = choose_scheduler_action(
        writer=writer,
        r5_status=r5_status,
        weather=weather,
        artifact_statuses=artifact_statuses,
        pending_writer_capable_jobs=pending_jobs,
    )
    command_checks = command_checks_for_scheduler(
        next_action["command"],
        r5_running=_r5_running(r5_status),
    )
    safety_flags = {
        **_safety_flags(),
        "status_report_only": True,
        "starts_duplicate_r5_watcher": command_checks["duplicate_r5_start_risk"],
        "installs_systemd_services": False,
    }
    acceptance = {
        "one_command_tells_operator_what_to_do": True,
        "missing_command_references": len(command_checks["unregistered_commands"]),
        "missing_command_references_zero": not command_checks["unregistered_commands"],
        "duplicate_r5_risk": int(command_checks["duplicate_r5_start_risk"]),
        "duplicate_r5_risk_zero": not command_checks["duplicate_r5_start_risk"],
        "no_paper_live_demo_orders": (
            not command_checks["contains_forbidden_trade_command"]
            and not safety_flags["creates_paper_trades"]
            and not safety_flags["places_exchange_orders"]
        ),
        "does_not_recommend_second_r5": not command_checks["duplicate_r5_start_risk"],
        "recommended_action_is_allowed": next_action["action"] in ACTION_ORDER,
    }
    return {
        **metadata,
        "phase": "3BB-R1",
        "phase_version": PHASE3BB_R1_VERSION,
        "mode": "PAPER_READ_ONLY_OPERATOR_SCHEDULER",
        "writer": writer,
        "r5_status": r5_status,
        "dashboard_truth": dashboard_truth,
        "weather_readiness": weather,
        "pending_writer_capable_jobs": pending_jobs,
        "stale_artifacts": stale_artifacts,
        "artifact_statuses": artifact_statuses,
        "next_action": next_action,
        "command_checks": command_checks,
        "systemd_examples": systemd_service_examples(),
        "safety_flags": safety_flags,
        "acceptance": acceptance,
    }


def choose_scheduler_action(
    *,
    writer: dict[str, Any],
    r5_status: dict[str, Any],
    weather: dict[str, Any],
    artifact_statuses: dict[str, Any],
    pending_writer_capable_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    writer_active = _writer_active(writer)
    r5_running = _r5_running(r5_status)
    r5_should_stop = _r5_should_stop(r5_status)
    if writer_active:
        if _writer_is_r5(writer) and r5_should_stop:
            return _action(
                "STOP_OVERRUN_R5",
                "Active writer is the guarded R5 watcher and the guard says should_stop=true.",
            )
        return _action(
            "BLOCKED_BY_ACTIVE_WRITER",
            "A DB writer is active; do not start write-capable work.",
            clearly_wait=True,
        )
    if r5_running and r5_should_stop:
        return _action(
            "STOP_OVERRUN_R5",
            "R5 is running but its guard says should_stop=true.",
        )
    if not r5_running:
        return _action(
            "START_R5",
            "No running guarded R5 watcher was detected.",
            starts_guarded_background_watcher=True,
        )
    if weather.get("ranking_job_due"):
        return _action(
            "RUN_WEATHER_RANKING",
            "Weather has linked/source-backed rows that need fresh ranked opportunity truth.",
            requires_writer_gate_clear=True,
        )
    if _dashboard_truth_due(artifact_statuses):
        return _action(
            "RUN_DASHBOARD_TRUTH",
            "Dashboard truth or unified paper-ready truth is missing or stale.",
        )
    if _settlement_health_due(artifact_statuses):
        return _action(
            "RUN_SETTLEMENT_HEALTH",
            "Settlement health has no fresh bounded confirmation artifact.",
        )
    if _category_gap_due(artifact_statuses):
        return _action(
            "RUN_CATEGORY_GAP",
            "Category gap/backlog truth is missing or stale.",
        )
    if any(job.get("blocked_by_writer") for job in pending_writer_capable_jobs):
        return _action(
            "BLOCKED_BY_ACTIVE_WRITER",
            "A pending writer-capable job is blocked by the current writer.",
            clearly_wait=True,
        )
    return _action(
        "WAIT",
        "One R5 watcher is active and no bounded scheduler lane is due.",
        clearly_wait=True,
    )


def command_checks_for_scheduler(command: str, *, r5_running: bool) -> dict[str, Any]:
    names = _command_names(command)
    unregistered = [name for name in names if name not in REGISTERED_SCHEDULER_COMMANDS]
    forbidden = [
        fragment for fragment in FORBIDDEN_COMMAND_FRAGMENTS if fragment in command.lower()
    ]
    r5_start_commands = [name for name in names if name in R5_START_COMMANDS]
    return {
        "commands": _command_lines(command),
        "command_names": names,
        "unregistered_commands": unregistered,
        "all_recommended_commands_registered": not unregistered,
        "forbidden_fragments": forbidden,
        "contains_forbidden_trade_command": bool(forbidden),
        "r5_start_commands": r5_start_commands,
        "duplicate_r5_start_risk": bool(r5_start_commands) and r5_running,
    }


def systemd_service_examples() -> list[dict[str, str]]:
    return [
        {
            "name": "kalshi-r5-watcher.service",
            "install": "example_only_not_installed",
            "unit": "\n".join(
                [
                    "[Unit]",
                    "Description=Kalshi paper-only guarded R5 watcher",
                    "After=network-online.target",
                    "",
                    "[Service]",
                    "WorkingDirectory=/opt/kalshi-predictive-bot",
                    "EnvironmentFile=/opt/kalshi-predictive-bot/.env",
                    "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot "
                    "phase3bc-r5-unattended-start --output-dir reports/phase3bc_r5",
                    "Restart=on-failure",
                    "",
                    "[Install]",
                    "WantedBy=multi-user.target",
                ]
            ),
        },
        {
            "name": "kalshi-operator-scheduler.service",
            "install": "example_only_not_installed",
            "unit": "\n".join(
                [
                    "[Unit]",
                    "Description=Kalshi paper-only operator scheduler",
                    "After=network-online.target",
                    "",
                    "[Service]",
                    "Type=oneshot",
                    "WorkingDirectory=/opt/kalshi-predictive-bot",
                    "EnvironmentFile=/opt/kalshi-predictive-bot/.env",
                    "ExecStart=/opt/kalshi-predictive-bot/.venv/bin/kalshi-bot "
                    "phase3bb-r1-operator-scheduler --output-dir reports/phase3bb_r1 "
                    "--reports-dir reports",
                ]
            ),
        },
        {
            "name": "kalshi-operator-scheduler.timer",
            "install": "example_only_not_installed",
            "unit": "\n".join(
                [
                    "[Timer]",
                    "OnBootSec=5min",
                    "OnUnitActiveSec=15min",
                    "Persistent=true",
                ]
            ),
        },
    ]


def _action(
    action: str,
    reason: str,
    *,
    clearly_wait: bool = False,
    requires_writer_gate_clear: bool = False,
    starts_guarded_background_watcher: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "command": ACTION_COMMANDS[action],
        "reason": reason,
        "clearly_wait": clearly_wait,
        "requires_writer_gate_clear": requires_writer_gate_clear,
        "starts_guarded_background_watcher": starts_guarded_background_watcher,
        "allow_paper_trade_creation": False,
        "allow_live_or_demo_orders": False,
    }


def _weather_readiness(reports_dir: Path, status: dict[str, Any]) -> dict[str, Any]:
    summary = status.get("summary") or {}
    weather_fast_lane = _read_json(reports_dir / "phase3bb" / "weather_fast_lane.json")
    weather_r2 = _read_json(
        reports_dir / "phase3ba_r2" / "weather_ranking_activation.json"
    )
    artifact_statuses = status.get("artifact_statuses") or {}
    weather_artifact = artifact_statuses.get("weather_ranking_activation") or {}
    current_rows = _to_int(summary.get("weather_current_rows"))
    first_blocker = str(summary.get("weather_first_blocker") or "")
    active_linked = _to_int(weather_fast_lane.get("active_linked_weather_rows"))
    rows_with_forecasts = _to_int(weather_fast_lane.get("weather_rows_with_forecasts"))
    rows_with_rankings = _to_int(weather_fast_lane.get("weather_rows_with_rankings"))
    r2_summary = weather_r2.get("summary") or {}
    rows_missing_rankings = _to_int(r2_summary.get("ranking_gap_rows"))
    ranking_artifact_stale = weather_artifact.get("freshness") in {
        "MISSING",
        "HISTORICAL_STALE",
        "UNKNOWN_AGE",
    }
    ranking_job_due = bool(
        current_rows > 0
        and (
            ranking_artifact_stale
            or rows_missing_rankings > 0
            or first_blocker in {"RANKING_MISSING", "EV_NOT_POSITIVE", "SNAPSHOT_MISSING"}
            or bool(weather_fast_lane.get("find_opportunities_weather_v2_ready_to_run"))
        )
    )
    return {
        "current_rows": current_rows,
        "paper_ready": bool(summary.get("weather_paper_ready")),
        "paper_ready_rows": _to_int(summary.get("weather_paper_ready_rows")),
        "first_blocker": first_blocker or "UNKNOWN",
        "active_linked_weather_rows": active_linked,
        "rows_with_forecasts": rows_with_forecasts,
        "rows_with_rankings": rows_with_rankings,
        "rows_missing_rankings": rows_missing_rankings,
        "ranking_artifact_freshness": weather_artifact.get("freshness"),
        "ranking_job_due": ranking_job_due,
        "source_reports": {
            "weather_fast_lane": str(reports_dir / "phase3bb" / "weather_fast_lane.json"),
            "weather_ranking_activation": str(
                reports_dir / "phase3ba_r2" / "weather_ranking_activation.json"
            ),
        },
    }


def _pending_writer_capable_jobs(
    *,
    writer: dict[str, Any],
    r5_status: dict[str, Any],
    weather: dict[str, Any],
    artifact_statuses: dict[str, Any],
) -> list[dict[str, Any]]:
    writer_active = _writer_active(writer)
    jobs = [
        {
            "action": "RUN_WEATHER_RANKING",
            "command": ACTION_COMMANDS["RUN_WEATHER_RANKING"],
            "writer_capable": True,
            "due": bool(weather.get("ranking_job_due")),
            "blocked_by_writer": writer_active and bool(weather.get("ranking_job_due")),
        },
        {
            "action": "START_R5",
            "command": ACTION_COMMANDS["START_R5"],
            "writer_capable": True,
            "due": not _r5_running(r5_status),
            "blocked_by_writer": writer_active and not _r5_running(r5_status),
        },
        {
            "action": "RUN_CATEGORY_GAP",
            "command": ACTION_COMMANDS["RUN_CATEGORY_GAP"],
            "writer_capable": False,
            "due": _category_gap_due(artifact_statuses),
            "blocked_by_writer": False,
        },
    ]
    return jobs


def _dashboard_truth_due(artifact_statuses: dict[str, Any]) -> bool:
    paper = artifact_statuses.get("paper_ready_truth") or {}
    weather = artifact_statuses.get("weather_paper_gate") or {}
    return paper.get("freshness") in {"MISSING", "HISTORICAL_STALE", "UNKNOWN_AGE"} or (
        weather.get("exists") is False
    )


def _settlement_health_due(artifact_statuses: dict[str, Any]) -> bool:
    settlement = artifact_statuses.get("settlement_health") or {}
    return settlement.get("freshness") in {"MISSING", "HISTORICAL_STALE", "UNKNOWN_AGE"}


def _category_gap_due(artifact_statuses: dict[str, Any]) -> bool:
    category = artifact_statuses.get("category_backlog") or {}
    return category.get("freshness") in {"MISSING", "HISTORICAL_STALE", "UNKNOWN_AGE"}


def _stale_artifacts(artifact_statuses: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name, artifact in artifact_statuses.items():
        if artifact.get("freshness") in {"MISSING", "HISTORICAL_STALE", "UNKNOWN_AGE"}:
            rows.append(
                {
                    "name": name,
                    "freshness": artifact.get("freshness"),
                    "path": artifact.get("path"),
                    "generated_at": artifact.get("generated_at"),
                    "age_seconds": artifact.get("age_seconds"),
                }
            )
    return rows


def _scheduler_artifact_statuses(reports_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        "settlement_health": _file_artifact(
            reports_dir / "phase3an" / "settlement_health_confirm.json",
            max_age_seconds=24 * 60 * 60,
        ),
        "market_coverage_doctor": _file_artifact(
            reports_dir / "market_coverage" / "market_coverage_doctor.json",
            max_age_seconds=6 * 60 * 60,
        ),
    }


def _file_artifact(path: Path, *, max_age_seconds: int) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "generated_at": None,
            "age_seconds": None,
            "freshness": "MISSING",
            "size_bytes": 0,
        }
    payload = _read_json(path)
    stat = path.stat()
    age_seconds = int(max(0, utc_now().timestamp() - stat.st_mtime))
    freshness = "CURRENT" if age_seconds <= max_age_seconds else "HISTORICAL_STALE"
    generated_at = payload.get("generated_at")
    if not generated_at:
        generated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
    return {
        "path": str(path),
        "exists": True,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "freshness": freshness,
        "size_bytes": stat.st_size,
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    action = payload["next_action"]
    writer = payload["writer"]
    r5_status = payload["r5_status"]
    weather = payload["weather_readiness"]
    lines = _metadata_lines(payload, "# Phase 3BB-R1 Operator Scheduler")
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Next action: `{action['action']}`",
            f"- Reason: {action['reason']}",
            f"- Active writer: `{_writer_active(writer)}`",
            f"- Writer PID: `{writer.get('current_writer_pid')}`",
            f"- Safe to start write: `{writer.get('safe_to_start_write')}`",
            f"- R5 running: `{_r5_running(r5_status)}`",
            f"- R5 guard status: `{(r5_status.get('guard') or {}).get('status')}`",
            f"- R5 should stop: `{_r5_should_stop(r5_status)}`",
            f"- Weather first blocker: `{weather['first_blocker']}`",
            f"- Weather ranking job due: `{weather['ranking_job_due']}`",
            "",
            "## Operator Next Command",
            "",
            "```bash",
            action["command"],
            "```",
            "",
            "## Stale Artifacts",
            "",
        ]
    )
    if payload["stale_artifacts"]:
        for row in payload["stale_artifacts"]:
            lines.append(
                f"- {row['name']}: `{row['freshness']}` path=`{row['path']}`"
            )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## systemd-Style Examples", ""])
    for example in payload["systemd_examples"]:
        lines.extend(
            [
                f"### {example['name']}",
                "",
                f"- Install status: `{example['install']}`",
                "",
                "```ini",
                example["unit"],
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    action = payload["next_action"]
    checks = payload["command_checks"]
    lines = _metadata_lines(payload, "# Phase 3BB-R1 Next Actions")
    lines.extend(
        [
            "",
            "## One Safe Next Action",
            "",
            f"- Action: `{action['action']}`",
            f"- Reason: {action['reason']}",
            f"- Clearly wait: `{action['clearly_wait']}`",
            f"- Requires writer gate clear: `{action['requires_writer_gate_clear']}`",
            f"- Starts guarded background watcher: `{action['starts_guarded_background_watcher']}`",
            "",
            "```bash",
            action["command"],
            "```",
            "",
            "## Command Registration",
            "",
            f"- Missing command references: `{len(checks['unregistered_commands'])}`",
            f"- Duplicate R5 start risk: `{int(checks['duplicate_r5_start_risk'])}`",
            "- Contains forbidden trade command: "
            f"`{checks['contains_forbidden_trade_command']}`",
        ]
    )
    for command in checks["command_names"]:
        lines.append(f"- Registered command: `{command}`")
    lines.extend(
        [
            "",
            "## Do Not Run",
            "",
            "- Do not create paper trades from this scheduler phase.",
            "- Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "- Do not start a second R5 watcher while one is active.",
            "- Do not install the systemd examples from this report automatically.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_script(payload: dict[str, Any]) -> str:
    action = payload["next_action"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"# Phase 3BB-R1 generated at {payload['generated_at']}",
        f"# Action: {action['action']}",
        f"# Reason: {action['reason']}",
        "# Safety: PAPER / READ-ONLY; no paper trades; no live/demo orders.",
        "",
    ]
    lines.extend(str(action["command"]).splitlines())
    return "\n".join(lines) + "\n"


def _command_lines(command: str) -> list[str]:
    lines = []
    for raw in command.replace("&&", "\n").splitlines():
        line = raw.strip()
        if line.startswith("kalshi-bot "):
            lines.append(line)
    return lines


def _command_names(command: str) -> list[str]:
    names = []
    for line in _command_lines(command):
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) > 1 and parts[0] == "kalshi-bot":
            names.append(parts[1])
    return names


def _writer_active(writer: dict[str, Any]) -> bool:
    return bool(writer.get("current_writer_pid"))


def _writer_is_r5(writer: dict[str, Any]) -> bool:
    command = str(writer.get("current_writer_command") or "").lower()
    return "phase3bc-r5" in command and "phase3bc-r5-status" not in command


def _r5_running(r5_status: dict[str, Any]) -> bool:
    process_status = str((r5_status.get("process") or {}).get("status") or "").upper()
    guard_status = str((r5_status.get("guard") or {}).get("status") or "").upper()
    return process_status == "RUNNING" or guard_status in {"RUNNING", "OVERRUNNING"}


def _r5_should_stop(r5_status: dict[str, Any]) -> bool:
    guard = r5_status.get("guard") or {}
    return bool(guard.get("should_stop")) or str(guard.get("status") or "").upper() == "OVERRUNNING"


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
