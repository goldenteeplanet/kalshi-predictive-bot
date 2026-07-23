from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import current_market_predicate, is_active_market_status
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    CryptoMarketLink,
    EconomicMarketLink,
    Feature,
    Forecast,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    NewsMarketLink,
    PaperFill,
    PaperOrder,
    PaperPnl,
    Settlement,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.roadmap.artifacts import write_signed_artifact
from kalshi_predictor.roadmap.category_census import build_category_ingestion_census
from kalshi_predictor.roadmap.category_contract import CATEGORY_NAMES
from kalshi_predictor.tournament.ranking import classify_market_category
from kalshi_predictor.utils.time import utc_now

FINAL_RESULTS = frozenset({"yes", "no"})
FILLED_STATUSES = frozenset({"filled", "executed"})
OPEN_ORDER_STATUSES = frozenset({"open", "pending", "resting"})
REJECTED_ORDER_STATUSES = frozenset(
    {"blocked", "cancelled", "canceled", "expired", "rejected", "skipped"}
)
REJECTION_ACTIONS = {
    "NO_MARKET": "Inspect active-market discovery and category coverage.",
    "NO_SNAPSHOT": "Restore fresh executable orderbook snapshots.",
    "NO_FORECAST": "Inspect source links, features, and forecast generation.",
    "NO_RANKING": "Inspect ranking inputs after forecast generation.",
    "INSUFFICIENT_EDGE": "Wait for genuine edge; do not lower thresholds.",
    "LIQUIDITY": "Wait for sufficient executable depth and spread quality.",
    "RISK": "Inspect the recorded risk decision; do not bypass the gate.",
    "DUPLICATE": "Keep the existing idempotency block in place.",
    "CATEGORY_QUOTA": "Wait for quota capacity or another category opportunity.",
}


def build_runtime_category_census(
    session: Session,
    *,
    generated_at: datetime | None = None,
    freshness_minutes: int = 30,
    market_limit: int = 500,
    ticker_scope: Iterable[str] | None = None,
) -> dict[str, Any]:
    now = generated_at or utc_now()
    cutoff = now - timedelta(minutes=max(1, freshness_minutes))
    bounded_limit = max(1, market_limit)
    scope_provided = ticker_scope is not None
    requested_tickers = list(
        dict.fromkeys(str(ticker).strip() for ticker in (ticker_scope or []) if str(ticker).strip())
    )[:bounded_limit]
    market_statement = select(Market)
    if scope_provided:
        market_statement = market_statement.where(Market.ticker.in_(requested_tickers))
    else:
        market_statement = market_statement.where(current_market_predicate(now=now))
    markets = list(
        session.scalars(market_statement.order_by(Market.last_seen_at.desc()).limit(bounded_limit))
    )
    active = [market for market in markets if is_active_market_status(market.status)]
    tickers = {market.ticker for market in active}
    legs = (
        list(session.scalars(select(MarketLeg).where(MarketLeg.ticker.in_(tickers))))
        if tickers
        else []
    )
    categories = _categories_by_ticker(active, legs)
    link_sets = _verified_link_sets(session, tickers)
    snapshots = _ticker_set(
        session, MarketSnapshot.ticker, MarketSnapshot.captured_at, cutoff, tickers
    )
    features = _ticker_set(session, Feature.ticker, Feature.generated_at, cutoff, tickers)
    forecasts = _ticker_set(session, Forecast.ticker, Forecast.forecasted_at, cutoff, tickers)
    rankings = _ticker_set(session, MarketRanking.ticker, MarketRanking.ranked_at, cutoff, tickers)
    opportunities = _ticker_set(
        session, MarketOpportunity.ticker, MarketOpportunity.detected_at, cutoff, tickers
    )
    risk = _ticker_set(
        session,
        AdvancedRiskDecisionLog.ticker,
        AdvancedRiskDecisionLog.decision_timestamp,
        cutoff,
        tickers,
    )
    complete_traces = _complete_paper_trace_tickers(
        session,
        tickers,
        order_limit=max(100, bounded_limit * 10),
    )
    rows_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    latest_seen: dict[str, datetime] = {}
    for market in active:
        category = categories[market.ticker]
        latest_seen[category] = max(
            latest_seen.get(category, market.last_seen_at), market.last_seen_at
        )
        rows_by_category[category].append(
            {
                "ticker": market.ticker,
                "active": True,
                "verified_link": market.ticker in link_sets[category],
                "fresh_snapshot": market.ticker in snapshots,
                "fresh_features": market.ticker in features,
                "forecast": market.ticker in forecasts,
                "ranking": market.ticker in rankings,
                "opportunity": market.ticker in opportunities,
                "risk_evidence": market.ticker in risk,
                "paper_trace": market.ticker in complete_traces,
                "source_identity": "runtime_database",
                "source_available_at": _iso(market.last_seen_at),
            }
        )

    payloads: dict[str, dict[str, Any]] = {}
    for category in CATEGORY_NAMES:
        rows = rows_by_category.get(category, [])
        available = latest_seen.get(category, now)
        payloads[category] = {
            "source": {
                "name": "runtime-database",
                "kind": "runtime_database",
                "state": "READY" if rows else "NO_DATA",
                "published_at": _iso(available),
                "available_at": _iso(available),
                "ingested_at": _iso(now),
            },
            "live_v1_allowed": False,
            "deterministic_blockers": ["DIRECT_SOURCE_LINEAGE_NOT_CERTIFIED"],
            "markets": rows,
            "counts": _aggregate_counts(rows),
        }
    result = build_category_ingestion_census(payloads, generated_at=now)
    result["runtime_evidence"] = {
        "database_read_only": True,
        "market_limit": bounded_limit,
        "paper_trace_order_limit": max(100, bounded_limit * 10),
        "ticker_scope_count": len(requested_tickers),
        "ticker_scope": requested_tickers,
        "ticker_scope_provided": scope_provided,
        "freshness_minutes": max(1, freshness_minutes),
        "active_markets_scanned": len(active),
        "truncated": len(markets) >= bounded_limit,
    }
    return result


