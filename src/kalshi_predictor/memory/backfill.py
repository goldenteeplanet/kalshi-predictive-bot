from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Forecast, MarketSnapshot, PaperFill, PaperOrder, Settlement
from kalshi_predictor.memory.capture import (
    capture_forecast_created,
    capture_market_snapshot,
    capture_paper_fill,
    capture_paper_order_created,
    capture_settlement_outcomes,
)
from kalshi_predictor.memory.contracts import INGESTION_BACKFILL


@dataclass(frozen=True)
class MemoryBackfillResult:
    dry_run: bool
    market_snapshots: int
    forecasts: int
    paper_orders: int
    paper_fills: int
    settlements: int


def backfill_memory_from_existing_tables(
    session: Session,
    *,
    dry_run: bool = True,
    limit: int | None = None,
) -> MemoryBackfillResult:
    snapshots = _rows(session, MarketSnapshot, limit)
    forecasts = _rows(session, Forecast, limit)
    orders = _rows(session, PaperOrder, limit)
    fills = _rows(session, PaperFill, limit)
    settlements = _rows(session, Settlement, limit)
    if not dry_run:
        for snapshot in snapshots:
            capture_market_snapshot(
                session,
                snapshot,
                ingestion_mode=INGESTION_BACKFILL,
            )
        for forecast in forecasts:
            capture_forecast_created(
                session,
                forecast,
                ingestion_mode=INGESTION_BACKFILL,
            )
        for order in orders:
            capture_paper_order_created(session, order)
        for fill in fills:
            capture_paper_fill(session, fill)
        for settlement in settlements:
            capture_settlement_outcomes(session, settlement)
    return MemoryBackfillResult(
        dry_run=dry_run,
        market_snapshots=len(snapshots),
        forecasts=len(forecasts),
        paper_orders=len(orders),
        paper_fills=len(fills),
        settlements=len(settlements),
    )


def _rows(session: Session, model: type, limit: int | None) -> list:
    statement = select(model)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))
