from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, desc, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    Alert,
    AlertEvent,
    BacktestRun,
    BacktestTrade,
    Forecast,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    ModelLeaderboard,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PaperPosition,
    PortfolioSnapshot,
    PositionHistory,
    Settlement,
    Watchlist,
    WatchlistMarket,
)
from kalshi_predictor.opportunities.market_identity import annotated_opportunity_row
from kalshi_predictor.paper.ledger import (
    get_latest_snapshot_for_ticker,
    get_paper_summary,
)
from kalshi_predictor.paper.models import ORDER_OPEN
from kalshi_predictor.paper.pnl import calculate_unrealized_pnl
from kalshi_predictor.utils.decimals import decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import utc_now

SUPPORTED_MODELS = [
    "market_implied_v1",
    "crypto_v2",
    "weather_v2",
    "economic_v1",
    "ensemble_v1",
    "ensemble_v2",
]

DEFAULT_WATCHLISTS = [
    "Default Watchlist",
    "High Conviction",
    "Crypto",
    "Weather",
    "Sports",
]

DEFAULT_ALERTS = [
    ("High opportunity score", "opportunity_score_exceeds", Decimal("80")),
    ("High model confidence", "model_confidence_exceeds", Decimal("75")),
    ("Spread widened", "spread_widens", Decimal("0.15")),
    ("Market expires soon", "market_expires_soon", Decimal("60")),
    ("Paper exposure limit", "position_exposure_exceeds", Decimal("5")),
]

LOCAL_DERIVED_COMPOSITE_PREFIXES = (
    "KXMVECROSSCATEGORY-",
    "KXMVESPORTSMULTIGAMEEXTENDED-",
)

LOCAL_DERIVED_COMPOSITE_NOTICE = (
    "Local derived composite tickers are not direct Kalshi exchange markets. "
    "They stay in the backlog until exact same-composite settlement evidence "
    "has produced realized paper P&L."
)

MARKET_MONITOR_STALE_AFTER_MINUTES = 15


def portfolio_summary(session: Session) -> dict[str, Any]:
    paper = get_paper_summary(session)
    positions = position_rows(session)
    latest_snapshot = latest_portfolio_snapshot(session)
    return {
        "portfolio_value": decimal_to_str(paper.total_pnl) or "0",
        "total_exposure": decimal_to_str(sum(_decimal(row["exposure"]) for row in positions))
        or "0",
        "open_positions": paper.active_positions,
        "realized_pnl": decimal_to_str(paper.total_realized_pnl) or "0",
        "unrealized_pnl": decimal_to_str(paper.estimated_unrealized_pnl) or "0",
        "total_pnl": decimal_to_str(paper.total_pnl) or "0",
        "open_orders": paper.open_orders,
        "positions": positions,
        "latest_snapshot": _portfolio_snapshot_row(latest_snapshot),
        "category_allocation": category_allocation(session, positions=positions),
        "pnl_series": portfolio_snapshot_series(session, field="total_pnl"),
        "exposure_series": portfolio_snapshot_series(session, field="total_exposure"),
    }


def portfolio_summary_fast(
    session: Session,
    *,
    positions_limit: int = 50,
    series_limit: int = 8,
) -> dict[str, Any]:
    positions = position_rows(
        session,
        limit=positions_limit,
        active_only=True,
        include_local_derived_composites=False,
    )
    realized = _total_realized_pnl_fast(session)
    unrealized = sum(_decimal(row["unrealized_pnl"]) for row in positions)
    exposure = sum(_decimal(row["exposure"]) for row in positions)
    active_positions = _active_position_count(
        session,
        include_local_derived_composites=False,
    )
    local_composites = _local_composite_position_summary(
        session,
        limit=positions_limit,
    )
    raw_active_positions = active_positions + local_composites["total_count"]
    open_orders = int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.status == ORDER_OPEN)
        )
        or 0
    )
    return {
        "portfolio_value": decimal_to_str(realized + unrealized) or "0",
        "total_exposure": decimal_to_str(exposure) or "0",
        "open_positions": active_positions,
        "realized_pnl": decimal_to_str(realized) or "0",
        "unrealized_pnl": decimal_to_str(unrealized) or "0",
        "total_pnl": decimal_to_str(realized + unrealized) or "0",
        "open_orders": open_orders,
        "positions": positions,
        "positions_are_direct_only": True,
        "positions_truncated": active_positions > len(positions),
        "raw_active_positions": raw_active_positions,
        "local_composite_total_count": local_composites["total_count"],
        "local_composite_resolved_count": local_composites["resolved_count"],
        "local_composite_settlement_ready_count": local_composites["settlement_ready_count"],
        "local_composite_backlog_count": local_composites["backlog_count"],
        "local_composite_backlog": local_composites["backlog_rows"],
        "local_composite_backlog_truncated": local_composites["backlog_count"]
        > len(local_composites["backlog_rows"]),
        "local_composite_notice": LOCAL_DERIVED_COMPOSITE_NOTICE,
        "latest_snapshot": _portfolio_snapshot_row(latest_portfolio_snapshot(session)),
        "category_allocation": category_allocation(session, positions=positions),
        "pnl_series": portfolio_snapshot_series(session, field="total_pnl", limit=series_limit),
        "exposure_series": portfolio_snapshot_series(
            session,
            field="total_exposure",
            limit=series_limit,
        ),
        "fast_bounded": True,
        "positions_limit": positions_limit,
    }


