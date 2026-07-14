from __future__ import annotations

import hashlib
import html
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.learning.safety import learning_status
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ba_status import build_phase3ba_status
from kalshi_predictor.utils.time import utc_now

PHASE3BA_INGESTION_STABILITY_VERSION = "phase3ba_ingestion_stability_v1"
DEFAULT_EV_TARGETS = (10, 30, 100)
COUNT_TABLES = (
    ("markets", "Markets", "last_seen_at"),
    ("market_snapshots", "Market snapshots", "captured_at"),
    ("forecasts", "Forecasts", "forecasted_at"),
    ("market_rankings", "Rankings", "ranked_at"),
    ("paper_orders", "Paper orders", "created_at"),
    ("paper_pnl", "Paper PnL rows", "calculated_at"),
    ("forecast_memory", "Forecast memory", None),
    ("trade_memory", "Trade memory", None),
    ("crypto_prices", "Crypto prices", "observed_at"),
    ("crypto_features", "Crypto features", "generated_at"),
    ("weather_observations", "Weather observations", "observed_at"),
    ("weather_forecasts", "Weather forecasts", "forecasted_at"),
    ("weather_features", "Weather features", "generated_at"),
)
TIMESTAMP_CANDIDATES = (
    "last_seen_at",
    "captured_at",
    "forecasted_at",
    "ranked_at",
    "created_at",
    "calculated_at",
    "observed_at",
    "generated_at",
    "updated_at",
    "detected_at",
)