def build_paper_settlement_throughput(
    session: Session,
    *,
    generated_at: datetime | None = None,
    overall_target: int = 100,
    live_category_target: int = 30,
    order_limit: int = 1_000,
) -> dict[str, Any]:
    now = generated_at or utc_now()
    bounded_order_limit = max(1, order_limit)
    latest_orders = list(
        session.scalars(
            select(PaperOrder)
            .order_by(PaperOrder.created_at.desc(), PaperOrder.id.desc())
            .limit(bounded_order_limit + 1)
        )
    )
    orders_truncated = len(latest_orders) > bounded_order_limit
    orders = sorted(
        latest_orders[:bounded_order_limit],
        key=lambda order: (order.created_at, order.id),
    )
    order_ids = {order.id for order in orders}
    order_tickers = {order.ticker for order in orders}
    fills_by_order: dict[int, list[PaperFill]] = defaultdict(list)
    fill_statement = select(PaperFill).where(PaperFill.paper_order_id.in_(order_ids))
    for fill in session.scalars(fill_statement) if order_ids else []:
        fills_by_order[fill.paper_order_id].append(fill)
    settlement_statement = select(Settlement).where(Settlement.ticker.in_(order_tickers))
    settlements = {
        row.ticker: row for row in (session.scalars(settlement_statement) if order_tickers else [])
    }
    pnl_statement = select(PaperPnl).where(PaperPnl.ticker.in_(order_tickers))
    pnl_tickers = {
        row.ticker
        for row in (session.scalars(pnl_statement) if order_tickers else [])
        if _final_result(row.settlement_result)
    }
    market_map = (
        {
            row.ticker: row
            for row in session.scalars(
                select(Market).where(Market.ticker.in_({o.ticker for o in orders}))
            )
        }
        if orders
        else {}
    )
    legs = (
        list(
            session.scalars(
                select(MarketLeg).where(MarketLeg.ticker.in_({o.ticker for o in orders}))
            )
        )
        if orders
        else []
    )
    categories = _categories_by_ticker(list(market_map.values()), legs)
    category_counts = {
        category: {
            "orders": 0,
            "open": 0,
            "filled": 0,
            "rejected": 0,
            "other_unfilled": 0,
            "settled": 0,
            "awaiting_settlement": 0,
        }
        for category in CATEGORY_NAMES
    }
    lineage_gaps: list[dict[str, Any]] = []
    rejection_reasons: Counter[str] = Counter()
    order_statuses: Counter[str] = Counter()
    pending_settlements: list[dict[str, Any]] = []
    settled_total = 0
    awaiting_total = 0
    for order in orders:
        category = categories.get(order.ticker) or _category_from_order(order)
        counts = category_counts[category]
        counts["orders"] += 1
        normalized_status = order.status.strip().lower() or "unknown"
        order_statuses[normalized_status] += 1
        filled = normalized_status in FILLED_STATUSES
        if filled:
            counts["filled"] += 1
        elif normalized_status in OPEN_ORDER_STATUSES:
            counts["open"] += 1
        elif normalized_status in REJECTED_ORDER_STATUSES:
            counts["rejected"] += 1
            rejection_reasons[_reason_code(order.reason, order.status)] += 1
        else:
            counts["other_unfilled"] += 1
        settlement = settlements.get(order.ticker)
        settled = (
            filled
            and settlement is not None
            and (
                _final_result(settlement.result)
                or settlement.yes_settlement_value not in {None, ""}
            )
        )
        if settled:
            counts["settled"] += 1
            settled_total += 1
        elif filled:
            counts["awaiting_settlement"] += 1
            awaiting_total += 1
            market = market_map.get(order.ticker)
            close_time = market.close_time if market is not None else None
            pending_settlements.append(
                {
                    "paper_order_id": order.id,
                    "ticker": order.ticker,
                    "category": category,
                    "created_at": _iso(order.created_at),
                    "age_hours": _age_hours(now, order.created_at),
                    "market_close_time": _iso(close_time) if close_time else None,
                    "past_market_close": bool(close_time and _as_utc(now) > _as_utc(close_time)),
                    "fill_rows": len(fills_by_order.get(order.id, [])),
                }
            )
        gaps: list[str] = []
        if order.forecast_id is None:
            gaps.append("FORECAST_ID_MISSING")
        if filled and not fills_by_order.get(order.id):
            gaps.append("FILL_LINEAGE_MISSING")
        if filled and settlement is None:
            gaps.append("SETTLEMENT_MISSING")
        if settled and order.ticker not in pnl_tickers:
            gaps.append("SETTLED_PNL_MISSING")
        if gaps:
            lineage_gaps.append(
                {
                    "paper_order_id": order.id,
                    "ticker": order.ticker,
                    "category": category,
                    "gaps": gaps,
                }
            )
    live_progress = {
        category: {
            "settled": category_counts[category]["settled"],
            "target": max(1, live_category_target),
            "remaining": max(0, live_category_target - category_counts[category]["settled"]),
            "passed": category_counts[category]["settled"] >= live_category_target,
        }
        for category in ("crypto", "weather")
    }
    zero_trade_reasons = dict(sorted(rejection_reasons.items()))
    if not orders:
        zero_trade_reasons = {"NO_PAPER_ORDERS": 1}
    elif not zero_trade_reasons and not settled_total:
        open_total = sum(order_statuses[status] for status in OPEN_ORDER_STATUSES)
        zero_trade_reasons = {"OPEN_PAPER_ORDERS": open_total} if open_total else {
            "NO_SETTLED_PAPER_TRADES": 1
        }
    rejected_total = sum(rejection_reasons.values())
    rejection_breakdown = [
        {
            "reason": reason,
            "count": count,
            "denominator": rejected_total,
            "rate": round(count / rejected_total, 4) if rejected_total else 0.0,
            "recommended_action": _rejection_action(reason),
        }
        for reason, count in sorted(
            rejection_reasons.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    overdue_count = sum(row["past_market_close"] for row in pending_settlements)
    next_actions = _paper_next_actions(
        orders=len(orders),
        open_orders=sum(order_statuses[status] for status in OPEN_ORDER_STATUSES),
        filled=sum(order_statuses[status] for status in FILLED_STATUSES),
        settled=settled_total,
        overdue=overdue_count,
        lineage_gaps=len(lineage_gaps),
        rejection_breakdown=rejection_breakdown,
    )
    return {
        "schema_version": "paper-settlement-throughput-v2",
        "generated_at": _iso(now),
        "mode": "READ_ONLY_PAPER_EVIDENCE",
        "safety": {
            "creates_orders": False,
            "updates_settlements": False,
            "updates_pnl": False,
            "enables_live_trading": False,
            "lowers_thresholds": False,
        },
        "summary": {
            "orders": len(orders),
            "open": sum(order_statuses[status] for status in OPEN_ORDER_STATUSES),
            "filled": sum(order_statuses[status] for status in FILLED_STATUSES),
            "rejected": rejected_total,
            "settled": settled_total,
            "awaiting_settlement": awaiting_total,
            "past_market_close": overdue_count,
            "lineage_gap_orders": len(lineage_gaps),
            "overall_target": max(1, overall_target),
            "overall_remaining": max(0, overall_target - settled_total),
            "overall_passed": settled_total >= overall_target,
        },
        "runtime_evidence": {
            "database_read_only": True,
            "order_limit": bounded_order_limit,
            "orders_truncated": orders_truncated,
        },
        "categories": category_counts,
        "live_category_progress": live_progress,
        "order_status_counts": dict(sorted(order_statuses.items())),
        "pending_settlements": pending_settlements,
        "lineage_gaps": lineage_gaps,
        "zero_trade_reasons": zero_trade_reasons,
        "rejection_breakdown": rejection_breakdown,
        "next_actions": next_actions,
    }


def write_runtime_roadmap_reports(
    session: Session,
    *,
    reports_root: Path,
    generated_at: datetime | None = None,
    freshness_minutes: int = 30,
    market_limit: int = 500,
    paper_order_limit: int = 1_000,
    ticker_scope: Iterable[str] | None = None,
) -> dict[str, Path]:
    now = generated_at or utc_now()
    census = build_runtime_category_census(
        session,
        generated_at=now,
        freshness_minutes=freshness_minutes,
        market_limit=market_limit,
        ticker_scope=ticker_scope,
    )
    throughput = build_paper_settlement_throughput(
        session,
        generated_at=now,
        order_limit=paper_order_limit,
    )
    return {
        "category_census": write_signed_artifact(
            reports_root / "roadmap/category_ingestion_census.json", census
        ),
        "paper_throughput": write_signed_artifact(
            reports_root / "roadmap/paper_settlement_throughput.json", throughput
        ),
    }


def _categories_by_ticker(markets: list[Market], legs: list[MarketLeg]) -> dict[str, str]:
    leg_categories: dict[str, set[str]] = defaultdict(set)
    for leg in legs:
        leg_categories[leg.ticker].add(str(leg.category).lower())
    result: dict[str, str] = {}
    for market in markets:
        categories = leg_categories.get(market.ticker, set())
        if "cross_category" in categories or len(categories - {"unknown", "general"}) > 1:
            result[market.ticker] = "composite"
        elif categories:
            category = (
                sorted(categories - {"unknown"})[0] if categories - {"unknown"} else "general"
            )
            result[market.ticker] = category if category in CATEGORY_NAMES else "general"
        else:
            text = " ".join(
                str(value or "") for value in (market.ticker, market.title, market.series_ticker)
            )
            category = classify_market_category(text)
            result[market.ticker] = category if category in CATEGORY_NAMES else "general"
    return result


def _verified_link_sets(session: Session, tickers: set[str]) -> dict[str, set[str]]:
    result = {category: set() for category in CATEGORY_NAMES}
    if not tickers:
        return result
    for category, model in (
        ("crypto", CryptoMarketLink),
        ("weather", WeatherMarketLink),
        ("economic", EconomicMarketLink),
        ("news", NewsMarketLink),
    ):
        result[category] = set(
            session.scalars(select(model.ticker).where(model.ticker.in_(tickers)))
        )
    sports = session.scalars(select(SportsMarketLink).where(SportsMarketLink.ticker.in_(tickers)))
    result["sports"] = {
        row.ticker for row in sports if _json_source(row.raw_json) == "verified_schedule"
    }
    return result


def _ticker_set(
    session: Session, ticker_column: Any, time_column: Any, cutoff: datetime, tickers: set[str]
) -> set[str]:
    if not tickers:
        return set()
    return set(
        session.scalars(
            select(ticker_column).where(ticker_column.in_(tickers), time_column >= cutoff)
        )
    )


def _complete_paper_trace_tickers(
    session: Session,
    tickers: set[str],
    *,
    order_limit: int,
) -> set[str]:
    if not tickers:
        return set()
    orders = list(
        session.scalars(
            select(PaperOrder)
            .where(PaperOrder.ticker.in_(tickers))
            .order_by(PaperOrder.created_at.desc(), PaperOrder.id.desc())
            .limit(max(1, order_limit))
        )
    )
    settled = {
        row.ticker
        for row in session.scalars(select(Settlement).where(Settlement.ticker.in_(tickers)))
        if _final_result(row.result)
    }
    pnl = {
        row.ticker
        for row in session.scalars(select(PaperPnl).where(PaperPnl.ticker.in_(tickers)))
        if _final_result(row.settlement_result)
    }
    return {
        row.ticker
        for row in orders
        if row.status.lower() in FILLED_STATUSES and row.ticker in settled and row.ticker in pnl
    }


def _aggregate_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    mapping = {
        "active_markets": "active",
        "verified_links": "verified_link",
        "fresh_snapshots": "fresh_snapshot",
        "fresh_features": "fresh_features",
        "forecasts": "forecast",
        "rankings": "ranking",
        "opportunity_rows": "opportunity",
        "risk_evidence_rows": "risk_evidence",
        "complete_paper_traces": "paper_trace",
    }
    return {name: sum(bool(row[field]) for row in rows) for name, field in mapping.items()}


def _category_from_order(order: PaperOrder) -> str:
    category = classify_market_category(f"{order.ticker} {order.model_name}")
    return category if category in CATEGORY_NAMES else "general"


def _reason_code(reason: str, status: str) -> str:
    raw = str(reason or status or "unknown").strip().upper()
    return "_".join(raw.split())[:120] or "UNKNOWN"


def _rejection_action(reason: str) -> str:
    for prefix, action in REJECTION_ACTIONS.items():
        if reason.startswith(prefix):
            return action
    return "Inspect the recorded order reason and upstream decision trace."


def _paper_next_actions(
    *,
    orders: int,
    open_orders: int,
    filled: int,
    settled: int,
    overdue: int,
    lineage_gaps: int,
    rejection_breakdown: list[dict[str, Any]],
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if overdue:
        actions.append(
            {
                "code": "SYNC_OVERDUE_SETTLEMENTS",
                "priority": "high",
                "action": (
                    "Run the paper-only settlement synchronizer and inspect unresolved markets."
                ),
            }
        )
    if lineage_gaps:
        actions.append(
            {
                "code": "REPAIR_LINEAGE",
                "priority": "high",
                "action": "Repair missing forecast, fill, settlement, or P&L lineage.",
            }
        )
    if orders == 0:
        actions.append(
            {
                "code": "DIAGNOSE_NO_ORDERS",
                "priority": "medium",
                "action": "Inspect the candidate funnel before considering paper activation.",
            }
        )
    elif filled == 0 and open_orders:
        actions.append(
            {
                "code": "INSPECT_OPEN_ORDERS",
                "priority": "medium",
                "action": "Inspect executable-price simulation for resting paper orders.",
            }
        )
    elif filled == 0 and rejection_breakdown:
        actions.append(
            {
                "code": "ADDRESS_PRIMARY_REJECTION",
                "priority": "medium",
                "action": rejection_breakdown[0]["recommended_action"],
            }
        )
    elif settled == 0:
        actions.append(
            {
                "code": "AWAIT_GENUINE_SETTLEMENTS",
                "priority": "medium",
                "action": "Continue settlement synchronization without fabricating outcomes.",
            }
        )
    return actions


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age_hours(now: datetime, created_at: datetime) -> float:
    return round(max(0.0, (_as_utc(now) - _as_utc(created_at)).total_seconds() / 3600), 2)


def _json_source(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "unknown"
    return (
        str(payload.get("source") or "unknown").lower() if isinstance(payload, dict) else "unknown"
    )


def _final_result(value: Any) -> bool:
    return str(value or "").strip().lower() in FINAL_RESULTS


def _iso(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()
