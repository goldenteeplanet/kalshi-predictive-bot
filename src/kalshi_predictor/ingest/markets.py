import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.utils.time import parse_datetime, utc_now

logger = logging.getLogger(__name__)

MarketPageCallback = Callable[[dict[str, Any]], None]


def sync_markets(
    *,
    status: str | None = "open",
    max_pages: int | None = None,
    limit: int = 100,
    series_ticker: str | None = None,
    event_ticker: str | None = None,
    start_cursor: str | None = None,
    deadline_monotonic: float | None = None,
    page_callback: MarketPageCallback | None = None,
    session: Session | None = None,
    client: KalshiClient | None = None,
) -> int:
    owns_session = session is None
    owns_client = client is None
    count = 0

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
            upsert_market(session, market)
            count += 1
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

    logger.info("Synced %s markets", count)
    return count


def sync_settlements(
    *,
    lookback_days: int = 30,
    max_pages: int | None = None,
    limit: int = 100,
    min_settled_ts: str | None = None,
    max_settled_ts: str | None = None,
    commit_every: int | None = None,
    session: Session | None = None,
    client: KalshiClient | None = None,
) -> int:
    owns_session = session is None
    owns_client = client is None
    count = 0
    now = utc_now()
    min_settled_at = parse_datetime(min_settled_ts) or now - timedelta(days=lookback_days)
    max_settled_at = parse_datetime(max_settled_ts)

    if session is None:
        engine = init_db()
        session = get_session_factory(engine)()
    if client is None:
        client = KalshiClient()

    try:
        for market in client.iter_markets(status="settled", limit=limit, max_pages=max_pages):
            settled_at = parse_datetime(
                market.get("settlement_ts")
                or market.get("settled_time")
                or market.get("settled_at")
            )
            if settled_at is not None and settled_at < min_settled_at:
                continue
            if (
                max_settled_at is not None
                and settled_at is not None
                and settled_at > max_settled_at
            ):
                continue
            if market.get("result") is None and market.get("settlement_value_dollars") is None:
                continue
            upsert_market(session, market)
            upsert_settlement(session, market)
            count += 1
            if commit_every is not None and commit_every > 0 and count % commit_every == 0:
                session.commit()
                session.expire_all()
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

    logger.info("Synced %s settlements", count)
    return count
