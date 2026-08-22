#!/usr/bin/env python3
"""Capture, forecast, and rank only the exact weather tickers prepared this cycle."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import desc, select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import Forecast, MarketRanking, MarketSnapshot
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.utils.time import utc_now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--fetch-workers", type=int, default=4)
    args = parser.parse_args()
    if args.fetch_workers < 1 or args.fetch_workers > 8:
        parser.error("--fetch-workers must be between 1 and 8")
    prepared = json.loads(args.preparation.read_text(encoding="utf-8"))
    tickers = _exact_tickers(prepared, args.limit)
    started_at = utc_now()
    rows = {ticker: {"ticker": ticker} for ticker in tickers}

    engine = init_db()
    session_factory = get_session_factory(engine)
    fetched = _fetch_tickers(tickers, workers=args.fetch_workers)
    try:
        with session_factory() as session:
            snapshots: list[MarketSnapshot] = []
            for ticker in tickers:
                market, orderbook, error = fetched[ticker]
                if error is None:
                    upsert_market(session, market)
                    snapshot = insert_market_snapshot(
                        session, market, orderbook, captured_at=utc_now()
                    )
                    snapshots.append(snapshot)
                    rows[ticker].update(
                        snapshot=True,
                        snapshot_id=snapshot.id,
                        snapshot_at=snapshot.captured_at.isoformat(),
                    )
                else:  # Keep bounded progress for sparse/closed markets.
                    rows[ticker].update(snapshot=False, snapshot_error=error)
            session.flush()
            forecast_summary = run_forecast_models(
                session, model_name="weather_v2", snapshots=snapshots
            )
            session.flush()
            ranking_summary = scan_opportunities(
                session,
                model_name="weather_v2",
                limit=max(1, len(tickers)),
                ticker_scope=set(tickers),
                scan_mode="CURRENT_EXACT_SUPPORTED_WEATHER",
            )
            session.commit()
            _fill_coverage(session, rows, started_at)
    finally:
        engine.dispose()

    snapshot_count = sum(bool(row.get("snapshot")) for row in rows.values())
    forecast_count = sum(bool(row.get("forecast")) for row in rows.values())
    ranking_count = sum(bool(row.get("ranking")) for row in rows.values())
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "preparation_generated_at": prepared.get("generated_at"),
        "exact_ticker_count": len(tickers),
        "snapshot_count": snapshot_count,
        "forecast_count": forecast_count,
        "ranking_count": ranking_count,
        "forecast_run": {
            "snapshots_scanned": forecast_summary.snapshots_scanned,
            "forecasts_inserted": forecast_summary.forecasts_inserted,
            "skipped": forecast_summary.skipped,
        },
        "ranking_run": {
            "markets_scanned": ranking_summary.markets_scanned,
            "rankings_inserted": ranking_summary.rankings_inserted,
            "opportunities_detected": ranking_summary.opportunities_detected,
        },
        "coverage_complete": bool(tickers) and forecast_count == len(tickers),
        "rows": list(rows.values()),
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload))
    return 0 if payload["coverage_complete"] else 1


def _fetch_tickers(
    tickers: list[str], *, workers: int
) -> dict[str, tuple[object | None, object | None, str | None]]:
    results: dict[str, tuple[object | None, object | None, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(tickers)))) as executor:
        futures = {executor.submit(_fetch_ticker, ticker): ticker for ticker in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                market, orderbook = future.result()
                results[ticker] = (market, orderbook, None)
            except Exception as error:
                results[ticker] = (None, None, str(error))
    return results


def _fetch_ticker(ticker: str) -> tuple[object, object]:
    client = KalshiClient()
    try:
        return client.get_market(ticker), client.get_orderbook(ticker)
    finally:
        client.close()


def _exact_tickers(payload: dict[str, object], limit: int) -> list[str]:
    values = payload.get("active_exact_tickers")
    if not isinstance(values, list):
        raise ValueError("preparation artifact lacks active_exact_tickers")
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))[
        : max(0, limit)
    ]


def _fill_coverage(
    session, rows: dict[str, dict[str, object]], started_at: datetime
) -> None:
    for ticker, row in rows.items():
        forecast = session.scalar(
            select(Forecast)
            .where(
                Forecast.ticker == ticker,
                Forecast.model_name == "weather_v2",
                Forecast.forecasted_at >= started_at,
            )
            .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
            .limit(1)
        )
        ranking = session.scalar(
            select(MarketRanking)
            .where(
                MarketRanking.ticker == ticker,
                MarketRanking.forecast_model == "weather_v2",
                MarketRanking.ranked_at >= started_at,
            )
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(1)
        )
        row.update(
            forecast=forecast is not None,
            forecast_id=forecast.id if forecast else None,
            forecast_at=forecast.forecasted_at.isoformat() if forecast else None,
            ranking=ranking is not None,
            ranking_id=ranking.id if ranking else None,
            ranking_at=ranking.ranked_at.isoformat() if ranking else None,
        )


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
