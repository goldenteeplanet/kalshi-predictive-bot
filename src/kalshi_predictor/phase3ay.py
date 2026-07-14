from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.jobs.collect_once import collect_once
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    write_paper_settlement_reconciliation,
)
from kalshi_predictor.phase3aa import write_phase3aa_report
from kalshi_predictor.phase3aa_r2 import write_phase3aa_r2_exact_settlement_harvest_report
from kalshi_predictor.phase3ad import write_phase_orchestrator_report
from kalshi_predictor.phase3ah_placeholder_watch import (
    write_phase3ah_sports_placeholder_watch_report,
)
from kalshi_predictor.phase3ah_placeholders import (
    write_phase3ah_round_placeholder_resolution_report,
)
from kalshi_predictor.phase3as import write_phase3as_report
from kalshi_predictor.phase3z import write_market_coverage_doctor
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE_3AY_VERSION = "phase3ay_v1"
MARKET_CHECKPOINT_FILE = "phase3ay_market_checkpoint.json"
UNATTENDED_PID_FILE = "unattended_health_job.pid"
UNATTENDED_META_FILE = "unattended_health_job.json"
UNATTENDED_STDOUT_FILE = "unattended_stdout.log"
UNATTENDED_STDERR_FILE = "unattended_stderr.log"
CLEAN_STOP_MARKET_STATUSES = {"TIMED_OUT_CLEANLY", "PARTIAL_REFRESH_CONTINUABLE"}


@dataclass(frozen=True)
class Phase3AYArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3AYUnattendedStart:
    output_dir: Path
    status: str
    pid: int | None
    started: bool
    pid_path: Path
    metadata_path: Path
    stdout_path: Path
    stderr_path: Path
    command: str
    message: str


StepJob = Callable[[], Any]