def paper_liquidity_plan(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    portfolio = portfolio_summary(session)
    starting_capital = _decimal(resolved.paper_liquidity_starting_capital)
    growth_target = _decimal(resolved.paper_liquidity_growth_target)
    max_position_fraction = _decimal(resolved.paper_liquidity_max_position_fraction)
    total_pnl = _decimal(portfolio["total_pnl"])
    exposure = _decimal(portfolio["total_exposure"])
    current_equity = starting_capital + total_pnl
    available_liquidity = max(Decimal("0"), current_equity - exposure)
    max_new_position = max(
        Decimal("0"),
        min(available_liquidity, current_equity * max_position_fraction),
    )
    progress_denominator = max(Decimal("0.01"), growth_target - starting_capital)
    progress = max(
        Decimal("0"),
        min(Decimal("1"), (current_equity - starting_capital) / progress_denominator),
    )
    return {
        "mode": "PAPER ONLY",
        "starting_capital": decimal_to_str(starting_capital) or "0",
        "growth_target": decimal_to_str(growth_target) or "0",
        "current_equity": decimal_to_str(current_equity) or "0",
        "total_pnl": decimal_to_str(total_pnl) or "0",
        "exposure": decimal_to_str(exposure) or "0",
        "available_liquidity": decimal_to_str(available_liquidity) or "0",
        "max_new_position": decimal_to_str(max_new_position) or "0",
        "max_position_fraction": decimal_to_str(max_position_fraction) or "0",
        "progress_percent": f"{(progress * Decimal('100')).quantize(Decimal('0.1'))}%",
        "progress_bar_width": f"{(progress * Decimal('100')).quantize(Decimal('0.1'))}%",
        "next_action": (
            "Let Learning Mode prefer fast-settlement paper trades and review realized "
            "paper P&L before increasing the simulated bankroll."
        ),
    }


def record_position_history(session: Session) -> list[PositionHistory]:
    rows: list[PositionHistory] = []
    now = utc_now()
    positions = list(session.scalars(select(PaperPosition).order_by(PaperPosition.ticker)))
    for position in positions:
        row = _position_history_row(session, position, recorded_at=now)
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def create_portfolio_snapshot(session: Session) -> PortfolioSnapshot:
    summary = get_paper_summary(session)
    positions = position_rows(session)
    snapshot = PortfolioSnapshot(
        snapshot_time=utc_now(),
        total_positions=summary.active_positions,
        total_exposure=decimal_to_str(sum(_decimal(row["exposure"]) for row in positions)) or "0",
        realized_pnl=decimal_to_str(summary.total_realized_pnl) or "0",
        unrealized_pnl=decimal_to_str(summary.estimated_unrealized_pnl) or "0",
        total_pnl=decimal_to_str(summary.total_pnl) or "0",
        open_orders=summary.open_orders,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def record_portfolio_state(session: Session) -> dict[str, Any]:
    history = record_position_history(session)
    snapshot = create_portfolio_snapshot(session)
    return {
        "positions_recorded": len(history),
        "snapshot_id": snapshot.id,
        "total_pnl": snapshot.total_pnl,
        "total_exposure": snapshot.total_exposure,
    }


def _active_position_count(
    session: Session,
    *,
    include_local_derived_composites: bool = True,
    local_derived_only: bool = False,
) -> int:
    statement = (
        select(func.count())
        .select_from(PaperPosition)
        .where(_active_position_clause())
    )
    if local_derived_only:
        statement = statement.where(_local_derived_composite_clause())
    elif not include_local_derived_composites:
        statement = statement.where(not_(_local_derived_composite_clause()))
    return int(session.scalar(statement) or 0)


def _total_realized_pnl_fast(session: Session) -> Decimal:
    return sum(
        (_decimal(value) for value in session.scalars(select(PaperPosition.realized_pnl))),
        Decimal("0"),
    )


def _active_position_clause() -> Any:
    return (PaperPosition.yes_contracts != 0) | (PaperPosition.no_contracts != 0)


def _local_derived_composite_clause() -> Any:
    return or_(
        *[
            PaperPosition.ticker.startswith(prefix)
            for prefix in LOCAL_DERIVED_COMPOSITE_PREFIXES
        ]
    )


def _is_local_derived_composite_ticker(ticker: str) -> bool:
    return ticker.startswith(LOCAL_DERIVED_COMPOSITE_PREFIXES)


def _local_composite_position_summary(
    session: Session,
    *,
    limit: int,
) -> dict[str, Any]:
    positions = list(
        session.scalars(
            select(PaperPosition)
            .where(_active_position_clause())
            .where(_local_derived_composite_clause())
            .order_by(PaperPosition.ticker)
        )
    )
    tickers = [position.ticker for position in positions]
    latest_pnl = _latest_pnl_by_ticker(session, tickers)
    settlement_tickers = _settlement_tickers(session, tickers)
    resolved_count = 0
    settlement_ready_count = 0
    backlog: list[tuple[PaperPosition, str]] = []
    for position in positions:
        state = _local_composite_state(
            position.ticker,
            latest_pnl=latest_pnl,
            settlement_tickers=settlement_tickers,
        )
        if state == "realized":
            resolved_count += 1
        else:
            if state == "settlement_ready":
                settlement_ready_count += 1
            backlog.append((position, state))

    backlog_rows = []
    for position, state in backlog[:limit]:
        row = position_row(session, position)
        row["local_composite_state"] = state
        row["settlement_status"] = _local_composite_status_label(state)
        backlog_rows.append(row)

    return {
        "total_count": len(positions),
        "resolved_count": resolved_count,
        "settlement_ready_count": settlement_ready_count,
        "backlog_count": len(backlog),
        "backlog_rows": backlog_rows,
    }


def _latest_pnl_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, PaperPnl]:
    if not tickers:
        return {}
    latest: dict[str, PaperPnl] = {}
    rows = session.scalars(
        select(PaperPnl)
        .where(PaperPnl.ticker.in_(tickers))
        .order_by(PaperPnl.ticker, desc(PaperPnl.calculated_at), desc(PaperPnl.id))
    )
    for row in rows:
        latest.setdefault(row.ticker, row)
    return latest


def _settlement_tickers(session: Session, tickers: list[str]) -> set[str]:
    if not tickers:
        return set()
    return set(
        session.scalars(select(Settlement.ticker).where(Settlement.ticker.in_(tickers)))
    )


def _local_composite_state(
    ticker: str,
    *,
    latest_pnl: dict[str, PaperPnl],
    settlement_tickers: set[str],
) -> str:
    pnl = latest_pnl.get(ticker)
    if pnl is not None and _paper_pnl_is_realized(pnl):
        return "realized"
    if ticker in settlement_tickers:
        return "settlement_ready"
    return "settlement_missing"


def _paper_pnl_is_realized(row: PaperPnl) -> bool:
    return (
        (row.notes or "").strip().lower() == "settled market realized paper p&l"
        and bool(str(row.settlement_result or "").strip())
    )


def _local_composite_status_label(state: str) -> str:
    if state == "settlement_ready":
        return "Exact composite settlement ready; paper P&L not realized yet"
    if state == "settlement_missing":
        return "Waiting for guarded exact local composite settlement"
    return "Realized from exact same-composite settlement"


def position_rows(
    session: Session,
    *,
    limit: int | None = None,
    active_only: bool = False,
    include_local_derived_composites: bool = True,
    local_derived_only: bool = False,
) -> list[dict[str, Any]]:
    statement = select(PaperPosition).order_by(PaperPosition.ticker)
    if active_only:
        statement = statement.where(_active_position_clause())
    if local_derived_only:
        statement = statement.where(_local_derived_composite_clause())
    elif not include_local_derived_composites:
        statement = statement.where(not_(_local_derived_composite_clause()))
    if limit is not None:
        statement = statement.limit(limit)
    positions = list(session.scalars(statement))
    return _position_rows_from_loaded(session, positions)


def position_row(session: Session, position: PaperPosition) -> dict[str, Any]:
    snapshot = get_latest_snapshot_for_ticker(session, position.ticker)
    market = session.get(Market, position.ticker)
    return _position_row_from_loaded(position, snapshot=snapshot, market=market)


def _position_rows_from_loaded(
    session: Session,
    positions: list[PaperPosition],
) -> list[dict[str, Any]]:
    tickers = [position.ticker for position in positions]
    snapshots = _latest_snapshots_for_tickers(session, tickers)
    markets = _markets_for_tickers(session, tickers)
    return [
        _position_row_from_loaded(
            position,
            snapshot=snapshots.get(position.ticker),
            market=markets.get(position.ticker),
        )
        for position in positions
    ]


def _latest_snapshots_for_tickers(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    if not tickers:
        return {}
    ranked = (
        select(
            MarketSnapshot,
            func.row_number()
            .over(
                partition_by=MarketSnapshot.ticker,
                order_by=(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)),
            )
            .label("row_number"),
        )
        .where(MarketSnapshot.ticker.in_(tickers))
        .subquery()
    )
    snapshot_alias = aliased(MarketSnapshot, ranked)
    rows = session.scalars(select(snapshot_alias).where(ranked.c.row_number == 1))
    return {snapshot.ticker: snapshot for snapshot in rows}


def _markets_for_tickers(
    session: Session,
    tickers: list[str],
) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        market.ticker: market
        for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    }


