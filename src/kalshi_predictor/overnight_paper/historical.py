"""Bounded historical evidence audit and point-in-time scoring, never simulation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .source_health import aware

PUBLIC_RESEARCH_TABLES = {"forecasts", "markets", "market_snapshots", "settlements"}
MAX_ROWS = 5000


def score_historical(row: dict[str, Any]) -> dict[str, Any]:
    """Require explicit acquisition visibility, training cutoff and final provenance.

    Provider event times alone do not prove that an input was available then.
    This function scores an existing probability; it never trains on final labels.
    """
    required = {
        "ticker",
        "event_ticker",
        "model_version",
        "forecast_probability",
        "forecast_at",
        "decision_at",
        "close_time",
        "snapshot_at",
        "snapshot_available_at",
        "source_updated_at",
        "source_available_at",
        "model_training_cutoff",
        "source_sha256",
        "snapshot_sha256",
        "forecast_sha256",
        "final_ticker",
        "final_result",
        "final_status",
        "final_settled_at",
        "final_source_sha256",
    }
    missing = sorted(key for key in required if row.get(key) in (None, ""))
    if missing:
        return {
            "status": "BLOCKED",
            "reason": "MISSING_POINT_IN_TIME_PROVENANCE",
            "missing": missing,
        }
    try:
        at, close = aware(row["decision_at"]), aware(row["close_time"])
        if at >= close:
            raise ValueError("DECISION_NOT_BEFORE_CLOSE")
        for key in (
            "forecast_at",
            "snapshot_at",
            "snapshot_available_at",
            "source_updated_at",
            "source_available_at",
            "model_training_cutoff",
        ):
            if aware(row[key]) > at:
                raise ValueError("FUTURE_INPUT_LEAKAGE:" + key)
        if aware(row["source_updated_at"]) > aware(row["source_available_at"]):
            raise ValueError("SOURCE_CLOCK_ORDER_INVALID")
        if aware(row["snapshot_at"]) > aware(row["snapshot_available_at"]):
            raise ValueError("SNAPSHOT_CLOCK_ORDER_INVALID")
        if row["final_ticker"] != row["ticker"]:
            raise ValueError("FINAL_IDENTITY_MISMATCH")
        if row["final_status"] not in {"settled", "finalized"}:
            raise ValueError("FINAL_RESULT_NOT_FINAL")
        if aware(row["final_settled_at"]) < close:
            raise ValueError("FINAL_BEFORE_CLOSE")
        if row["final_result"] not in {"yes", "no"}:
            raise ValueError("FINAL_BINARY_RESULT_REQUIRED")
        probability = float(row["forecast_probability"])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("INVALID_PROBABILITY")
    except (ValueError, TypeError, AttributeError) as exc:
        return {"status": "BLOCKED", "reason": str(exc)}
    outcome = int(row["final_result"] == "yes")
    likelihood = probability if outcome else 1 - probability
    return {
        "status": "HISTORICAL_EVALUATED",
        "ticker": row["ticker"],
        "event_ticker": row["event_ticker"],
        "model_version": row["model_version"],
        "brier": (probability - outcome) ** 2,
        "log_loss": -math.log(likelihood) if likelihood > 0 else "Infinity",
        "evidence_sha256": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
        "local_paper_settled": False,
        "shadow_settled": False,
    }


def summarize_historical(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [score_historical(row) for row in rows]
    good = [score for score in scores if score["status"] == "HISTORICAL_EVALUATED"]
    # Event-balanced Brier keeps a multi-threshold event from dominating aggregate quality.
    event_brier: dict[str, list[float]] = {}
    for score in good:
        event_brier.setdefault(score["event_ticker"], []).append(score["brier"])
    event_means = [sum(values) / len(values) for values in event_brier.values()]
    return {
        "scores": scores,
        "historical_evaluated_contracts": len(good),
        "historical_independent_events": len(event_brier),
        "event_balanced_brier": sum(event_means) / len(event_means) if event_means else None,
        "blocked_reasons": dict(Counter(s["reason"] for s in scores if s["status"] == "BLOCKED")),
        "local_paper_settled_events": 0,
        "shadow_settled_events": 0,
    }


def audit_research_database(path: Path, *, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Schema first; at most max_rows forecast rows. No portfolio or account reads."""
    if not 0 < max_rows <= MAX_ROWS:
        raise ValueError("ROW_LIMIT_REFUSED")
    result: dict[str, Any] = {
        "database": str(path),
        "mode": "ro",
        "query_only": True,
        "row_limit": max_rows,
        "rows_selected": 0,
        "database_writes": 0,
        "historical_evaluated_contracts": 0,
        "historical_independent_events": 0,
        "schema": {},
    }
    connection = None
    deadline = time.monotonic() + 10
    try:
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

        def authorize(action, table, column, *rest):
            if action == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if table in PUBLIC_RESEARCH_TABLES else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_PRAGMA:
                return (
                    sqlite3.SQLITE_OK
                    if table == "table_info" and column in PUBLIC_RESEARCH_TABLES
                    else sqlite3.SQLITE_DENY
                )
            return (
                sqlite3.SQLITE_OK
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION}
                else sqlite3.SQLITE_DENY
            )

        connection.set_authorizer(authorize)
        for table in sorted(PUBLIC_RESEARCH_TABLES):
            result["schema"][table] = [
                r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')
            ]
        needed = {"id", "ticker", "forecasted_at", "feature_json"}
        if not needed.issubset(result["schema"]["forecasts"]):
            result.update(status="BLOCKED", reason="FORECAST_SCHEMA_MISSING")
            return result
        rows = connection.execute(
            "SELECT id,ticker,forecasted_at,feature_json FROM forecasts ORDER BY id LIMIT ?",
            (max_rows,),
        ).fetchall()
        result["rows_selected"] = len(rows)
        result["forecast_sample"] = []
        coverage: Counter[str] = Counter()
        for forecast_id, ticker, forecasted_at, features in rows:
            try:
                payload = json.loads(features)
                if not isinstance(payload, dict):
                    payload = {}
            except (TypeError, ValueError):
                payload = {}
            for key in ("source_available_at", "snapshot_available_at", "model_training_cutoff"):
                coverage[key] += int(bool(payload.get(key)))
            if len(result["forecast_sample"]) < 5:
                result["forecast_sample"].append(
                    {
                        "id": forecast_id,
                        "ticker": ticker,
                        "forecasted_at": forecasted_at,
                        "feature_keys": sorted(payload),
                    }
                )
        result["explicit_visibility_field_coverage"] = dict(coverage)
        result.update(
            status="BLOCKED",
            reason=("NO_FORECAST_ROWS" if not rows else "HISTORICAL_PROVENANCE_JOIN_NOT_CERTIFIED"),
        )
    except sqlite3.Error as exc:
        result.update(status="BLOCKED", reason="RESEARCH_DATABASE_READ_ERROR", error=str(exc))
    finally:
        if connection is not None:
            connection.close()
    return result