@dataclass(frozen=True)
class Phase3BAIngestionStabilityArtifactSet:
    output_dir: Path
    executive_summary_path: Path
    json_path: Path
    markdown_path: Path
    ingestion_volume_svg_path: Path
    conversion_funnel_svg_path: Path
    stability_projection_svg_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ba_ingestion_stability_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_ingestion_stability"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    runtime_hours: float = 165.0,
    observed_positive_ev: int | None = None,
) -> Phase3BAIngestionStabilityArtifactSet:
    payload = build_phase3ba_ingestion_stability(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        runtime_hours=runtime_hours,
        observed_positive_ev=observed_positive_ev,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    graphics_dir = output_dir / "graphics"
    graphics_dir.mkdir(parents=True, exist_ok=True)

    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    json_path = output_dir / "ingestion_stability.json"
    markdown_path = output_dir / "ingestion_stability.md"
    ingestion_volume_svg_path = graphics_dir / "ingestion_volume.svg"
    conversion_funnel_svg_path = graphics_dir / "conversion_funnel.svg"
    stability_projection_svg_path = graphics_dir / "stability_projection.svg"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown_report(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    ingestion_volume_svg_path.write_text(_render_ingestion_volume_svg(payload), encoding="utf-8")
    conversion_funnel_svg_path.write_text(_render_conversion_funnel_svg(payload), encoding="utf-8")
    stability_projection_svg_path.write_text(
        _render_stability_projection_svg(payload),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            json_path,
            markdown_path,
            ingestion_volume_svg_path,
            conversion_funnel_svg_path,
            stability_projection_svg_path,
            next_actions_path,
        ],
    )
    return Phase3BAIngestionStabilityArtifactSet(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        json_path=json_path,
        markdown_path=markdown_path,
        ingestion_volume_svg_path=ingestion_volume_svg_path,
        conversion_funnel_svg_path=conversion_funnel_svg_path,
        stability_projection_svg_path=stability_projection_svg_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ba_ingestion_stability(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ba_ingestion_stability"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    runtime_hours: float = 165.0,
    observed_positive_ev: int | None = None,
) -> dict[str, Any]:
    generated_at = utc_now()
    resolved = settings or get_settings()
    status = build_phase3ba_status(
        session,
        output_dir=reports_dir / "phase3ba_status",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3ba-ingestion-stability-report", "embedded_status"],
    )
    learning = learning_status(session, settings=resolved)
    table_observations = _table_observations(session)
    forecast_models = _group_counts(
        session,
        table_name="forecasts",
        group_column="model_name",
        timestamp_column="forecasted_at",
    )
    ranking_models = _group_counts(
        session,
        table_name="market_rankings",
        group_column="forecast_model",
        timestamp_column="ranked_at",
    )
    observed_ev = _observed_ev_value(observed_positive_ev, status)
    ev_pace = _positive_ev_pace_summary(
        runtime_hours=runtime_hours,
        observed_positive_ev=observed_ev,
        targets=DEFAULT_EV_TARGETS,
    )
    stability = _model_stability_assessment(
        learning=learning,
        status=status,
        ev_pace=ev_pace,
    )
    ingestion_health = _ingestion_health(table_observations=table_observations)
    summary = _summary(
        status=status,
        learning=learning,
        ingestion_health=ingestion_health,
        ev_pace=ev_pace,
        stability=stability,
    )
    return {
        **_metadata(
            session,
            settings=resolved,
            generated_at=generated_at.isoformat(),
            command_args=command_args or [],
        ),
        "phase": "3BA-INGESTION-STABILITY",
        "phase_version": PHASE3BA_INGESTION_STABILITY_VERSION,
        "mode": "PAPER_READ_ONLY_INGESTION_STABILITY_REPORT",
        "output_dir": str(output_dir),
        "runtime_hours": runtime_hours,
        "reports_dir": str(reports_dir),
        "status_truth": status,
        "learning_status": learning,
        "table_observations": table_observations,
        "forecast_models": forecast_models,
        "ranking_models": ranking_models,
        "ingestion_health": ingestion_health,
        "positive_ev_pace": ev_pace,
        "model_stability": stability,
        "summary": summary,
        "next_action": _next_action(summary),
        "operator_should_not_run": _operator_should_not_run(),
        "safety_flags": _safety_flags(),
    }


def _observed_ev_value(observed_positive_ev: int | None, status: dict[str, Any]) -> int:
    if observed_positive_ev is not None:
        return max(0, int(observed_positive_ev))
    summary = status.get("summary") or {}
    try:
        return max(0, int(summary.get("positive_ev_rows") or 0))
    except (TypeError, ValueError):
        return 0


def _positive_ev_pace_summary(
    *,
    runtime_hours: float,
    observed_positive_ev: int,
    targets: tuple[int, ...] = DEFAULT_EV_TARGETS,
) -> dict[str, Any]:
    safe_hours = max(0.0, float(runtime_hours))
    rate_per_hour = (observed_positive_ev / safe_hours) if safe_hours > 0 else 0.0
    rate_per_day = rate_per_hour * 24.0
    projections: list[dict[str, Any]] = []
    for target in targets:
        remaining = max(0, int(target) - int(observed_positive_ev))
        hours_remaining = (remaining / rate_per_hour) if rate_per_hour > 0 else None
        days_remaining = (hours_remaining / 24.0) if hours_remaining is not None else None
        projections.append(
            {
                "target_positive_ev_rows": target,
                "remaining_positive_ev_rows": remaining,
                "hours_remaining_at_observed_pace": _round_or_none(hours_remaining, 1),
                "days_remaining_at_observed_pace": _round_or_none(days_remaining, 1),
            }
        )
    return {
        "runtime_hours": safe_hours,
        "observed_positive_ev_rows": observed_positive_ev,
        "positive_ev_per_hour": round(rate_per_hour, 4),
        "positive_ev_per_day": round(rate_per_day, 3),
        "targets": projections,
        "interpretation": (
            "LOW_YIELD"
            if observed_positive_ev <= 0 or rate_per_day < 1.0
            else "GENERATING_POSITIVE_EV"
        ),
    }


def _model_stability_assessment(
    *,
    learning: dict[str, Any],
    status: dict[str, Any],
    ev_pace: dict[str, Any],
) -> dict[str, Any]:
    settled = _to_int(learning.get("settled_paper_trades"))
    target = max(1, _to_int(learning.get("target_settled_trades")) or 500)
    remaining = max(0, target - settled)
    daily_paper_trades = _to_int(learning.get("daily_paper_trades"))
    paper_ready_rows = _to_int((status.get("summary") or {}).get("paper_ready_rows"))
    ev_per_day = float(ev_pace.get("positive_ev_per_day") or 0.0)
    days_at_daily_trade_pace = (
        math.ceil(remaining / daily_paper_trades) if daily_paper_trades > 0 else None
    )
    proxy_days_at_ev_pace = (remaining / ev_per_day) if ev_per_day > 0 else None
    status_label = "STABLE_TARGET_REACHED" if remaining == 0 else "NOT_STABLE_ENOUGH"
    if remaining > 0 and paper_ready_rows <= 0:
        status_label = "NOT_STABLE_AND_GATE_CLOSED"
    return {
        "status": status_label,
        "settled_paper_trades": settled,
        "target_settled_trades": target,
        "remaining_settled_trades": remaining,
        "learning_progress_percent": learning.get("progress_percent"),
        "daily_paper_trades": daily_paper_trades,
        "paper_ready_rows": paper_ready_rows,
        "days_to_target_at_current_daily_paper_trade_pace": days_at_daily_trade_pace,
        "proxy_days_to_target_if_each_observed_ev_became_a_settled_trade": _round_or_none(
            proxy_days_at_ev_pace,
            1,
        ),
        "completion_estimate": learning.get("expected_completion"),
        "why": _stability_reason(
            remaining=remaining,
            paper_ready_rows=paper_ready_rows,
            daily_paper_trades=daily_paper_trades,
        ),
    }


def _stability_reason(
    *,
    remaining: int,
    paper_ready_rows: int,
    daily_paper_trades: int,
) -> str:
    if remaining <= 0:
        return "Settled paper-trade target has been reached."
    if paper_ready_rows <= 0:
        return (
            "The model is still short of the settled-trade target and the current "
            "paper-ready gate is closed."
        )
    if daily_paper_trades <= 0:
        return (
            "The settled-trade target is not reached and there is no current daily "
            "paper-trade pace for a reliable completion estimate."
        )
    return "The model is still collecting settled paper-trade evidence."


def _summary(
    *,
    status: dict[str, Any],
    learning: dict[str, Any],
    ingestion_health: dict[str, Any],
    ev_pace: dict[str, Any],
    stability: dict[str, Any],
) -> dict[str, Any]:
    status_summary = status.get("summary") or {}
    return {
        "bottom_line": _bottom_line(
            ingestion_health=ingestion_health,
            ev_pace=ev_pace,
            stability=stability,
        ),
        "ingestion_status": ingestion_health["status"],
        "model_stability_status": stability["status"],
        "opportunity_conversion_status": ev_pace["interpretation"],
        "runtime_hours": ev_pace["runtime_hours"],
        "observed_positive_ev_rows": ev_pace["observed_positive_ev_rows"],
        "positive_ev_per_day": ev_pace["positive_ev_per_day"],
        "settled_paper_trades": stability["settled_paper_trades"],
        "target_settled_trades": stability["target_settled_trades"],
        "remaining_settled_trades": stability["remaining_settled_trades"],
        "learning_progress_percent": learning.get("progress_percent"),
        "paper_ready_rows": status_summary.get("paper_ready_rows"),
        "current_positive_ev_rows_from_status": status_summary.get("positive_ev_rows"),
        "true_first_blocker": status_summary.get("true_first_blocker"),
        "crypto_first_blocker": status_summary.get("crypto_first_blocker"),
        "weather_first_blocker": status_summary.get("weather_first_blocker"),
        "stable_model_estimate": stability["completion_estimate"],
        "days_to_target_at_current_daily_paper_trade_pace": (
            stability["days_to_target_at_current_daily_paper_trade_pace"]
        ),
        "proxy_days_to_target_if_each_observed_ev_became_a_settled_trade": (
            stability["proxy_days_to_target_if_each_observed_ev_became_a_settled_trade"]
        ),
    }


def _bottom_line(
    *,
    ingestion_health: dict[str, Any],
    ev_pace: dict[str, Any],
    stability: dict[str, Any],
) -> str:
    if stability["status"] == "STABLE_TARGET_REACHED":
        return "The settled-trade target is reached; move to confidence review."
    if ev_pace["interpretation"] == "LOW_YIELD":
        return (
            "Ingestion is collecting data, but opportunity conversion is too sparse "
            "for a stable model: positive EV is appearing at less than one row per day "
            "and the paper gate is still closed."
        )
    if ingestion_health["status"] != "HEALTHY":
        return "The model is not stable yet and ingestion freshness needs attention."
    return "Ingestion is healthy, but the model still needs more settled paper outcomes."


def _ingestion_health(*, table_observations: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {row["table"]: row for row in table_observations if row.get("exists")}
    total_core_rows = sum(
        int(counts.get(table, {}).get("row_count") or 0)
        for table in ("markets", "market_snapshots", "forecasts", "market_rankings")
    )
    missing_core = [
        table
        for table in ("markets", "market_snapshots", "forecasts", "market_rankings")
        if not counts.get(table)
    ]
    status = "HEALTHY"
    if missing_core:
        status = "CORE_TABLE_MISSING"
    elif total_core_rows <= 0:
        status = "NO_CORE_INGESTION_ROWS"
    return {
        "status": status,
        "core_row_count": total_core_rows,
        "missing_core_tables": missing_core,
        "latest_core_watermarks": {
            table: counts.get(table, {}).get("max_timestamp")
            for table in ("markets", "market_snapshots", "forecasts", "market_rankings")
        },
    }


def _table_observations(session: Session) -> list[dict[str, Any]]:
    observations = []
    for table_name, label, preferred_timestamp_column in COUNT_TABLES:
        observations.append(
            _table_observation(
                session,
                table_name=table_name,
                label=label,
                preferred_timestamp_column=preferred_timestamp_column,
            )
        )
    return observations


def _table_observation(
    session: Session,
    *,
    table_name: str,
    label: str,
    preferred_timestamp_column: str | None,
) -> dict[str, Any]:
    if not _safe_identifier(table_name):
        raise ValueError(f"Unsafe table name: {table_name}")
    columns = _table_columns(session, table_name)
    if not columns:
        return {
            "table": table_name,
            "label": label,
            "exists": False,
            "row_count": 0,
            "timestamp_column": None,
            "min_timestamp": None,
            "max_timestamp": None,
        }
    timestamp_column = _choose_timestamp_column(columns, preferred_timestamp_column)
    if timestamp_column is None:
        count = _rowid_count_estimate(session, table_name)
        return {
            "table": table_name,
            "label": label,
            "exists": True,
            "row_count": int(count),
            "row_count_method": "sqlite_max_rowid_estimate",
            "timestamp_column": None,
            "min_timestamp": None,
            "max_timestamp": None,
        }
    if not _safe_identifier(timestamp_column):
        raise ValueError(f"Unsafe timestamp column: {timestamp_column}")
    row_count = _rowid_count_estimate(session, table_name)
    first_timestamp = _rowid_boundary_value(
        session,
        table_name=table_name,
        column_name=timestamp_column,
        direction="ASC",
    )
    latest_timestamp = _rowid_boundary_value(
        session,
        table_name=table_name,
        column_name=timestamp_column,
        direction="DESC",
    )
    return {
        "table": table_name,
        "label": label,
        "exists": True,
        "row_count": int(row_count),
        "row_count_method": "sqlite_max_rowid_estimate",
        "timestamp_column": timestamp_column,
        "min_timestamp": _str_or_none(first_timestamp),
        "max_timestamp": _str_or_none(latest_timestamp),
    }


def _rowid_count_estimate(session: Session, table_name: str) -> int:
    try:
        value = session.execute(text(f'SELECT MAX(rowid) FROM "{table_name}"')).scalar()
    except Exception:
        try:
            value = session.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()
        except Exception:
            value = 0
    return int(value or 0)


def _rowid_boundary_value(
    session: Session,
    *,
    table_name: str,
    column_name: str,
    direction: str,
) -> Any:
    order = "DESC" if direction.upper() == "DESC" else "ASC"
    try:
        return session.execute(
            text(
                f'SELECT "{column_name}" FROM "{table_name}" '
                f'WHERE "{column_name}" IS NOT NULL ORDER BY rowid {order} LIMIT 1'
            )
        ).scalar()
    except Exception:
        return None


def _table_columns(session: Session, table_name: str) -> set[str]:
    try:
        rows = session.execute(text(f'PRAGMA table_info("{table_name}")')).mappings().all()
    except Exception:
        return set()
    return {str(row.get("name")) for row in rows if row.get("name")}


def _choose_timestamp_column(
    columns: set[str],
    preferred_timestamp_column: str | None,
) -> str | None:
    if preferred_timestamp_column and preferred_timestamp_column in columns:
        return preferred_timestamp_column
    for candidate in TIMESTAMP_CANDIDATES:
        if candidate in columns:
            return candidate
    return None


def _group_counts(
    session: Session,
    *,
    table_name: str,
    group_column: str,
    timestamp_column: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    if not all(_safe_identifier(value) for value in (table_name, group_column, timestamp_column)):
        raise ValueError("Unsafe identifier in grouped count query.")
    columns = _table_columns(session, table_name)
    if group_column not in columns or timestamp_column not in columns:
        return []
    max_rowid = _rowid_count_estimate(session, table_name)
    min_rowid = max(0, max_rowid - 50000)
    rows = session.execute(
        text(
            f'SELECT "{group_column}" AS group_value, COUNT(*) AS row_count, '
            f'MAX("{timestamp_column}") AS latest_at FROM "{table_name}" '
            'WHERE rowid > :min_rowid '
            f'GROUP BY "{group_column}" ORDER BY row_count DESC LIMIT :limit'
        ),
        {"limit": limit, "min_rowid": min_rowid},
    ).mappings()
    return [
        {
            "model": str(row.get("group_value") or "UNKNOWN"),
            "row_count": int(row.get("row_count") or 0),
            "latest_at": _str_or_none(row.get("latest_at")),
        }
        for row in rows
    ]


def _safe_identifier(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char == "_" for char in value)


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
            "command": "kalshi-bot phase3ba-ingestion-stability-report",
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
        "latest_market_seen_at": _latest_value(session, "markets", "last_seen_at"),
        "latest_snapshot_captured_at": _latest_value(
            session,
            "market_snapshots",
            "captured_at",
        ),
        "latest_forecasted_at": _latest_value(session, "forecasts", "forecasted_at"),
        "latest_ranking_at": _latest_value(session, "market_rankings", "ranked_at"),
    }


def _latest_value(session: Session, table_name: str, column_name: str) -> str | None:
    try:
        value = session.execute(
            text(
                f'SELECT "{column_name}" FROM "{table_name}" '
                f'WHERE "{column_name}" IS NOT NULL ORDER BY rowid DESC LIMIT 1'
            )
        ).scalar()
    except Exception:
        return None
    return _str_or_none(value)


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


def _safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "lowers_thresholds": False,
        "fabricates_evidence": False,
    }


def _next_action(summary: dict[str, Any]) -> dict[str, Any]:
    if summary["paper_ready_rows"]:
        return {
            "stage": "PAPER_ONLY_OPERATOR_REVIEW",
            "command": (
                "kalshi-bot phase3ba-status --output-dir reports/phase3ba_status "
                "--reports-dir reports"
            ),
            "reason": "Paper-ready rows exist; review gates without creating trades automatically.",
            "allow_paper_trade_creation": False,
        }
    return {
        "stage": "BUILD_OPPORTUNITY_CONVERSION_DIAGNOSTICS",
        "command": (
            "kalshi-bot db-writer-monitor --json && kalshi-bot "
            "phase3ba-r4-crypto-executable-book-watch --output-dir "
            "reports/phase3ba_r4 --reports-dir reports && kalshi-bot "
            "phase3ba-r2-weather-ranking-activation --output-dir reports/phase3ba_r2 "
            "--reports-dir reports"
        ),
        "reason": (
            "Ingestion volume is not the main blocker; the next work is reducing "
            "current forecast-to-executable-opportunity loss."
        ),
        "allow_paper_trade_creation": False,
    }


def _operator_should_not_run() -> list[str]:
    return [
        "Do not run accelerate-learning while paper-ready rows are 0.",
        "Do not create paper trades from this diagnostic report.",
        "Do not submit, cancel, replace, or amend live/demo exchange orders.",
        "Do not lower EV, liquidity, spread, settlement, or risk thresholds.",
        "Do not fabricate source evidence, order books, forecasts, or links.",
    ]


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    ev_pace = payload["positive_ev_pace"]
    stability = payload["model_stability"]
    lines = _metadata_lines(payload, title="# Phase 3BA Ingestion Stability Executive Summary")
    lines.extend(
        [
            "",
            "## Bottom Line",
            "",
            summary["bottom_line"],
            "",
            "## Current State",
            "",
            f"- Ingestion status: `{summary['ingestion_status']}`",
            f"- Opportunity conversion: `{summary['opportunity_conversion_status']}`",
            f"- Model stability: `{summary['model_stability_status']}`",
            f"- Runtime measured: `{summary['runtime_hours']}` hours",
            f"- Observed positive EV rows: `{summary['observed_positive_ev_rows']}`",
            f"- Positive EV pace: `{summary['positive_ev_per_day']}` per day",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- True first blocker: `{summary['true_first_blocker']}`",
            f"- Settled paper trades: `{summary['settled_paper_trades']} / "
            f"{summary['target_settled_trades']}`",
            f"- Learning progress: `{summary['learning_progress_percent']}`",
            f"- Stable model estimate: `{summary['stable_model_estimate']}`",
            f"- Current daily-trade ETA: "
            f"`{summary['days_to_target_at_current_daily_paper_trade_pace']}` day(s)",
            f"- EV-pace proxy ETA: "
            f"`{summary['proxy_days_to_target_if_each_observed_ev_became_a_settled_trade']}` "
            "day(s)",
            "",
            "## EV Pace Targets",
            "",
        ]
    )
    for row in ev_pace["targets"]:
        lines.append(
            f"- {row['target_positive_ev_rows']} EV rows: "
            f"`{row['days_remaining_at_observed_pace']}` day(s) remaining at observed pace"
        )
    lines.extend(
        [
            "",
            "## Stability Reason",
            "",
            stability["why"],
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, title="# Phase 3BA Ingestion Stability Report")
    lines.extend(
        [
            "",
            "## Read This First",
            "",
            summary["bottom_line"],
            "",
            "## Graphics",
            "",
            "![Ingestion volume](graphics/ingestion_volume.svg)",
            "",
            "![Conversion funnel](graphics/conversion_funnel.svg)",
            "",
            "![Stability projection](graphics/stability_projection.svg)",
            "",
            "## Table Observations",
            "",
            "Rows use fast SQLite rowid estimates so the report can finish under a "
            "bounded runtime.",
            "",
            "| Area | Rows | First timestamp | Latest timestamp |",
            "| --- | ---: | --- | --- |",
        ]
    )
    for row in payload["table_observations"]:
        lines.append(
            f"| {_md(row['label'])} | {row['row_count']} | "
            f"{_md(row.get('min_timestamp'))} | {_md(row.get('max_timestamp'))} |"
        )
    lines.extend(
        [
            "",
            "## Forecast Models",
            "",
            "| Model | Forecast rows | Latest forecast |",
            "| --- | ---: | --- |",
        ]
    )
    for row in payload["forecast_models"]:
        lines.append(f"| {_md(row['model'])} | {row['row_count']} | {_md(row['latest_at'])} |")
    lines.extend(
        [
            "",
            "## Ranking Models",
            "",
            "| Model | Ranking rows | Latest ranking |",
            "| --- | ---: | --- |",
        ]
    )
    for row in payload["ranking_models"]:
        lines.append(f"| {_md(row['model'])} | {row['row_count']} | {_md(row['latest_at'])} |")
    lines.extend(
        [
            "",
            "## Projection Notes",
            "",
            "- The EV projection uses the operator-supplied count for this run, "
            "not a promise that every EV row becomes a trade.",
            "- The stable-model target uses settled paper trades. Current daily "
            "paper trades are the only reliable completion pace.",
            "- If paper-ready rows stay at 0, the model cannot produce more "
            "settled training examples without fixing the conversion blocker.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    next_action = payload["next_action"]
    lines = _metadata_lines(payload, title="# Phase 3BA Ingestion Stability Next Actions")
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


def _render_ingestion_volume_svg(payload: dict[str, Any]) -> str:
    rows = [
        {"label": row["label"], "value": int(row["row_count"] or 0)}
        for row in payload["table_observations"]
        if row.get("exists")
    ]
    return _bar_chart_svg(
        title="Ingestion Volume By Store",
        subtitle="Log-scale bar widths so small stores remain visible",
        rows=rows,
        color="#138a7e",
    )


def _render_conversion_funnel_svg(payload: dict[str, Any]) -> str:
    table_counts = {
        row["table"]: int(row["row_count"] or 0) for row in payload["table_observations"]
    }
    stability = payload["model_stability"]
    rows = [
        {"label": "Markets", "value": table_counts.get("markets", 0)},
        {"label": "Snapshots", "value": table_counts.get("market_snapshots", 0)},
        {"label": "Forecasts", "value": table_counts.get("forecasts", 0)},
        {"label": "Rankings", "value": table_counts.get("market_rankings", 0)},
        {
            "label": "Observed positive EV",
            "value": payload["positive_ev_pace"]["observed_positive_ev_rows"],
        },
        {"label": "Paper-ready now", "value": stability["paper_ready_rows"]},
        {"label": "Settled paper trades", "value": stability["settled_paper_trades"]},
    ]
    return _bar_chart_svg(
        title="Forecast To Paper Funnel",
        subtitle="Log-scale widths; settled rows are historical training evidence",
        rows=rows,
        color="#2c6ed5",
    )


def _render_stability_projection_svg(payload: dict[str, Any]) -> str:
    ev_targets = payload["positive_ev_pace"]["targets"]
    stability = payload["model_stability"]
    rows = [
        {
            "label": f"{row['target_positive_ev_rows']} EV rows",
            "value": float(row["days_remaining_at_observed_pace"] or 0),
        }
        for row in ev_targets
    ]
    if stability["proxy_days_to_target_if_each_observed_ev_became_a_settled_trade"] is not None:
        rows.append(
            {
                "label": "500 settled proxy",
                "value": float(
                    stability["proxy_days_to_target_if_each_observed_ev_became_a_settled_trade"]
                    or 0
                ),
            }
        )
    return _bar_chart_svg(
        title="How Long At Current Positive-EV Pace",
        subtitle="Days remaining; proxy assumes each EV row eventually becomes settled evidence",
        rows=rows,
        color="#a4661f",
        value_suffix=" days",
        linear=True,
    )


def _bar_chart_svg(
    *,
    title: str,
    subtitle: str,
    rows: list[dict[str, Any]],
    color: str,
    value_suffix: str = "",
    linear: bool = False,
) -> str:
    width = 980
    margin_left = 250
    margin_right = 150
    row_height = 36
    top = 86
    height = top + max(1, len(rows)) * row_height + 48
    plot_width = width - margin_left - margin_right
    values = [max(0.0, float(row.get("value") or 0)) for row in rows]
    if linear:
        max_value = max(values) if values else 1.0
        scaled = [(value / max_value if max_value > 0 else 0.0) for value in values]
    else:
        max_value = max(math.log10(value + 1.0) for value in values) if values else 1.0
        scaled = [
            (math.log10(value + 1.0) / max_value if max_value > 0 else 0.0)
            for value in values
        ]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_xml(title)}">',
        "<style>"
        "text{font-family:Inter,Segoe UI,Arial,sans-serif;fill:#111827}"
        ".sub{fill:#64748b;font-size:15px}"
        ".label{font-size:15px}"
        ".value{font-size:14px;fill:#334155}"
        ".grid{stroke:#e5e7eb;stroke-width:1}"
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="28" y="34" font-size="25" font-weight="700">{_xml(title)}</text>',
        f'<text x="28" y="60" class="sub">{_xml(subtitle)}</text>',
    ]
    for idx, row in enumerate(rows):
        y = top + idx * row_height
        raw_value = max(0.0, float(row.get("value") or 0))
        bar_width = max(2.0 if raw_value > 0 else 0.0, scaled[idx] * plot_width)
        parts.extend(
            [
                f'<line x1="{margin_left}" y1="{y + 18}" x2="{width - margin_right}" '
                f'y2="{y + 18}" class="grid"/>',
                f'<text x="28" y="{y + 22}" class="label">{_xml(str(row["label"]))}</text>',
                f'<rect x="{margin_left}" y="{y + 6}" width="{bar_width:.1f}" height="22" '
                f'rx="5" fill="{color}"/>',
                f'<text x="{margin_left + bar_width + 10:.1f}" y="{y + 22}" '
                f'class="value">{_xml(_format_number(raw_value, suffix=value_suffix))}</text>',
            ]
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


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


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|")


def _xml(value: str) -> str:
    return html.escape(value, quote=True)


def _format_number(value: float, *, suffix: str = "") -> str:
    if value >= 1000:
        rendered = f"{value:,.0f}"
    elif value == int(value):
        rendered = str(int(value))
    else:
        rendered = f"{value:.1f}"
    return f"{rendered}{suffix}"


def _to_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    if math.isinf(value) or math.isnan(value):
        return None
    return round(value, digits)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.relative_to(path.parent)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
