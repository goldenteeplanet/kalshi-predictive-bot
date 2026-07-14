from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import CryptoMarketLink, Market, MarketSnapshot, SportsMarketLink
from kalshi_predictor.utils.time import utc_now

ACTIVE_MARKET_STATUSES = {
    "active",
    "open",
    "opened",
    "trading",
}

INACTIVE_MARKET_STATUSES = {
    "closed",
    "expired",
    "finalized",
    "inactive",
    "resolved",
    "settled",
}

PHASE3AS_DEPRECATED_FLAG = "phase3as_deprecated"


@dataclass(frozen=True)
class LinkedMarketState:
    source: str
    ticker: str
    link_id: int
    title: str | None
    market_status: str | None
    status_bucket: str
    has_snapshot: bool
    latest_snapshot_at: str | None
    link_deprecated: bool
    link_deprecated_reason: str | None
    symbol_or_game: str | None
    link_reason: str | None


def normalize_market_status(status: Any) -> str:
    return str(status or "").strip().lower().replace("-", "_")


def is_active_market_status(status: Any) -> bool:
    return normalize_market_status(status) in ACTIVE_MARKET_STATUSES


def is_inactive_market_status(status: Any) -> bool:
    return normalize_market_status(status) in INACTIVE_MARKET_STATUSES


def market_status_bucket(status: Any) -> str:
    if is_inactive_market_status(status):
        return "inactive"
    if is_active_market_status(status):
        return "active"
    return "unknown"


def market_status_for_ticker(session: Session, ticker: str) -> str | None:
    market = session.get(Market, ticker)
    if market is not None and market.status:
        return market.status
    snapshot = latest_snapshot_for_ticker(session, ticker)
    return snapshot.status if snapshot is not None else None


def is_ticker_eligible_for_new_forecasts(session: Session, ticker: str) -> bool:
    """Return false only for explicitly closed/inactive markets.

    Unknown status stays eligible for read-only diagnostics so we avoid hiding data
    gaps, while exact closed/settled markets stay out of new forecasts and trades.
    """
    return not is_inactive_market_status(market_status_for_ticker(session, ticker))


def latest_snapshot_for_ticker(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def latest_links_for_table(
    session: Session,
    link_table: type[CryptoMarketLink] | type[SportsMarketLink],
    *,
    limit: int | None = None,
) -> list[CryptoMarketLink | SportsMarketLink]:
    timestamp_col = (
        CryptoMarketLink.detected_at
        if link_table is CryptoMarketLink
        else SportsMarketLink.created_at
    )
    row_number = (
        func.row_number()
        .over(partition_by=link_table.ticker, order_by=[desc(timestamp_col), desc(link_table.id)])
        .label("row_number")
    )
    subquery = select(link_table.id.label("id"), row_number).subquery()
    statement = (
        select(link_table)
        .join(subquery, link_table.id == subquery.c.id)
        .where(subquery.c.row_number == 1)
        .order_by(desc(timestamp_col), desc(link_table.id))
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def linked_market_states(
    session: Session,
    *,
    source: str,
    links: Iterable[CryptoMarketLink | SportsMarketLink],
) -> list[LinkedMarketState]:
    return [linked_market_state(session, source=source, link=link) for link in links]


def linked_market_state(
    session: Session,
    *,
    source: str,
    link: CryptoMarketLink | SportsMarketLink,
) -> LinkedMarketState:
    market = session.get(Market, link.ticker)
    snapshot = latest_snapshot_for_ticker(session, link.ticker)
    status = market.status if market is not None and market.status else (
        snapshot.status if snapshot is not None else None
    )
    raw = decode_json(link.raw_json)
    return LinkedMarketState(
        source=source,
        ticker=link.ticker,
        link_id=link.id,
        title=market.title if market is not None else None,
        market_status=status,
        status_bucket=market_status_bucket(status),
        has_snapshot=snapshot is not None,
        latest_snapshot_at=snapshot.captured_at.isoformat() if snapshot is not None else None,
        link_deprecated=bool(raw.get(PHASE3AS_DEPRECATED_FLAG)),
        link_deprecated_reason=_deprecated_reason(raw),
        symbol_or_game=_symbol_or_game(link),
        link_reason=_link_reason(link),
    )


def mark_link_deprecated(
    link: CryptoMarketLink | SportsMarketLink,
    *,
    market_status: str | None,
    reason: str = "closed_or_inactive_market",
) -> bool:
    raw = decode_json(link.raw_json)
    if raw.get(PHASE3AS_DEPRECATED_FLAG) and raw.get("phase3as_deprecated_reason") == reason:
        return False
    raw[PHASE3AS_DEPRECATED_FLAG] = True
    raw["phase3as_deprecated_reason"] = reason
    raw["phase3as_market_status"] = market_status
    raw["phase3as_deprecated_at"] = utc_now().isoformat()
    raw["phase3as_next_action"] = "Exclude from new forecasts; keep for settlement history."
    link.raw_json = encode_json(raw)
    return True


def is_link_deprecated(link: CryptoMarketLink | SportsMarketLink) -> bool:
    return bool(decode_json(link.raw_json).get(PHASE3AS_DEPRECATED_FLAG))


def _deprecated_reason(raw: dict[str, Any]) -> str | None:
    value = raw.get("phase3as_deprecated_reason")
    return str(value) if value else None


def _symbol_or_game(link: CryptoMarketLink | SportsMarketLink) -> str | None:
    if isinstance(link, CryptoMarketLink):
        return link.symbol
    return link.game_key


def _link_reason(link: CryptoMarketLink | SportsMarketLink) -> str | None:
    if isinstance(link, CryptoMarketLink):
        return link.reason
    return link.link_reason
