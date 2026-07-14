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
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import ProbeRunner
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    DEFAULT_SERVICE_NAME,
)
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    DEFAULT_UI_TIMEOUT_SECONDS,
    UiApiProbeRunner,
)
from kalshi_predictor.phase3bb_r33_cloud_paper_only_operations_readiness import (
    build_phase3bb_r33_cloud_paper_only_operations_readiness,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R34_VERSION = "phase3bb_r34_cloud_multicategory_refresh_scheduler_review_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r34")
READY_R33_STATUSES = {"PAPER_ONLY_MONITORING_READY", "PAPER_ONLY_OPERATOR_REVIEW_READY"}

REGISTERED_SCHEDULER_COMMANDS = {
    "db-writer-monitor",
    "market-coverage-doctor",
    "market-legs-parse",
    "phase3ba-status",
    "phase3az-r12-weather-activation-preview",
    "phase3bb-r2-weather-fast-lane",
    "phase3bb-r3-free-source-inventory",
    "phase3bb-r4-economic-parser-backfill",
    "phase3bb-r5-usda-source-activation",
    "phase3bb-r6-sports-provenance-repair",
    "phase3bb-r7-news-event-discovery",
    "phase3bb-r8-unified-paper-gate",
    "phase3bb-r33-cloud-paper-only-operations-readiness",
    "phase3bb-r60-weather-next-window-lead-time-scheduler-repair",
    "phase3bc-r5-status",
    "sync-markets",
}

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
    "tailscale funnel",
    "systemctl start",
    "systemctl restart",
    "systemctl enable",
)


