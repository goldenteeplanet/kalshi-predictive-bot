from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_active_market_status
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


def build_runtime_category_census(
    session: Session,
    *,
    generated_at: datetime | None = None,
    freshness_minutes: int = 30,
    market_limit: int = 5_000,
) -> dict[str, Any]:
    now = generated_at or utc_now()
    cutoff = now - timedelta(minutes=max(1, freshness_minutes))
    markets = list(
        session.scalars(
            select(Market).order_by(Market.last_seen_at.desc()).limit(max(1, market_limit))
        )
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
    complete_traces = _complete_paper_trace_tickers(session, tickers)
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
        "market_limit": max(1, market_limit),
        "freshness_minutes": max(1, freshness_minutes),
        "active_markets_scanned": len(active),
        "truncated": len(markets) >= max(1, market_limit),
    }
    return result


def build_paper_settlement_throughput(
    session: Session,
    *,
    generated_at: datetime | None = None,
    overall_target: int = 100,
    live_category_target: int = 30,
) -> dict[str, Any]:
    now = generated_at or utc_now()
    orders = list(
        session.scalars(select(PaperOrder).order_by(PaperOrder.created_at, PaperOrder.id))
    )
    fills_by_order: dict[int, list[PaperFill]] = defaultdict(list)
    for fill in session.scalars(select(PaperFill)):
        fills_by_order[fill.paper_order_id].append(fill)
    settlements = {row.ticker: row for row in session.scalars(select(Settlement))}
    pnl_tickers = {
        row.ticker
        for row in session.scalars(select(PaperPnl))
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
        category: {"orders": 0, "filled": 0, "settled": 0, "awaiting_settlement": 0}
        for category in CATEGORY_NAMES
    }
    lineage_gaps: list[dict[str, Any]] = []
    rejection_reasons: Counter[str] = Counter()
    settled_total = 0
    awaiting_total = 0
    for order in orders:
        category = categories.get(order.ticker) or _category_from_order(order)
        counts = category_counts[category]
        counts["orders"] += 1
        filled = order.status.strip().lower() in FILLED_STATUSES
        if filled:
            counts["filled"] += 1
        else:
            rejection_reasons[_reason_code(order.reason, order.status)] += 1
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
    return {
        "schema_version": "paper-settlement-throughput-v1",
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
            "settled": settled_total,
            "awaiting_settlement": awaiting_total,
            "lineage_gap_orders": len(lineage_gaps),
            "overall_target": max(1, overall_target),
            "overall_remaining": max(0, overall_target - settled_total),
            "overall_passed": settled_total >= overall_target,
        },
        "categories": category_counts,
        "live_category_progress": live_progress,
        "lineage_gaps": lineage_gaps,
        "zero_trade_reasons": zero_trade_reasons,
    }


def write_runtime_roadmap_reports(
    session: Session,
    *,
    reports_root: Path,
    generated_at: datetime | None = None,
    freshness_minutes: int = 30,
) -> dict[str, Path]:
    now = generated_at or utc_now()
    census = build_runtime_category_census(
        session,
        generated_at=now,
        freshness_minutes=freshness_minutes,
    )
    throughput = build_paper_settlement_throughput(session, generated_at=now)
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


def _complete_paper_trace_tickers(session: Session, tickers: set[str]) -> set[str]:
    if not tickers:
        return set()
    orders = list(session.scalars(select(PaperOrder).where(PaperOrder.ticker.in_(tickers))))
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
