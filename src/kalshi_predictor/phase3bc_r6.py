from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BC_R6_VERSION = "phase3bc_r6_guarded_crypto_freshness_runner"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
DEFAULT_CRYPTO_SYMBOLS = "BTC,ETH,SOL,XRP,DOGE"
DEFAULT_CRYPTO_SERIES_TICKERS = "KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE"
DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES = 15
DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT = 40
DEFAULT_NEAR_MONEY_WINDOW_LIMIT = 20
DEFAULT_SNAPSHOT_FETCH_CONCURRENCY = 2
DEFAULT_MARKET_PAGE_LIMIT = 150
DEFAULT_CRYPTO_MARKET_SCAN_LIMIT = 2500
DEFAULT_CRYPTO_LINK_SCAN_LIMIT = 500
UNATTENDED_PID_FILE = "phase3bc_r5_unattended_job.pid"
UNATTENDED_META_FILE = "phase3bc_r5_unattended_job.json"
UNATTENDED_STDOUT_FILE = "phase3bc_r5_unattended_stdout.log"
UNATTENDED_STDERR_FILE = "phase3bc_r5_unattended_stderr.log"
R5_REPORT_FILE = "phase3bc_r5_crypto_freshness_watch.json"
R5_HISTORY_FILE = "phase3bc_r5_crypto_freshness_watch_history.jsonl"


@dataclass(frozen=True)
class Phase3BCR6ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3BCR6UnattendedStart:
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


