import json
import logging
from collections.abc import Callable, Collection
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import upsert_market, upsert_settlement
from kalshi_predictor.data.schema import Market, PaperOrder, Settlement
from kalshi_predictor.kalshi.client import KalshiClient
from kalshi_predictor.utils.time import parse_datetime, utc_now

logger = logging.getLogger(__name__)

MarketPageCallback = Callable[[dict[str, Any]], None]
SettlementTelemetryCallback = Callable[[dict[str, Any]], None]

AUTHORIZED_CRYPTO_SETTLEMENT_SERIES = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")


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
    recover_paper_tickers: bool = True,
    recover_due_local_tickers: bool = True,
    exact_recovery_limit: int = 100,
    exact_recovery_seen_within_hours: int = 48,
    exact_recovery_tickers: Collection[str] = (),
    telemetry_callback: SettlementTelemetryCallback | None = None,
) -> int:
    owns_session = session is None
    owns_client = client is None
    count = 0
    now = utc_now()
    min_settled_at = parse_datetime(min_settled_ts) or now - timedelta(days=lookback_days)
    max_settled_at = parse_datetime(max_settled_ts)
    telemetry: dict[str, Any] = {
        "global_page_seen": 0,
        "global_page_canonical": 0,
        "global_page_unresolved": 0,
        "global_page_outside_window": 0,
        "paper_exact_candidates": 0,
        "due_local_exact_candidates": 0,
        "explicit_exact_candidates": len(set(exact_recovery_tickers)),
        "exact_unique_candidates": 0,
        "exact_canonical": 0,
        "exact_unresolved": 0,
        "exact_api_errors": 0,
        "persisted": 0,
        "commits": 0,
        "exact_recovery_budget": exact_recovery_limit,
        "explicit_exact_admitted": 0,
        "paper_exact_admitted": 0,
        "series_candidates": {},
    }

    if session is None:
        engine = init_db()
        session = get_session_factory(engine)()
    if client is None:
        client = KalshiClient()

    try:
        for market in client.iter_markets(status="settled", limit=limit, max_pages=max_pages):
            telemetry["global_page_seen"] += 1
            settled_at = parse_datetime(
                market.get("settlement_ts")
                or market.get("settled_time")
                or market.get("settled_at")
            )
            if settled_at is not None and settled_at < min_settled_at:
                telemetry["global_page_outside_window"] += 1
                continue
            if (
                max_settled_at is not None
                and settled_at is not None
                and settled_at > max_settled_at
            ):
                telemetry["global_page_outside_window"] += 1
                continue
            if market.get("result") is None and market.get("settlement_value_dollars") is None:
                telemetry["global_page_unresolved"] += 1
                continue
            telemetry["global_page_canonical"] += 1
            upsert_market(session, market)
            upsert_settlement(session, market)
            count += 1
            if commit_every is not None and commit_every > 0 and count % commit_every == 0:
                session.commit()
                session.expire_all()
                telemetry["commits"] += 1
        missing_tickers: set[str] = set()
        paper_candidates: set[str] = set()
        if recover_paper_tickers:
            paper_candidates = set(
                session.scalars(
                    select(PaperOrder.ticker)
                    .outerjoin(Settlement, Settlement.ticker == PaperOrder.ticker)
                    .where(Settlement.ticker.is_(None))
                    .distinct()
                )
            )
            telemetry["paper_exact_candidates"] = len(paper_candidates)
            admitted_paper = sorted(paper_candidates)[:exact_recovery_limit]
            missing_tickers.update(admitted_paper)
            telemetry["paper_exact_admitted"] = len(admitted_paper)
        remaining_budget = max(0, exact_recovery_limit - len(missing_tickers))
        explicit_by_series = {
            root: sorted(
                ticker for ticker in set(exact_recovery_tickers) if ticker.startswith(f"{root}-")
            )
            for root in AUTHORIZED_CRYPTO_SETTLEMENT_SERIES
        }
        if recover_due_local_tickers:
            series_count = len(AUTHORIZED_CRYPTO_SETTLEMENT_SERIES)
            base_quota, remainder = divmod(remaining_budget, series_count)
            for index, series_root in enumerate(AUTHORIZED_CRYPTO_SETTLEMENT_SERIES):
                series_quota = base_quota + (1 if index < remainder else 0)
                admitted_explicit = explicit_by_series[series_root][:series_quota]
                missing_tickers.update(admitted_explicit)
                telemetry["explicit_exact_admitted"] += len(admitted_explicit)
                local_capacity = series_quota - len(admitted_explicit)
                queried_candidates = list(
                    session.scalars(
                        select(Market.ticker)
                        .outerjoin(Settlement, Settlement.ticker == Market.ticker)
                        .where(
                            Settlement.ticker.is_(None),
                            Market.close_time.is_not(None),
                            Market.close_time <= now,
                            Market.event_ticker >= f"{series_root}-",
                            Market.event_ticker < f"{series_root}.",
                            Market.last_seen_at
                            >= now - timedelta(hours=exact_recovery_seen_within_hours),
                        )
                        .order_by(Market.close_time, Market.ticker)
                        .limit(local_capacity + len(admitted_explicit))
                    )
                )
                series_candidates = [
                    ticker for ticker in queried_candidates if ticker not in missing_tickers
                ][:local_capacity]
                telemetry["series_candidates"][series_root] = len(series_candidates)
                telemetry["due_local_exact_candidates"] += len(series_candidates)
                missing_tickers.update(series_candidates)
        else:
            for series_root in AUTHORIZED_CRYPTO_SETTLEMENT_SERIES:
                admitted = explicit_by_series[series_root][:remaining_budget]
                missing_tickers.update(admitted)
                telemetry["explicit_exact_admitted"] += len(admitted)
                remaining_budget -= len(admitted)
                if remaining_budget <= 0:
                    break
        telemetry["exact_unique_candidates"] = len(missing_tickers)
        for ticker in sorted(missing_tickers):
            try:
                market = client.get_market(ticker)
            except Exception as exc:
                telemetry["exact_api_errors"] += 1
                logger.warning("Exact settlement recovery failed for %s: %s", ticker, exc)
                continue
            if market.get("result") is None and market.get("settlement_value_dollars") is None:
                telemetry["exact_unresolved"] += 1
                continue
            telemetry["exact_canonical"] += 1
            upsert_market(session, market)
            upsert_settlement(session, market)
            count += 1
        if owns_session:
            session.commit()
            telemetry["commits"] += 1
    except Exception:
        if owns_session:
            session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
        if owns_client:
            client.close()

    telemetry["persisted"] = count
    logger.info("Settlement sync telemetry: %s", json.dumps(telemetry, sort_keys=True))
    if telemetry_callback is not None:
        telemetry_callback(telemetry)
    logger.info("Synced %s settlements", count)
    return count