def _position_row_from_loaded(
    position: PaperPosition,
    *,
    snapshot: MarketSnapshot | None,
    market: Market | None,
) -> dict[str, Any]:
    market_price = _market_price(snapshot)
    avg_cost = _avg_cost(position)
    realized = _decimal(position.realized_pnl)
    unrealized = calculate_unrealized_pnl(position, snapshot)
    exposure = _position_exposure(position)
    is_local_derived_composite = _is_local_derived_composite_ticker(position.ticker)
    return {
        "ticker": position.ticker,
        "title": (market.title if market else None) or position.ticker,
        "category": market_category(market),
        "position_kind": (
            "local_derived_composite"
            if is_local_derived_composite
            else "direct_exchange_position"
        ),
        "is_local_derived_composite": is_local_derived_composite,
        "settlement_status": (
            "Guarded local composite settlement required"
            if is_local_derived_composite
            else "Exchange-backed paper position"
        ),
        "position_size": position.yes_contracts - position.no_contracts,
        "yes_contracts": position.yes_contracts,
        "no_contracts": position.no_contracts,
        "avg_cost": decimal_to_str(avg_cost),
        "market_price": decimal_to_str(market_price),
        "realized_pnl": decimal_to_str(realized) or "0",
        "unrealized_pnl": decimal_to_str(unrealized) or "0",
        "total_pnl": decimal_to_str(realized + unrealized) or "0",
        "exposure": decimal_to_str(exposure) or "0",
        "updated_at": position.updated_at.isoformat(),
    }


def position_detail(session: Session, ticker: str) -> dict[str, Any] | None:
    position = session.get(PaperPosition, ticker)
    market = session.get(Market, ticker)
    if position is None and market is None:
        return None
    current = position_row(session, position) if position else _empty_position_row(ticker, market)
    return {
        "current": current,
        "history": position_history(session, ticker=ticker, limit=50),
        "recent_fills": recent_fills(session, ticker=ticker, limit=20),
        "forecasts": recent_forecasts(session, ticker=ticker, limit=20),
        "opportunities": recent_opportunities(session, ticker=ticker, limit=20),
        "backtests": recent_backtests(session, ticker=ticker, limit=20),
    }


def position_history(session: Session, *, ticker: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(PositionHistory)
        .where(PositionHistory.ticker == ticker)
        .order_by(desc(PositionHistory.recorded_at), desc(PositionHistory.id))
        .limit(limit)
    )
    return [_position_history_dict(row) for row in rows]


