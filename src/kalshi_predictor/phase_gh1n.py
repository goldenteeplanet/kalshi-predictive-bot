from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx

from kalshi_predictor.kalshi.orderbook import LocalOrderbook
from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.utils.time import utc_now


def synchronized_market_implied_audit(*, ticker: str, payload: Mapping[str, Any],
                                     fetch_started_at: str, fetch_completed_at: str) -> dict[str, Any]:
    book = LocalOrderbook(ticker)
    book.apply_rest_snapshot(payload, resume_sequence=0)
    midpoint = book.midpoint
    yes_ask = book.best_yes_ask
    no_ask = book.best_no_ask
    yes_edge = midpoint - yes_ask if midpoint is not None and yes_ask is not None else None
    no_probability = Decimal("1") - midpoint if midpoint is not None else None
    no_edge = no_probability - no_ask if no_probability is not None and no_ask is not None else None
    executable_edges = [edge for edge in (yes_edge, no_edge) if edge is not None]
    best_edge = max(executable_edges) if executable_edges else None
    return {
        "ticker": ticker, "fetch_started_at": fetch_started_at,
        "fetch_completed_at": fetch_completed_at, "midpoint_forecast": _string(midpoint),
        "yes_ask": _string(yes_ask), "no_ask": _string(no_ask),
        "yes_executable_edge": _string(yes_edge), "no_executable_edge": _string(no_edge),
        "best_executable_edge": _string(best_edge),
        "synchronized_edge_positive": bool(best_edge is not None and best_edge > 0),
        "explanation": "Market-implied midpoint compared with an executable ask includes half-spread cost.",
    }


def write_gh1n_report(*, gh1m_report: Path, database_path: Path, output_dir: Path,
                      rest_base_url: str = PRODUCTION_PUBLIC_REST_URL) -> Path:
    prior = json.loads(gh1m_report.read_text(encoding="utf-8"))
    tickers = [row["ticker"] for row in prior["ticker_attribution"]]
    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        with httpx.Client(base_url=rest_base_url, timeout=15.0) as client:
            for ticker in tickers:
                started = utc_now()
                response = client.get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                response.raise_for_status()
                completed = utc_now()
                row = synchronized_market_implied_audit(
                    ticker=ticker, payload=response.json(),
                    fetch_started_at=started.isoformat(), fetch_completed_at=completed.isoformat(),
                )
                stored = connection.execute(
                    "SELECT f.forecasted_at, s.captured_at FROM forecasts f "
                    "JOIN market_snapshots s ON s.ticker=f.ticker WHERE f.ticker=? "
                    "AND f.model_name='market_implied_v1' ORDER BY f.forecasted_at DESC, s.captured_at DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                row["stored_forecast_at"] = stored[0] if stored else None
                row["stored_snapshot_at"] = stored[1] if stored else None
                row["stored_forecast_snapshot_skew_seconds"] = _skew(stored[0], stored[1]) if stored else None
                row["fetch_duration_ms"] = str((completed - started).total_seconds() * 1000)
                rows.append(row)
    finally:
        connection.close()
    positive = sum(row["synchronized_edge_positive"] for row in rows)
    report = {
        "phase": "GH-1N", "generated_at": utc_now().isoformat(),
        "mode": "SYNCHRONIZED_PUBLIC_REST_READ_ONLY", "execution_enabled": False,
        "database_writes": 0, "thresholds_changed": False,
        "ticker_timing_audit": rows,
        "summary": {"tickers_audited": len(rows), "positive_synchronized_edges": positive,
                    "nonpositive_synchronized_edges": len(rows) - positive,
                    "candidate_set_status": "CLOSED_NEGATIVE_EDGE" if positive == 0 else "KEEP_OPEN_REVIEW_POSITIVE_EDGE",
                    "next_action": "RETURN_TO_BOUNDED_DISCOVERY" if positive == 0 else "REVIEW_POSITIVE_SYNCHRONIZED_EDGE"},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "gh1n_forecast_executable_price_timing_audit.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _skew(forecasted_at: str, captured_at: str) -> str | None:
    try:
        return str(abs((datetime.fromisoformat(forecasted_at) - datetime.fromisoformat(captured_at)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
