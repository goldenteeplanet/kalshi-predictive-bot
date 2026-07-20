from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import Forecast, MarketSnapshot
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.opportunities.repository import insert_market_ranking
from kalshi_predictor.opportunities.scanner import build_market_ranking
from kalshi_predictor.utils.time import utc_now


def apply_gh1l(
    *, session_factory: Callable[[], Session], settings: Settings, gh1j_report: Path,
    gh1k_report: Path, writer_monitor_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    writer = (writer_monitor_fn or (lambda: db_writer_monitor(settings=settings)))()
    if not writer.get("safe_to_start_write", False):
        return {"status": "BLOCKED_ACTIVE_WRITER", "database_writes": 0,
                "execution_enabled": False, "writer_monitor": writer}
    audit = json.loads(gh1j_report.read_text(encoding="utf-8"))
    preview = json.loads(gh1k_report.read_text(encoding="utf-8"))
    tickers = [row["ticker"] for row in audit["ticker_audits"] if row["calibration_ranking_advance"]]
    snapshots_inserted = rankings_inserted = 0
    with session_factory() as session:
        try:
            for staged_path in preview["weather_staged_files"]:
                payload = json.loads(Path(staged_path).read_text(encoding="utf-8"))
                insert_market_snapshot(
                    session, market_json=payload["market"], orderbook_json=payload["orderbook"],
                    captured_at=utc_now(),
                )
                snapshots_inserted += 1
            snapshots = []
            for ticker in tickers:
                snapshot = session.scalar(
                    select(MarketSnapshot).where(MarketSnapshot.ticker == ticker)
                    .order_by(desc(MarketSnapshot.captured_at)).limit(1)
                )
                if snapshot is not None:
                    snapshots.append(snapshot)
            forecast_summary = run_forecast_models(
                session, model_name="market_implied_v1", snapshots=snapshots
            )
            for snapshot in snapshots:
                forecast = session.scalar(
                    select(Forecast).where(
                        Forecast.ticker == snapshot.ticker,
                        Forecast.model_name == "market_implied_v1",
                    ).order_by(desc(Forecast.forecasted_at)).limit(1)
                )
                if forecast is None:
                    continue
                ranking = build_market_ranking(
                    forecast=forecast, snapshot=snapshot, settings=settings, ranked_at=utc_now()
                )
                insert_market_ranking(session, ranking)
                rankings_inserted += 1
            session.commit()
        except Exception:
            session.rollback()
            raise
    return {
        "status": "COMPLETE" if rankings_inserted == len(tickers) else "PARTIAL",
        "execution_enabled": False, "orders_submitted": 0,
        "single_writer_session_count": 1, "qualified_tickers": tickers,
        "weather_snapshots_inserted": snapshots_inserted,
        "forecasts_inserted": forecast_summary.forecasts_inserted,
        "rankings_inserted": rankings_inserted,
        "writer_monitor": writer,
    }