def market_monitor_rows(
    session: Session,
    *,
    limit: int = 100,
    category: str | None = None,
    model: str | None = None,
    search: str | None = None,
    min_score: Decimal | None = None,
    min_liquidity: Decimal | None = None,
    min_confidence: Decimal | None = None,
) -> list[dict[str, Any]]:
    statement = select(MarketRanking).order_by(
        desc(MarketRanking.ranked_at),
        desc(MarketRanking.opportunity_score),
        desc(MarketRanking.id),
    )
    if model:
        statement = statement.where(MarketRanking.forecast_model == model)
    rankings = []
    candidate_limit = max(limit * 5, limit + 250)
    ranking_scan_limit = max(candidate_limit * 10, 2_000)
    candidate_unique_limit = (
        ranking_scan_limit
        if any(
            value is not None
            for value in (category, search, min_score, min_liquidity, min_confidence)
        )
        else candidate_limit
    )
    seen: set[str] = set()
    candidates: list[MarketRanking] = []
    for ranking in session.scalars(statement.limit(ranking_scan_limit)):
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        candidates.append(ranking)
        if len(candidates) >= candidate_unique_limit:
            break

    tickers = [ranking.ticker for ranking in candidates]
    markets_by_ticker = _markets_by_ticker(session, tickers)
    snapshots_by_ticker = _latest_market_snapshots(session, tickers)
    multileg_tickers = [
        ranking.ticker
        for ranking in candidates
        if _looks_like_multileg_sports_market(
            ranking.title
            or (
                markets_by_ticker[ranking.ticker].title
                if ranking.ticker in markets_by_ticker
                else None
            )
            or ranking.ticker,
            ranking=ranking,
            category=market_category(markets_by_ticker.get(ranking.ticker), ranking=ranking),
        )
    ]
    leg_labels_by_ticker = _market_monitor_leg_labels_by_ticker(session, multileg_tickers)

    for ranking in candidates:
        market = markets_by_ticker.get(ranking.ticker)
        snapshot = snapshots_by_ticker.get(ranking.ticker)
        row_category = market_category(market, ranking=ranking)
        if category and category.lower() != row_category.lower():
            continue
        if search and search.lower() not in _market_search_text(market, ranking).lower():
            continue
        liquidity = _market_liquidity_value(ranking, snapshot, market) or Decimal("0")
        confidence = _decimal(ranking.model_confidence_score)
        score = _decimal(ranking.opportunity_score)
        if min_score is not None and score < min_score:
            continue
        if min_liquidity is not None and liquidity < min_liquidity:
            continue
        if min_confidence is not None and confidence < min_confidence:
            continue
        current_price = _display_market_price(ranking, snapshot)
        spread = _display_market_spread(ranking, snapshot)
        display_liquidity = _display_market_liquidity(ranking, snapshot, market)
        data_quality = _market_monitor_data_quality(
            current_price=current_price,
            spread=spread,
            liquidity=display_liquidity,
            observed_at=snapshot.captured_at if snapshot is not None else ranking.ranked_at,
            market=market,
        )
        raw_title = ranking.title or (market.title if market else None) or ranking.ticker
        rankings.append(
            {
                "ticker": ranking.ticker,
                "market": _market_monitor_title(
                    session,
                    market=market,
                    ranking=ranking,
                    category=row_category,
                    leg_labels=leg_labels_by_ticker.get(ranking.ticker),
                ),
                "category": row_category,
                "current_price": current_price,
                "spread": spread,
                "liquidity": display_liquidity,
                "data_freshness": _display_market_freshness(ranking, snapshot),
                "data_quality": data_quality["label"],
                "snapshot_repair_status": _snapshot_repair_status(data_quality["label"]),
                "opportunity_score": ranking.opportunity_score,
                "best_model": ranking.forecast_model,
                "model_confidence": ranking.model_confidence_score,
                "recommended_action": _market_monitor_recommended_action(
                    ranking,
                    data_quality=data_quality["label"],
                ),
                "_quality_sort": data_quality["sort"],
                "_score_sort": score,
                "_ranked_at_sort": _aware_datetime(ranking.ranked_at).timestamp(),
                "_group_missing_multileg_sports": (
                    data_quality["label"] == "Missing market data"
                    and _looks_like_multileg_sports_market(
                        raw_title,
                        ranking=ranking,
                        category=row_category,
                    )
                ),
            }
        )
        if len(rankings) >= candidate_limit:
            break
    rankings.sort(
        key=lambda row: (
            row["_quality_sort"],
            -float(row["_score_sort"]),
            -float(row["_ranked_at_sort"]),
        )
    )
    grouped_missing = [row for row in rankings if row["_group_missing_multileg_sports"]]
    visible = [row for row in rankings if not row["_group_missing_multileg_sports"]][:limit]
    if grouped_missing and len(visible) < limit:
        visible.append(_grouped_missing_multileg_sports_row(grouped_missing))
    for row in visible:
        row.pop("_quality_sort", None)
        row.pop("_score_sort", None)
        row.pop("_ranked_at_sort", None)
        row.pop("_group_missing_multileg_sports", None)
    return visible


def model_performance_rows(session: Session) -> list[dict[str, Any]]:
    latest = _latest_leaderboard_by_model(session)
    rows = []
    roi_values = [
        _decimal(row.roi_on_exposure)
        for row in latest.values()
        if row.roi_on_exposure is not None
    ]
    best_roi = max(roi_values) if roi_values else None
    worst_roi = min(roi_values) if roi_values else None
    for model in SUPPORTED_MODELS:
        row = latest.get(model)
        roi = _decimal(row.roi_on_exposure) if row else Decimal("0")
        rows.append(
            {
                "model_name": model,
                "forecast_count": row.forecast_count if row else 0,
                "trade_count": row.paper_trade_count if row else 0,
                "roi": row.roi_on_exposure if row else None,
                "win_rate": row.win_rate if row else None,
                "brier_score": row.brier_score if row else None,
                "log_loss": row.log_loss if row else None,
                "max_drawdown": row.max_drawdown if row else None,
                "rank_color": _rank_color(roi, best_roi=best_roi, worst_roi=worst_roi),
                "notes": row.notes if row else "No leaderboard row yet.",
            }
        )
    return rows


def analytics_summary(session: Session) -> dict[str, Any]:
    snapshots = list(
        session.scalars(
            select(PortfolioSnapshot)
            .order_by(desc(PortfolioSnapshot.snapshot_time), desc(PortfolioSnapshot.id))
            .limit(90)
        )
    )
    opportunities = list(
        session.scalars(
            select(MarketOpportunity)
            .order_by(desc(MarketOpportunity.detected_at), desc(MarketOpportunity.id))
            .limit(200)
        )
    )
    forecasts = list(
        session.scalars(select(Forecast).order_by(desc(Forecast.forecasted_at)).limit(200))
    )
    orders = list(
        session.scalars(select(PaperOrder).order_by(desc(PaperOrder.created_at)).limit(200))
    )
    return {
        "daily_pnl": _bucket_snapshots(snapshots, days=1),
        "weekly_pnl": _bucket_snapshots(snapshots, days=7),
        "monthly_pnl": _bucket_snapshots(snapshots, days=30),
        "forecast_accuracy_trend": _trend_rows(forecasts, "forecasted_at", "yes_probability"),
        "opportunity_trend": _trend_rows(opportunities, "detected_at", "opportunity_score"),
        "model_ranking_trend": model_performance_rows(session),
        "paper_trade_growth": _trend_rows(orders, "created_at", "quantity"),
    }


def ensure_default_watchlists(session: Session) -> list[Watchlist]:
    rows: list[Watchlist] = []
    for name in DEFAULT_WATCHLISTS:
        row = session.scalar(select(Watchlist).where(Watchlist.name == name).limit(1))
        if row is None:
            row = Watchlist(name=name, description=f"{name} markets", created_at=utc_now())
            session.add(row)
            session.flush()
        rows.append(row)
    return rows


def watchlists_summary(
    session: Session,
    *,
    ensure_defaults: bool = True,
) -> list[dict[str, Any]]:
    watchlists = (
        ensure_default_watchlists(session)
        if ensure_defaults
        else list(session.scalars(select(Watchlist).order_by(Watchlist.name)))
    )
    rows = []
    for watchlist in watchlists:
        markets = list(
            session.scalars(
                select(WatchlistMarket)
                .where(WatchlistMarket.watchlist_id == watchlist.id)
                .order_by(desc(WatchlistMarket.added_at), desc(WatchlistMarket.id))
            )
        )
        rows.append(
            {
                "id": watchlist.id,
                "name": watchlist.name,
                "description": watchlist.description,
                "count": len(markets),
                "markets": [_watchlist_market_row(session, item) for item in markets],
            }
        )
    return rows


