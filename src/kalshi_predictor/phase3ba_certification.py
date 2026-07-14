from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.schema import Forecast, Market, MarketRanking, MarketSnapshot
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ba_status import build_phase3ba_status
from kalshi_predictor.utils.time import utc_now

PHASE3BA_R9_VERSION = "phase3ba_r9_paper_only_certification_v1"
FOCUSED_TESTS = (
    "tests/test_phase3ba_r1_writer_unlock.py",
    "tests/test_phase3ba_r2_weather_ranking_activation.py",
    "tests/test_phase3ba_r3_weather_paper_gate.py",
    "tests/test_phase3ba_r4_crypto_executable_book_watch.py",
    "tests/test_phase3ba_r5_paper_ready_truth.py",
    "tests/test_phase3ba_r6_noncrypto_engine_backlog.py",
    "tests/test_phase3ba_r7_composite_market_plan.py",
    "tests/test_phase3ba_status.py",
)
CRYPTO_ACCEPTED_BLOCKERS = {
    "EXECUTABLE_BOOK_MISSING",
    "LIQUIDITY_TOO_LOW",
    "PAPER_READY",
    "POSITIVE_EV_NO_BOOK",
    "SPREAD_TOO_WIDE",
    "WAITING_FOR_EXECUTABLE_BOOK",
    "ZERO_VISIBLE_DEPTH",
}
WEATHER_ACCEPTED_BLOCKERS = {
    "EV_NOT_POSITIVE",
    "EXECUTABLE_EV_NOT_POSITIVE",
    "FORECAST_MISSING",
    "LIQUIDITY_TOO_LOW",
    "PAPER_READY",
    "RANKING_MISSING",
    "RISK_NOT_ELIGIBLE",
    "SETTLEMENT_TERMS_UNKNOWN",
    "SNAPSHOT_MISSING",
    "SNAPSHOT_STALE",
    "SPREAD_TOO_WIDE",
}