def start_phase3ay_unattended_refresh(
    *,
    output_dir: Path = Path("reports/phase3ay"),
    cycles: int = 1,
    interval_seconds: int = 300,
    duration_hours: float = 0.0,
    all_markets: bool = False,
    market_collect: bool = True,
    market_limit: int = 100,
    market_max_pages: int = 1,
    include_orderbook: bool = True,
    settlement_sync: bool = True,
    settlement_lookback_days: int = 90,
    settlement_limit: int = 200,
    settlement_max_pages: int = 10,
    settlement_commit_every: int = 0,
    realize_paper: bool = True,
    settlement_only: bool = False,
    stop_on_error: bool = False,
    timeout_grace_seconds: int = 600,
) -> Phase3AYUnattendedStart:
    """Start Phase 3AY as an owned background process with PID/log metadata."""

    output_dir.mkdir(parents=True, exist_ok=True)
    pid_path = output_dir / UNATTENDED_PID_FILE
    metadata_path = output_dir / UNATTENDED_META_FILE
    stdout_path = output_dir / UNATTENDED_STDOUT_FILE
    stderr_path = output_dir / UNATTENDED_STDERR_FILE
    current = build_phase3ay_status(output_dir=output_dir)
    process = current.get("process") or {}
    if process.get("phase3ay_process_running"):
        pids = list(process.get("phase3ay_pids") or [])
        pid = int(pids[0]) if pids else _read_pid(pid_path)
        return Phase3AYUnattendedStart(
            output_dir=output_dir,
            status="ALREADY_RUNNING",
            pid=pid,
            started=False,
            pid_path=pid_path,
            metadata_path=metadata_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            command="",
            message="Phase 3AY unattended refresh is already running.",
        )

    command = _phase3ay_health_refresh_command(
        output_dir=output_dir,
        cycles=cycles,
        interval_seconds=interval_seconds,
        duration_hours=duration_hours,
        all_markets=all_markets,
        market_collect=market_collect,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        include_orderbook=include_orderbook,
        settlement_sync=settlement_sync,
        settlement_lookback_days=settlement_lookback_days,
        settlement_limit=settlement_limit,
        settlement_max_pages=settlement_max_pages,
        settlement_commit_every=settlement_commit_every,
        realize_paper=realize_paper,
        settlement_only=settlement_only,
        stop_on_error=stop_on_error,
    )
    timeout_seconds = _unattended_timeout_seconds(
        cycles=cycles,
        interval_seconds=interval_seconds,
        duration_hours=duration_hours,
        timeout_grace_seconds=timeout_grace_seconds,
    )
    started_at = utc_now()
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        process_handle = subprocess.Popen(  # noqa: S603 - command is internal CLI argv.
            command,
            cwd=Path.cwd(),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=os.name != "nt",
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    pid_path.write_text(str(process_handle.pid), encoding="utf-8")
    metadata = {
        "phase": "3BA-R2",
        "status": "STARTED",
        "started_at": started_at.isoformat(),
        "pid": process_handle.pid,
        "pid_path": str(pid_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": _command_display(command),
        "argv": command,
        "duration_budget_seconds": duration_hours * 3600 if duration_hours > 0 else None,
        "timeout_seconds": timeout_seconds,
        "timeout_grace_seconds": timeout_grace_seconds,
        "cycles": cycles,
        "interval_seconds": interval_seconds,
        "all_markets": all_markets,
        "market_collect": market_collect,
        "market_limit": market_limit,
        "market_max_pages": market_max_pages,
        "include_orderbook": include_orderbook,
        "settlement_sync": settlement_sync,
        "settlement_limit": settlement_limit,
        "settlement_max_pages": settlement_max_pages,
        "settlement_commit_every": settlement_commit_every,
        "realize_paper": realize_paper,
        "settlement_only": settlement_only,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "exact_ticker_settlement_required": True,
        "live_or_demo_execution": False,
    }
    _write_json(metadata_path, metadata)
    return Phase3AYUnattendedStart(
        output_dir=output_dir,
        status="STARTED",
        pid=process_handle.pid,
        started=True,
        pid_path=pid_path,
        metadata_path=metadata_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        command=metadata["command"],
        message="Phase 3AY unattended refresh started.",
    )


def write_phase3ay_unattended_guard_report(
    *,
    output_dir: Path = Path("reports/phase3ay"),
    stop_overrun: bool = False,
    terminate_grace_seconds: int = 30,
) -> Phase3AYArtifactSet:
    payload = build_phase3ay_unattended_guard(
        output_dir=output_dir,
        stop_overrun=stop_overrun,
        terminate_grace_seconds=terminate_grace_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ay_unattended_guard.json"
    markdown_path = output_dir / "phase3ay_unattended_guard.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_guard_markdown(payload), encoding="utf-8")
    return Phase3AYArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ay_unattended_guard(
    *,
    output_dir: Path = Path("reports/phase3ay"),
    stop_overrun: bool = False,
    terminate_grace_seconds: int = 30,
) -> dict[str, Any]:
    before = build_phase3ay_status(output_dir=output_dir)
    guard = dict(before.get("guard") or {})
    action: dict[str, Any] = {
        "requested_stop_overrun": stop_overrun,
        "terminated_pid": None,
        "termination_result": None,
    }
    if stop_overrun and guard.get("should_stop") and guard.get("pid") is not None:
        pid = int(guard["pid"])
        action["terminated_pid"] = pid
        action["termination_result"] = _terminate_pid(pid, grace_seconds=terminate_grace_seconds)

    after = build_phase3ay_status(output_dir=output_dir)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BA-R2",
        "mode": "READ_ONLY_UNATTENDED_REFRESH_GUARD",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "before": before,
        "after": after,
        "action": action,
        "status": (after.get("guard") or {}).get("status", "UNKNOWN"),
        "recommended_next_action": (after.get("guard") or {}).get(
            "recommended_next_action",
            "Review Phase 3AY status before starting another refresh.",
        ),
    }


def write_phase3ay_health_refresh_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ay"),
    settings: Settings | None = None,
    cycle_index: int = 1,
    cycles: int = 1,
    interval_seconds: int = 300,
    market_collect: bool = True,
    market_limit: int = 100,
    market_max_pages: int = 1,
    include_orderbook: bool = True,
    settlement_sync: bool = True,
    settlement_lookback_days: int = 90,
    settlement_limit: int = 200,
    settlement_max_pages: int = 10,
    settlement_commit_every: int = 0,
    realize_paper: bool = True,
    settlement_only: bool = False,
    stop_on_error: bool = False,
    collect_job: StepJob | None = None,
    step_jobs: dict[str, StepJob] | None = None,
    duration_budget_seconds: float | None = None,
    deadline_monotonic: float | None = None,
    resume_market_cursor: bool = True,
) -> Phase3AYArtifactSet:
    payload = build_phase3ay_health_refresh_report(
        session,
        output_dir=output_dir,
        settings=settings,
        cycle_index=cycle_index,
        cycles=cycles,
        interval_seconds=interval_seconds,
        market_collect=market_collect,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        include_orderbook=include_orderbook,
        settlement_sync=settlement_sync,
        settlement_lookback_days=settlement_lookback_days,
        settlement_limit=settlement_limit,
        settlement_max_pages=settlement_max_pages,
        settlement_commit_every=settlement_commit_every,
        realize_paper=realize_paper,
        settlement_only=settlement_only,
        stop_on_error=stop_on_error,
        collect_job=collect_job,
        step_jobs=step_jobs,
        duration_budget_seconds=duration_budget_seconds,
        deadline_monotonic=deadline_monotonic,
        resume_market_cursor=resume_market_cursor,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ay_health_refresh.json"
    markdown_path = output_dir / "phase3ay_health_refresh.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    _append_cycle_history(output_dir / "phase3ay_health_refresh_history.jsonl", payload)
    return Phase3AYArtifactSet(output_dir, json_path, markdown_path)


def write_phase3ay_status_report(
    *,
    output_dir: Path = Path("reports/phase3ay"),
) -> Phase3AYArtifactSet:
    payload = build_phase3ay_status(output_dir=output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ay_status.json"
    markdown_path = output_dir / "phase3ay_status.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_status_markdown(payload), encoding="utf-8")
    return Phase3AYArtifactSet(output_dir, json_path, markdown_path)


def build_phase3ay_status(*, output_dir: Path = Path("reports/phase3ay")) -> dict[str, Any]:
    latest = _load_json(output_dir / "phase3ay_health_refresh.json")
    metadata = _load_json(output_dir / UNATTENDED_META_FILE)
    pid_path = output_dir / UNATTENDED_PID_FILE
    pid = _read_pid(pid_path)
    process = _phase3ay_process_status(pid)
    stdout_path = Path(str(metadata.get("stdout_path") or output_dir / UNATTENDED_STDOUT_FILE))
    stderr_path = Path(str(metadata.get("stderr_path") or output_dir / UNATTENDED_STDERR_FILE))
    history_path = output_dir / "phase3ay_health_refresh_history.jsonl"
    checkpoint = _load_json(output_dir / MARKET_CHECKPOINT_FILE)
    guard = _unattended_guard_status(
        output_dir=output_dir,
        pid=pid,
        process=process,
        metadata=metadata,
        latest=latest,
        checkpoint=checkpoint,
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY_STATUS",
        "mode": "READ_ONLY_HEALTH_REFRESH_STATUS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "pid_file": str(pid_path),
        "metadata_file": str(output_dir / UNATTENDED_META_FILE),
        "pid": pid,
        "process": process,
        "guard": guard,
        "market_checkpoint": checkpoint,
        "latest_report_generated_at": latest.get("generated_at"),
        "latest_status": latest.get("status"),
        "latest_summary": latest.get("summary") or {},
        "history_rows": _jsonl_count(history_path),
        "logs": {
            "stdout_path": str(stdout_path),
            "stdout_bytes": _path_size(stdout_path),
            "stderr_path": str(stderr_path),
            "stderr_bytes": _path_size(stderr_path),
            "stderr_tail": _tail_text(stderr_path),
        },
        "recommended_next_action": _status_next_action(process, latest, guard),
    }


def build_phase3ay_health_refresh_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ay"),
    settings: Settings | None = None,
    cycle_index: int = 1,
    cycles: int = 1,
    interval_seconds: int = 300,
    market_collect: bool = True,
    market_limit: int = 100,
    market_max_pages: int = 1,
    include_orderbook: bool = True,
    settlement_sync: bool = True,
    settlement_lookback_days: int = 90,
    settlement_limit: int = 200,
    settlement_max_pages: int = 10,
    settlement_commit_every: int = 0,
    realize_paper: bool = True,
    settlement_only: bool = False,
    stop_on_error: bool = False,
    collect_job: StepJob | None = None,
    step_jobs: dict[str, StepJob] | None = None,
    duration_budget_seconds: float | None = None,
    deadline_monotonic: float | None = None,
    resume_market_cursor: bool = True,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    steps: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}

    writer_status = db_writer_monitor(settings=resolved)
    if writer_status.get("status") == "WRITER_ACTIVE":
        return _blocked_payload(
            output_dir=output_dir,
            cycle_index=cycle_index,
            cycles=cycles,
            interval_seconds=interval_seconds,
            writer_status=writer_status,
            settings=resolved,
            settlement_only=settlement_only,
        )

    overrides = step_jobs or {}

    def job(name: str, default: StepJob) -> StepJob:
        return overrides.get(name, default)

    def add_step(name: str, enabled: bool, action: StepJob) -> None:
        if enabled and _deadline_reached(deadline_monotonic):
            steps.append(_budget_skipped_step(name))
            return
        step = _run_step(
            session,
            name=name,
            enabled=enabled,
            action=action,
            stop_on_error=stop_on_error,
        )
        steps.append(step)
        if step["status"] == "OK":
            summaries[name] = step.get("summary")

    market_checkpoint_path = output_dir / MARKET_CHECKPOINT_FILE
    prior_market_checkpoint = _load_json(market_checkpoint_path)
    market_start_cursor = (
        _checkpoint_resume_cursor(prior_market_checkpoint) if resume_market_cursor else None
    )
    market_page_callback = _market_checkpoint_callback(
        path=market_checkpoint_path,
        cycle_index=cycle_index,
        cycles=cycles,
        interval_seconds=interval_seconds,
        start_cursor=market_start_cursor,
        duration_budget_seconds=duration_budget_seconds,
        deadline_active=deadline_monotonic is not None,
    )

    add_step(
        "market_collect",
        market_collect,
        job(
            "market_collect",
            collect_job
            or (
                lambda: collect_once(
                    status="open",
                    limit=market_limit,
                    max_pages=None if market_max_pages <= 0 else market_max_pages,
                    start_cursor=market_start_cursor,
                    deadline_monotonic=deadline_monotonic,
                    page_callback=market_page_callback,
                    include_orderbook=include_orderbook,
                    session=session,
                )
            ),
        ),
    )
    _finalize_market_checkpoint(
        path=market_checkpoint_path,
        summary=summaries.get("market_collect"),
        cycle_index=cycle_index,
        cycles=cycles,
        interval_seconds=interval_seconds,
        duration_budget_seconds=duration_budget_seconds,
        deadline_reached=_deadline_reached(deadline_monotonic),
    )
    add_step(
        "settlement_sync",
        settlement_sync,
        job(
            "settlement_sync",
            lambda: sync_settlements(
                lookback_days=settlement_lookback_days,
                limit=settlement_limit,
                max_pages=None if settlement_max_pages <= 0 else settlement_max_pages,
                commit_every=settlement_commit_every or None,
                session=session,
            ),
        ),
    )
    add_step(
        "exact_settlement_harvest",
        True,
        job(
            "exact_settlement_harvest",
            lambda: write_phase3aa_r2_exact_settlement_harvest_report(
                session,
                output_dir=Path("reports/phase3aa_r2"),
            ),
        ),
    )
    add_step(
        "paper_realize",
        True,
        job(
            "paper_realize",
            lambda: write_phase3aa_report(
                session,
                output_dir=Path("reports/phase3aa"),
                settings=resolved,
                sync=False,
                dry_run=not realize_paper,
            ),
        ),
    )
    add_step(
        "paper_settlement_doctor",
        True,
        job(
            "paper_settlement_doctor",
            lambda: write_paper_settlement_reconciliation(
                session,
                output_dir=Path("reports/paper_settlement_reconciliation"),
            ),
        ),
    )
    add_step(
        "market_coverage_doctor",
        not settlement_only,
        job(
            "market_coverage_doctor",
            lambda: write_market_coverage_doctor(
                session,
                output_dir=Path("reports/market_coverage"),
                settings=resolved,
                parse_first=True,
            ),
        ),
    )
    add_step(
        "active_universe_doctor",
        not settlement_only,
        job(
            "active_universe_doctor",
            lambda: write_phase3as_report(
                session,
                output_dir=Path("reports/phase3as"),
                mark_deprecated=True,
            ),
        ),
    )
    add_step(
        "sports_placeholder_resolution",
        not settlement_only,
        job(
            "sports_placeholder_resolution",
            lambda: write_phase3ah_round_placeholder_resolution_report(
                output_dir=Path("reports/phase3ah_sports"),
            ),
        ),
    )
    add_step(
        "sports_placeholder_watch",
        not settlement_only,
        job(
            "sports_placeholder_watch",
            lambda: write_phase3ah_sports_placeholder_watch_report(
                output_dir=Path("reports/phase3ah_sports"),
            ),
        ),
    )
    add_step(
        "phase_orchestrator",
        not settlement_only,
        job(
            "phase_orchestrator",
            lambda: write_phase_orchestrator_report(
                session,
                output_path=Path("reports/phase_orchestrator.md"),
                json_path=Path("reports/phase_orchestrator.json"),
                next_prompt_path=Path("prompts/next_phase.md"),
                settings=resolved,
                scan_limit=100,
            ),
        ),
    )

    paper = _paper_health()
    market = _market_health(summaries.get("market_collect"))
    market_checkpoint = _load_json(market_checkpoint_path)
    sports = _sports_placeholder_health()
    settlement = _settlement_harvest_health()
    status = _overall_status(steps, paper, market, sports, settlement_only=settlement_only)
    if not any(step["status"] == "ERROR" for step in steps):
        if market.get("collection_status") == "TIMED_OUT_CLEANLY":
            status = "TIMED_OUT_CLEANLY"
        elif market.get("collection_status") == "PARTIAL_REFRESH_CONTINUABLE":
            status = "PARTIAL_REFRESH_CONTINUABLE"
        elif _has_budget_skip(steps):
            status = "TIMED_OUT_CLEANLY"
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": PHASE_3AY_VERSION,
        "mode": (
            "PAPER_SETTLEMENT_ONLY_REFRESH_LOOP"
            if settlement_only
            else "PAPER_MARKET_HEALTH_REFRESH_LOOP"
        ),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "cycle": {
            "cycle_index": cycle_index,
            "cycles": cycles,
            "interval_seconds": interval_seconds,
            "next_cycle_due_seconds": interval_seconds if cycle_index < cycles else None,
        },
        "safety": {
            "live_or_demo_execution": False,
            "live_orders_created": 0,
            "exact_ticker_settlement_required": True,
            "realize_paper_enabled": realize_paper,
            "execution_enabled_setting": bool(resolved.execution_enabled),
        },
        "writer_status": writer_status,
        "refresh_guard": {
            "duration_budget_seconds": duration_budget_seconds,
            "deadline_active": deadline_monotonic is not None,
            "deadline_reached": _deadline_reached(deadline_monotonic),
            "resume_market_cursor": resume_market_cursor,
            "market_start_cursor": market_start_cursor,
            "market_checkpoint_path": str(market_checkpoint_path),
            "market_checkpoint": market_checkpoint,
            "settlement_only": settlement_only,
        },
        "status": status,
        "summary": {
            "steps_ok": sum(1 for step in steps if step["status"] == "OK"),
            "steps_error": sum(1 for step in steps if step["status"] == "ERROR"),
            "steps_skipped": sum(1 for step in steps if step["status"] == "SKIPPED"),
            "paper_status": paper["status"],
            "market_status": market["status"],
            "sports_placeholder_gate": sports["phase3ae_gate_status"],
            "due_or_overdue": paper["due_or_overdue"],
            "eligible_exact_settlements": paper["eligible_exact_settlements"],
            "exact_settlements_written": settlement["exact_settlements_written"],
            "paper_pnl_realized": paper["paper_pnl_realized"],
            "market_snapshots_inserted": market["snapshots_inserted"],
            "settlement_only": settlement_only,
        },
        "paper_health": paper,
        "market_health": market,
        "sports_placeholder_health": sports,
        "settlement_harvest": settlement,
        "steps": steps,
        "next_commands": _next_commands(interval_seconds, settlement_only=settlement_only),
        "recommended_next_action": _recommended_next_action(
            status,
            interval_seconds,
            settlement_only=settlement_only,
        ),
    }


def run_phase3ay_health_refresh_loop(
    session_factory: Callable[[], Session],
    *,
    output_dir: Path = Path("reports/phase3ay"),
    settings: Settings | None = None,
    cycles: int = 1,
    interval_seconds: int = 300,
    market_collect: bool = True,
    market_limit: int = 100,
    market_max_pages: int = 1,
    include_orderbook: bool = True,
    settlement_sync: bool = True,
    settlement_lookback_days: int = 90,
    settlement_limit: int = 200,
    settlement_max_pages: int = 10,
    settlement_commit_every: int = 0,
    realize_paper: bool = True,
    settlement_only: bool = False,
    stop_on_error: bool = False,
    duration_budget_seconds: float | None = None,
) -> list[Phase3AYArtifactSet]:
    artifacts: list[Phase3AYArtifactSet] = []
    deadline_monotonic = (
        time.monotonic() + duration_budget_seconds
        if duration_budget_seconds is not None and duration_budget_seconds > 0
        else None
    )
    for cycle in range(1, cycles + 1):
        if _deadline_reached(deadline_monotonic):
            break
        with session_factory() as session:
            artifacts.append(
                write_phase3ay_health_refresh_report(
                    session,
                    output_dir=output_dir,
                    settings=settings,
                    cycle_index=cycle,
                    cycles=cycles,
                    interval_seconds=interval_seconds,
                    market_collect=market_collect,
                    market_limit=market_limit,
                    market_max_pages=market_max_pages,
                    include_orderbook=include_orderbook,
                    settlement_sync=settlement_sync,
                    settlement_lookback_days=settlement_lookback_days,
                    settlement_limit=settlement_limit,
                    settlement_max_pages=settlement_max_pages,
                    settlement_commit_every=settlement_commit_every,
                    realize_paper=realize_paper,
                    settlement_only=settlement_only,
                    stop_on_error=stop_on_error,
                    duration_budget_seconds=duration_budget_seconds,
                    deadline_monotonic=deadline_monotonic,
                )
            )
        if _deadline_reached(deadline_monotonic):
            break
        if cycle < cycles and interval_seconds > 0:
            sleep_seconds = interval_seconds
            if deadline_monotonic is not None:
                remaining_budget_seconds = max(0.0, deadline_monotonic - time.monotonic())
                sleep_seconds = min(interval_seconds, remaining_budget_seconds)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return artifacts


def _phase3ay_health_refresh_command(
    *,
    output_dir: Path,
    cycles: int,
    interval_seconds: int,
    duration_hours: float,
    all_markets: bool,
    market_collect: bool,
    market_limit: int,
    market_max_pages: int,
    include_orderbook: bool,
    settlement_sync: bool,
    settlement_lookback_days: int,
    settlement_limit: int,
    settlement_max_pages: int,
    settlement_commit_every: int,
    realize_paper: bool,
    settlement_only: bool,
    stop_on_error: bool,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "kalshi_predictor.cli",
        "phase3ay-health-refresh",
        "--output-dir",
        str(output_dir),
        "--cycles",
        str(cycles),
        "--interval-seconds",
        str(interval_seconds),
        "--duration-hours",
        str(duration_hours),
        "--market-limit",
        str(market_limit),
        "--market-max-pages",
        str(market_max_pages),
        "--settlement-lookback-days",
        str(settlement_lookback_days),
        "--settlement-limit",
        str(settlement_limit),
        "--settlement-max-pages",
        str(settlement_max_pages),
    ]
    if settlement_commit_every > 0:
        command.extend(["--settlement-commit-every", str(settlement_commit_every)])
    command.append("--all-markets" if all_markets else "--paged-markets")
    command.append("--market-collect" if market_collect else "--no-market-collect")
    command.append("--orderbook" if include_orderbook else "--no-orderbook")
    command.append("--settlement-sync" if settlement_sync else "--no-settlement-sync")
    command.append("--realize-paper" if realize_paper else "--dry-run-paper")
    if settlement_only:
        command.append("--settlement-only")
    if stop_on_error:
        command.append("--stop-on-error")
    return command


def _unattended_timeout_seconds(
    *,
    cycles: int,
    interval_seconds: int,
    duration_hours: float,
    timeout_grace_seconds: int,
) -> int:
    if duration_hours > 0:
        budget = int(duration_hours * 3600)
    else:
        cycle_wait = max(cycles - 1, 0) * max(interval_seconds, 0)
        budget = cycle_wait + timeout_grace_seconds
    return max(1, budget + max(timeout_grace_seconds, 0))


def _command_display(command: list[str]) -> str:
    return " ".join(_quote_arg(part) for part in command)


def _quote_arg(part: str) -> str:
    if not part or any(char.isspace() for char in part):
        return "'" + part.replace("'", "'\\''") + "'"
    return part


def _unattended_guard_status(
    *,
    output_dir: Path,
    pid: int | None,
    process: dict[str, Any],
    metadata: dict[str, Any],
    latest: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    started_at = parse_datetime(metadata.get("started_at"))
    elapsed_seconds = _elapsed_seconds_since(started_at)
    timeout_seconds = _int_or_none(metadata.get("timeout_seconds"))
    duration_budget_seconds = _int_or_none(metadata.get("duration_budget_seconds"))
    running = bool(process.get("phase3ay_process_running"))
    latest_status = str(latest.get("status") or "NO_REPORT")
    clean_stop = latest_status in CLEAN_STOP_MARKET_STATUSES
    status = "NO_UNATTENDED_JOB"
    if running and timeout_seconds is not None and elapsed_seconds is not None:
        status = "OVERRUNNING" if elapsed_seconds > timeout_seconds else "RUNNING"
    elif running:
        status = "RUNNING_UNKNOWN_BUDGET"
    elif clean_stop:
        status = latest_status
    elif metadata and pid is not None:
        status = "STOPPED_WITH_STALE_PID"
    elif latest:
        status = "STOPPED"

    should_stop = status == "OVERRUNNING"
    return {
        "phase": "3BA-R2",
        "status": status,
        "pid": pid,
        "running": running,
        "pid_file": str(output_dir / UNATTENDED_PID_FILE),
        "metadata_file": str(output_dir / UNATTENDED_META_FILE),
        "stdout_path": str(metadata.get("stdout_path") or output_dir / UNATTENDED_STDOUT_FILE),
        "stderr_path": str(metadata.get("stderr_path") or output_dir / UNATTENDED_STDERR_FILE),
        "started_at": metadata.get("started_at"),
        "elapsed_seconds": elapsed_seconds,
        "duration_budget_seconds": duration_budget_seconds,
        "timeout_seconds": timeout_seconds,
        "seconds_until_timeout": _seconds_until_timeout(
            elapsed_seconds=elapsed_seconds,
            timeout_seconds=timeout_seconds,
        ),
        "should_stop": should_stop,
        "latest_status": latest_status,
        "checkpoint_status": checkpoint.get("status"),
        "resume_cursor": checkpoint.get("resume_cursor"),
        "safe_to_resume": bool(checkpoint.get("safe_to_resume")),
        "resume_command": checkpoint.get("resume_command") or _market_resume_command(0),
        "recommended_next_action": _guard_next_action(
            status=status,
            clean_stop=clean_stop,
            safe_to_resume=bool(checkpoint.get("safe_to_resume")),
        ),
    }


def _elapsed_seconds_since(started_at: Any) -> int | None:
    if started_at is None:
        return None
    return max(0, int((utc_now() - started_at).total_seconds()))


def _seconds_until_timeout(
    *,
    elapsed_seconds: int | None,
    timeout_seconds: int | None,
) -> int | None:
    if elapsed_seconds is None or timeout_seconds is None:
        return None
    return max(0, timeout_seconds - elapsed_seconds)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _guard_next_action(*, status: str, clean_stop: bool, safe_to_resume: bool) -> str:
    if status == "OVERRUNNING":
        return (
            "Run phase3ay-unattended-guard --stop-overrun to stop the stale "
            "refresh process, then run post-refresh diagnostics."
        )
    if status == "RUNNING":
        return "Refresh is running within the configured timeout budget."
    if clean_stop and safe_to_resume:
        return "Refresh stopped cleanly at a resumable checkpoint; diagnostics can run now."
    if clean_stop:
        return "Refresh stopped cleanly; run post-refresh diagnostics before the next phase."
    if status == "STOPPED_WITH_STALE_PID":
        return "No process is running; stale PID metadata can be overwritten by the next start."
    return "No unattended refresh is active."


def _terminate_pid(pid: int, *, grace_seconds: int) -> dict[str, Any]:
    if not _pid_exists(pid):
        return {"status": "ALREADY_STOPPED", "pid": pid}
    if os.name == "nt":
        return _terminate_pid_windows(pid, grace_seconds=grace_seconds)
    return _terminate_pid_posix(pid, grace_seconds=grace_seconds)


def _terminate_pid_posix(pid: int, *, grace_seconds: int) -> dict[str, Any]:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {"status": "TERM_FAILED", "pid": pid, "error": str(exc)}
    deadline = time.monotonic() + max(grace_seconds, 0)
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return {"status": "STOPPED_AFTER_TERM", "pid": pid}
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return {"status": "KILL_FAILED", "pid": pid, "error": str(exc)}
    return {"status": "KILLED_AFTER_GRACE", "pid": pid}


def _terminate_pid_windows(pid: int, *, grace_seconds: int) -> dict[str, Any]:
    del grace_seconds
    completed = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "status": "TASKKILL_OK" if completed.returncode == 0 else "TASKKILL_FAILED",
        "pid": pid,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
    }


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        probe = (
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) "
            "{ exit 0 } else { exit 1 }"
        )
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            probe,
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        return completed.returncode == 0
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not _posix_pid_is_zombie(pid)