def add_market_to_watchlist(
    session: Session,
    *,
    watchlist_id: int,
    ticker: str,
    notes: str | None = None,
) -> WatchlistMarket:
    existing = session.scalar(
        select(WatchlistMarket)
        .where(WatchlistMarket.watchlist_id == watchlist_id, WatchlistMarket.ticker == ticker)
        .limit(1)
    )
    if existing is not None:
        return existing
    item = WatchlistMarket(
        watchlist_id=watchlist_id,
        ticker=ticker,
        added_at=utc_now(),
        notes=notes,
    )
    session.add(item)
    session.flush()
    return item


def remove_market_from_watchlist(session: Session, *, item_id: int) -> int:
    result = session.execute(delete(WatchlistMarket).where(WatchlistMarket.id == item_id))
    return int(result.rowcount or 0)


def ensure_default_alerts(session: Session) -> list[Alert]:
    rows: list[Alert] = []
    for name, alert_type, threshold in DEFAULT_ALERTS:
        row = session.scalar(select(Alert).where(Alert.alert_type == alert_type).limit(1))
        if row is None:
            row = Alert(
                name=name,
                alert_type=alert_type,
                threshold=decimal_to_str(threshold),
                enabled=1,
                created_at=utc_now(),
                raw_json=encode_json({"default": True}),
            )
            session.add(row)
            session.flush()
        rows.append(row)
    return rows


def evaluate_alerts(session: Session) -> list[AlertEvent]:
    alerts = ensure_default_alerts(session)
    rows: list[AlertEvent] = []
    rankings = market_monitor_rows(session, limit=100)
    positions = position_rows(session)
    for alert in alerts:
        threshold = _decimal(alert.threshold)
        if not alert.enabled:
            continue
        if alert.alert_type == "opportunity_score_exceeds":
            rows.extend(_opportunity_alerts(session, alert, rankings, threshold))
        elif alert.alert_type == "model_confidence_exceeds":
            rows.extend(_confidence_alerts(session, alert, rankings, threshold))
        elif alert.alert_type == "spread_widens":
            rows.extend(_spread_alerts(session, alert, rankings, threshold))
        elif alert.alert_type == "market_expires_soon":
            rows.extend(_expiry_alerts(session, alert, threshold))
        elif alert.alert_type == "position_exposure_exceeds":
            rows.extend(_exposure_alerts(session, alert, positions, threshold))
    session.flush()
    return rows


def alerts_summary(
    session: Session,
    *,
    limit: int = 50,
    ensure_defaults: bool = True,
) -> dict[str, Any]:
    alerts = (
        ensure_default_alerts(session)
        if ensure_defaults
        else list(session.scalars(select(Alert).order_by(Alert.name)))
    )
    events = list(
        session.scalars(
            select(AlertEvent)
            .order_by(desc(AlertEvent.created_at), desc(AlertEvent.id))
            .limit(limit)
        )
    )
    return {
        "alerts": [_alert_row(alert) for alert in alerts],
        "events": [_alert_event_row(event) for event in events],
        "open_count": sum(1 for event in events if event.acknowledged_at is None),
    }


def recent_fills(session: Session, *, ticker: str, limit: int = 20) -> list[dict[str, Any]]:
    fills = session.scalars(
        select(PaperFill)
        .where(PaperFill.ticker == ticker)
        .order_by(desc(PaperFill.filled_at), desc(PaperFill.id))
        .limit(limit)
    )
    return [
        {
            "filled_at": fill.filled_at.isoformat(),
            "side": fill.side,
            "price": fill.price,
            "quantity": fill.quantity,
            "fee": fill.fee,
        }
        for fill in fills
    ]


def recent_forecasts(session: Session, *, ticker: str, limit: int = 20) -> list[dict[str, Any]]:
    forecasts = session.scalars(
        select(Forecast)
        .where(Forecast.ticker == ticker)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(limit)
    )
    return [
        {
            "forecasted_at": forecast.forecasted_at.isoformat(),
            "model_name": forecast.model_name,
            "yes_probability": forecast.yes_probability,
            "market_mid_probability": forecast.market_mid_probability,
            "notes": forecast.notes,
        }
        for forecast in forecasts
    ]


def recent_opportunities(
    session: Session,
    *,
    ticker: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(MarketOpportunity)
        .where(MarketOpportunity.ticker == ticker)
        .order_by(desc(MarketOpportunity.detected_at), desc(MarketOpportunity.id))
        .limit(limit)
    )
    market = session.get(Market, ticker)
    return [
        annotated_opportunity_row(
            session,
            {
                "ticker": ticker,
                "detected_at": row.detected_at.isoformat(),
                "model_name": row.model_name,
                "side": row.side,
                "price": row.price,
                "estimated_edge": row.estimated_edge,
                "opportunity_score": row.opportunity_score,
                "reason": row.reason,
            },
            ticker=ticker,
            market=market,
        )
        for row in rows
    ]


def recent_backtests(session: Session, *, ticker: str, limit: int = 20) -> list[dict[str, Any]]:
    statement = (
        select(BacktestTrade, BacktestRun)
        .join(BacktestRun, BacktestTrade.backtest_run_id == BacktestRun.id)
        .where(BacktestTrade.ticker == ticker)
        .order_by(desc(BacktestTrade.simulated_at), desc(BacktestTrade.id))
        .limit(limit)
    )
    return [
        {
            "simulated_at": trade.simulated_at.isoformat(),
            "model_name": run.model_name,
            "side": trade.side,
            "price": trade.price,
            "edge": trade.edge,
            "pnl": trade.pnl,
            "settlement_result": trade.settlement_result,
        }
        for trade, run in session.execute(statement).all()
    ]


