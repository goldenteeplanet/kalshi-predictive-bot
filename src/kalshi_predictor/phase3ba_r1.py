from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location, get_session_factory, make_engine
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.schema import (
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
)
from kalshi_predictor.phase3bc_r6 import build_phase3bc_r5_status
from kalshi_predictor.utils.time import utc_now

PHASE3BA_R1_VERSION = "phase3ba_r1_writer_unlock_guarded_r5_restart_v1"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
R5_OUTPUT_DIR = Path("reports/phase3bc_r5")
R5_WATCH_MARKER = "phase3bc-r5-crypto-freshness-watch"
R5_GUARD_COMMAND = "phase3bc-r5-unattended-guard"
R5_START_COMMAND = "phase3bc-r5-unattended-start"


@dataclass(frozen=True)
class Phase3BAR1ArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    writer_unlock_path: Path
    r5_restart_status_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_r1_writer_unlock_report(
    *,
    output_dir: Path = Path("reports/phase3ba_r1"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    post_stop_wait_seconds: int = 60,
    poll_interval_seconds: float = 2.0,
    terminate_grace_seconds: int = 30,
    command_timeout_seconds: int = 90,
) -> Phase3BAR1ArtifactSet:
    payload = build_phase3ba_r1_writer_unlock(
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        post_stop_wait_seconds=post_stop_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        terminate_grace_seconds=terminate_grace_seconds,
        command_timeout_seconds=command_timeout_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    writer_unlock_path = output_dir / "writer_unlock.json"
    r5_restart_status_path = output_dir / "r5_restart_status.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    writer_unlock_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    r5_restart_status_path.write_text(_render_restart_status(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            writer_unlock_path,
            r5_restart_status_path,
            next_actions_path,
        ],
    )
    return Phase3BAR1ArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        writer_unlock_path=writer_unlock_path,
        r5_restart_status_path=r5_restart_status_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_r1_writer_unlock(
    *,
    output_dir: Path = Path("reports/phase3ba_r1"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    post_stop_wait_seconds: int = 60,
    poll_interval_seconds: float = 2.0,
    terminate_grace_seconds: int = 30,
    command_timeout_seconds: int = 90,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    started_at = utc_now()
    r5_output_dir = reports_dir / "phase3bc_r5"
    before_writer = _monitor_writer(resolved)
    before_status = build_phase3bc_r5_status(output_dir=r5_output_dir)
    metadata = _metadata(
        settings=resolved,
        command_args=command_args or [],
        generated_at=started_at.isoformat(),
    )
    validations = _preflight_validations(
        before_writer=before_writer,
        before_status=before_status,
    )
    steps: list[dict[str, Any]] = [
        {
            "step": "inspect_active_writer",
            "status": before_writer.get("status"),
            "safe_to_start_write": before_writer.get("safe_to_start_write"),
            "current_writer_pid": before_writer.get("current_writer_pid"),
            "current_writer_command": before_writer.get("current_writer_command"),
        },
        {
            "step": "inspect_r5_guard",
            "status": (before_status.get("guard") or {}).get("status"),
            "should_stop": (before_status.get("guard") or {}).get("should_stop"),
            "pid": (before_status.get("guard") or {}).get("pid"),
        },
    ]
    status = "BLOCKED_PRECONDITION_FAILED"
    stop_result: dict[str, Any] | None = None
    restart_result: dict[str, Any] | None = None
    writer_after_stop = before_writer
    writer_clear_observations: list[dict[str, Any]] = []
    after_restart_status: dict[str, Any] = before_status
    final_writer = before_writer

    if not validations["ok"]:
        status = validations["status"]
    else:
        stop_command = [
            R5_GUARD_COMMAND,
            "--output-dir",
            str(r5_output_dir),
            "--stop-overrun",
            "--terminate-grace-seconds",
            str(max(terminate_grace_seconds, 0)),
        ]
        stop_result = _run_registered_command(
            stop_command,
            timeout_seconds=max(command_timeout_seconds, 1),
        )
        steps.append(
            {
                "step": "run_guarded_stop",
                "command": _display_command(stop_command),
                **stop_result,
            }
        )
        if stop_result["status"] != "COMPLETED" or stop_result["returncode"] != 0:
            status = "BLOCKED_GUARD_STOP_FAILED"
        else:
            writer_after_stop, writer_clear_observations = _wait_for_writer_clear(
                settings=resolved,
                old_pid=before_writer.get("current_writer_pid"),
                wait_seconds=max(post_stop_wait_seconds, 0),
                poll_interval_seconds=max(poll_interval_seconds, 0.1),
            )
            steps.append(
                {
                    "step": "confirm_writer_clear",
                    "status": writer_after_stop.get("status"),
                    "safe_to_start_write": writer_after_stop.get("safe_to_start_write"),
                    "current_writer_pid": writer_after_stop.get("current_writer_pid"),
                    "observations": writer_clear_observations,
                }
            )
            if not bool(writer_after_stop.get("safe_to_start_write")) or writer_after_stop.get(
                "current_writer_pid"
            ):
                status = "BLOCKED_WRITER_DID_NOT_CLEAR"
            else:
                start_command = [R5_START_COMMAND, "--output-dir", str(r5_output_dir)]
                restart_result = _run_registered_command(
                    start_command,
                    timeout_seconds=max(command_timeout_seconds, 1),
                )
                steps.append(
                    {
                        "step": "restart_guarded_r5",
                        "command": _display_command(start_command),
                        **restart_result,
                    }
                )
                after_restart_status = build_phase3bc_r5_status(output_dir=r5_output_dir)
                final_writer = _monitor_writer(resolved)
                running_pids = _running_r5_pids(after_restart_status)
                old_pid = _target_stop_pid(before_writer, before_status)
                if restart_result["status"] != "COMPLETED" or restart_result["returncode"] != 0:
                    status = "BLOCKED_R5_RESTART_FAILED"
                elif len(running_pids) != 1:
                    status = "BLOCKED_DUPLICATE_OR_MISSING_R5"
                elif old_pid is not None and running_pids == [old_pid]:
                    status = "BLOCKED_OLD_R5_PID_STILL_RUNNING"
                else:
                    status = "RESTARTED_ONE_R5_WATCHER"

    final_summary = _summary(
        status=status,
        before_writer=before_writer,
        before_status=before_status,
        writer_after_stop=writer_after_stop,
        after_restart_status=after_restart_status,
        final_writer=final_writer,
    )
    payload = {
        **metadata,
        "phase": "3BA-R1",
        "phase_version": PHASE3BA_R1_VERSION,
        "mode": "PAPER_ONLY_WRITER_UNLOCK_GUARDED_R5_RESTART",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "r5_output_dir": str(r5_output_dir),
        "status": status,
        "summary": final_summary,
        "preflight_validations": validations,
        "before_writer": before_writer,
        "before_r5_status": before_status,
        "stop_result": stop_result,
        "writer_after_stop": writer_after_stop,
        "writer_clear_observations": writer_clear_observations,
        "restart_result": restart_result,
        "after_restart_r5_status": after_restart_status,
        "final_writer": final_writer,
        "steps": steps,
        "acceptance": _acceptance(status, final_summary),
        "next_action": _next_action(status, final_summary),
        "operator_guardrails": [
            "Only the registered Phase 3BC-R5 unattended guard may stop the overrun watcher.",
            "No arbitrary process kill is allowed from this phase.",
            "No live/demo exchange orders are submitted, canceled, replaced, or amended.",
            "No paper trades are created.",
            "Do not start a second R5 watcher if one is already running.",
        ],
    }
    payload["completed_at"] = utc_now().isoformat()
    return payload


def _preflight_validations(
    *,
    before_writer: dict[str, Any],
    before_status: dict[str, Any],
) -> dict[str, Any]:
    writer_pid = _int_or_none(before_writer.get("current_writer_pid"))
    writer_command = str(before_writer.get("current_writer_command") or "")
    guard = before_status.get("guard") or {}
    guard_pid = _int_or_none(guard.get("pid"))
    running_pids = _running_r5_pids(before_status)
    writer_is_r5 = _is_r5_watch_command(writer_command)
    pid_matches_r5 = writer_pid is not None and (
        writer_pid == guard_pid or writer_pid in running_pids
    )
    guard_overrun = guard.get("status") == "OVERRUNNING" and guard.get("should_stop") is True
    if writer_pid is None and guard_overrun and guard_pid is not None and guard_pid in running_pids:
        return {
            "ok": True,
            "status": "PRECONDITIONS_MET_NO_ACTIVE_WRITER_R5_OVERRUNNING",
            "reason": (
                "db-writer-monitor is clear, but the guarded R5 process is still "
                "overrunning and should be stopped through the registered guard."
            ),
            "writer_pid": writer_pid,
            "guard_pid": guard_pid,
            "running_r5_pids": running_pids,
            "writer_is_r5": False,
            "guard_overrun": guard_overrun,
        }
    if writer_pid is None:
        return {
            "ok": False,
            "status": "NO_ACTIVE_WRITER_TO_UNLOCK",
            "reason": "db-writer-monitor did not report an active writer.",
            "writer_pid": writer_pid,
            "guard_pid": guard_pid,
            "running_r5_pids": running_pids,
        }
    if not writer_is_r5 or not pid_matches_r5:
        return {
            "ok": False,
            "status": "BLOCKED_WRITER_NOT_MATCHING_R5",
            "reason": "Active writer is not the guarded R5 crypto freshness watcher.",
            "writer_pid": writer_pid,
            "writer_is_r5": writer_is_r5,
            "guard_pid": guard_pid,
            "running_r5_pids": running_pids,
        }
    if not guard_overrun:
        return {
            "ok": False,
            "status": "BLOCKED_R5_NOT_OVERRUNNING",
            "reason": "R5 guard did not report OVERRUNNING with should_stop=true.",
            "writer_pid": writer_pid,
            "guard_status": guard.get("status"),
            "guard_should_stop": guard.get("should_stop"),
        }
    return {
        "ok": True,
        "status": "PRECONDITIONS_MET",
        "writer_pid": writer_pid,
        "guard_pid": guard_pid,
        "running_r5_pids": running_pids,
        "writer_is_r5": writer_is_r5,
        "guard_overrun": guard_overrun,
    }


def _wait_for_writer_clear(
    *,
    settings: Settings,
    old_pid: Any,
    wait_seconds: int,
    poll_interval_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + max(wait_seconds, 0)
    observations: list[dict[str, Any]] = []
    old_pid_int = _int_or_none(old_pid)
    while True:
        current = _monitor_writer(settings)
        observation = {
            "observed_at": utc_now().isoformat(),
            "status": current.get("status"),
            "safe_to_start_write": current.get("safe_to_start_write"),
            "current_writer_pid": current.get("current_writer_pid"),
            "old_writer_pid_still_reported": old_pid_int is not None
            and _int_or_none(current.get("current_writer_pid")) == old_pid_int,
        }
        observations.append(observation)
        if bool(current.get("safe_to_start_write")) and not current.get("current_writer_pid"):
            return current, observations
        if time.monotonic() >= deadline:
            return current, observations
        time.sleep(poll_interval_seconds)


def _run_registered_command(command_args: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    argv = [sys.executable, "-m", "kalshi_predictor.cli", *command_args]
    try:
        completed = subprocess.run(
            argv,
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "returncode": None,
            "timeout_seconds": timeout_seconds,
            "stdout": (exc.stdout or "")[-4000:],
            "stderr": (exc.stderr or "")[-4000:],
        }
    except OSError as exc:
        return {
            "status": "FAILED_TO_START",
            "returncode": None,
            "timeout_seconds": timeout_seconds,
            "error": str(exc),
        }
    return {
        "status": "COMPLETED",
        "returncode": completed.returncode,
        "timeout_seconds": timeout_seconds,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def _metadata(
    *,
    settings: Settings,
    command_args: list[str],
    generated_at: str,
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    redacted_db_url = redact_database_url(db_url)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redacted_db_url,
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(db_url),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-r1-writer-unlock",
            "argv": command_args,
        },
        "data_watermark": _data_watermark(db_url),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "safety_flags": _safety_flags(),
    }


def _database_fingerprint(db_url: str) -> dict[str, Any]:
    redacted = redact_database_url(db_url)
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {
            "kind": "non_sqlite",
            "database_url_hash": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
        }
    if str(sqlite_path) == ":memory:":
        return {"kind": "sqlite_memory", "path": ":memory:"}
    path = sqlite_path.expanduser().resolve()
    if not path.exists():
        return {"kind": "missing_sqlite_file", "path": str(path)}
    stat = path.stat()
    payload = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _data_watermark(db_url: str) -> dict[str, Any]:
    try:
        engine = make_engine(db_url)
        session_factory = get_session_factory(engine)
        with session_factory() as session:
            return {
                "latest_market_seen_at": _latest_iso(session, Market.last_seen_at),
                "latest_snapshot_at": _latest_iso(session, MarketSnapshot.captured_at),
                "latest_forecast_at": _latest_iso(session, Forecast.forecasted_at),
                "latest_ranking_at": _latest_iso(session, MarketRanking.ranked_at),
                "latest_paper_order_at": _latest_iso(session, PaperOrder.created_at),
                "latest_paper_pnl_at": _latest_iso(session, PaperPnl.calculated_at),
            }
    except Exception as exc:  # noqa: BLE001 - metadata must not prevent guard cleanup.
        return {
            "status": "UNAVAILABLE",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            engine.dispose()  # type: ignore[possibly-undefined]
        except Exception:
            pass


def _latest_iso(session: Any, column: Any) -> str | None:
    value = session.scalar(select(func.max(column)))
    return value.isoformat() if hasattr(value, "isoformat") else value


def _migration_revision(db_url: str) -> str | None:
    try:
        engine = make_engine(db_url)
        with engine.connect() as connection:
            value = connection.execute(
                text("select version_num from alembic_version limit 1")
            ).scalar()
            return str(value) if value is not None else None
    except Exception:
        return None
    finally:
        try:
            engine.dispose()  # type: ignore[possibly-undefined]
        except Exception:
            pass


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "live_trading_enabled": False,
        "demo_exchange_writes_enabled": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "stops_arbitrary_processes": False,
        "allowed_process_stop": "guarded_overrun_phase3bc_r5_only",
        "force_kill_after_failed_clear": False,
        "starts_duplicate_watchers": False,
        "normal_link_remediation": False,
        "accelerate_learning": False,
    }


def _summary(
    *,
    status: str,
    before_writer: dict[str, Any],
    before_status: dict[str, Any],
    writer_after_stop: dict[str, Any],
    after_restart_status: dict[str, Any],
    final_writer: dict[str, Any],
) -> dict[str, Any]:
    before_guard = before_status.get("guard") or {}
    after_guard = after_restart_status.get("guard") or {}
    running_pids = _running_r5_pids(after_restart_status)
    old_pid = _int_or_none(before_writer.get("current_writer_pid"))
    old_r5_pid = _int_or_none(before_guard.get("pid"))
    target_pid = old_pid if old_pid is not None else old_r5_pid
    final_writer_pid = _int_or_none(final_writer.get("current_writer_pid"))
    return {
        "status": status,
        "old_writer_pid": old_pid,
        "old_r5_pid": old_r5_pid,
        "target_stop_pid": target_pid,
        "old_writer_command": before_writer.get("current_writer_command"),
        "old_writer_was_r5": _is_r5_watch_command(before_writer.get("current_writer_command")),
        "before_guard_status": before_guard.get("status"),
        "before_guard_should_stop": before_guard.get("should_stop"),
        "writer_clear_after_stop": bool(writer_after_stop.get("safe_to_start_write"))
        and not writer_after_stop.get("current_writer_pid"),
        "old_writer_pid_cleared": old_pid is None
        or _int_or_none(writer_after_stop.get("current_writer_pid")) != old_pid,
        "target_r5_pid_cleared_after_restart": target_pid is None
        or target_pid not in running_pids,
        "running_r5_watchers_after_restart": len(running_pids),
        "running_r5_pids_after_restart": running_pids,
        "exactly_one_r5_watcher_running": len(running_pids) == 1,
        "new_r5_pid": running_pids[0] if len(running_pids) == 1 else None,
        "new_r5_is_old_pid": (
            len(running_pids) == 1 and target_pid is not None and running_pids[0] == target_pid
        ),
        "after_guard_status": after_guard.get("status"),
        "after_guard_should_stop": after_guard.get("should_stop"),
        "final_writer_status": final_writer.get("status"),
        "final_writer_pid": final_writer_pid,
        "final_writer_is_new_r5": final_writer_pid is not None and final_writer_pid in running_pids,
        "write_capable_local_commands_unblocked_after_stop": bool(
            writer_after_stop.get("safe_to_start_write")
        )
        and not writer_after_stop.get("current_writer_pid"),
        "live_demo_execution_blocked": True,
        "order_submission_cancel_replace_blocked": True,
        "paper_trade_creation_blocked": True,
    }


def _acceptance(status: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "overrun_r5_stopped_through_guard": status
        in {
            "RESTARTED_ONE_R5_WATCHER",
            "BLOCKED_R5_RESTART_FAILED",
            "BLOCKED_DUPLICATE_OR_MISSING_R5",
        }
        and summary["target_r5_pid_cleared_after_restart"],
        "target_overrun_r5_pid_cleared": summary["target_r5_pid_cleared_after_restart"],
        "db_writer_monitor_no_longer_reports_old_pid": summary["old_writer_pid_cleared"],
        "exactly_one_guarded_r5_watcher_running": summary["exactly_one_r5_watcher_running"],
        "no_live_or_demo_exchange_writes": True,
        "no_paper_trades_created": True,
        "write_capable_local_commands_unblocked_after_stop": summary[
            "write_capable_local_commands_unblocked_after_stop"
        ],
        "next_actions_has_exact_command": bool(_next_action(status, summary).get("command")),
    }


def _next_action(status: str, summary: dict[str, Any]) -> dict[str, Any]:
    if status == "RESTARTED_ONE_R5_WATCHER":
        if summary.get("final_writer_is_new_r5"):
            return {
                "stage": "WAIT_FOR_NEW_R5_CYCLE_TO_RELEASE_WRITER",
                "command": "kalshi-bot db-writer-monitor --json",
                "reason": (
                    "Old overrun writer cleared and one new R5 watcher is running; "
                    "wait for the new cycle to release the DB lane before "
                    "weather/opportunity writes."
                ),
            }
        return {
            "stage": "RUN_WEATHER_OPPORTUNITY_RANKINGS",
            "command": (
                "kalshi-bot find-opportunities --model-name weather_v2 --limit 100 "
                "--output reports/weather_opportunities.md"
            ),
            "reason": "Writer lane cleared and exactly one R5 watcher is running.",
        }
    if status == "BLOCKED_WRITER_DID_NOT_CLEAR":
        return {
            "stage": "WAIT_OR_INSPECT_WRITER",
            "command": "kalshi-bot db-writer-monitor --json",
            "reason": (
                "Guarded stop returned, but db-writer-monitor still reports an active "
                "writer. No force kill was attempted."
            ),
        }
    if status == "BLOCKED_GUARD_STOP_FAILED":
        return {
            "stage": "INSPECT_GUARD_FAILURE",
            "command": "kalshi-bot phase3bc-r5-unattended-guard --output-dir reports/phase3bc_r5",
            "reason": "The registered guarded stop command did not complete successfully.",
        }
    if status == "BLOCKED_R5_RESTART_FAILED":
        return {
            "stage": "RETRY_R5_START_AFTER_WRITER_CHECK",
            "command": "kalshi-bot db-writer-monitor --json",
            "reason": "Writer cleared, but the guarded R5 restart command failed.",
        }
    if status == "BLOCKED_DUPLICATE_OR_MISSING_R5":
        return {
            "stage": "INSPECT_R5_PROCESS_SET",
            "command": "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
            "reason": "Post-restart process scan did not show exactly one guarded R5 watcher.",
        }
    return {
        "stage": "PRECONDITION_BLOCKED",
        "command": "kalshi-bot db-writer-monitor --json",
        "reason": "Preconditions failed; this phase only stops a matching overrun R5 writer.",
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R1 Writer Unlock Executive Summary")
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Old writer PID: `{summary['old_writer_pid']}`",
            f"- Old writer was R5: `{summary['old_writer_was_r5']}`",
            f"- Before guard status: `{summary['before_guard_status']}`",
            f"- Writer clear after stop: `{summary['writer_clear_after_stop']}`",
            "- Running R5 watchers after restart: "
            f"`{summary['running_r5_watchers_after_restart']}`",
            f"- Running R5 PIDs after restart: `{summary['running_r5_pids_after_restart']}`",
            f"- Final writer PID: `{summary['final_writer_pid']}`",
            f"- Live/demo execution blocked: `{summary['live_demo_execution_blocked']}`",
            "- Order submit/cancel/replace blocked: "
            f"`{summary['order_submission_cancel_replace_blocked']}`",
            f"- Paper trade creation blocked: `{summary['paper_trade_creation_blocked']}`",
            "",
            "## Next Action",
            "",
            f"- Stage: `{payload['next_action']['stage']}`",
            f"- Command: `{payload['next_action']['command']}`",
            f"- Reason: {payload['next_action']['reason']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_restart_status(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R1 R5 Restart Status")
    lines.extend(
        [
            "",
            "## Guarded Stop",
            "",
            f"- Preconditions: `{payload['preflight_validations']['status']}`",
            f"- Stop command status: `{(payload.get('stop_result') or {}).get('status')}`",
            f"- Stop command return code: `{(payload.get('stop_result') or {}).get('returncode')}`",
            f"- Old writer PID cleared: `{summary['old_writer_pid_cleared']}`",
            "",
            "## Guarded Restart",
            "",
            f"- Restart command status: `{(payload.get('restart_result') or {}).get('status')}`",
            "- Restart command return code: "
            f"`{(payload.get('restart_result') or {}).get('returncode')}`",
            f"- Exactly one R5 watcher running: `{summary['exactly_one_r5_watcher_running']}`",
            f"- New R5 PID: `{summary['new_r5_pid']}`",
            f"- New R5 is old PID: `{summary['new_r5_is_old_pid']}`",
            f"- Final writer is new R5: `{summary['final_writer_is_new_r5']}`",
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, title="# Phase 3BA-R1 Next Actions")
    lines.extend(
        [
            "",
            "## Exact Next Operator Command",
            "",
            f"```bash\n{payload['next_action']['command']}\n```",
            "",
            f"- Stage: `{payload['next_action']['stage']}`",
            f"- Reason: {payload['next_action']['reason']}",
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in payload["operator_guardrails"]:
        lines.append(f"- {guardrail}")
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], *, title: str) -> list[str]:
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Data watermark: `{json.dumps(payload['data_watermark'], sort_keys=True, default=str)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Safety flags: `{json.dumps(payload['safety_flags'], sort_keys=True)}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
    ]


def _monitor_writer(settings: Settings) -> dict[str, Any]:
    try:
        return db_writer_monitor(settings=settings)
    except Exception as exc:  # noqa: BLE001 - fail closed into report.
        return {
            "status": "UNKNOWN",
            "safe_to_start_write": False,
            "current_writer_pid": None,
            "current_writer_command": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _target_stop_pid(before_writer: dict[str, Any], before_status: dict[str, Any]) -> int | None:
    writer_pid = _int_or_none(before_writer.get("current_writer_pid"))
    if writer_pid is not None:
        return writer_pid
    guard = before_status.get("guard") or {}
    return _int_or_none(guard.get("pid"))


def _running_r5_pids(status_payload: dict[str, Any]) -> list[int]:
    process = status_payload.get("process") or {}
    pids: list[int] = []
    for pid in process.get("phase3bc_r5_pids") or []:
        parsed = _int_or_none(pid)
        if parsed is not None:
            pids.append(parsed)
    return sorted(set(pids))


def _is_r5_watch_command(command: Any) -> bool:
    lowered = str(command or "").lower()
    return (
        R5_WATCH_MARKER in lowered
        and "phase3bc-r5-status" not in lowered
        and "phase3bc-r5-unattended" not in lowered
        and "grep" not in lowered
    )


def _display_command(args: list[str]) -> str:
    return "kalshi-bot " + " ".join(args)


def _git_value(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "UNKNOWN"


def _git_dirty_status() -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    if result.returncode != 0:
        return "UNKNOWN"
    return "dirty" if result.stdout.strip() else "clean"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