def _run_step(
    session: Session,
    *,
    name: str,
    enabled: bool,
    action: StepJob,
    stop_on_error: bool,
) -> dict[str, Any]:
    started_at = utc_now()
    if not enabled:
        return {
            "name": name,
            "status": "SKIPPED",
            "started_at": started_at.isoformat(),
            "finished_at": utc_now().isoformat(),
            "seconds": 0.0,
            "summary": None,
            "artifacts": {},
        }
    try:
        result = action()
        session.commit()
        finished_at = utc_now()
        return {
            "name": name,
            "status": "OK",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "seconds": round((finished_at - started_at).total_seconds(), 3),
            "summary": _result_summary(result),
            "artifacts": _artifact_paths(result),
        }
    except Exception as exc:
        session.rollback()
        if stop_on_error:
            raise
        finished_at = utc_now()
        return {
            "name": name,
            "status": "ERROR",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "seconds": round((finished_at - started_at).total_seconds(), 3),
            "summary": None,
            "artifacts": {},
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def _deadline_reached(deadline_monotonic: float | None) -> bool:
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


def _budget_skipped_step(name: str) -> dict[str, Any]:
    now = utc_now().isoformat()
    return {
        "name": name,
        "status": "SKIPPED",
        "started_at": now,
        "finished_at": now,
        "seconds": 0.0,
        "summary": {"skip_reason": "duration_budget_exhausted"},
        "artifacts": {},
    }


def _has_budget_skip(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        summary = step.get("summary")
        if isinstance(summary, dict) and summary.get("skip_reason") == "duration_budget_exhausted":
            return True
    return False


def _checkpoint_resume_cursor(checkpoint: dict[str, Any]) -> str | None:
    if checkpoint.get("status") not in CLEAN_STOP_MARKET_STATUSES:
        return None
    cursor = checkpoint.get("resume_cursor")
    return cursor if isinstance(cursor, str) and cursor else None


def _market_checkpoint_callback(
    *,
    path: Path,
    cycle_index: int,
    cycles: int,
    interval_seconds: int,
    start_cursor: str | None,
    duration_budget_seconds: float | None,
    deadline_active: bool,
) -> Callable[[str, dict[str, Any]], None]:
    totals: dict[str, Any] = {
        "market_sync_pages": 0,
        "snapshot_capture_pages": 0,
        "markets_seen_on_pages": 0,
    }

    def callback(stage: str, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "page":
            page_count = int(payload.get("pages_seen") or 0)
            if stage == "market_sync":
                totals["market_sync_pages"] = page_count
            elif stage == "snapshot_capture":
                totals["snapshot_capture_pages"] = page_count
            totals["markets_seen_on_pages"] = int(totals["markets_seen_on_pages"] or 0) + int(
                payload.get("markets_on_page") or 0
            )
            status = "IN_PROGRESS"
        elif payload.get("stop_reason") == "deadline":
            status = "TIMED_OUT_CLEANLY"
        else:
            status = "PARTIAL_REFRESH_CONTINUABLE"

        resume_cursor = payload.get("resume_cursor")
        checkpoint = {
            "phase": "3BA",
            "status": status,
            "updated_at": utc_now().isoformat(),
            "cycle_index": cycle_index,
            "cycles": cycles,
            "interval_seconds": interval_seconds,
            "duration_budget_seconds": duration_budget_seconds,
            "deadline_active": deadline_active,
            "stage": stage,
            "event": event,
            "stop_reason": payload.get("stop_reason"),
            "start_cursor": start_cursor,
            "request_cursor": payload.get("request_cursor"),
            "resume_cursor": resume_cursor if isinstance(resume_cursor, str) else None,
            "has_more": bool(payload.get("has_more")),
            "market_sync_pages": int(totals["market_sync_pages"] or 0),
            "snapshot_capture_pages": int(totals["snapshot_capture_pages"] or 0),
            "markets_seen_on_pages": int(totals["markets_seen_on_pages"] or 0),
            "safe_to_resume": isinstance(resume_cursor, str) and bool(resume_cursor),
            "resume_command": _market_resume_command(interval_seconds),
            "safety_note": (
                "Resume only continues public market refresh pagination; it does not "
                "enable live/demo exchange writes."
            ),
        }
        _write_json(path, checkpoint)

    return callback


def _finalize_market_checkpoint(
    *,
    path: Path,
    summary: Any,
    cycle_index: int,
    cycles: int,
    interval_seconds: int,
    duration_budget_seconds: float | None,
    deadline_reached: bool,
) -> None:
    existing = _load_json(path)
    if not isinstance(summary, dict):
        if deadline_reached:
            _write_json(
                path,
                {
                    **existing,
                    "phase": "3BA",
                    "status": "TIMED_OUT_CLEANLY",
                    "updated_at": utc_now().isoformat(),
                    "cycle_index": cycle_index,
                    "cycles": cycles,
                    "interval_seconds": interval_seconds,
                    "duration_budget_seconds": duration_budget_seconds,
                    "safe_to_resume": bool(existing.get("resume_cursor")),
                    "resume_command": _market_resume_command(interval_seconds),
                },
            )
        return

    collection_status = str(summary.get("collection_status") or "COMPLETE")
    resume_cursor = summary.get("resume_cursor")
    if collection_status == "COMPLETE":
        resume_cursor = None
    final_status = (
        "TIMED_OUT_CLEANLY"
        if collection_status == "TIMED_OUT_CLEANLY" or deadline_reached
        else collection_status
    )
    checkpoint = {
        **existing,
        "phase": "3BA",
        "status": final_status,
        "updated_at": utc_now().isoformat(),
        "cycle_index": cycle_index,
        "cycles": cycles,
        "interval_seconds": interval_seconds,
        "duration_budget_seconds": duration_budget_seconds,
        "stopped_reason": summary.get("stopped_reason"),
        "resume_cursor": resume_cursor if isinstance(resume_cursor, str) else None,
        "markets_seen": int(summary.get("markets_seen") or 0),
        "snapshots_inserted": int(summary.get("snapshots_inserted") or 0),
        "forecasts_inserted": int(summary.get("forecasts_inserted") or 0),
        "market_sync_pages": int(summary.get("market_pages_processed") or 0),
        "snapshot_capture_pages": int(summary.get("snapshot_pages_processed") or 0),
        "safe_to_resume": isinstance(resume_cursor, str) and bool(resume_cursor),
        "resume_command": _market_resume_command(interval_seconds),
    }
    _write_json(path, checkpoint)


def _market_resume_command(interval_seconds: int) -> str:
    return (
        "kalshi-bot phase3ay-health-refresh --cycles 1 "
        f"--interval-seconds {interval_seconds} --all-markets"
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _blocked_payload(
    *,
    output_dir: Path,
    cycle_index: int,
    cycles: int,
    interval_seconds: int,
    writer_status: dict[str, Any],
    settings: Settings,
    settlement_only: bool = False,
) -> dict[str, Any]:
    del output_dir
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AY",
        "phase_version": PHASE_3AY_VERSION,
        "mode": (
            "PAPER_SETTLEMENT_ONLY_REFRESH_LOOP"
            if settlement_only
            else "PAPER_MARKET_HEALTH_REFRESH_LOOP"
        ),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "cycle": {
            "cycle_index": cycle_index,
            "cycles": cycles,
            "interval_seconds": interval_seconds,
            "next_cycle_due_seconds": interval_seconds if cycle_index < cycles else None,
        },
        "safety": {
            "live_or_demo_execution": False,
            "live_orders_created": 0,
            "exact_ticker_settlement_required": True,
            "realize_paper_enabled": False,
            "execution_enabled_setting": bool(settings.execution_enabled),
        },
        "writer_status": writer_status,
        "status": "WAITING_FOR_DB_WRITER",
        "summary": {
            "steps_ok": 0,
            "steps_error": 0,
            "steps_skipped": 0,
            "paper_status": "STALE",
            "market_status": "STALE",
            "sports_placeholder_gate": "UNKNOWN",
            "due_or_overdue": None,
            "eligible_exact_settlements": None,
            "exact_settlements_written": None,
            "paper_pnl_realized": False,
            "market_snapshots_inserted": None,
        },
        "paper_health": {},
        "market_health": {},
        "sports_placeholder_health": {},
        "settlement_harvest": {},
        "steps": [],
        "next_commands": _next_commands(interval_seconds, settlement_only=settlement_only),
        "recommended_next_action": (
            "Another local writer is active. Wait for it to finish before running "
            "paper/market health refresh."
        ),
    }


def _result_summary(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, int | float | str | bool):
        return {"value": result}
    if is_dataclass(result) and not isinstance(result, type):
        payload = asdict(result)
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {
            key: getattr(result, key)
            for key in dir(result)
            if not key.startswith("_") and not callable(getattr(result, key))
        }
    if "json_path" in payload:
        report = _load_json(Path(payload["json_path"]))
        if isinstance(report, dict):
            return report.get("summary") or _compact_report(report)
    return _json_safe(payload)


def _artifact_paths(result: Any) -> dict[str, str]:
    if result is None:
        return {}
    if is_dataclass(result) and not isinstance(result, type):
        payload = asdict(result)
    else:
        payload = getattr(result, "__dict__", {})
    paths: dict[str, str] = {}
    for key, value in payload.items():
        if key.endswith("_path") or key == "output_dir":
            paths[key] = str(value)
        elif key.endswith("_paths") and isinstance(value, tuple | list):
            paths[key] = ", ".join(str(path) for path in value)
    return paths


def _paper_health() -> dict[str, Any]:
    phase3aa = _load_json(Path("reports/phase3aa/phase3aa_outcome_realizer.json"))
    doctor = _load_json(
        Path("reports/paper_settlement_reconciliation/paper_settlement_reconciliation.json")
    )
    eta = (phase3aa.get("eta_schedule") or {}).get("summary") if phase3aa else {}
    doctor_summary = doctor.get("summary") if isinstance(doctor, dict) else {}
    due = int((eta or {}).get("due_or_overdue") or 0)
    eta_eligible = int((eta or {}).get("eligible_exact_settlements") or 0)
    eligible = int(phase3aa.get("eligible_after_realize", eta_eligible) or 0)
    realized = bool(phase3aa.get("pnl_realized")) if isinstance(phase3aa, dict) else False
    if eligible:
        status = "READY_TO_REALIZE"
    elif due:
        status = "WATCHING_EXACT_SETTLEMENTS"
    else:
        status = "HEALTHY"
    return {
        "status": status,
        "active_unsettled": int((eta or {}).get("active_unsettled") or 0),
        "due_or_overdue": due,
        "eligible_exact_settlements": eligible,
        "eta_eligible_exact_settlements": eta_eligible,
        "paper_pnl_realized": realized,
        "doctor_summary": doctor_summary or {},
    }


def _market_health(collect_summary: Any) -> dict[str, Any]:
    coverage = _load_json(Path("reports/market_coverage/market_coverage_doctor.json"))
    active = _load_json(Path("reports/phase3as/phase3as_active_universe.json"))
    coverage_rows = coverage.get("coverage_rows") if isinstance(coverage, dict) else []
    if not isinstance(coverage_rows, list):
        coverage_rows = []
    unhealthy = [
        row
        for row in coverage_rows
        if isinstance(row, dict)
        and row.get("health") not in {"HEALTHY", "NO_COMPATIBLE_ACTIVE_MARKETS"}
    ]
    active_summary = active.get("summary") if isinstance(active, dict) else {}
    collect = collect_summary if isinstance(collect_summary, dict) else {}
    collection_status = str(collect.get("collection_status") or "COMPLETE")
    status = "HEALTHY" if not unhealthy else "NEEDS_COVERAGE_REPAIR"
    return {
        "status": status,
        "collection_status": collection_status,
        "stopped_reason": collect.get("stopped_reason"),
        "resume_cursor": collect.get("resume_cursor"),
        "market_pages_processed": int(collect.get("market_pages_processed") or 0),
        "snapshot_pages_processed": int(collect.get("snapshot_pages_processed") or 0),
        "markets_seen": int(collect.get("markets_seen") or 0),
        "snapshots_inserted": int(collect.get("snapshots_inserted") or 0),
        "forecasts_inserted": int(collect.get("forecasts_inserted") or 0),
        "coverage_recommendations": coverage.get("recommendations", [])
        if isinstance(coverage, dict)
        else [],
        "unhealthy_coverage_rows": len(unhealthy),
        "active_universe_summary": active_summary or {},
    }


def _sports_placeholder_health() -> dict[str, Any]:
    watch = _load_json(Path("reports/phase3ah_sports/phase3ah_sports_placeholder_watch.json"))
    summary = watch.get("summary") if isinstance(watch, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "phase3ae_gate_status": summary.get("phase3ae_gate_status", "UNKNOWN"),
        "placeholder_rows_reviewed": int(summary.get("placeholder_rows_reviewed") or 0),
        "safe_to_apply_rows": int(summary.get("safe_to_apply_rows") or 0),
        "still_placeholder_rows": int(summary.get("still_placeholder_rows") or 0),
    }


def _settlement_harvest_health() -> dict[str, Any]:
    harvest = _load_json(Path("reports/phase3aa_r2/phase3aa_r2_exact_settlement_harvest.json"))
    summary = harvest.get("summary") if isinstance(harvest, dict) else {}
    return {
        "exact_tickers_checked": int(summary.get("exact_tickers_checked") or 0),
        "exact_settlements_written": int(summary.get("exact_settlements_written") or 0),
        "eligible_exact_settlements_after": int(
            summary.get("eligible_exact_settlements_after") or 0
        ),
        "fetch_errors": int(summary.get("fetch_errors") or 0),
        "source_settled_without_usable_outcome": int(
            summary.get("source_settled_without_usable_outcome") or 0
        ),
    }


def _overall_status(
    steps: list[dict[str, Any]],
    paper: dict[str, Any],
    market: dict[str, Any],
    sports: dict[str, Any],
    *,
    settlement_only: bool = False,
) -> str:
    if any(step["status"] == "ERROR" for step in steps):
        return "DEGRADED_STEP_ERRORS"
    if not settlement_only and market["status"] != "HEALTHY":
        return "DEGRADED_MARKET_COVERAGE"
    if paper["status"] == "READY_TO_REALIZE":
        return "PAPER_REALIZATION_PENDING"
    if paper["status"] == "WATCHING_EXACT_SETTLEMENTS":
        return "FRESH_WATCHING_SETTLEMENTS"
    if not settlement_only and sports["phase3ae_gate_status"] == "HOLD_PLACEHOLDER_UPGRADES":
        return "FRESH_PLACEHOLDER_WATCHING"
    if settlement_only:
        return "FRESH_SETTLEMENT_ONLY"
    return "FRESH_HEALTHY"


def _next_commands(interval_seconds: int, *, settlement_only: bool = False) -> list[str]:
    if settlement_only:
        return [
            (
                "kalshi-bot phase3ay-health-refresh --settlement-only --cycles 999 "
                f"--interval-seconds {interval_seconds} --paged-markets "
                "--market-limit 100 --market-max-pages 1"
            ),
            "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2",
            "kalshi-bot phase3aa-realize --no-dry-run --no-sync-settlements",
            (
                "kalshi-bot paper-settlement-doctor "
                "--output-dir reports/paper_settlement_reconciliation"
            ),
        ]
    return [
        (
            "kalshi-bot phase3ay-health-refresh --cycles 999 "
            f"--interval-seconds {interval_seconds} --all-markets"
        ),
        "kalshi-bot phase3aa-r2-exact-settlement-harvest --output-dir reports/phase3aa_r2",
        "kalshi-bot phase3aa-realize --no-dry-run --no-sync-settlements",
        "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        (
            "kalshi-bot phase-orchestrator --analyze "
            "--output reports/phase_orchestrator.md "
            "--json-output reports/phase_orchestrator.json "
            "--next-prompt prompts/next_phase.md"
        ),
    ]


def _recommended_next_action(
    status: str,
    interval_seconds: int,
    *,
    settlement_only: bool = False,
) -> str:
    if status == "TIMED_OUT_CLEANLY":
        return (
            "Phase 3AY stopped at its duration budget and left a market cursor. "
            "Let the post-refresh watcher run diagnostics, then resume with the checkpoint."
        )
    if status == "PARTIAL_REFRESH_CONTINUABLE":
        return (
            "Market refresh reached a clean page/window boundary with a resume cursor. "
            "Continue Phase 3AY later instead of restarting from the first market page."
        )
    if status == "DEGRADED_STEP_ERRORS":
        return "Inspect the failed step in reports/phase3ay before leaving the loop unattended."
    if status == "DEGRADED_MARKET_COVERAGE":
        return "Keep the refresh loop running, then inspect market coverage recommendations."
    if status == "FRESH_WATCHING_SETTLEMENTS":
        if settlement_only:
            return (
                "Leave the settlement-only Phase 3AY loop running. It will keep harvesting "
                "exact tickers and realize paper P&L only when exact settlement rows become "
                "eligible."
            )
        return (
            "Leave phase3ay-health-refresh running. It will keep harvesting exact tickers "
            "and realize paper P&L when exact settlement rows become eligible."
        )
    if status == "FRESH_SETTLEMENT_ONLY":
        return (
            "Settlement-only refresh is current. Keep the separate crypto R5 watcher active "
            "for opportunity freshness; next settlement cycle can run in "
            f"{interval_seconds} seconds."
        )
    return f"Health refresh is current. Next cycle can run in {interval_seconds} seconds."


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _phase3ay_process_status(pid: int | None) -> dict[str, Any]:
    running_pids = _phase3ay_running_pids()
    pid_running = pid in running_pids if pid is not None else False
    return {
        "pid_running": pid_running,
        "phase3ay_process_running": bool(running_pids),
        "phase3ay_pids": running_pids,
        "status": "RUNNING" if running_pids else "STOPPED",
    }


def _phase3ay_running_pids() -> list[int]:
    if os.name != "nt":
        return _posix_phase3ay_running_pids()

    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -like '*phase3ay-health-refresh*' "
            "-and $_.CommandLine -notlike '*phase3ay-status*' } | "
            "ForEach-Object { $_.ProcessId }"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return sorted(set(pids))


def _posix_phase3ay_running_pids() -> list[int]:
    proc = Path("/proc")
    if not proc.exists():
        return []
    pids: list[int] = []
    current_pid = os.getpid()
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid == current_pid or _posix_pid_is_zombie(pid):
            continue
        command = _posix_cmdline(pid)
        if "phase3ay-health-refresh" not in command:
            continue
        if "kalshi-bot" not in command and "kalshi_predictor.cli" not in command:
            continue
        if "phase3ay-status" in command or "pgrep" in command or "grep" in command:
            continue
        pids.append(pid)
    return sorted(set(pids))


def _posix_cmdline(pid: int) -> str:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode(errors="replace").strip()


def _posix_pid_is_zombie(pid: int) -> bool:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return False
    parts = stat.split()
    return len(parts) > 2 and parts[2] == "Z"


def _path_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _tail_text(path: Path, *, max_lines: int = 20) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]


def _status_next_action(
    process: dict[str, Any],
    latest: dict[str, Any],
    guard: dict[str, Any],
) -> str:
    if guard.get("should_stop"):
        return guard["recommended_next_action"]
    if guard.get("status") in CLEAN_STOP_MARKET_STATUSES:
        return guard["recommended_next_action"]
    if guard.get("status") in {"RUNNING", "RUNNING_UNKNOWN_BUDGET"}:
        return guard["recommended_next_action"]
    if process["phase3ay_process_running"]:
        return "Health refresh is still running; wait for the next cycle report."
    if latest:
        if latest.get("status") in CLEAN_STOP_MARKET_STATUSES:
            return (
                "Health refresh stopped cleanly at a resumable boundary. "
                "Run post-refresh diagnostics, then resume Phase 3AY from the checkpoint."
            )
        return "Health refresh is stopped. Run phase3az-gap-analysis before choosing next work."
    return "No Phase 3AY report exists yet. Start phase3ay-health-refresh."


def _compact_report(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "phase",
        "mode",
        "status",
        "eligible_after_realize",
        "eligible_after_sync",
        "pnl_realized",
        "recommended_next_action",
    )
    return {key: payload[key] for key in keys if key in payload}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _append_cycle_history(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "generated_at": payload["generated_at"],
        "status": payload["status"],
        "summary": payload["summary"],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AY Paper + Market Health Refresh",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Status: {payload['status']}",
        "- Live/demo execution: false",
        f"- Realize paper from exact settlements: {payload['safety']['realize_paper_enabled']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    guard = payload.get("refresh_guard") or {}
    checkpoint = guard.get("market_checkpoint") or {}
    if guard:
        lines.extend(
            [
                "",
                "## Refresh Guard",
                "",
                f"- Deadline active: {guard.get('deadline_active')}",
                f"- Deadline reached: {guard.get('deadline_reached')}",
                f"- Market checkpoint: `{guard.get('market_checkpoint_path')}`",
                f"- Checkpoint status: {checkpoint.get('status') or 'none'}",
                f"- Resume cursor present: {bool(checkpoint.get('resume_cursor'))}",
            ]
        )
    lines.extend(
        [
            "",
            "## Steps",
            "",
            "| Step | Status | Seconds | Summary |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for step in payload["steps"]:
        summary = step.get("summary")
        if isinstance(summary, dict):
            summary_text = ", ".join(
                f"{key}={value}" for key, value in list(summary.items())[:5]
            )
        else:
            summary_text = "" if summary is None else str(summary)
        lines.append(
            f"| {step['name']} | {step['status']} | {step['seconds']} | "
            f"{_md(summary_text)} |"
        )
    if not payload["steps"]:
        lines.append("| none | SKIPPED | 0 | Waiting for active writer to finish. |")
    lines.extend(
        [
            "",
            "## Next Commands",
            "",
        ]
    )
    for command in payload["next_commands"]:
        lines.append(f"- `{command}`")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _render_status_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AY Health Refresh Status",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Process status: {payload['process']['status']}",
        f"- Guard status: {payload.get('guard', {}).get('status')}",
        f"- Phase 3AY PIDs: {payload['process']['phase3ay_pids']}",
        f"- Latest report: {payload.get('latest_report_generated_at')}",
        f"- Latest status: {payload.get('latest_status')}",
        f"- History rows: {payload['history_rows']}",
        "",
        "## Guard",
        "",
    ]
    guard = payload.get("guard") or {}
    for key in (
        "pid",
        "started_at",
        "elapsed_seconds",
        "timeout_seconds",
        "seconds_until_timeout",
        "should_stop",
        "checkpoint_status",
        "safe_to_resume",
        "resume_command",
    ):
        lines.append(f"- {key}: {guard.get(key)}")
    lines.extend(
        [
            "",
            "## Latest Summary",
            "",
        ]
    )
    for key, value in payload.get("latest_summary", {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Logs", ""])
    logs = payload["logs"]
    lines.extend(
        [
            f"- stdout: `{logs['stdout_path']}` ({logs['stdout_bytes']} bytes)",
            f"- stderr: `{logs['stderr_path']}` ({logs['stderr_bytes']} bytes)",
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _render_guard_markdown(payload: dict[str, Any]) -> str:
    action = payload.get("action") or {}
    after_guard = ((payload.get("after") or {}).get("guard") or {})
    lines = [
        "# Phase 3AY Unattended Guard",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Status: {payload['status']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Stop overrun requested: {action.get('requested_stop_overrun')}",
        f"- Terminated PID: {action.get('terminated_pid')}",
        f"- Termination result: {action.get('termination_result')}",
        "",
        "## After",
        "",
        f"- Running: {after_guard.get('running')}",
        f"- Should stop: {after_guard.get('should_stop')}",
        f"- Checkpoint status: {after_guard.get('checkpoint_status')}",
        f"- Safe to resume: {after_guard.get('safe_to_resume')}",
        f"- Resume command: `{after_guard.get('resume_command')}`",
        "",
        "## Recommended Next Action",
        "",
        payload["recommended_next_action"],
        "",
    ]
    return "\n".join(lines)


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