def portfolio_snapshot_series(
    session: Session,
    *,
    field: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    snapshots = list(
        session.scalars(
            select(PortfolioSnapshot)
            .order_by(desc(PortfolioSnapshot.snapshot_time), desc(PortfolioSnapshot.id))
            .limit(limit)
        )
    )
    return [
        {
            "time": snapshot.snapshot_time.isoformat(),
            "value": getattr(snapshot, field),
        }
        for snapshot in reversed(snapshots)
    ]


def latest_portfolio_snapshot(session: Session) -> PortfolioSnapshot | None:
    return session.scalar(
        select(PortfolioSnapshot)
        .order_by(desc(PortfolioSnapshot.snapshot_time), desc(PortfolioSnapshot.id))
        .limit(1)
    )


def market_category(market: Market | None, *, ranking: MarketRanking | None = None) -> str:
    text = " ".join(
        str(part or "")
        for part in (
            getattr(market, "ticker", None),
            getattr(market, "title", None),
            getattr(market, "subtitle", None),
            getattr(market, "series_ticker", None),
            getattr(market, "event_ticker", None),
            getattr(ranking, "ticker", None),
            getattr(ranking, "title", None),
            getattr(ranking, "series_ticker", None),
            getattr(ranking, "event_ticker", None),
        )
    ).lower()
    if any(word in text for word in ("nfl", "nba", "mlb", "sports", "esports", "game")):
        return "Sports"
    if any(word in text for word in ("weather", "temperature", "rain", "snow", "hurricane")):
        return "Weather"
    if any(word in text for word in ("crypto", "bitcoin", "btc", "ethereum", "eth")):
        return "Crypto"
    if any(word in text for word in ("fed", "cpi", "inflation", "econom", "rates")):
        return "Economics"
    return "General"


def category_allocation(
    session: Session,
    *,
    positions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = positions if positions is not None else position_rows(session)
    totals: Counter[str] = Counter()
    for row in rows:
        totals[str(row["category"])] += float(_decimal(row["exposure"]))
    return [{"category": key, "exposure": value} for key, value in sorted(totals.items())]


def _position_history_row(
    session: Session,
    position: PaperPosition,
    *,
    recorded_at: Any,
) -> PositionHistory:
    snapshot = get_latest_snapshot_for_ticker(session, position.ticker)
    avg_cost = _avg_cost(position)
    market_price = _market_price(snapshot)
    realized = _decimal(position.realized_pnl)
    unrealized = calculate_unrealized_pnl(position, snapshot)
    exposure = _position_exposure(position)
    return PositionHistory(
        ticker=position.ticker,
        recorded_at=recorded_at,
        position_size=position.yes_contracts - position.no_contracts,
        avg_cost=decimal_to_str(avg_cost),
        market_price=decimal_to_str(market_price),
        realized_pnl=decimal_to_str(realized) or "0",
        unrealized_pnl=decimal_to_str(unrealized) or "0",
        total_pnl=decimal_to_str(realized + unrealized) or "0",
        exposure=decimal_to_str(exposure) or "0",
    )


def _position_history_dict(row: PositionHistory) -> dict[str, Any]:
    return {
        "recorded_at": row.recorded_at.isoformat(),
        "position_size": row.position_size,
        "avg_cost": row.avg_cost,
        "market_price": row.market_price,
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "total_pnl": row.total_pnl,
        "exposure": row.exposure,
    }


def _empty_position_row(ticker: str, market: Market | None) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "title": (market.title if market else None) or ticker,
        "category": market_category(market),
        "position_size": 0,
        "yes_contracts": 0,
        "no_contracts": 0,
        "avg_cost": None,
        "market_price": None,
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "total_pnl": "0",
        "exposure": "0",
        "updated_at": None,
    }


def _avg_cost(position: PaperPosition) -> Decimal | None:
    yes = to_decimal(position.avg_yes_price)
    no = to_decimal(position.avg_no_price)
    yes_contracts = Decimal(position.yes_contracts)
    no_contracts = Decimal(position.no_contracts)
    total_contracts = yes_contracts + no_contracts
    if total_contracts == 0:
        return None
    total = (yes or Decimal("0")) * yes_contracts + (no or Decimal("0")) * no_contracts
    return total / total_contracts


def _market_price(snapshot: MarketSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is not None and ask is not None:
        return midpoint(bid, ask)
    return bid or ask or to_decimal(snapshot.last_price_dollars)


def _latest_market_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        market.ticker: market
        for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    }


def _latest_market_snapshots(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    if not tickers:
        return {}
    ranked_snapshots = (
        select(
            MarketSnapshot.id.label("snapshot_id"),
            func.row_number()
            .over(
                partition_by=MarketSnapshot.ticker,
                order_by=(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)),
            )
            .label("snapshot_rank"),
        )
        .where(MarketSnapshot.ticker.in_(tickers))
        .subquery()
    )
    rows = session.scalars(
        select(MarketSnapshot)
        .join(ranked_snapshots, MarketSnapshot.id == ranked_snapshots.c.snapshot_id)
        .where(ranked_snapshots.c.snapshot_rank == 1)
    )
    return {snapshot.ticker: snapshot for snapshot in rows}


def _display_market_price(ranking: MarketRanking, snapshot: MarketSnapshot | None) -> str:
    ranking_price = _first_nonzero_decimal(ranking.best_price, ranking.midpoint)
    if ranking_price is not None:
        return decimal_to_str(ranking_price) or "n/a"
    snapshot_price = _market_price(snapshot)
    if snapshot_price is not None and snapshot_price != 0:
        return decimal_to_str(snapshot_price) or "n/a"
    return "n/a"


def _display_market_spread(ranking: MarketRanking, snapshot: MarketSnapshot | None) -> str:
    ranking_spread = _first_nonzero_decimal(ranking.spread)
    if ranking_spread is not None:
        return decimal_to_str(ranking_spread) or "n/a"
    snapshot_spread = _first_nonzero_decimal(snapshot.spread if snapshot else None)
    if snapshot_spread is not None:
        return decimal_to_str(snapshot_spread) or "n/a"
    if snapshot is None:
        return "n/a"
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    if bid is None or ask is None:
        return "n/a"
    spread = abs(ask - bid)
    if spread == 0:
        return "n/a"
    return decimal_to_str(spread) or "n/a"


def _display_market_liquidity(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
    market: Market | None,
) -> str:
    liquidity = _market_liquidity_value(ranking, snapshot, market)
    return decimal_to_str(liquidity) if liquidity is not None else "n/a"


def _market_liquidity_value(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
    market: Market | None,
) -> Decimal | None:
    return _first_nonzero_decimal(
        ranking.liquidity,
        market.liquidity_dollars if market else None,
        snapshot.open_interest_fp if snapshot else None,
        snapshot.volume_24h_fp if snapshot else None,
        snapshot.volume_fp if snapshot else None,
    )


def _display_market_freshness(
    ranking: MarketRanking,
    snapshot: MarketSnapshot | None,
) -> str:
    if snapshot is not None:
        return _aware_datetime(snapshot.captured_at).isoformat()
    return _aware_datetime(ranking.ranked_at).isoformat()


def _market_monitor_data_quality(
    *,
    current_price: str,
    spread: str,
    liquidity: str,
    observed_at: datetime,
    market: Market | None,
) -> dict[str, Any]:
    now = utc_now()
    if market is not None and (
        is_inactive_market_status(market.status)
        or (market.close_time is not None and _aware_datetime(market.close_time) <= now)
    ):
        return {"label": "Expired market", "sort": 3}
    if now - _aware_datetime(observed_at) > timedelta(minutes=MARKET_MONITOR_STALE_AFTER_MINUTES):
        return {"label": "Stale market data", "sort": 2}
    missing = sum(value == "n/a" for value in (current_price, spread, liquidity))
    if missing == 0:
        return {"label": "Usable market data", "sort": 0}
    if missing < 3:
        return {"label": "Partial market data", "sort": 1}
    return {"label": "Missing market data", "sort": 4}


def _grouped_missing_multileg_sports_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    best_score = max((_decimal(row["opportunity_score"]) for row in rows), default=Decimal("0"))
    models = sorted({str(row["best_model"]) for row in rows if row.get("best_model")})
    examples = "; ".join(str(row["market"]).split(":", 1)[-1].strip() for row in rows[:2])
    example_suffix = f" Examples: {examples}" if examples else ""
    group_label = (
        "multi-leg sports markets"
        if all(row.get("category") == "Sports" for row in rows)
        else "multi-leg markets"
    )
    return {
        "ticker": "GROUPED-MISSING-SPORTS-MULTILEG",
        "market": (
            f"{len(rows)} {group_label} missing price/liquidity data."
            f"{example_suffix}"
        ),
        "category": "Sports",
        "current_price": "n/a",
        "spread": "n/a",
        "liquidity": "n/a",
        "data_quality": "Missing market data",
        "snapshot_repair_status": "Grouped unresolved",
        "data_freshness": max((str(row["data_freshness"]) for row in rows), default="n/a"),
        "opportunity_score": decimal_to_str(best_score) or "0",
        "best_model": "mixed" if len(models) > 1 else (models[0] if models else "n/a"),
        "model_confidence": "n/a",
        "recommended_action": "Collect fresh snapshots before ranking",
    }


def _snapshot_repair_status(data_quality: str) -> str:
    if data_quality == "Usable market data":
        return "Snapshot OK"
    if data_quality == "Partial market data":
        return "Partial snapshot"
    if data_quality == "Stale market data":
        return "Refresh required"
    if data_quality == "Expired market":
        return "Not active"
    return "Needs repair"


def _market_monitor_title(
    session: Session,
    *,
    market: Market | None,
    ranking: MarketRanking,
    category: str,
    leg_labels: list[str] | None = None,
) -> str:
    raw_title = ranking.title or (market.title if market else None) or ranking.ticker
    if not _looks_like_multileg_sports_market(raw_title, ranking=ranking, category=category):
        return raw_title

    resolved_leg_labels = (
        _market_monitor_leg_labels(session, ranking.ticker) if leg_labels is None else leg_labels
    )
    if not resolved_leg_labels:
        resolved_leg_labels = _split_multileg_title(raw_title)
    if len(resolved_leg_labels) < 2:
        return raw_title

    preview = "; ".join(resolved_leg_labels[:3])
    remaining = len(resolved_leg_labels) - 3
    suffix = f" +{remaining} more" if remaining > 0 else ""
    kind = "sports market" if category == "Sports" or _has_sports_terms(raw_title) else "market"
    return f"Multi-leg {kind} ({len(resolved_leg_labels)} legs): {preview}{suffix}"


def _looks_like_multileg_sports_market(
    title: str,
    *,
    ranking: MarketRanking,
    category: str,
) -> bool:
    text = f"{title or ''} {ranking.ticker}".lower()
    has_leg_separators = "," in title and (text.count(",yes ") + text.count(",no ") >= 1)
    if not has_leg_separators:
        return False
    return (
        category == "Sports"
        or "sportsmultigame" in text
        or "crosscategory" in text
        or _has_sports_terms(title)
    )


def _has_sports_terms(title: str) -> bool:
    text = str(title or "").lower()
    return any(
        token in text
        for token in (
            "runs scored",
            "wins by",
            "strikeout",
            "bregman",
            "soto",
            "dodgers",
            "yankees",
            "chicago c",
            "chicago ws",
            "baltimore",
            "kansas city",
            "cleveland",
            "texas",
            "detroit",
        )
    )


def _market_monitor_leg_labels(session: Session, ticker: str) -> list[str]:
    legs = session.scalars(
        select(MarketLeg)
        .where(MarketLeg.ticker == ticker)
        .order_by(MarketLeg.leg_index)
        .limit(12)
    )
    labels = []
    for leg in legs:
        label = _clean_multileg_label(leg.raw_text or leg.entity_name or "")
        if label and label not in labels:
            labels.append(label)
    return labels


def _market_monitor_leg_labels_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, list[str]]:
    if not tickers:
        return {}
    grouped: dict[str, list[str]] = {}
    legs = session.scalars(
        select(MarketLeg)
        .where(MarketLeg.ticker.in_(tickers))
        .order_by(MarketLeg.ticker, MarketLeg.leg_index)
    )
    for leg in legs:
        labels = grouped.setdefault(leg.ticker, [])
        if len(labels) >= 12:
            continue
        label = _clean_multileg_label(leg.raw_text or leg.entity_name or "")
        if label and label not in labels:
            labels.append(label)
    return grouped