@dataclass(frozen=True)
class Phase3BBR34CloudMulticategoryRefreshSchedulerReviewArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    jobs_csv_path: Path
    checks_csv_path: Path
    scheduler_draft_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r34_cloud_multicategory_refresh_scheduler_review_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    scheduler_probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
) -> Phase3BBR34CloudMulticategoryRefreshSchedulerReviewArtifacts:
    payload = build_phase3bb_r34_cloud_multicategory_refresh_scheduler_review(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        private_base_url=private_base_url,
        ui_timeout_seconds=ui_timeout_seconds,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        scheduler_probe_runner=scheduler_probe_runner,
        ui_probe_runner=ui_probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_multicategory_refresh_scheduler_review.md"
    json_path = output_dir / "cloud_multicategory_refresh_scheduler_review.json"
    jobs_csv_path = output_dir / "refresh_jobs.csv"
    checks_csv_path = output_dir / "scheduler_checks.csv"
    scheduler_draft_path = output_dir / "scheduler_draft.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_rows_csv(jobs_csv_path, payload["refresh_jobs"])
    _write_rows_csv(checks_csv_path, payload["scheduler_checks"])
    scheduler_draft_path.write_text(_render_scheduler_draft(payload), encoding="utf-8")
    _mark_executable(scheduler_draft_path)
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            jobs_csv_path,
            checks_csv_path,
            scheduler_draft_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR34CloudMulticategoryRefreshSchedulerReviewArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        jobs_csv_path=jobs_csv_path,
        checks_csv_path=checks_csv_path,
        scheduler_draft_path=scheduler_draft_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r34_cloud_multicategory_refresh_scheduler_review(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    scheduler_probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
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
        "command": "kalshi-bot phase3bb-r34-cloud-multicategory-refresh-scheduler-review",
        "argv": command_args or [],
    }
    r33 = build_phase3bb_r33_cloud_paper_only_operations_readiness(
        session,
        output_dir=output_dir / "r33_operations_readiness",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r33-cloud-paper-only-operations-readiness"],
        private_base_url=private_base_url,
        ui_timeout_seconds=ui_timeout_seconds,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        scheduler_probe_runner=scheduler_probe_runner,
        ui_probe_runner=ui_probe_runner,
    )
    scorecard_rows = _read_category_scorecard(reports_dir)
    refresh_jobs = _refresh_jobs(scorecard_rows)
    checks = _scheduler_checks(r33, refresh_jobs)
    decision = _decision(r33, refresh_jobs, checks)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "scheduler_review_only": True,
        "installs_systemd_services": False,
        "writes_service_files": False,
        "starts_scheduler": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
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
        "phase": "3BB-R34-CLOUD-MULTICATEGORY-REFRESH-SCHEDULER-REVIEW",
        "phase_version": PHASE3BB_R34_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_REVIEW_NO_INSTALL",
        "reports_dir": str(reports_dir),
        "r33_operations_readiness": r33,
        "category_scorecard_source": str(reports_dir / "phase3bb_r3" / "category_scorecard.csv"),
        "category_scorecard_rows": scorecard_rows,
        "refresh_jobs": refresh_jobs,
        "scheduler_checks": checks,
        "scheduler_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _read_category_scorecard(reports_dir: Path) -> list[dict[str, Any]]:
    path = reports_dir / "phase3bb_r3" / "category_scorecard.csv"
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _refresh_jobs(scorecard_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scorecard = {str(row.get("category")): row for row in scorecard_rows}
    weather = scorecard.get("weather", {})
    economic = scorecard.get("economic", {})
    news = scorecard.get("news", {})
    sports = scorecard.get("sports", {})
    crypto = scorecard.get("crypto", {})
    return [
        _job(
            job_id="operations_readiness_monitor",
            category="system",
            cadence_minutes=15,
            priority=10,
            command=(
                "kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness "
                "--output-dir reports/phase3bb_r33 --reports-dir reports"
            ),
            purpose=(
                "Keep cloud UI, systemd R5, paper-only guardrails, and dashboard "
                "truth checked."
            ),
            writer_capable=False,
            source_state="R33",
        ),
        _job(
            job_id="unified_paper_gate",
            category="all",
            cadence_minutes=15,
            priority=20,
            command=(
                "kalshi-bot phase3bb-r8-unified-paper-gate "
                "--output-dir reports/phase3bb_r8 --reports-dir reports"
            ),
            purpose="Refresh current category-aware paper-ready blockers without creating trades.",
            writer_capable=False,
            source_state="R8",
        ),
        _job(
            job_id="weather_current_catalog_refresh",
            category="weather",
            cadence_minutes=10,
            priority=25,
            command=(
                "kalshi-bot phase3bb-r60-weather-next-window-lead-time-scheduler-repair "
                "--output-dir reports/phase3bb_r60 --reports-dir reports "
                "--max-wait-seconds 120 --poll-interval-seconds 10 "
                "--min-minutes-before-target 20 --max-minutes-before-target 90 "
                "--refresh-timeout-seconds 240 --r57-timeout-seconds 300 "
                "--per-probe-timeout-seconds 90"
            ),
            purpose=(
                "Run the weather lead-time gate every 10 minutes so targeted KXTEMPNYCH "
                "catalog/parse/R57 work starts early in the next live window and skips once "
                "the selected target is too close to expiry."
            ),
            writer_capable=True,
            source_state="phase3bb_r60_weather_next_window_lead_time",
        ),
        _job(
            job_id="weather_fast_lane",
            category="weather",
            cadence_minutes=30,
            priority=30,
            command=(
                "kalshi-bot phase3bb-r2-weather-fast-lane "
                "--output-dir reports/phase3bb_r2 --reports-dir reports"
            ),
            purpose=_category_purpose(
                weather,
                fallback="Weather is the highest non-crypto lane and needs ranking refresh.",
            ),
            writer_capable=True,
            source_state=_category_state(weather),
        ),
        _job(
            job_id="free_source_inventory",
            category="all",
            cadence_minutes=360,
            priority=40,
            command=(
                "kalshi-bot phase3bb-r3-free-source-inventory "
                "--output-dir reports/phase3bb_r3 --reports-dir reports"
            ),
            purpose="Refresh the non-crypto category scorecard and next-category backlog.",
            writer_capable=False,
            source_state="category_scorecard",
        ),
        _job(
            job_id="economic_parser_backfill_review",
            category="economic",
            cadence_minutes=720,
            priority=50,
            command=(
                "kalshi-bot phase3bb-r4-economic-parser-backfill "
                "--output-dir reports/phase3bb_r4 --reports-dir reports"
            ),
            purpose=_category_purpose(
                economic,
                fallback="Economic is source-rich but needs active parser/backfill review.",
            ),
            writer_capable=False,
            source_state=_category_state(economic),
        ),
        _job(
            job_id="news_event_discovery_review",
            category="news",
            cadence_minutes=720,
            priority=60,
            command=(
                "kalshi-bot phase3bb-r7-news-event-discovery "
                "--output-dir reports/phase3bb_r7 --reports-dir reports"
            ),
            purpose=_category_purpose(
                news,
                fallback="News needs exact source-backed event discovery before forecasts.",
            ),
            writer_capable=False,
            source_state=_category_state(news),
        ),
        _job(
            job_id="sports_provenance_review",
            category="sports",
            cadence_minutes=720,
            priority=70,
            command=(
                "kalshi-bot phase3bb-r6-sports-provenance-repair "
                "--output-dir reports/phase3bb_r6 --reports-dir reports"
            ),
            purpose=_category_purpose(
                sports,
                fallback="Sports needs exact provenance repair while composites remain parked.",
            ),
            writer_capable=False,
            source_state=_category_state(sports),
        ),
        _job(
            job_id="coverage_doctor_review",
            category="coverage",
            cadence_minutes=360,
            priority=80,
            command="kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
            purpose="Keep category/link coverage harmonized with parked-composite logic.",
            writer_capable=False,
            source_state="coverage",
        ),
        _job(
            job_id="crypto_background_status",
            category="crypto",
            cadence_minutes=15,
            priority=90,
            command="kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
            purpose=_category_purpose(
                crypto,
                fallback=(
                    "Crypto stays a background status/watch lane; do not start a "
                    "duplicate R5."
                ),
            ),
            writer_capable=False,
            source_state=_category_state(crypto),
        ),
    ]


def _job(
    *,
    job_id: str,
    category: str,
    cadence_minutes: int,
    priority: int,
    command: str,
    purpose: str,
    writer_capable: bool,
    source_state: str,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "category": category,
        "cadence_minutes": cadence_minutes,
        "priority": priority,
        "command": command,
        "purpose": purpose,
        "writer_capable": writer_capable,
        "requires_db_writer_gate": writer_capable,
        "max_runtime_seconds": 120 if writer_capable else 90,
        "exclusive_lock": "kalshi-refresh-scheduler.lock" if writer_capable else "",
        "source_state": source_state,
        "enabled_in_draft": True,
        "installs_or_starts_service": False,
        "creates_paper_trades": False,
        "allows_live_or_demo_orders": False,
    }


def _scheduler_checks(
    r33: dict[str, Any],
    refresh_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    r33_decision = r33.get("readiness_decision") or {}
    names = _all_command_names(refresh_jobs)
    unregistered = sorted({name for name in names if name not in REGISTERED_SCHEDULER_COMMANDS})
    forbidden = sorted(
        {
            fragment
            for row in refresh_jobs
            for fragment in FORBIDDEN_COMMAND_FRAGMENTS
            if fragment in str(row.get("command", "")).lower()
        }
    )
    duplicate_r5_start = any(
        "phase3bc-r5-unattended-start" in str(row.get("command", ""))
        for row in refresh_jobs
    )
    writer_jobs_without_gate = [
        row["job_id"]
        for row in refresh_jobs
        if row.get("writer_capable") and not row.get("requires_db_writer_gate")
    ]
    return [
        _check(
            "r33_operations_ready",
            r33_decision.get("status") in READY_R33_STATUSES
            and bool(r33_decision.get("readiness_passed")),
            f"R33 status is {r33_decision.get('status')}.",
        ),
        _check(
            "paper_gate_not_auto_trading",
            int(r33_decision.get("paper_ready_candidates") or 0) == 0
            or r33_decision.get("status") == "PAPER_ONLY_OPERATOR_REVIEW_READY",
            f"paper_ready_candidates={r33_decision.get('paper_ready_candidates')}.",
        ),
        _check(
            "all_scheduled_commands_registered",
            not unregistered,
            f"unregistered={','.join(unregistered) if unregistered else 'none'}.",
        ),
        _check(
            "no_forbidden_trade_or_service_commands",
            not forbidden,
            f"forbidden={','.join(forbidden) if forbidden else 'none'}.",
        ),
        _check(
            "no_duplicate_r5_start_in_schedule",
            not duplicate_r5_start,
            "Draft observes R5 status only and does not start R5.",
        ),
        _check(
            "writer_capable_jobs_have_writer_gate",
            not writer_jobs_without_gate,
            (
                "missing_gate="
                f"{','.join(writer_jobs_without_gate) if writer_jobs_without_gate else 'none'}."
            ),
        ),
        _check(
            "schedule_has_weather_and_paper_gate",
            {"weather_fast_lane", "unified_paper_gate"}.issubset(
                {str(row.get("job_id")) for row in refresh_jobs}
            ),
            "Draft includes weather fast lane and unified paper gate.",
        ),
    ]


def _decision(
    r33: dict[str, Any],
    refresh_jobs: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r33_decision = r33.get("readiness_decision") or {}
    paper_ready = int(r33_decision.get("paper_ready_candidates") or 0)
    if failed:
        status = "BLOCKED_SCHEDULER_REVIEW"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R34 - Resolve Cloud Scheduler Review"
        command = (
            "kalshi-bot phase3bb-r34-cloud-multicategory-refresh-scheduler-review "
            "--output-dir reports/phase3bb_r34 --reports-dir reports"
        )
    elif paper_ready > 0:
        status = "PAPER_ONLY_OPERATOR_REVIEW_FIRST"
        reason = (
            "Paper-ready candidates exist; operator review comes before scheduling more "
            "refresh work."
        )
        next_step = "Phase 3BB-R34 - Paper-Only Candidate Operator Review"
        command = "Open the private UI Opportunities page and inspect paper-only risk gates."
    else:
        status = "READY_FOR_NO_START_SCHEDULER_DRY_RUN"
        reason = (
            "Cloud is paper-only ready, R5 is systemd-owned, and the multi-category "
            "refresh schedule draft has registered commands and writer gates."
        )
        next_step = "Phase 3BB-R35 - Cloud Multi-Category Scheduler No-Start Dry Run"
        command = "Review reports/phase3bb_r34/scheduler_draft.sh before R35."
    return {
        "status": status,
        "review_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "job_count": len(refresh_jobs),
        "writer_gated_job_count": sum(1 for row in refresh_jobs if row.get("writer_capable")),
        "paper_ready_candidates": paper_ready,
        "r5_pid": r33_decision.get("r5_pid"),
        "watch_state": r33_decision.get("watch_state"),
        "primary_reason": reason,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _all_command_names(refresh_jobs: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in refresh_jobs:
        parts = shlex.split(str(row.get("command") or ""))
        for index, part in enumerate(parts):
            if (
                (part == "kalshi-bot" or part.endswith("/kalshi-bot"))
                and index + 1 < len(parts)
            ):
                names.append(parts[index + 1])
    return names


def _category_purpose(row: dict[str, Any], *, fallback: str) -> str:
    blocker = row.get("top_blocker")
    next_step = row.get("next_implementation_step")
    if blocker or next_step:
        return f"{blocker or 'UNKNOWN'}: {next_step or fallback}"
    return fallback


def _category_state(row: dict[str, Any]) -> str:
    if not row:
        return "missing_scorecard_row"
    return (
        f"score={row.get('score')}; active={row.get('active_markets')}; "
        f"linked={row.get('linked_markets')}; blocker={row.get('top_blocker')}"
    )


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R34 Cloud Scheduler Review")
    decision = payload["scheduler_decision"]
    r33_decision = (payload["r33_operations_readiness"] or {}).get("readiness_decision") or {}
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Review passed: `{decision['review_passed']}`",
            f"- Jobs in draft: `{decision['job_count']}`",
            f"- Writer-gated jobs: `{decision['writer_gated_job_count']}`",
            f"- R33 status: `{r33_decision.get('status')}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- Watch state: `{decision['watch_state']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- This phase only reviews a scheduler draft.",
            "- It does not install, enable, start, or restart services.",
            "- It does not start or stop R5.",
            "- It does not run category refresh jobs.",
            "- It does not create paper trades or live/demo orders.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R34 Scheduler Detail")
    decision = payload["scheduler_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Jobs", ""])
    for row in payload["refresh_jobs"]:
        gate = "writer-gated" if row["writer_capable"] else "read-only/report"
        lines.append(
            f"- `{row['job_id']}` ({row['category']}, every {row['cadence_minutes']}m, "
            f"{gate}): `{row['command']}`"
        )
    lines.extend(["", "## Checks", ""])
    for row in payload["scheduler_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    return "\n".join(lines) + "\n"


def _render_scheduler_draft(payload: dict[str, Any]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "echo '[phase3bb-r34] no-install scheduler draft; no jobs run by default'",
        "echo '[phase3bb-r34] review-only artifact generated by Codex'",
        "",
        "# Draft job commands. R35 should turn this into a no-start dry run, not an install.",
    ]
    for row in payload["refresh_jobs"]:
        lines.extend(
            [
                "",
                f"# job_id={row['job_id']} cadence_minutes={row['cadence_minutes']}",
                f"# writer_capable={str(row['writer_capable']).lower()}",
            ]
        )
        if row["writer_capable"]:
            lines.append("# guard: kalshi-bot db-writer-monitor --json must be clear first")
        lines.append(f"# command: {row['command']}")
    lines.append("")
    return "\n".join(lines)


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["scheduler_decision"]["operator_next_command"]
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
    lines = _metadata_lines(payload, "# Phase 3BB-R34 Next Actions")
    decision = payload["scheduler_decision"]
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
            "- Do not install scheduler services from R34.",
            "- Do not start a duplicate R5 watcher.",
            "- Do not run weather/economic/news/sports refresh jobs without writer gates.",
            "- Do not create paper trades from this review.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


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
