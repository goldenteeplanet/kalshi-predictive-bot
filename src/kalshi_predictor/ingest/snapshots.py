import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_market_snapshot
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.kalshi.client import KalshiClient, KalshiClientError
from kalshi_predictor.utils.time import utc_now

logger = logging.getLogger(__name__)

MarketPageCallback = Callable[[dict[str, Any]], None]


def capture_snapshots(
    *,
    status: str | None = "open",
    max_pages: int | None = 1,
    limit: int = 100,
    series_ticker: str | None = None,
    event_ticker: str | None = None,
    start_cursor: str | None = None,
    deadline_monotonic: float | None = None,
    page_callback: MarketPageCallback | None = None,
    include_orderbook: bool = True,
    orderbook_throttle_seconds: float = 0.1,
    session: Session | None = None,
    client: KalshiClient | None = None,
) -> list[MarketSnapshot]:
    owns_session = session is None
    owns_client = client is None
    snapshots: list[MarketSnapshot] = []

    if session is None:
        engine = init_db()
        session = get_session_factory(engine)()
    if client is None:
        client = KalshiClient()

    try:
        for market in client.iter_markets(
            status=status,
            limit=limit,
            max_pages=max_pages,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
            start_cursor=start_cursor,
            deadline_monotonic=deadline_monotonic,
            page_callback=page_callback,
        ):
            orderbook_json = None
            ticker = str(market.get("ticker", ""))
            if include_orderbook and ticker:
                try:
                    orderbook_json = client.get_orderbook(ticker)
                except KalshiClientError as exc:
                    logger.warning("Skipping orderbook for %s after client error: %s", ticker, exc)
                if orderbook_throttle_seconds > 0:
                    time.sleep(orderbook_throttle_seconds)

            snapshot = insert_market_snapshot(
                session=session,
                market_json=market,
                orderbook_json=orderbook_json,
                captured_at=utc_now(),
            )
            snapshots.append(snapshot)

        if owns_session:
            session.commit()
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
        if owns_client:
            client.close()

    logger.info("Captured %s market snapshots", len(snapshots))
    return snapshots