def _split_multileg_title(title: str) -> list[str]:
    return [
        cleaned
        for cleaned in (_clean_multileg_label(part) for part in str(title or "").split(","))
        if cleaned
    ]


def _clean_multileg_label(value: str) -> str:
    cleaned = " ".join(str(value or "").replace("\n", " ").split())
    lower = cleaned.lower()
    for prefix in ("yes ", "no "):
        if lower.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned[:80].rstrip()


def _first_nonzero_decimal(*values: Any) -> Decimal | None:
    for value in values:
        decimal = to_decimal(value)
        if decimal is not None and decimal != 0:
            return decimal
    return None


def _position_exposure(position: PaperPosition) -> Decimal:
    yes = (to_decimal(position.avg_yes_price) or Decimal("0")) * position.yes_contracts
    no = (to_decimal(position.avg_no_price) or Decimal("0")) * position.no_contracts
    return yes + no


def _portfolio_snapshot_row(row: PortfolioSnapshot | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "snapshot_time": row.snapshot_time.isoformat(),
        "total_positions": row.total_positions,
        "total_exposure": row.total_exposure,
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "total_pnl": row.total_pnl,
        "open_orders": row.open_orders,
    }


def _latest_leaderboard_by_model(session: Session) -> dict[str, ModelLeaderboard]:
    rows = session.scalars(
        select(ModelLeaderboard).order_by(
            ModelLeaderboard.model_name,
            desc(ModelLeaderboard.generated_at),
            desc(ModelLeaderboard.id),
        )
    )
    latest: dict[str, ModelLeaderboard] = {}
    for row in rows:
        latest.setdefault(row.model_name, row)
    return latest


