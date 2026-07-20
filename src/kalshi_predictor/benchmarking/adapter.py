from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from kalshi_predictor.benchmarking.comparison import compare_forecast_rankings


def export_forecast_ranking_rows_read_only(
    database_path: Path, *, model_name: str
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT f.ticker, f.yes_probability, COALESCE(r.opportunity_score, 0) "
            "AS ranking_score FROM forecasts f LEFT JOIN rankings r ON r.ticker=f.ticker "
            "AND r.forecast_model=f.model_name WHERE f.model_name=? "
            "ORDER BY f.ticker, f.forecasted_at DESC",
            (model_name,),
        ).fetchall()
    finally:
        connection.close()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(str(row["ticker"]), dict(row))
    return [latest[ticker] for ticker in sorted(latest)]


def compare_database_model_versions(
    database_path: Path, *, baseline_model: str, candidate_model: str
) -> dict[str, Any]:
    baseline = export_forecast_ranking_rows_read_only(
        database_path, model_name=baseline_model
    )
    candidate = export_forecast_ranking_rows_read_only(
        database_path, model_name=candidate_model
    )
    result = compare_forecast_rankings(baseline, candidate)
    result.update({"database_mode": "read_only", "database_writes": 0,
                   "baseline_model": baseline_model, "candidate_model": candidate_model})
    return result