@dataclass(frozen=True)
class Phase3BACertificationArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_paper_certification_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_cert"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    test_timeout_seconds: int = 180,
) -> Phase3BACertificationArtifactSet:
    payload = build_phase3ba_paper_certification(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        test_timeout_seconds=test_timeout_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "certification.json"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(manifest_path, [executive_summary_path, json_path, next_actions_path])
    return Phase3BACertificationArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_paper_certification(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_cert"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    test_timeout_seconds: int = 180,
) -> dict[str, Any]:
    generated_at = utc_now()
    resolved = settings or get_settings()
    status = build_phase3ba_status(
        session,
        output_dir=reports_dir / "phase3ba_status",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3ba-paper-certification", "embedded_status"],
    )
    tests = _run_focused_tests(timeout_seconds=test_timeout_seconds)
    checks = _certification_checks(status=status, tests=tests)
    summary = _summary(status=status, checks=checks, tests=tests)
    return {
        **_metadata(
            session,
            settings=resolved,
            generated_at=generated_at.isoformat(),
            command_args=command_args or [],
        ),
        "phase": "3BA-R9",
        "phase_version": PHASE3BA_R9_VERSION,
        "mode": "PAPER_READ_ONLY_CERTIFICATION",
        "output_dir": str(output_dir),
        "status_truth": status,
        "focused_tests": tests,
        "checks": checks,
        "summary": summary,
        "status": "CERTIFIED_PAPER_ONLY" if summary["certified"] else "CERTIFICATION_BLOCKED",
        "next_action": _next_action(summary, status),
        "operator_should_not_run": _operator_should_not_run(status),
        "safety_flags": _safety_flags(),
    }


def _certification_checks(*, status: dict[str, Any], tests: dict[str, Any]) -> dict[str, Any]:
    summary = status["summary"]
    command_checks = status["command_checks"]
    r5_pids = _running_r5_pids(status["r5_status"])
    crypto_blocker = str(summary.get("crypto_first_blocker") or "")
    weather_blocker = str(summary.get("weather_first_blocker") or "")
    checks = {
        "ui_and_reports_agree": _ui_and_reports_agree(status),
        "no_active_unsafe_writer": _no_active_unsafe_writer(status),
        "one_r5_watcher_max": len(r5_pids) <= 1,
        "crypto_waiting_for_executable_book_or_ready": (
            bool(summary.get("crypto_paper_ready")) or crypto_blocker in CRYPTO_ACCEPTED_BLOCKERS
        ),
        "weather_ranked_or_exact_blockers": (
            bool(summary.get("weather_paper_ready")) or weather_blocker in WEATHER_ACCEPTED_BLOCKERS
        ),
        "paper_ready_truth_current": bool(status.get("dashboard_truth")),
        "noncrypto_backlog_clear": bool(
            (status.get("category_backlog") or {}).get("immediate_work", {}).get("category")
        ),
        "composites_parked": (
            (status.get("composite_parking") or {}).get("parking_status")
            == "PARKED_OUTSIDE_SINGLE_MARKET_LINK_REMEDIATION"
        ),
        "recommended_commands_registered": command_checks["all_recommended_commands_registered"],
        "no_forbidden_trade_commands_recommended": (
            not command_checks["contains_forbidden_trade_command"]
        ),
        "dashboard_truth_available": bool(status.get("dashboard_truth")),
        "focused_tests_passed": tests["status"] == "PASSED",
        "no_live_demo_execution": not status["live_or_demo_execution"]
        and not status["order_submission"]
        and not status["order_cancel_replace"],
        "no_fake_evidence": _no_fake_evidence(status),
        "thresholds_not_lowered": not status["thresholds_lowered"],
    }
    return checks


def _summary(
    *,
    status: dict[str, Any],
    checks: dict[str, Any],
    tests: dict[str, Any],
) -> dict[str, Any]:
    status_summary = status["summary"]
    failed = [key for key, value in checks.items() if not value]
    return {
        "certified": not failed,
        "failed_checks": failed,
        "app_safe": status_summary["app_safe"],
        "active_writer": status_summary["active_writer"],
        "active_writer_pid": status_summary["active_writer_pid"],
        "r5_running": status_summary["r5_running"],
        "r5_pids": _running_r5_pids(status["r5_status"]),
        "r5_watcher_count": len(_running_r5_pids(status["r5_status"])),
        "crypto_paper_ready": status_summary["crypto_paper_ready"],
        "crypto_first_blocker": status_summary["crypto_first_blocker"],
        "weather_paper_ready": status_summary["weather_paper_ready"],
        "weather_first_blocker": status_summary["weather_first_blocker"],
        "paper_ready_rows": status_summary["paper_ready_rows"],
        "true_first_blocker": status_summary["true_first_blocker"],
        "phase3ap_is_stale": status_summary["phase3ap_is_stale"],
        "noncrypto_next": status_summary["what_codex_should_build_next"],
        "composite_rows_parked": status_summary["composite_rows_parked"],
        "focused_tests_status": tests["status"],
        "focused_tests_exit_code": tests.get("returncode"),
    }


def _next_action(summary: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    if summary["certified"]:
        return {
            "stage": "RUN_R8_OPERATOR_NEXT_COMMAND",
            "command": (
                "kalshi-bot phase3ba-status --output-dir "
                "reports/phase3ba_status --reports-dir reports"
            ),
            "reason": (
                "Paper-only certification passed; continue with the one-command status "
                "workflow."
            ),
            "allow_paper_trade_creation": False,
        }
    if "focused_tests_passed" in summary["failed_checks"]:
        return {
            "stage": "FIX_FOCUSED_TEST_FAILURE",
            "command": "pytest " + " ".join(FOCUSED_TESTS) + " -q",
            "reason": "Focused certification tests failed or timed out.",
            "allow_paper_trade_creation": False,
        }
    if "no_active_unsafe_writer" in summary["failed_checks"]:
        return {
            "stage": "WAIT_FOR_WRITER_CLEAR",
            "command": "kalshi-bot db-writer-monitor --json",
            "reason": "Certification is blocked by an unsafe active writer.",
            "allow_paper_trade_creation": False,
        }
    return {
        "stage": "REFRESH_PHASE3BA_STATUS",
        "command": str(
            (status.get("next_action") or {}).get("command")
            or "kalshi-bot phase3ba-status"
        ),
        "reason": "Certification checks are blocked; refresh status and address failed checks.",
        "allow_paper_trade_creation": False,
    }


def _run_focused_tests(*, timeout_seconds: int) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", *FOCUSED_TESTS, "-q"]
    existing_tests = [path for path in FOCUSED_TESTS if Path(path).exists()]
    if len(existing_tests) != len(FOCUSED_TESTS):
        missing = sorted(set(FOCUSED_TESTS) - set(existing_tests))
        return {
            "status": "MISSING_TESTS",
            "command": command,
            "missing_tests": missing,
            "returncode": None,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    try:
        result = subprocess.run(
            command,
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "command": command,
            "returncode": None,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": _tail(exc.stdout or ""),
            "stderr_tail": _tail(exc.stderr or ""),
        }
    return {
        "status": "PASSED" if result.returncode == 0 else "FAILED",
        "command": command,
        "returncode": result.returncode,
        "timeout_seconds": timeout_seconds,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


def _ui_and_reports_agree(status: dict[str, Any]) -> bool:
    dashboard = status.get("dashboard_truth") or {}
    summary = status.get("summary") or {}
    metrics = dashboard.get("metrics") or {}
    return (
        metrics.get("paper_ready_rows") == summary.get("paper_ready_rows")
        and metrics.get("positive_ev_rows") == summary.get("positive_ev_rows")
        and bool(dashboard.get("summary"))
    )


def _no_active_unsafe_writer(status: dict[str, Any]) -> bool:
    writer = status.get("writer") or {}
    if not writer.get("current_writer_pid"):
        return True
    command = str(writer.get("current_writer_command") or "").lower()
    return "phase3bc-r5-crypto-freshness-watch" in command


def _running_r5_pids(r5_status: dict[str, Any]) -> list[int]:
    process = r5_status.get("process") or {}
    pids: list[int] = []
    for pid in process.get("phase3bc_r5_pids") or []:
        try:
            pids.append(int(str(pid)))
        except (TypeError, ValueError):
            continue
    if not pids:
        try:
            pid = int(str(r5_status.get("pid")))
        except (TypeError, ValueError):
            pid = None
        if pid is not None and str(process.get("status") or "").upper() == "RUNNING":
            pids.append(pid)
    return sorted(set(pids))


def _no_fake_evidence(status: dict[str, Any]) -> bool:
    safety = status.get("safety_flags") or {}
    composite = status.get("composite_parking") or {}
    return (
        not safety.get("recommended_command_contains_forbidden_trade_command")
        and composite.get("exact_component_evidence_rows") in {0, None}
    )


def _operator_should_not_run(status: dict[str, Any]) -> list[str]:
    blocked = list(status.get("operator_should_not_run") or [])
    blocked.extend(
        [
            "Do not lower thresholds for certification.",
            "Do not fabricate source, forecast, ranking, book, or component evidence.",
            "Do not treat stale Phase 3AP as current truth.",
        ]
    )
    return sorted(dict.fromkeys(blocked))


def _metadata(
    session: Session,
    *,
    settings: Settings,
    generated_at: str,
    command_args: list[str],
) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    return {
        "generated_at": generated_at,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": _git_dirty_status(),
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": redact_database_url(db_url),
        "database_fingerprint": _database_fingerprint(db_url),
        "database_location": describe_db_location(db_url),
        "migration_revision": _migration_revision(session),
        "timezone": getattr(settings, "timezone", None) or "UTC",
        "command_arguments": {
            "command": "kalshi-bot phase3ba-paper-certification",
            "argv": command_args,
        },
        "data_watermark": _data_watermark(session),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "diagnostic_only": True,
        "certification_only": True,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "lowers_thresholds": False,
        "fabricates_evidence": False,
        "normal_single_market_remediation_for_composites": False,
    }


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "latest_market_seen_at": _latest_iso(session, Market.last_seen_at),
        "latest_snapshot_captured_at": _latest_iso(session, MarketSnapshot.captured_at),
        "latest_forecasted_at": _latest_iso(session, Forecast.forecasted_at),
        "latest_ranking_at": _latest_iso(session, MarketRanking.ranked_at),
    }


def _latest_iso(session: Session, column: Any) -> str | None:
    value = session.scalar(func.max(column))
    return value.isoformat() if hasattr(value, "isoformat") else value


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
    payload = {"path": str(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return {
        "kind": "sqlite_file_stat",
        **payload,
        "fingerprint": hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def _migration_revision(session: Session) -> str | None:
    try:
        return session.execute(text("select version_num from alembic_version limit 1")).scalar()
    except Exception:
        return None


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


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R9 Paper-Only Certification")
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{payload['status']}`",
            f"- Certified: `{summary['certified']}`",
            f"- Failed checks: `{summary['failed_checks']}`",
            f"- Active writer: `{summary['active_writer']}` pid=`{summary['active_writer_pid']}`",
            f"- R5 running: `{summary['r5_running']}`",
            f"- R5 watcher count: `{summary['r5_watcher_count']}`",
            f"- Crypto paper-ready: `{summary['crypto_paper_ready']}`",
            f"- Crypto blocker: `{summary['crypto_first_blocker']}`",
            f"- Weather paper-ready: `{summary['weather_paper_ready']}`",
            f"- Weather blocker: `{summary['weather_first_blocker']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- True first blocker: `{summary['true_first_blocker']}`",
            f"- Non-crypto next: `{summary['noncrypto_next']}`",
            f"- Composite rows parked: `{summary['composite_rows_parked']}`",
            f"- Focused tests: `{summary['focused_tests_status']}`",
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["checks"].items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    next_action = payload["next_action"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R9 Next Actions")
    lines.extend(
        [
            "",
            "## Next Operator Command",
            "",
            "```bash",
            next_action["command"],
            "```",
            "",
            f"- Stage: `{next_action['stage']}`",
            f"- Reason: {next_action['reason']}",
            f"- Paper trade creation allowed: `{next_action['allow_paper_trade_creation']}`",
            "",
            "## Do Not Run",
            "",
        ]
    )
    for item in payload["operator_should_not_run"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _metadata_lines(payload: dict[str, Any], *, title: str) -> list[str]:
    return [
        title,
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['git_commit']}`",
        f"- DB fingerprint: `{json.dumps(payload['database_fingerprint'], sort_keys=True)}`",
        f"- Command args: `{json.dumps(payload['command_arguments'], sort_keys=True)}`",
        f"- Data watermark: `{json.dumps(payload['data_watermark'], sort_keys=True)}`",
        f"- Safety flags: `{json.dumps(payload['safety_flags'], sort_keys=True)}`",
        f"- Live/demo execution: `{payload['live_or_demo_execution']}`",
        "- Order submission/cancel/replace: "
        f"`{payload['order_submission'] or payload['order_cancel_replace']}`",
        f"- Paper trade creation: `{payload['paper_trade_creation']}`",
        f"- Thresholds lowered: `{payload['thresholds_lowered']}`",
    ]


def _tail(text: str, *, max_chars: int = 4000) -> str:
    return text[-max_chars:] if len(text) > max_chars else text


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