def _rank_color(
    roi: Decimal,
    *,
    best_roi: Decimal | None,
    worst_roi: Decimal | None,
) -> str:
    if best_roi is not None and roi == best_roi:
        return "green"
    if worst_roi is not None and roi == worst_roi and best_roi != worst_roi:
        return "red"
    return "yellow"


def _recommended_action(ranking: MarketRanking) -> str:
    score = _decimal(ranking.opportunity_score)
    if score >= Decimal("80"):
        return "High conviction paper review"
    if score >= Decimal("60"):
        return "Watch closely"
    return "Monitor"


def _market_monitor_recommended_action(
    ranking: MarketRanking,
    *,
    data_quality: str,
) -> str:
    if data_quality == "Expired market":
        return "Exclude expired market"
    if data_quality == "Stale market data":
        return "Reconnect or refresh snapshot"
    if data_quality == "Missing market data":
        return "Collect market snapshot"
    return _recommended_action(ranking)


def _market_search_text(market: Market | None, ranking: MarketRanking) -> str:
    return " ".join(
        str(part or "")
        for part in (
            ranking.ticker,
            ranking.title,
            ranking.forecast_model,
            market.title if market else None,
            market.subtitle if market else None,
        )
    )


def _bucket_snapshots(rows: list[PortfolioSnapshot], *, days: int) -> list[dict[str, Any]]:
    cutoff = utc_now() - timedelta(days=days)
    filtered = [row for row in rows if _aware_datetime(row.snapshot_time) >= cutoff]
    if not filtered:
        filtered = rows[: min(len(rows), 10)]
    return [
        {"time": _aware_datetime(row.snapshot_time).isoformat(), "total_pnl": row.total_pnl}
        for row in reversed(filtered)
    ]


def _trend_rows(rows: list[Any], time_attr: str, value_attr: str) -> list[dict[str, Any]]:
    result = []
    for row in reversed(rows[:30]):
        value = getattr(row, value_attr, None)
        result.append(
            {
                "time": getattr(row, time_attr).isoformat(),
                "value": value if isinstance(value, int) else str(value or "0"),
            }
        )
    return result


def _watchlist_market_row(session: Session, item: WatchlistMarket) -> dict[str, Any]:
    market = session.get(Market, item.ticker)
    ranking = session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == item.ticker)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )
    return annotated_opportunity_row(
        session,
        {
            "id": item.id,
            "ticker": item.ticker,
            "title": (market.title if market else None) or item.ticker,
            "category": market_category(market, ranking=ranking),
            "added_at": item.added_at.isoformat(),
            "notes": item.notes,
            "opportunity_score": ranking.opportunity_score if ranking else "n/a",
        },
        ticker=item.ticker,
        ranking=ranking,
        market=market,
    )


def _opportunity_alerts(
    session: Session,
    alert: Alert,
    rankings: list[dict[str, Any]],
    threshold: Decimal,
) -> list[AlertEvent]:
    return [
        _insert_event(
            session,
            alert=alert,
            ticker=row["ticker"],
            severity="INFO",
            message=f"{row['ticker']} opportunity score is {row['opportunity_score']}.",
            raw=row,
        )
        for row in rankings[:10]
        if _decimal(row["opportunity_score"]) >= threshold
    ]


def _confidence_alerts(
    session: Session,
    alert: Alert,
    rankings: list[dict[str, Any]],
    threshold: Decimal,
) -> list[AlertEvent]:
    return [
        _insert_event(
            session,
            alert=alert,
            ticker=row["ticker"],
            severity="INFO",
            message=f"{row['ticker']} model confidence is {row['model_confidence']}.",
            raw=row,
        )
        for row in rankings[:10]
        if _decimal(row["model_confidence"]) >= threshold
    ]


def _spread_alerts(
    session: Session,
    alert: Alert,
    rankings: list[dict[str, Any]],
    threshold: Decimal,
) -> list[AlertEvent]:
    return [
        _insert_event(
            session,
            alert=alert,
            ticker=row["ticker"],
            severity="WARNING",
            message=f"{row['ticker']} spread widened to {row['spread']}.",
            raw=row,
        )
        for row in rankings[:10]
        if _decimal(row["spread"]) >= threshold
    ]


def _expiry_alerts(session: Session, alert: Alert, threshold: Decimal) -> list[AlertEvent]:
    now = utc_now()
    markets = session.scalars(select(Market).where(Market.close_time.is_not(None)).limit(100))
    rows = []
    for market in markets:
        close_time = _aware_datetime(market.close_time)
        minutes = Decimal(str((close_time - now).total_seconds() / 60))
        if Decimal("0") <= minutes <= threshold:
            rows.append(
                _insert_event(
                    session,
                    alert=alert,
                    ticker=market.ticker,
                    severity="WARNING",
                    message=f"{market.ticker} closes in {minutes.quantize(Decimal('1'))} minutes.",
                    raw={"minutes_to_close": str(minutes), "ticker": market.ticker},
                )
            )
    return rows


def _exposure_alerts(
    session: Session,
    alert: Alert,
    positions: list[dict[str, Any]],
    threshold: Decimal,
) -> list[AlertEvent]:
    return [
        _insert_event(
            session,
            alert=alert,
            ticker=row["ticker"],
            severity="WARNING",
            message=f"{row['ticker']} paper exposure is {row['exposure']}.",
            raw=row,
        )
        for row in positions
        if _decimal(row["exposure"]) >= threshold
    ]


def _insert_event(
    session: Session,
    *,
    alert: Alert,
    ticker: str | None,
    severity: str,
    message: str,
    raw: Mapping[str, Any],
) -> AlertEvent:
    event = AlertEvent(
        alert_id=alert.id,
        created_at=utc_now(),
        alert_type=alert.alert_type,
        ticker=ticker,
        severity=severity,
        message=message,
        raw_json=encode_json(dict(raw)),
        acknowledged_at=None,
    )
    session.add(event)
    return event


def _alert_row(alert: Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "name": alert.name,
        "alert_type": alert.alert_type,
        "threshold": alert.threshold,
        "enabled": bool(alert.enabled),
        "created_at": alert.created_at.isoformat(),
        "raw": decode_json(alert.raw_json),
    }


def _alert_event_row(event: AlertEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "alert_id": event.alert_id,
        "created_at": event.created_at.isoformat(),
        "alert_type": event.alert_type,
        "ticker": event.ticker,
        "severity": event.severity,
        "message": event.message,
        "raw": decode_json(event.raw_json),
        "acknowledged_at": event.acknowledged_at.isoformat() if event.acknowledged_at else None,
    }


def _decimal(value: Any) -> Decimal:
    return to_decimal(value) or Decimal("0")


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
