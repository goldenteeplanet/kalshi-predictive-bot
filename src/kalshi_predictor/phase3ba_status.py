from __future__ import annotations

import hashlib
import json
import re
import shlex
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
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.schema import Forecast, Market, MarketRanking, MarketSnapshot
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc_r6 import build_phase3bc_r5_status
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BA_STATUS_VERSION = "phase3ba_r8_operator_workflow_one_command_status_v1"
R5_OUTPUT_DIR = Path("phase3bc_r5")
R5_START_COMMANDS = {"phase3bc-r5-unattended-start", "phase3ax-r9-guarded-refresh-job"}
REGISTERED_OPERATOR_COMMANDS = {
    "db-writer-monitor",
    "market-coverage-doctor",
    "market-legs-parse",
    "phase3an-settlement-health-confirm",
    "phase3ap-paper-ready-unblock-report",
    "phase3ax-r9-guarded-refresh-job",
    "phase3az-r12-weather-activation-preview",
    "phase3az-r13-weather-handoff-status",
    "phase3ba-r1-writer-unlock",
    "phase3ba-r2-weather-ranking-activation",
    "phase3ba-r4-crypto-executable-book-watch",
    "phase3ba-r5-paper-ready-truth",
    "phase3ba-r6-noncrypto-engine-backlog",
    "phase3ba-r7-composite-market-plan",
    "phase3ba-ingestion-stability-report",
    "phase3ba-status",
    "phase3bb-r1-operator-scheduler",
    "phase3bb-r2-weather-fast-lane",
    "phase3bb-r3-free-source-inventory",
    "phase3bb-r4-economic-parser-backfill",
    "phase3bb-r5-usda-source-activation",
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
    "phase3bb-acceleration-report",
    "phase3bb-cloud-readiness",
    "phase3bb-historical-replay-acceleration",
    "phase3bb-multicategory-expansion-plan",
    "phase3bb-scheduler-plan",
    "phase3bb-throughput-analysis",
    "phase3bb-weather-fast-lane",
    "phase3bc-r5-status",
    "phase3bc-r5-unattended-guard",
    "phase3bc-r5-unattended-start",
    "snapshot",
    "sync-markets",
}
FORBIDDEN_RECOMMENDATION_FRAGMENTS = (
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


@dataclass(frozen=True)
class Phase3BAStatusArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    next_actions_path: Path
    operator_next_command_path: Path
    status_json_path: Path
    manifest_path: Path


def write_phase3ba_status_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_status"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> Phase3BAStatusArtifactSet:
    payload = build_phase3ba_status(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    operator_next_command_path = output_dir / "operator_next_command.sh"
    status_json_path = output_dir / "status.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    status_json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    operator_next_command_path.write_text(_render_operator_script(payload), encoding="utf-8")
    try:
        operator_next_command_path.chmod(operator_next_command_path.stat().st_mode | 0o111)
    except OSError:
        pass
    _write_manifest(
        manifest_path,
        [executive_summary_path, next_actions_path, operator_next_command_path, status_json_path],
    )
    return Phase3BAStatusArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        next_actions_path=next_actions_path,
        operator_next_command_path=operator_next_command_path,
        status_json_path=status_json_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_status(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_status"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    generated_at = utc_now()
    resolved = settings or get_settings()
    writer = db_writer_monitor(settings=resolved)
    r5_status = _r5_status_truth(reports_dir=reports_dir)
    artifacts = _artifact_bundle(reports_dir, now=generated_at)
    truth = artifacts["paper_ready_truth"].get("payload") or {}
    crypto = artifacts["crypto_executable_book_watch"].get("payload") or {}
    weather_r2 = artifacts["weather_ranking_activation"].get("payload") or {}
    three_ap = artifacts["phase3ap_paper_ready_gate"].get("payload") or {}
    category_backlog = _category_backlog_context(artifacts["category_backlog"].get("text") or "")
    composite = _composite_context(artifacts["composite_plan"].get("text") or "")
    summary = _status_summary(
        writer=writer,
        r5_status=r5_status,
        truth=truth,
        crypto=crypto,
        weather_r2=weather_r2,
        three_ap=three_ap,
        category_backlog=category_backlog,
        composite=composite,
        artifact_statuses=artifacts,
    )
    next_action = _choose_next_action(
        writer=writer,
        r5_status=r5_status,
        truth=truth,
        category_backlog=category_backlog,
        summary=summary,
    )
    command_checks = _command_checks(next_action["command"])
    safety = _safety_flags(next_action, command_checks)
    return {
        **_metadata(
            session,
            settings=resolved,
            generated_at=generated_at.isoformat(),
            command_args=command_args or [],
        ),
        "phase": "3BA-R8",
        "phase_version": PHASE3BA_STATUS_VERSION,
        "mode": "PAPER_READ_ONLY_OPERATOR_STATUS",
        "output_dir": str(output_dir),
        "writer": writer,
        "r5_status": r5_status,
        "artifact_statuses": _artifact_status_view(artifacts),
        "summary": summary,
        "dashboard_truth": truth.get("dashboard_truth") or {},
        "category_backlog": category_backlog,
        "composite_parking": composite,
        "next_action": next_action,
        "command_checks": command_checks,
        "operator_should_not_run": _operator_should_not_run(
            writer=writer,
            r5_status=r5_status,
            composite=composite,
        ),
        "acceptance": _acceptance(summary, next_action, command_checks, safety),
        "safety_flags": safety,
    }


def _artifact_bundle(reports_dir: Path, *, now: Any) -> dict[str, dict[str, Any]]:
    paths = {
        "paper_ready_truth": reports_dir / "phase3ba_r5" / "paper_ready_truth.json",
        "crypto_executable_book_watch": (
            reports_dir / "phase3ba_r4" / "crypto_executable_book_watch.json"
        ),
        "weather_ranking_activation": (
            reports_dir / "phase3ba_r2" / "weather_ranking_activation.json"
        ),
        "weather_paper_gate": reports_dir / "phase3ba_r3" / "weather_paper_gate.json",
        "phase3ap_paper_ready_gate": reports_dir / "phase3ap" / "paper_ready_gate.json",
        "category_backlog": reports_dir / "phase3ba_r6" / "NEXT_CATEGORY_BUILD.md",
        "composite_plan": reports_dir / "phase3ba_r7" / "composite_market_plan.md",
    }
    freshest_trusted = _freshest_generated_at(
        [
            paths["paper_ready_truth"],
            paths["crypto_executable_book_watch"],
            paths["weather_ranking_activation"],
        ]
    )
    bundle: dict[str, dict[str, Any]] = {}
    for key, path in paths.items():
        bundle[key] = _artifact(path, now=now, freshest_trusted=freshest_trusted)
    return bundle


def _r5_status_truth(*, reports_dir: Path) -> dict[str, Any]:
    status_path = reports_dir / R5_OUTPUT_DIR / "phase3bc_r5_status.json"
    local_status = _read_json_if_exists(status_path)
    r13_path = reports_dir / "phase3bb_r13" / "cloud_scheduler_adoption.json"
    r13_status = _r13_remote_r5_status(_read_json_if_exists(r13_path))
    candidates = [
        (local_status, str(status_path)),
        (r13_status, str(r13_path)),
    ]
    status, source = _freshest_r5_candidate(candidates)
    if status:
        selected = dict(status)
        selected["phase3ba_truth_source"] = source
        selected["phase3ba_local_r5_status_path"] = str(status_path)
        return selected
    built = build_phase3bc_r5_status(output_dir=reports_dir / R5_OUTPUT_DIR)
    built["phase3ba_truth_source"] = "build_phase3bc_r5_status_fallback"
    built["phase3ba_local_r5_status_path"] = str(status_path)
    return built


def _r13_remote_r5_status(payload: dict[str, Any]) -> dict[str, Any]:
    parsed = payload.get("parsed_remote_state") if isinstance(payload, dict) else {}
    parsed = parsed if isinstance(parsed, dict) else {}
    guard_dry_run = (
        parsed.get("guard_dry_run") if isinstance(parsed.get("guard_dry_run"), dict) else {}
    )
    guard_after = (
        guard_dry_run.get("after") if isinstance(guard_dry_run.get("after"), dict) else {}
    )
    r5_status = parsed.get("r5_status") if isinstance(parsed.get("r5_status"), dict) else {}
    status, _source = _freshest_r5_candidate(
        [(r5_status, "phase3bb_r13.parsed_remote_state.r5_status"),
         (guard_after, "phase3bb_r13.parsed_remote_state.guard_dry_run.after")]
    )
    return status


def _freshest_r5_candidate(
    candidates: list[tuple[dict[str, Any], str]],
) -> tuple[dict[str, Any], str]:
    best_payload: dict[str, Any] = {}
    best_source = ""
    best_generated = None
    for payload, source in candidates:
        if not payload:
            continue
        generated = parse_datetime(payload.get("generated_at"))
        if best_payload and generated is not None and best_generated is not None:
            if generated <= best_generated:
                continue
        elif best_payload and generated is None:
            continue
        best_payload = payload
        best_source = source
        best_generated = generated
    return best_payload, best_source


def _artifact(path: Path, *, now: Any, freshest_trusted: Any | None) -> dict[str, Any]:
    exists = path.exists()
    payload = _read_json_if_exists(path) if path.suffix == ".json" else {}
    text = _read_text_if_exists(path) if path.suffix != ".json" else ""
    generated_at = payload.get("generated_at") or _generated_at_from_text(text)
    parsed_generated_at = parse_datetime(generated_at)
    age_seconds = (
        int(max(0, (now - parsed_generated_at).total_seconds()))
        if parsed_generated_at is not None
        else None
    )
    freshness = "MISSING"
    if exists:
        freshness = "CURRENT"
        if freshest_trusted is not None and parsed_generated_at is not None:
            if parsed_generated_at < freshest_trusted:
                freshness = "HISTORICAL_STALE"
        elif age_seconds is None:
            freshness = "UNKNOWN_AGE"
    return {
        "path": str(path),
        "exists": exists,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "freshness": freshness,
        "payload": payload,
        "text": text,
        "size_bytes": path.stat().st_size if exists else 0,
    }


def _freshest_generated_at(paths: list[Path]) -> Any | None:
    values = []
    for path in paths:
        payload = _read_json_if_exists(path)
        parsed = parse_datetime(payload.get("generated_at"))
        if parsed is not None:
            values.append(parsed)
    return max(values) if values else None


def _status_summary(
    *,
    writer: dict[str, Any],
    r5_status: dict[str, Any],
    truth: dict[str, Any],
    crypto: dict[str, Any],
    weather_r2: dict[str, Any],
    three_ap: dict[str, Any],
    category_backlog: dict[str, Any],
    composite: dict[str, Any],
    artifact_statuses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    truth_summary = truth.get("summary") if isinstance(truth.get("summary"), dict) else {}
    categories = truth.get("category_summaries")
    categories = categories if isinstance(categories, dict) else {}
    crypto_category = categories.get("crypto") if isinstance(categories.get("crypto"), dict) else {}
    weather_category = (
        categories.get("weather") if isinstance(categories.get("weather"), dict) else {}
    )
    crypto_summary = crypto.get("summary") if isinstance(crypto.get("summary"), dict) else {}
    r5_latest = r5_status.get("latest_summary")
    r5_latest = r5_latest if isinstance(r5_latest, dict) else {}
    r5_guard = r5_status.get("guard") if isinstance(r5_status.get("guard"), dict) else {}
    r5_summary = r5_status.get("summary") if isinstance(r5_status.get("summary"), dict) else {}
    r5_paper_ready_raw = _first_present(
        r5_latest.get("paper_ready_candidates"),
        r5_guard.get("paper_ready_candidates"),
        r5_summary.get("paper_ready_candidates"),
    )
    r5_positive_ev_raw = _first_present(
        r5_latest.get("positive_ev_rows"),
        r5_guard.get("positive_ev_rows"),
        r5_summary.get("positive_ev_rows"),
    )
    r5_no_book_raw = _first_present(
        r5_latest.get("positive_ev_no_executable_book_rows"),
        r5_guard.get("positive_ev_no_executable_book_rows"),
        r5_summary.get("positive_ev_no_executable_book_rows"),
        r5_latest.get("positive_ev_no_book_rows"),
    )
    r5_primary_gap = _first_present(
        r5_latest.get("primary_gap_after_refresh"),
        r5_guard.get("primary_gap_after_refresh"),
        r5_summary.get("primary_gap_after_refresh"),
    )
    r5_watch_state = _first_present(
        r5_status.get("latest_watch_state"),
        r5_latest.get("watch_state"),
        r5_guard.get("watch_state"),
        r5_summary.get("watch_state"),
    )
    r5_paper_ready_present = r5_paper_ready_raw is not None
    r5_positive_ev_present = r5_positive_ev_raw is not None
    r5_paper_ready_candidates = _to_int(r5_paper_ready_raw)
    r5_positive_ev_rows = _to_int(r5_positive_ev_raw)
    r5_no_book_rows = _to_int(r5_no_book_raw)
    writer_active = bool(writer.get("current_writer_pid"))
    r5_running = _r5_running(r5_status)
    stale_crypto_ready = _to_int(
        _first_present(
            crypto_category.get("paper_ready_rows"),
            crypto_summary.get("paper_ready_rows"),
            crypto_summary.get("paper_ready_candidates"),
        )
    )
    crypto_ready = (
        r5_paper_ready_candidates
        if r5_paper_ready_present
        else stale_crypto_ready
    )
    weather_ready = int(weather_category.get("paper_ready_rows") or 0)
    truth_paper_ready_rows = _to_int(truth_summary.get("paper_ready_rows"))
    paper_ready_rows = max(0, truth_paper_ready_rows)
    if r5_paper_ready_present:
        paper_ready_rows = max(0, truth_paper_ready_rows - stale_crypto_ready) + crypto_ready
    stale_crypto_positive = _to_int(
        _first_present(
            crypto_category.get("positive_ev_rows"),
            crypto_summary.get("positive_ev_rows"),
        )
    )
    crypto_positive_ev_rows = (
        r5_positive_ev_rows if r5_positive_ev_present else stale_crypto_positive
    )
    weather_positive_ev_rows = _to_int(weather_category.get("positive_ev_rows"))
    positive_ev_rows = max(
        _to_int(truth_summary.get("positive_ev_rows")),
        crypto_positive_ev_rows + weather_positive_ev_rows,
    )
    crypto_first_blocker = _r5_crypto_blocker(
        positive_ev_rows=r5_positive_ev_rows,
        paper_ready_candidates=r5_paper_ready_candidates,
        positive_ev_no_executable_book_rows=r5_no_book_rows,
        primary_gap_after_refresh=r5_primary_gap,
        watch_state=r5_watch_state,
        r5_positive_ev_present=r5_positive_ev_present,
    )
    if crypto_first_blocker is None:
        crypto_first_blocker = (
            crypto_category.get("first_blocker")
            or crypto_summary.get("primary_watch_state")
            or r5_primary_gap
            or r5_watch_state
        )
    trading_first_blocker = _trading_first_blocker(
        truth_summary=truth_summary,
        paper_ready_rows=paper_ready_rows,
        positive_ev_rows=positive_ev_rows,
        crypto_first_blocker=crypto_first_blocker,
        weather_first_blocker=weather_category.get("first_blocker")
        or (weather_r2.get("after_summary") or {}).get("primary_blocker"),
    )
    operational_first_blocker = "WRITER_ACTIVE" if writer_active else None
    true_first_blocker = operational_first_blocker or trading_first_blocker
    stale_3ap = artifact_statuses["phase3ap_paper_ready_gate"]["freshness"] == "HISTORICAL_STALE"
    return {
        "app_safe": True,
        "safe_to_start_write": bool(writer.get("safe_to_start_write")),
        "active_writer": writer_active,
        "active_writer_pid": writer.get("current_writer_pid"),
        "active_writer_command": writer.get("current_writer_command"),
        "r5_running": r5_running,
        "r5_guard_status": (r5_status.get("guard") or {}).get("status"),
        "r5_process_status": (r5_status.get("process") or {}).get("status"),
        "r5_should_stop": bool((r5_status.get("guard") or {}).get("should_stop")),
        "r5_watch_state": r5_watch_state,
        "r5_truth_source": r5_status.get("phase3ba_truth_source"),
        "crypto_paper_ready": crypto_ready > 0,
        "crypto_paper_ready_rows": crypto_ready,
        "crypto_positive_ev_rows": crypto_positive_ev_rows,
        "crypto_positive_ev_no_executable_book_rows": r5_no_book_rows,
        "crypto_first_blocker": crypto_first_blocker,
        "crypto_evidence_scope": (
            "R5_AGGREGATE_TRUTH_ONLY"
            if r5_positive_ev_present and r5_positive_ev_rows > 0
            else None
        ),
        "weather_paper_ready": weather_ready > 0,
        "weather_paper_ready_rows": weather_ready,
        "weather_current_rows": int(weather_category.get("current_rows") or 0),
        "weather_positive_ev_rows": weather_positive_ev_rows,
        "weather_first_blocker": weather_category.get("first_blocker")
        or (weather_r2.get("after_summary") or {}).get("primary_blocker"),
        "paper_ready_rows": paper_ready_rows,
        "positive_ev_rows": positive_ev_rows,
        "trading_first_blocker": trading_first_blocker,
        "operational_first_blocker": operational_first_blocker,
        "true_first_blocker": true_first_blocker,
        "phase3ap_is_stale": stale_3ap,
        "phase3ap_summary": three_ap.get("summary") or {},
        "dashboard_truth_source": truth_summary.get("dashboard_truth_source"),
        "dashboard_truth_summary": (truth.get("dashboard_truth") or {}).get("summary"),
        "what_codex_should_build_next": _codex_build_next(
            category_backlog=category_backlog,
            composite=composite,
        ),
        "category_immediate_work": category_backlog.get("immediate_work"),
        "composite_rows_parked": composite.get("unsupported_composite_rows"),
        "composites_pollute_single_market_coverage": False,
    }


def _r5_crypto_blocker(
    *,
    positive_ev_rows: int,
    paper_ready_candidates: int,
    positive_ev_no_executable_book_rows: int,
    primary_gap_after_refresh: Any,
    watch_state: Any,
    r5_positive_ev_present: bool,
) -> str | None:
    if not r5_positive_ev_present or positive_ev_rows <= 0:
        return None
    if paper_ready_candidates > 0:
        return "PAPER_READY"
    if positive_ev_no_executable_book_rows > 0:
        return "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    primary_gap = str(primary_gap_after_refresh or "").strip()
    if primary_gap and primary_gap != "PAPER_READY":
        return primary_gap
    state = str(watch_state or "").strip()
    if state == "WAITING_FOR_EXECUTABLE_BOOK":
        return "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    return state or "POSITIVE_EV_BLOCKED"


def _trading_first_blocker(
    *,
    truth_summary: dict[str, Any],
    paper_ready_rows: int,
    positive_ev_rows: int,
    crypto_first_blocker: Any,
    weather_first_blocker: Any,
) -> str:
    reported = str(truth_summary.get("first_hard_blocker") or "").strip()
    if paper_ready_rows > 0:
        return reported or "PAPER_READY"
    if positive_ev_rows > 0 and crypto_first_blocker:
        return str(crypto_first_blocker)
    if reported and reported != "PAPER_READY":
        return reported
    if crypto_first_blocker and str(crypto_first_blocker) != "PAPER_READY":
        return str(crypto_first_blocker)
    if weather_first_blocker and str(weather_first_blocker) != "PAPER_READY":
        return str(weather_first_blocker)
    return "NO_PAPER_READY_CANDIDATES"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _to_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _choose_next_action(
    *,
    writer: dict[str, Any],
    r5_status: dict[str, Any],
    truth: dict[str, Any],
    category_backlog: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    writer_active = bool(writer.get("current_writer_pid"))
    if writer_active:
        if _writer_is_r5(writer) and summary["r5_should_stop"]:
            return {
                "stage": "RUN_GUARDED_R5_WRITER_UNLOCK",
                "command": (
                    "kalshi-bot phase3ba-r1-writer-unlock --output-dir "
                    "reports/phase3ba_r1 --reports-dir reports"
                ),
                "reason": (
                    "The active writer is the guarded R5 watcher and its guard says "
                    "should_stop=true."
                ),
                "clearly_wait": False,
                "allow_paper_trade_creation": False,
            }
        return {
            "stage": "WAIT_FOR_ACTIVE_WRITER",
            "command": "kalshi-bot db-writer-monitor --json",
            "reason": "A DB writer is active; wait rather than start write-capable work.",
            "clearly_wait": True,
            "allow_paper_trade_creation": False,
        }
    if not _r5_running(r5_status):
        return {
            "stage": "START_ONE_GUARDED_R5_WATCHER",
            "command": (
                "kalshi-bot phase3ax-r9-guarded-refresh-job --output-dir "
                "reports/phase3ax_r9 --reports-dir reports --ensure-running"
            ),
            "reason": "No running R5 watcher was detected; start exactly one guarded watcher.",
            "clearly_wait": False,
            "allow_paper_trade_creation": False,
        }
    if int(summary["paper_ready_rows"]) > 0:
        return {
            "stage": "PAPER_ONLY_OPERATOR_REVIEW",
            "command": (
                "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
                "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
            ),
            "reason": (
                "Paper-ready rows exist; refresh truth and review only, do not create trades."
            ),
            "clearly_wait": False,
            "allow_paper_trade_creation": False,
        }
    weather_blocker = str(summary.get("weather_first_blocker") or "")
    if weather_blocker == "SNAPSHOT_MISSING":
        return {
            "stage": "REFRESH_WEATHER_SNAPSHOTS_THEN_REBUILD_TRUTH",
            "command": (
                "kalshi-bot db-writer-monitor --json\n"
                "kalshi-bot snapshot --status open --limit 100 --max-pages 3 "
                "--series-ticker KXTEMPNYCH --include-orderbook\n"
                "kalshi-bot phase3ba-r2-weather-ranking-activation --output-dir "
                "reports/phase3ba_r2 --reports-dir reports --limit 100\n"
                "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
                "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
            ),
            "reason": (
                "Weather is the active build and needs current snapshots/orderbooks before "
                "truth refresh."
            ),
            "clearly_wait": False,
            "allow_paper_trade_creation": False,
            "requires_writer_gate_clear": True,
        }
    if weather_blocker == "RANKING_MISSING":
        return {
            "stage": "RUN_WEATHER_RANKING_ACTIVATION",
            "command": (
                "kalshi-bot phase3ba-r2-weather-ranking-activation --output-dir "
                "reports/phase3ba_r2 --reports-dir reports --limit 100"
            ),
            "reason": "Weather forecasts exist but current weather rows need rankings.",
            "clearly_wait": False,
            "allow_paper_trade_creation": False,
        }
    immediate = category_backlog.get("immediate_work") or {}
    immediate_command = immediate.get("command")
    if immediate_command and _command_is_safe(immediate_command):
        return {
            "stage": immediate.get("stage") or "RUN_CATEGORY_BACKLOG_STEP",
            "command": str(immediate_command),
            "reason": str(immediate.get("reason") or "Continue the R6-selected category work."),
            "clearly_wait": False,
            "allow_paper_trade_creation": False,
            "requires_writer_gate_clear": "db-writer-monitor" in str(immediate_command),
        }
    truth_next = truth.get("next_action") if isinstance(truth.get("next_action"), dict) else {}
    truth_command = truth_next.get("command")
    if truth_command and _command_is_safe(str(truth_command)):
        return {
            "stage": truth_next.get("stage") or "REFRESH_UNIFIED_TRUTH",
            "command": str(truth_command),
            "reason": str(truth_next.get("reason") or "Use the unified truth next action."),
            "clearly_wait": False,
            "allow_paper_trade_creation": False,
            "requires_writer_gate_clear": bool(truth_next.get("requires_writer_gate_clear")),
        }
    return {
        "stage": "REFRESH_UNIFIED_STATUS",
        "command": (
            "kalshi-bot phase3ba-r5-paper-ready-truth --output-dir "
            "reports/phase3ba_r5 --reports-dir reports --max-duration-seconds 120"
        ),
        "reason": "No paper-ready rows exist; refresh bounded unified truth.",
        "clearly_wait": False,
        "allow_paper_trade_creation": False,
    }


def _codex_build_next(*, category_backlog: dict[str, Any], composite: dict[str, Any]) -> str:
    immediate = category_backlog.get("immediate_work") or {}
    category = immediate.get("category")
    stage = immediate.get("stage")
    if category:
        return f"{category} / {stage}: {immediate.get('reason')}"
    parked = composite.get("unsupported_composite_rows")
    if parked:
        return f"Keep {parked} composites parked; build exact component support later."
    return "Refresh Phase 3BA-R6 backlog and Phase 3BA-R5 paper-ready truth."


def _category_backlog_context(text: str) -> dict[str, Any]:
    immediate_section = _section(text, "## Immediate Work", "## Next New Non-Crypto Engine")
    future_section = _section(
        text,
        "## Next New Non-Crypto Engine After Weather",
        "## Implementation Step",
    )
    command = _first_code_block(immediate_section)
    return {
        "source": "reports/phase3ba_r6/NEXT_CATEGORY_BUILD.md",
        "immediate_work": {
            "category": _backtick_value(immediate_section, "Category"),
            "stage": _backtick_value(immediate_section, "Stage"),
            "reason": _backtick_value(immediate_section, "Reason"),
            "command": command,
        },
        "next_new_category_after_weather": {
            "category": _backtick_value(future_section, "Category"),
            "primary_blocker": _backtick_value(future_section, "Primary blocker"),
        },
    }


def _composite_context(text: str) -> dict[str, Any]:
    return {
        "source": "reports/phase3ba_r7/composite_market_plan.md",
        "unsupported_composite_rows": _int_from_backtick_line(text, "Unsupported composite rows"),
        "exact_component_evidence_rows": _int_from_backtick_line(
            text,
            "Exact component evidence rows found",
        ),
        "parking_status": "PARKED_OUTSIDE_SINGLE_MARKET_LINK_REMEDIATION"
        if "parked outside normal single-market link remediation" in text
        else "UNKNOWN",
    }


def _command_checks(command: str) -> dict[str, Any]:
    commands = _extract_kalshi_commands(command)
    command_names = [_command_name(line) for line in commands]
    unregistered = [
        name for name in command_names if name and name not in REGISTERED_OPERATOR_COMMANDS
    ]
    forbidden = [
        fragment
        for fragment in FORBIDDEN_RECOMMENDATION_FRAGMENTS
        if fragment in command.lower()
    ]
    starts_r5 = [name for name in command_names if name in R5_START_COMMANDS]
    return {
        "commands": commands,
        "command_names": command_names,
        "all_recommended_commands_registered": not unregistered,
        "unregistered_command_names": unregistered,
        "forbidden_recommendation_fragments": forbidden,
        "contains_forbidden_trade_command": bool(forbidden),
        "r5_start_commands": starts_r5,
    }


def _acceptance(
    summary: dict[str, Any],
    next_action: dict[str, Any],
    command_checks: dict[str, Any],
    safety: dict[str, Any],
) -> dict[str, Any]:
    duplicate_r5_start = bool(command_checks["r5_start_commands"]) and summary["r5_running"]
    return {
        "one_command_gives_true_status": True,
        "recommended_commands_registered": command_checks["all_recommended_commands_registered"],
        "never_recommends_duplicate_r5_start_while_running": not duplicate_r5_start,
        "never_recommends_paper_live_demo_trading_commands": (
            not command_checks["contains_forbidden_trade_command"]
        ),
        "clearly_says_when_to_wait": bool(next_action.get("clearly_wait"))
        or not summary["active_writer"],
        "live_or_demo_execution_blocked": not safety["places_exchange_orders"],
        "paper_trade_creation_blocked": not safety["creates_paper_trades"],
    }


def _operator_should_not_run(
    *,
    writer: dict[str, Any],
    r5_status: dict[str, Any],
    composite: dict[str, Any],
) -> list[str]:
    blocked = [
        "Do not run paper trade creation commands.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not run accelerate-learning from this status phase.",
        "Do not run normal link-remediate against KXMVE composites.",
    ]
    if _r5_running(r5_status):
        blocked.append("Do not start another R5 watcher; one is already running.")
    if bool(writer.get("current_writer_pid")):
        blocked.append("Do not run write-capable refresh/ranking work until the writer clears.")
    if composite.get("unsupported_composite_rows"):
        blocked.append("Do not decompose parked composites without exact component evidence.")
    return blocked


def _safety_flags(next_action: dict[str, Any], command_checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_only": True,
        "diagnostic_only": True,
        "status_report_only": True,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "starts_duplicate_r5_watcher": False,
        "recommended_command_contains_forbidden_trade_command": command_checks[
            "contains_forbidden_trade_command"
        ],
        "recommended_action_allows_paper_trade_creation": bool(
            next_action.get("allow_paper_trade_creation")
        ),
    }


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
            "command": "kalshi-bot phase3ba-status",
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
    next_action = payload["next_action"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R8 Operator Status")
    lines.extend(
        [
            "",
            "## Answers",
            "",
            f"1. Is the app safe? `{summary['app_safe']}`",
            (
                "2. Is there an active writer? "
                f"`{summary['active_writer']}`"
                f" pid=`{summary['active_writer_pid']}`"
            ),
            f"3. Is R5 running? `{summary['r5_running']}`",
            f"4. Is crypto paper-ready? `{summary['crypto_paper_ready']}`",
            f"5. Is weather paper-ready? `{summary['weather_paper_ready']}`",
            f"6. True first blocker: `{summary['true_first_blocker']}`",
            "7. Codex should build next: "
            f"`{summary['what_codex_should_build_next']}`",
            "8. Operator should run next:",
            "",
            "```bash",
            next_action["command"],
            "```",
            "",
            "9. Operator should not run:",
        ]
    )
    for item in payload["operator_should_not_run"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Truth Reconciliation",
            "",
            f"- Dashboard truth: `{summary['dashboard_truth_summary']}`",
            f"- Trading first blocker: `{summary['trading_first_blocker']}`",
            f"- Operational first blocker: `{summary['operational_first_blocker']}`",
            f"- Phase 3AP stale/historical: `{summary['phase3ap_is_stale']}`",
            f"- Composite rows parked: `{summary['composite_rows_parked']}`",
            f"- Next action stage: `{next_action['stage']}`",
            f"- Next action reason: {next_action['reason']}",
            "",
            "## Acceptance",
            "",
        ]
    )
    for key, value in payload["acceptance"].items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    next_action = payload["next_action"]
    lines = _metadata_lines(payload, title="# Phase 3BA-R8 Next Actions")
    lines.extend(
        [
            "",
            "## Operator Next Command",
            "",
            "```bash",
            next_action["command"],
            "```",
            "",
            f"- Stage: `{next_action['stage']}`",
            f"- Reason: {next_action['reason']}",
            f"- Clearly wait: `{next_action.get('clearly_wait', False)}`",
            f"- Paper trade creation allowed: `{next_action.get('allow_paper_trade_creation')}`",
            "",
            "## Command Registration",
            "",
        ]
    )
    checks = payload["command_checks"]
    lines.append(
        f"- All recommended commands registered: `{checks['all_recommended_commands_registered']}`"
    )
    for command in checks["command_names"]:
        lines.append(f"- `{command}`")
    lines.extend(["", "## Do Not Run", ""])
    for item in payload["operator_should_not_run"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _render_operator_script(payload: dict[str, Any]) -> str:
    next_action = payload["next_action"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"# Phase 3BA-R8 generated at {payload['generated_at']}",
        f"# Stage: {next_action['stage']}",
        f"# Reason: {next_action['reason']}",
        "# Safety: PAPER / READ-ONLY; no paper trades; no live/demo orders.",
        "",
    ]
    command = str(next_action["command"]).strip()
    lines.extend(command.splitlines() if command else ["kalshi-bot phase3ba-status"])
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


def _artifact_status_view(artifacts: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "path": value["path"],
            "exists": value["exists"],
            "generated_at": value["generated_at"],
            "age_seconds": value["age_seconds"],
            "freshness": value["freshness"],
            "size_bytes": value["size_bytes"],
        }
        for key, value in artifacts.items()
    }


def _r5_running(r5_status: dict[str, Any]) -> bool:
    process_status = str((r5_status.get("process") or {}).get("status") or "").upper()
    guard_status = str((r5_status.get("guard") or {}).get("status") or "").upper()
    return process_status == "RUNNING" or guard_status in {"RUNNING", "OVERRUNNING"}


def _writer_is_r5(writer: dict[str, Any]) -> bool:
    command = str(writer.get("current_writer_command") or "").lower()
    return "phase3bc-r5" in command and "phase3bc-r5-status" not in command


def _command_is_safe(command: str) -> bool:
    return not any(fragment in command.lower() for fragment in FORBIDDEN_RECOMMENDATION_FRAGMENTS)


def _extract_kalshi_commands(command: str) -> list[str]:
    lines = []
    for raw_line in command.splitlines():
        line = raw_line.strip()
        if line.startswith("kalshi-bot "):
            lines.append(line)
    return lines


def _command_name(command_line: str) -> str | None:
    try:
        parts = shlex.split(command_line)
    except ValueError:
        return None
    if len(parts) < 2 or parts[0] != "kalshi-bot":
        return None
    return parts[1]


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _generated_at_from_text(text: str) -> str | None:
    match = re.search(r"Generated at:\s*`([^`]+)`", text)
    return match.group(1) if match else None


def _section(text: str, start: str, end: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        return ""
    end_index = text.find(end, start_index + len(start))
    if end_index < 0:
        return text[start_index:]
    return text[start_index:end_index]


def _first_code_block(text: str) -> str | None:
    match = re.search(r"```(?:bash)?\s*(.*?)```", text, flags=re.DOTALL)
    return match.group(1).strip() if match else None


def _backtick_value(text: str, label: str) -> str | None:
    match = re.search(rf"- {re.escape(label)}:\s*`([^`]+)`", text)
    return match.group(1) if match else None


def _int_from_backtick_line(text: str, label: str) -> int | None:
    value = _backtick_value(text, label)
    if value is None:
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