def start_phase3bc_r5_unattended_watch(
    *,
    output_dir: Path = Path("reports/phase3bc_r5"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Path = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Path = Path("reports/phase3bc_r4"),
    phase3bc_r7_output_dir: Path = Path("reports/phase3bc_r7"),
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: str = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: str = "coinbase",
    refresh_open_markets: bool = True,
    external_crypto_ingest: bool = True,
    repair_snapshots: bool = False,
    forecast_current_windows_only: bool = True,
    generate_opportunity_report: bool = False,
    market_limit: int = DEFAULT_MARKET_PAGE_LIMIT,
    market_max_pages: int = 1,
    crypto_market_scan_limit: int = DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    crypto_link_limit: int = DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    forecast_limit: int = 1000,
    opportunity_limit: int = 500,
    phase3bc_limit: int = 1000,
    cadence_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    freshness_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    max_preflight: int = 10,
    risk_preflight: bool = True,
    ranking_repair: bool = True,
    ranking_repair_limit: int = 500,
    cycles: int = 32,
    interval_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    duration_hours: float = 8.0,
    timeout_grace_seconds: int = 900,
    near_money_only: bool = True,
    near_money_per_symbol_limit: int = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: int = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: int = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
) -> Phase3BCR6UnattendedStart:
    """Start the R5 crypto watch as an owned background process with PID/log metadata."""

    output_dir.mkdir(parents=True, exist_ok=True)
    pid_path = output_dir / UNATTENDED_PID_FILE
    metadata_path = output_dir / UNATTENDED_META_FILE
    stdout_path = output_dir / UNATTENDED_STDOUT_FILE
    stderr_path = output_dir / UNATTENDED_STDERR_FILE
    current = build_phase3bc_r5_status(output_dir=output_dir)
    process = current.get("process") or {}
    if process.get("phase3bc_r5_process_running"):
        pids = list(process.get("phase3bc_r5_pids") or [])
        pid = int(pids[0]) if pids else _read_pid(pid_path)
        return Phase3BCR6UnattendedStart(
            output_dir=output_dir,
            status="ALREADY_RUNNING",
            pid=pid,
            started=False,
            pid_path=pid_path,
            metadata_path=metadata_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            command="",
            message="Phase 3BC-R5 crypto freshness watch is already running.",
        )

    command = _phase3bc_r5_watch_command(
        output_dir=output_dir,
        phase3bc_output_dir=phase3bc_output_dir,
        phase3bc_r3_output_dir=phase3bc_r3_output_dir,
        phase3bc_r4_output_dir=phase3bc_r4_output_dir,
        phase3bc_r7_output_dir=phase3bc_r7_output_dir,
        symbols=symbols,
        crypto_series_tickers=crypto_series_tickers,
        source=source,
        refresh_open_markets=refresh_open_markets,
        external_crypto_ingest=external_crypto_ingest,
        repair_snapshots=repair_snapshots,
        forecast_current_windows_only=forecast_current_windows_only,
        generate_opportunity_report=generate_opportunity_report,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        crypto_market_scan_limit=crypto_market_scan_limit,
        crypto_link_limit=crypto_link_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
        phase3bc_limit=phase3bc_limit,
        cadence_minutes=cadence_minutes,
        freshness_minutes=freshness_minutes,
        max_preflight=max_preflight,
        risk_preflight=risk_preflight,
        ranking_repair=ranking_repair,
        ranking_repair_limit=ranking_repair_limit,
        cycles=cycles,
        interval_minutes=interval_minutes,
        near_money_only=near_money_only,
        near_money_per_symbol_limit=near_money_per_symbol_limit,
        near_money_window_limit=near_money_window_limit,
        snapshot_fetch_concurrency=snapshot_fetch_concurrency,
    )
    timeout_seconds = _timeout_seconds(
        cycles=cycles,
        interval_minutes=interval_minutes,
        duration_hours=duration_hours,
        timeout_grace_seconds=timeout_grace_seconds,
    )
    started_at = utc_now()
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        process_handle = subprocess.Popen(  # noqa: S603 - internal CLI argv only.
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

    metadata = {
        "phase": "3BC-R6",
        "phase_version": PHASE3BC_R6_VERSION,
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
        "interval_minutes": interval_minutes,
        "cadence_minutes": cadence_minutes,
        "freshness_minutes": freshness_minutes,
        "ranking_repair": ranking_repair,
        "ranking_repair_limit": ranking_repair_limit,
        "near_money_only": near_money_only,
        "near_money_per_symbol_limit": near_money_per_symbol_limit,
        "near_money_window_limit": near_money_window_limit,
        "snapshot_fetch_concurrency": snapshot_fetch_concurrency,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "risk_preflight_only": True,
    }
    pid_path.write_text(str(process_handle.pid), encoding="utf-8")
    _write_json(metadata_path, metadata)
    return Phase3BCR6UnattendedStart(
        output_dir=output_dir,
        status="STARTED",
        pid=process_handle.pid,
        started=True,
        pid_path=pid_path,
        metadata_path=metadata_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        command=metadata["command"],
        message="Phase 3BC-R5 guarded crypto freshness watch started.",
    )


def write_phase3bc_r5_status_report(
    *,
    output_dir: Path = Path("reports/phase3bc_r5"),
) -> Phase3BCR6ArtifactSet:
    payload = build_phase3bc_r5_status(output_dir=output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r5_status.json"
    markdown_path = output_dir / "phase3bc_r5_status.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_status_markdown(payload), encoding="utf-8")
    return Phase3BCR6ArtifactSet(output_dir, json_path, markdown_path)


def write_phase3bc_r5_unattended_guard_report(
    *,
    output_dir: Path = Path("reports/phase3bc_r5"),
    stop_overrun: bool = False,
    terminate_grace_seconds: int = 30,
) -> Phase3BCR6ArtifactSet:
    payload = build_phase3bc_r5_unattended_guard(
        output_dir=output_dir,
        stop_overrun=stop_overrun,
        terminate_grace_seconds=terminate_grace_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r5_unattended_guard.json"
    markdown_path = output_dir / "phase3bc_r5_unattended_guard.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_guard_markdown(payload), encoding="utf-8")
    return Phase3BCR6ArtifactSet(output_dir, json_path, markdown_path)


def build_phase3bc_r5_unattended_guard(
    *,
    output_dir: Path = Path("reports/phase3bc_r5"),
    stop_overrun: bool = False,
    terminate_grace_seconds: int = 30,
) -> dict[str, Any]:
    before = build_phase3bc_r5_status(output_dir=output_dir)
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
    after = build_phase3bc_r5_status(output_dir=output_dir)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BC-R6",
        "phase_version": PHASE3BC_R6_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_FRESHNESS_GUARD",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "before": before,
        "after": after,
        "action": action,
        "status": (after.get("guard") or {}).get("status", "UNKNOWN"),
        "recommended_next_action": (after.get("guard") or {}).get(
            "recommended_next_action",
            "Review Phase 3BC-R5 status before starting another crypto watch.",
        ),
    }


def build_phase3bc_r5_status(*, output_dir: Path = Path("reports/phase3bc_r5")) -> dict[str, Any]:
    latest = _load_json(output_dir / R5_REPORT_FILE)
    metadata = _load_json(output_dir / UNATTENDED_META_FILE)
    pid_path = output_dir / UNATTENDED_PID_FILE
    pid = _read_pid(pid_path)
    process = _phase3bc_r5_process_status(pid)
    stdout_path = Path(str(metadata.get("stdout_path") or output_dir / UNATTENDED_STDOUT_FILE))
    stderr_path = Path(str(metadata.get("stderr_path") or output_dir / UNATTENDED_STDERR_FILE))
    history_path = output_dir / R5_HISTORY_FILE
    guard = _guard_status(
        output_dir=output_dir,
        pid=pid,
        process=process,
        metadata=metadata,
        latest=latest,
    )
    summary = latest.get("summary") if isinstance(latest, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    latest_stage_duration_seconds = (
        latest.get("stage_duration_seconds")
        or latest.get("stage_durations_seconds")
        or summary.get("stage_duration_seconds")
        or summary.get("stage_durations_seconds")
        or {}
    )
    latest_slowest_stage = _latest_slowest_stage(
        summary=summary,
        stage_duration_seconds=latest_stage_duration_seconds,
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BC-R5_STATUS",
        "phase_version": PHASE3BC_R6_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_FRESHNESS_STATUS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "pid_file": str(pid_path),
        "metadata_file": str(output_dir / UNATTENDED_META_FILE),
        "pid": pid,
        "process": process,
        "guard": guard,
        "latest_report_generated_at": latest.get("generated_at"),
        "latest_watch_state": summary.get("watch_state"),
        "latest_summary": summary,
        "latest_stage_duration_seconds": latest_stage_duration_seconds,
        "latest_slowest_stage": latest_slowest_stage,
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


def _latest_slowest_stage(
    *,
    summary: Mapping[str, Any],
    stage_duration_seconds: Mapping[str, Any],
) -> dict[str, Any]:
    summary_stage = summary.get("slowest_stage")
    summary_seconds = summary.get("slowest_stage_seconds")
    if summary_stage:
        return {"stage": summary_stage, "duration_seconds": summary_seconds}
    numeric_durations: dict[str, float] = {}
    for stage, raw_seconds in stage_duration_seconds.items():
        try:
            numeric_durations[str(stage)] = float(raw_seconds)
        except (TypeError, ValueError):
            continue
    if not numeric_durations:
        return {"stage": None, "duration_seconds": None}
    stage, seconds = max(numeric_durations.items(), key=lambda item: item[1])
    return {"stage": stage, "duration_seconds": round(seconds, 3)}


def _phase3bc_r5_watch_command(
    *,
    output_dir: Path,
    phase3bc_output_dir: Path,
    phase3bc_r3_output_dir: Path,
    phase3bc_r4_output_dir: Path,
    phase3bc_r7_output_dir: Path,
    symbols: str,
    crypto_series_tickers: str,
    source: str,
    refresh_open_markets: bool,
    external_crypto_ingest: bool,
    repair_snapshots: bool,
    forecast_current_windows_only: bool,
    generate_opportunity_report: bool,
    market_limit: int,
    market_max_pages: int,
    crypto_market_scan_limit: int,
    crypto_link_limit: int,
    forecast_limit: int,
    opportunity_limit: int,
    phase3bc_limit: int,
    cadence_minutes: int,
    freshness_minutes: int,
    max_preflight: int,
    risk_preflight: bool,
    ranking_repair: bool,
    ranking_repair_limit: int,
    cycles: int,
    interval_minutes: int,
    near_money_only: bool,
    near_money_per_symbol_limit: int,
    near_money_window_limit: int,
    snapshot_fetch_concurrency: int,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "kalshi_predictor.cli",
        "phase3bc-r5-crypto-freshness-watch",
        "--output-dir",
        str(output_dir),
        "--phase3bc-output-dir",
        str(phase3bc_output_dir),
        "--phase3bc-r3-output-dir",
        str(phase3bc_r3_output_dir),
        "--phase3bc-r4-output-dir",
        str(phase3bc_r4_output_dir),
        "--phase3bc-r7-output-dir",
        str(phase3bc_r7_output_dir),
        "--symbols",
        symbols,
        "--crypto-series-tickers",
        crypto_series_tickers,
        "--source",
        source,
        "--market-limit",
        str(market_limit),
        "--market-max-pages",
        str(market_max_pages),
        "--crypto-market-scan-limit",
        str(crypto_market_scan_limit),
        "--crypto-link-limit",
        str(crypto_link_limit),
        "--forecast-limit",
        str(forecast_limit),
        "--opportunity-limit",
        str(opportunity_limit),
        "--phase3bc-limit",
        str(phase3bc_limit),
        "--cadence-minutes",
        str(cadence_minutes),
        "--freshness-minutes",
        str(freshness_minutes),
        "--max-preflight",
        str(max_preflight),
        "--ranking-repair-limit",
        str(ranking_repair_limit),
        "--cycles",
        str(cycles),
        "--interval-minutes",
        str(interval_minutes),
        "--near-money-per-symbol-limit",
        str(near_money_per_symbol_limit),
        "--near-money-window-limit",
        str(near_money_window_limit),
        "--snapshot-fetch-concurrency",
        str(snapshot_fetch_concurrency),
    ]
    command.append(
        "--refresh-open-markets" if refresh_open_markets else "--skip-open-market-refresh"
    )
    command.append(
        "--external-crypto-ingest"
        if external_crypto_ingest
        else "--skip-external-crypto-ingest"
    )
    command.append("--repair-snapshots" if repair_snapshots else "--diagnose-snapshots")
    command.append(
        "--forecast-current-windows-only"
        if forecast_current_windows_only
        else "--forecast-all-active-crypto"
    )
    command.append(
        "--generate-opportunity-report"
        if generate_opportunity_report
        else "--skip-opportunity-report"
    )
    command.append("--risk-preflight" if risk_preflight else "--no-risk-preflight")
    command.append("--ranking-repair" if ranking_repair else "--skip-ranking-repair")
    command.append("--near-money-only" if near_money_only else "--full-strike-ladder")
    return command


def _timeout_seconds(
    *,
    cycles: int,
    interval_minutes: int,
    duration_hours: float,
    timeout_grace_seconds: int,
) -> int:
    if duration_hours > 0:
        budget = int(duration_hours * 3600)
    else:
        cycle_wait = max(cycles - 1, 0) * max(interval_minutes, 0) * 60
        per_cycle_budget = max(cycles, 1) * max(timeout_grace_seconds, 0)
        budget = cycle_wait + per_cycle_budget
    return max(1, budget + max(timeout_grace_seconds, 0))


def _guard_status(
    *,
    output_dir: Path,
    pid: int | None,
    process: dict[str, Any],
    metadata: dict[str, Any],
    latest: dict[str, Any],
) -> dict[str, Any]:
    metadata_pid_stale = _metadata_pid_stale(pid, process)
    live_pid = _single_live_r5_pid(process)
    guard_pid = live_pid if metadata_pid_stale and live_pid is not None else pid
    started_at = None if metadata_pid_stale else parse_datetime(metadata.get("started_at"))
    elapsed_seconds = _elapsed_seconds_since(started_at)
    timeout_seconds = None if metadata_pid_stale else _int_or_none(metadata.get("timeout_seconds"))
    duration_budget_seconds = (
        None if metadata_pid_stale else _int_or_none(metadata.get("duration_budget_seconds"))
    )
    running = bool(process.get("phase3bc_r5_process_running"))
    latest_generated_at = parse_datetime(latest.get("generated_at"))
    latest_age_seconds = _elapsed_seconds_since(latest_generated_at)
    summary = latest.get("summary") if isinstance(latest, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    r8_fields_available = "true_ranking_gap_after_repair" in summary
    cadence_minutes = _int_or_none(
        (latest.get("options") or {}).get("cadence_minutes") if isinstance(latest, dict) else None
    )
    if cadence_minutes is None:
        cadence_minutes = _int_or_none(metadata.get("cadence_minutes")) or 15
    freshness_minutes = _int_or_none(
        (latest.get("options") or {}).get("freshness_minutes") if isinstance(latest, dict) else None
    )
    if freshness_minutes is None:
        freshness_minutes = _int_or_none(metadata.get("freshness_minutes")) or cadence_minutes
    freshness_window_minutes = max(cadence_minutes, freshness_minutes, 1)

    status = "NO_UNATTENDED_JOB"
    if running and metadata_pid_stale:
        status = "RUNNING"
    elif running and timeout_seconds is not None and elapsed_seconds is not None:
        status = "OVERRUNNING" if elapsed_seconds > timeout_seconds else "RUNNING"
    elif running:
        status = "RUNNING_UNKNOWN_BUDGET"
    elif metadata and pid is not None:
        status = "STOPPED_WITH_STALE_PID"
    elif latest:
        status = "STOPPED"

    should_stop = status == "OVERRUNNING"
    stale_report = (
        latest_age_seconds is None or latest_age_seconds > freshness_window_minutes * 60
    )
    return {
        "phase": "3BC-R6",
        "status": status,
        "pid": guard_pid,
        "metadata_pid": pid,
        "metadata_pid_stale": metadata_pid_stale,
        "running": running,
        "pid_file": str(output_dir / UNATTENDED_PID_FILE),
        "metadata_file": str(output_dir / UNATTENDED_META_FILE),
        "stdout_path": str(metadata.get("stdout_path") or output_dir / UNATTENDED_STDOUT_FILE),
        "stderr_path": str(metadata.get("stderr_path") or output_dir / UNATTENDED_STDERR_FILE),
        "started_at": metadata.get("started_at"),
        "metadata_started_at": metadata.get("started_at"),
        "elapsed_seconds": elapsed_seconds,
        "duration_budget_seconds": duration_budget_seconds,
        "timeout_seconds": timeout_seconds,
        "seconds_until_timeout": _seconds_until_timeout(
            elapsed_seconds=elapsed_seconds,
            timeout_seconds=timeout_seconds,
        ),
        "should_stop": should_stop,
        "latest_generated_at": latest.get("generated_at"),
        "latest_age_seconds": latest_age_seconds,
        "cadence_minutes": cadence_minutes,
        "freshness_minutes": freshness_minutes,
        "freshness_window_minutes": freshness_window_minutes,
        "stale_report": stale_report,
        "watch_state": summary.get("watch_state"),
        "active_pure_crypto_rows": int(summary.get("active_pure_crypto_rows") or 0),
        "missing_or_stale_ranking_rows": int(summary.get("missing_or_stale_ranking_rows") or 0),
        "r8_fields_available": r8_fields_available,
        "true_ranking_gap_after_repair": _int_or_none(
            summary.get("true_ranking_gap_after_repair")
        ),
        "snapshot_stale_rows": _int_or_none(summary.get("snapshot_stale_rows")),
        "forecast_stale_rows": _int_or_none(summary.get("forecast_stale_rows")),
        "positive_ev_rows": _int_or_none(summary.get("positive_ev_rows")),
        "clean_execution_rows": _int_or_none(summary.get("clean_execution_rows")),
        "risk_ready_rows": _int_or_none(summary.get("risk_ready_rows")),
        "paper_ready_candidates": int(summary.get("paper_ready_candidates") or 0),
        "positive_ev_preflight_candidates": int(
            summary.get("positive_ev_preflight_candidates") or 0
        ),
        "primary_gap_after_refresh": summary.get("primary_gap_after_refresh"),
        "recommended_next_action": _guard_next_action(
            status=status,
            stale_report=stale_report,
            watch_state=str(summary.get("watch_state") or "UNKNOWN"),
            r8_fields_available=r8_fields_available,
            true_ranking_gap=_int_or_none(summary.get("true_ranking_gap_after_repair")),
            snapshot_stale=_int_or_none(summary.get("snapshot_stale_rows")),
            forecast_stale=_int_or_none(summary.get("forecast_stale_rows")),
        ),
    }


def _guard_next_action(
    *,
    status: str,
    stale_report: bool,
    watch_state: str,
    r8_fields_available: bool,
    true_ranking_gap: int | None,
    snapshot_stale: int | None,
    forecast_stale: int | None,
) -> str:
    if status == "OVERRUNNING":
        return (
            "Run phase3bc-r5-unattended-guard --stop-overrun, then restart the "
            "guarded crypto freshness watch."
        )
    if status == "RUNNING":
        if not r8_fields_available:
            return (
                "Crypto watch is running with a pre-R8 report; restart at a safe break "
                "to enable blocker attribution."
            )
        if (true_ranking_gap or 0) > 0:
            return "Crypto watch is running; next cycles should continue true ranking repair."
        if (snapshot_stale or 0) > 0:
            return "Crypto watch is running; next cycles should refresh exact-ticker snapshots."
        if (forecast_stale or 0) > 0:
            return "Crypto watch is running; next cycles should refresh crypto_v2 forecasts."
        return "Crypto watch is running inside its timeout budget."
    if status == "RUNNING_UNKNOWN_BUDGET":
        return "Crypto watch is running, but metadata does not include a timeout budget."
    if status == "STOPPED_WITH_STALE_PID":
        return "No crypto watch process is running; stale PID metadata can be overwritten."
    if stale_report:
        return "Start phase3bc-r5-unattended-start so crypto rankings refresh every 15 minutes."
    if watch_state == "REFRESH_RANKINGS":
        return "Run another R5 cycle or the guarded start; some active pure crypto rows are stale."
    if watch_state == "REFRESH_SNAPSHOTS":
        return "Run another R5 cycle; exact-ticker crypto snapshots are stale."
    if watch_state == "REFRESH_FORECASTS":
        return "Run another R5 cycle; crypto_v2 forecasts need to catch up to snapshots."
    if watch_state == "WAITING_FOR_POSITIVE_EV":
        return "Crypto data is fresh; keep the watch running until positive EV appears."
    return "Crypto watch status is current; leave the guarded runner active for freshness."


def _status_next_action(
    process: dict[str, Any],
    latest: dict[str, Any],
    guard: dict[str, Any],
) -> str:
    del latest
    if guard.get("should_stop"):
        return str(guard.get("recommended_next_action"))
    if process.get("phase3bc_r5_process_running"):
        return str(guard.get("recommended_next_action"))
    return str(guard.get("recommended_next_action"))


def _single_live_r5_pid(process: dict[str, Any]) -> int | None:
    pids: list[int] = []
    for value in process.get("phase3bc_r5_pids") or []:
        try:
            pids.append(int(value))
        except (TypeError, ValueError):
            continue
    return pids[0] if len(pids) == 1 else None


def _metadata_pid_stale(pid: int | None, process: dict[str, Any]) -> bool:
    live_pid = _single_live_r5_pid(process)
    return bool(
        pid is not None
        and live_pid is not None
        and pid != live_pid
        and process.get("phase3bc_r5_process_running")
    )


def _phase3bc_r5_process_status(pid: int | None) -> dict[str, Any]:
    if pid is not None:
        pid_running = _pid_matches_phase3bc_r5_watch(pid)
        if pid_running:
            return {
                "pid_running": True,
                "phase3bc_r5_process_running": True,
                "phase3bc_r5_pids": [pid],
                "status": "RUNNING",
                "discovered_by": "pid_file",
                "process_scan_skipped": True,
                "process_scan_limited": False,
            }
        running_pids, scan_limited = _phase3bc_r5_running_pids_with_limit()
        if running_pids:
            return {
                "pid_running": False,
                "phase3bc_r5_process_running": True,
                "phase3bc_r5_pids": running_pids,
                "status": "RUNNING",
                "discovered_by": "process_scan_after_pid_miss",
                "process_scan_skipped": False,
                "process_scan_limited": scan_limited,
            }
        return {
            "pid_running": False,
            "phase3bc_r5_process_running": False,
            "phase3bc_r5_pids": [],
            "status": "STOPPED",
            "discovered_by": "pid_file_stale",
            "process_scan_skipped": False,
            "process_scan_limited": scan_limited,
        }

    running_pids, scan_limited = _phase3bc_r5_running_pids_with_limit()
    return {
        "pid_running": False,
        "phase3bc_r5_process_running": bool(running_pids),
        "phase3bc_r5_pids": running_pids,
        "status": "RUNNING" if running_pids else "STOPPED",
        "discovered_by": "process_scan",
        "process_scan_skipped": False,
        "process_scan_limited": scan_limited,
    }


def _phase3bc_r5_running_pids() -> list[int]:
    pids, _scan_limited = _phase3bc_r5_running_pids_with_limit()
    return pids


def _phase3bc_r5_running_pids_with_limit() -> tuple[list[int], bool]:
    if os.name != "nt":
        return _posix_phase3bc_r5_running_pids()
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -like '*phase3bc-r5-crypto-freshness-watch*' "
            "-and $_.CommandLine -notlike '*phase3bc-r5-status*' "
            "-and $_.CommandLine -notlike '*phase3bc-r5-unattended*' } | "
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
        return [], True
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return sorted(set(pids)), False


def _posix_phase3bc_r5_running_pids(
    *,
    max_scan_seconds: float = 2.0,
    max_entries: int = 4096,
) -> tuple[list[int], bool]:
    proc = Path("/proc")
    if not proc.exists():
        return [], False
    pids: list[int] = []
    current_pid = os.getpid()
    started = time.monotonic()
    scanned = 0
    scan_limited = False
    for pid_dir in proc.iterdir():
        scanned += 1
        if scanned > max_entries or time.monotonic() - started > max_scan_seconds:
            scan_limited = True
            break
        if not pid_dir.name.isdigit():
            continue
        pid = int(pid_dir.name)
        if pid == current_pid:
            continue
        if _pid_matches_phase3bc_r5_watch(pid):
            pids.append(pid)
    return sorted(set(pids)), scan_limited


def _pid_matches_phase3bc_r5_watch(pid: int) -> bool:
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\" "
                "| Select-Object -ExpandProperty CommandLine)"
            ),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return False
        return _is_phase3bc_r5_watch_command(completed.stdout)
    if not _pid_exists(pid):
        return False
    return _is_phase3bc_r5_watch_command(_posix_cmdline(pid))


def _is_phase3bc_r5_watch_command(command: str) -> bool:
    if "phase3bc-r5-crypto-freshness-watch" not in command:
        return False
    if "kalshi-bot" not in command and "kalshi_predictor.cli" not in command:
        return False
    if "phase3bc-r5-status" in command or "phase3bc-r5-unattended" in command:
        return False
    if "pgrep" in command or "grep" in command:
        return False
    return True


def _terminate_pid(pid: int, *, grace_seconds: int) -> dict[str, Any]:
    if not _pid_exists(pid):
        return {"status": "ALREADY_STOPPED", "pid": pid}
    if os.name == "nt":
        return _terminate_pid_windows(pid)
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


def _terminate_pid_windows(pid: int) -> dict[str, Any]:
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
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", probe],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0
    try:
        os.kill(pid, 0)
    except PermissionError:
        # A process owned by another user is still alive even when this
        # operator cannot signal it. The command-line check that follows
        # decides whether it is the expected R5 watcher.
        return not _posix_pid_is_zombie(pid)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return not _posix_pid_is_zombie(pid)


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


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _path_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _tail_text(path: Path, *, max_lines: int = 20, max_bytes: int = 65536) -> list[str]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(-max_bytes, os.SEEK_END)
            raw = handle.read(max_bytes)
    except OSError:
        return []
    return raw.decode(encoding="utf-8", errors="replace").splitlines()[-max_lines:]


def _command_display(command: list[str]) -> str:
    return " ".join(_quote_arg(part) for part in command)


def _quote_arg(part: str) -> str:
    if not part or any(char.isspace() for char in part):
        return "'" + part.replace("'", "'\\''") + "'"
    return part


def _render_status_markdown(payload: dict[str, Any]) -> str:
    guard = payload.get("guard") or {}
    lines = [
        "# Phase 3BC-R5 Crypto Freshness Status",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Process status: {payload['process']['status']}",
        f"- Guard status: {guard.get('status')}",
        f"- PIDs: {payload['process']['phase3bc_r5_pids']}",
        f"- Latest report: {payload.get('latest_report_generated_at')}",
        f"- Watch state: {payload.get('latest_watch_state')}",
        f"- History rows: {payload['history_rows']}",
        "- Live/demo execution: false",
        "- Order submission/cancel/replace: false",
        "",
        "## Guard",
        "",
    ]
    for key in (
        "pid",
        "started_at",
        "elapsed_seconds",
        "timeout_seconds",
        "seconds_until_timeout",
        "should_stop",
        "stale_report",
        "watch_state",
        "missing_or_stale_ranking_rows",
        "true_ranking_gap_after_repair",
        "snapshot_stale_rows",
        "forecast_stale_rows",
        "positive_ev_rows",
        "clean_execution_rows",
        "risk_ready_rows",
        "paper_ready_candidates",
        "positive_ev_preflight_candidates",
        "primary_gap_after_refresh",
    ):
        lines.append(f"- {key}: {guard.get(key)}")
    lines.extend(["", "## Latest Summary", ""])
    for key, value in payload.get("latest_summary", {}).items():
        lines.append(f"- {key}: {value}")
    logs = payload["logs"]
    lines.extend(
        [
            "",
            "## Logs",
            "",
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
        "# Phase 3BC-R5 Crypto Freshness Guard",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Status: {payload['status']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Stop overrun requested: {action.get('requested_stop_overrun')}",
        f"- Terminated PID: {action.get('terminated_pid')}",
        f"- Termination result: {action.get('termination_result')}",
        "- Live/demo execution: false",
        "- Order submission/cancel/replace: false",
        "",
        "## After",
        "",
        f"- Running: {after_guard.get('running')}",
        f"- Should stop: {after_guard.get('should_stop')}",
        f"- Watch state: {after_guard.get('watch_state')}",
        f"- Stale report: {after_guard.get('stale_report')}",
        "",
        "## Recommended Next Action",
        "",
        payload["recommended_next_action"],
        "",
    ]
    return "\n".join(lines)
