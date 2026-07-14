from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    CryptoFeature,
    CryptoMarketLink,
    CryptoPrice,
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    PositionSizingDecisionLog,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    SportsSignal,
    WeatherFeature,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.phase3ap import MIN_EXECUTABLE_LIQUIDITY_SCORE, QUOTE_STALE_AFTER_MINUTES
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_csv,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BB_R8_VERSION = "phase3bb_r8_unified_paper_gate_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r8")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_LIMIT_PER_CATEGORY = 500
ACTIVE_MARKET_STATUSES = {"active", "open"}
APPROVED_RISK_ACTIONS = {"APPROVE", "APPROVED", "PROCEED", "ALLOW"}

FUNNEL_STEPS = (
    "active current market",
    "verified Kalshi link",
    "source/snapshot fresh",
    "feature exists",
    "forecast exists",
    "ranking exists",
    "positive EV",
    "executable EV",
    "book/liquidity/spread",
    "settlement terms",
    "risk/position sizing",
    "paper-ready",
)

ROW_FIELDS = [
    "category",
    "model_name",
    "ticker",
    "market_title",
    "market_status",
    "active_current_market",
    "verified_kalshi_link",
    "source_evidence_fresh",
    "snapshot_fresh",
    "feature_exists",
    "forecast_exists",
    "ranking_exists",
    "positive_ev",
    "executable_ev",
    "executable_book",
    "liquidity_pass",
    "spread_pass",
    "settlement_terms_known",
    "position_size_pass",
    "risk_approved",
    "paper_ready",
    "first_blocker",
    "link_detail",
    "source_detail",
    "snapshot_at",
    "forecast_at",
    "ranking_at",
    "estimated_edge",
    "opportunity_score",
    "liquidity_score",
    "spread_score",
    "next_action",
]

BLOCKER_FIELDS = [
    "category",
    "model_name",
    "current_rows",
    "paper_ready_rows",
    "blocked_rows",
    "positive_ev_rows",
    "first_blocker",
    "blocker",
    "blocker_count",
    "next_action",
]


@dataclass(frozen=True)
class Phase3BBR8UnifiedPaperGateArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    rows_csv_path: Path
    category_blockers_csv_path: Path
    manifest_path: Path


def write_phase3bb_r8_unified_paper_gate_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit_per_category: int = DEFAULT_LIMIT_PER_CATEGORY,
) -> Phase3BBR8UnifiedPaperGateArtifacts:
    payload = build_phase3bb_r8_unified_paper_gate(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        limit_per_category=limit_per_category,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "unified_paper_gate.md"
    rows_csv_path = output_dir / "paper_gate_rows.csv"
    category_blockers_csv_path = output_dir / "category_blockers.csv"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    _write_csv(rows_csv_path, payload["paper_gate_rows"], ROW_FIELDS)
    _write_csv(category_blockers_csv_path, payload["category_blockers"], BLOCKER_FIELDS)
    _write_manifest(
        manifest_path,
        [executive_summary_path, markdown_path, rows_csv_path, category_blockers_csv_path],
    )
    return Phase3BBR8UnifiedPaperGateArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        rows_csv_path=rows_csv_path,
        category_blockers_csv_path=category_blockers_csv_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r8_unified_paper_gate(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    limit_per_category: int = DEFAULT_LIMIT_PER_CATEGORY,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r8-unified-paper-gate",
        "argv": command_args or [],
    }
    rows = build_unified_paper_gate_rows(
        session,
        reports_dir=reports_dir,
        settings=resolved,
        now=now,
        limit_per_category=limit_per_category,
    )
    summary = _summary(rows)
    category_summaries = _category_summaries(rows)
    category_blockers = _category_blockers(category_summaries)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "lowers_thresholds": False,
        "fabricates_evidence": False,
        "uses_stale_3ap_only_truth": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R8",
        "phase_version": PHASE3BB_R8_VERSION,
        "mode": "PAPER_READ_ONLY_CATEGORY_AWARE_UNIFIED_PAPER_GATE",
        "reports_dir": str(reports_dir),
        "parameters": {
            "limit_per_category": limit_per_category,
            "quote_stale_after_minutes": str(QUOTE_STALE_AFTER_MINUTES),
            "min_executable_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
            "opportunity_min_score": str(resolved.opportunity_min_score),
        },
        "funnel": list(FUNNEL_STEPS),
        "summary": summary,
        "category_summaries": category_summaries,
        "category_blockers": category_blockers,
        "paper_gate_rows": rows,
        "acceptance": _acceptance(summary, rows),
        "stale_3ap_only_truth_used": False,
        "safety_flags": safety,
        "operator_guardrails": [
            "PAPER / READ-ONLY diagnostic only.",
            "No paper trades are created by this phase.",
            "No live/demo exchange orders.",
            "No threshold lowering.",
            "No fabricated source evidence.",
            "Do not treat positive EV alone as paper-ready.",
        ],
        "next_action": _next_action(summary, category_summaries),
    }


def build_unified_paper_gate_rows(
    session: Session,
    *,
    reports_dir: Path,
    settings: Settings,
    now: datetime,
    limit_per_category: int,
) -> list[dict[str, Any]]:
    contexts = _candidate_contexts(
        session,
        reports_dir=reports_dir,
        now=now,
        limit_per_category=limit_per_category,
    )
    tickers = sorted({ctx["ticker"] for ctx in contexts})
    markets = _markets_by_ticker(session, tickers)
    snapshots = _latest_by_ticker(session, MarketSnapshot, tickers, MarketSnapshot.captured_at)
    forecasts = _latest_forecasts_by_ticker(session, tickers)
    rankings = _latest_rankings_by_ticker(session, tickers)
    sizing = _latest_by_ticker(
        session,
        PositionSizingDecisionLog,
        tickers,
        PositionSizingDecisionLog.decision_timestamp,
    )
    risk = _latest_by_ticker(
        session,
        AdvancedRiskDecisionLog,
        tickers,
        AdvancedRiskDecisionLog.decision_timestamp,
    )
    rows = []
    for ctx in contexts:
        ticker = ctx["ticker"]
        rows.append(
            _paper_gate_row(
                session,
                ctx,
                market=markets.get(ticker),
                snapshot=snapshots.get(ticker),
                forecast=forecasts.get(ticker),
                ranking=rankings.get(ticker),
                sizing=sizing.get(ticker),
                risk=risk.get(ticker),
                settings=settings,
                now=now,
            )
        )
    return rows


def _candidate_contexts(
    session: Session,
    *,
    reports_dir: Path,
    now: datetime,
    limit_per_category: int,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    contexts.extend(
        _link_contexts(
            session,
            category="crypto",
            model_name="crypto_v2",
            link_cls=CryptoMarketLink,
            order_col=CryptoMarketLink.detected_at,
            now=now,
            limit=limit_per_category,
            detail_fn=lambda link: {
                "link_detail": f"symbol={link.symbol}; confidence={link.confidence}",
                "symbol": link.symbol,
            },
        )
    )
    contexts.extend(
        _link_contexts(
            session,
            category="weather",
            model_name="weather_v2",
            link_cls=WeatherMarketLink,
            order_col=WeatherMarketLink.detected_at,
            now=now,
            limit=limit_per_category,
            detail_fn=lambda link: {
                "link_detail": (
                    f"location={link.location_key}; target={_iso(link.target_time)}; "
                    f"confidence={link.confidence}"
                ),
                "location_key": link.location_key,
                "target_time": link.target_time,
            },
        )
    )
    contexts.extend(
        _link_contexts(
            session,
            category="economic",
            model_name="economic_v1",
            link_cls=EconomicMarketLink,
            order_col=EconomicMarketLink.detected_at,
            now=now,
            limit=limit_per_category,
            detail_fn=lambda link: {
                "link_detail": f"event_key={link.event_key}; confidence={link.confidence}",
                "event_key": link.event_key,
            },
        )
    )
    contexts.extend(
        _link_contexts(
            session,
            category="sports",
            model_name="sports_v1",
            link_cls=SportsMarketLink,
            order_col=SportsMarketLink.created_at,
            now=now,
            limit=limit_per_category,
            detail_fn=lambda link: {
                "link_detail": (
                    f"league={link.league}; game_key={link.game_key}; "
                    f"confidence={link.link_confidence}"
                ),
                "league": link.league,
                "game_key": link.game_key,
            },
        )
    )
    contexts.extend(_news_contexts(session, now=now, limit=limit_per_category))
    contexts.extend(
        _agriculture_general_contexts(
            session,
            reports_dir,
            now=now,
            limit=limit_per_category,
        )
    )
    return contexts


def _link_contexts(
    session: Session,
    *,
    category: str,
    model_name: str,
    link_cls: type[Any],
    order_col: Any,
    now: datetime,
    limit: int,
    detail_fn: Any,
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(link_cls)
        .join(Market, Market.ticker == link_cls.ticker)
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
        .order_by(desc(order_col), desc(link_cls.id))
        .limit(max(limit * 5, limit))
    )
    contexts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in rows:
        if link.ticker in seen:
            continue
        market = session.get(Market, link.ticker)
        if not _active_current_market(market, now=now):
            continue
        seen.add(link.ticker)
        contexts.append(
            {
                "category": category,
                "model_name": model_name,
                "ticker": link.ticker,
                "verified_kalshi_link": True,
                **detail_fn(link),
            }
        )
        if len(contexts) >= limit:
            break
    return contexts


def _news_contexts(session: Session, *, now: datetime, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(NewsMarketLink, NewsItem)
        .join(Market, Market.ticker == NewsMarketLink.ticker)
        .join(NewsItem, NewsItem.id == NewsMarketLink.news_item_id)
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
        .order_by(desc(NewsMarketLink.created_at), desc(NewsMarketLink.id))
        .limit(max(limit * 5, limit))
    )
    contexts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link, item in rows:
        if link.ticker in seen:
            continue
        market = session.get(Market, link.ticker)
        if not _active_current_market(market, now=now):
            continue
        seen.add(link.ticker)
        contexts.append(
            {
                "category": "news",
                "model_name": "news_v1",
                "ticker": link.ticker,
                "verified_kalshi_link": True,
                "link_detail": (
                    f"news_item_id={link.news_item_id}; "
                    f"confidence={link.link_confidence}"
                ),
                "news_item_id": link.news_item_id,
                "news_source_url": item.source_url,
            }
        )
        if len(contexts) >= limit:
            break
    return contexts


def _agriculture_general_contexts(
    session: Session,
    reports_dir: Path,
    *,
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    usda_rows = _read_csv(reports_dir / "phase3bb_r5" / "usda_rows.csv")
    for row in usda_rows[:limit]:
        ticker = row.get("market_ticker") or ""
        if not ticker:
            continue
        market = session.get(Market, ticker)
        if not _active_current_market(market, now=now):
            continue
        contexts.append(
            {
                "category": "agriculture_general",
                "model_name": "agriculture_general",
                "ticker": ticker,
                "verified_kalshi_link": False,
                "link_detail": "USDA/general evidence preview; no specialized link table row",
                "usda_source_promoted": _truthy(row.get("promoted_source_evidence")),
                "usda_candidate_feature": _truthy(row.get("candidate_feature_row")),
                "usda_first_blocker": row.get("first_blocker") or "",
                "source_detail": row.get("source_url") or row.get("source_name") or "",
            }
        )
    if contexts:
        return contexts
    leg_rows = session.execute(
        select(MarketLeg.ticker, MarketLeg.category)
        .join(Market, Market.ticker == MarketLeg.ticker)
        .where(MarketLeg.category.in_(["general", "agriculture", "agriculture_usda"]))
        .where(func.lower(Market.status).in_(ACTIVE_MARKET_STATUSES))
        .order_by(desc(MarketLeg.parsed_at), MarketLeg.ticker)
        .limit(max(limit * 5, limit))
    )
    seen: set[str] = set()
    for ticker, category in leg_rows:
        if ticker in seen:
            continue
        market = session.get(Market, ticker)
        if not _active_current_market(market, now=now):
            continue
        seen.add(ticker)
        contexts.append(
            {
                "category": "agriculture_general",
                "model_name": "agriculture_general",
                "ticker": ticker,
                "verified_kalshi_link": False,
                "link_detail": f"parsed {category} leg; no specialized link table row",
            }
        )
        if len(contexts) >= limit:
            break
    return contexts


def _paper_gate_row(
    session: Session,
    ctx: dict[str, Any],
    *,
    market: Market | None,
    snapshot: MarketSnapshot | None,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    sizing: PositionSizingDecisionLog | None,
    risk: AdvancedRiskDecisionLog | None,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    category = ctx["category"]
    source = _source_status(session, ctx)
    feature_exists = _feature_exists(session, ctx)
    snapshot_age = _age_minutes(snapshot.captured_at, now) if snapshot else None
    snapshot_fresh = bool(
        snapshot is not None
        and snapshot_age is not None
        and snapshot_age <= QUOTE_STALE_AFTER_MINUTES
    )
    active_current_market = _active_current_market(market, now=now)
    verified_kalshi_link = bool(ctx.get("verified_kalshi_link"))
    forecast_exists = forecast is not None
    ranking_exists = ranking is not None
    edge = to_decimal(getattr(ranking, "estimated_edge", None)) if ranking else None
    positive_ev = bool(edge is not None and edge > Decimal("0"))
    executable_ev = bool(
        positive_ev
        and ranking is not None
        and getattr(ranking, "best_side", None)
        and to_decimal(getattr(ranking, "best_price", None)) is not None
    )
    executable_book = bool(snapshot is not None and _snapshot_has_book(snapshot))
    liquidity_score = to_decimal(getattr(ranking, "liquidity_score", None)) if ranking else None
    spread_score = to_decimal(getattr(ranking, "spread_score", None)) if ranking else None
    liquidity_pass = bool(
        liquidity_score is not None and liquidity_score >= MIN_EXECUTABLE_LIQUIDITY_SCORE
    )
    spread_pass = bool(spread_score is not None and spread_score > Decimal("0"))
    settlement_terms_known = bool(
        market is not None and (market.rules_primary or market.rules_secondary)
    )
    position_size_pass = bool(sizing is not None and sizing.proposed_contracts > 0)
    risk_action = str(getattr(risk, "action", "") or "").upper()
    risk_approved = bool(risk_action in APPROVED_RISK_ACTIONS)
    first_blocker = _first_blocker(
        active_current_market=active_current_market,
        verified_kalshi_link=verified_kalshi_link,
        source_evidence_fresh=source["fresh"],
        snapshot_present=snapshot is not None,
        snapshot_fresh=snapshot_fresh,
        feature_exists=feature_exists,
        forecast_exists=forecast_exists,
        ranking_exists=ranking_exists,
        positive_ev=positive_ev,
        executable_ev=executable_ev,
        executable_book=executable_book,
        liquidity_pass=liquidity_pass,
        spread_pass=spread_pass,
        settlement_terms_known=settlement_terms_known,
        position_size_pass=position_size_pass,
        risk_approved=risk_approved,
        category_specific_blocker=ctx.get("usda_first_blocker"),
    )
    paper_ready = first_blocker == "PAPER_READY"
    return {
        "category": category,
        "model_name": ctx["model_name"],
        "ticker": ctx["ticker"],
        "market_title": getattr(market, "title", "") or "",
        "market_status": getattr(market, "status", "") or "",
        "active_current_market": active_current_market,
        "verified_kalshi_link": verified_kalshi_link,
        "source_evidence_fresh": source["fresh"],
        "snapshot_fresh": snapshot_fresh,
        "feature_exists": feature_exists,
        "forecast_exists": forecast_exists,
        "ranking_exists": ranking_exists,
        "positive_ev": positive_ev,
        "executable_ev": executable_ev,
        "executable_book": executable_book,
        "liquidity_pass": liquidity_pass,
        "spread_pass": spread_pass,
        "settlement_terms_known": settlement_terms_known,
        "position_size_pass": position_size_pass,
        "risk_approved": risk_approved,
        "paper_ready": paper_ready,
        "first_blocker": first_blocker,
        "link_detail": ctx.get("link_detail", ""),
        "source_detail": ctx.get("source_detail") or source["detail"],
        "snapshot_at": _iso(snapshot.captured_at if snapshot else None),
        "forecast_at": _iso(forecast.forecasted_at if forecast else None),
        "ranking_at": _iso(ranking.ranked_at if ranking else None),
        "estimated_edge": getattr(ranking, "estimated_edge", "") if ranking else "",
        "opportunity_score": getattr(ranking, "opportunity_score", "") if ranking else "",
        "liquidity_score": getattr(ranking, "liquidity_score", "") if ranking else "",
        "spread_score": getattr(ranking, "spread_score", "") if ranking else "",
        "next_action": _row_next_action(first_blocker, category),
    }


def _source_status(session: Session, ctx: dict[str, Any]) -> dict[str, Any]:
    category = ctx["category"]
    if category == "crypto":
        source = _latest_crypto_price(session, ctx.get("symbol"))
        return {
            "fresh": source is not None,
            "detail": f"crypto_price_at={_iso(source.observed_at if source else None)}",
        }
    if category == "weather":
        source = _latest_weather_forecast(session, ctx.get("location_key"))
        return {
            "fresh": source is not None,
            "detail": (
                "weather_forecast_generated_at="
                f"{_iso(source.forecast_generated_at if source else None)}"
            ),
        }
    if category == "economic":
        source = _latest_economic_event(session, ctx.get("event_key"))
        return {
            "fresh": source is not None,
            "detail": f"economic_event_time={_iso(source.event_time if source else None)}",
        }
    if category == "sports":
        source = _sports_game(session, ctx.get("league"), ctx.get("game_key"))
        return {
            "fresh": source is not None,
            "detail": f"sports_game_status={getattr(source, 'status', '') if source else ''}",
        }
    if category == "news":
        return {
            "fresh": bool(ctx.get("news_source_url")),
            "detail": str(ctx.get("news_source_url") or ""),
        }
    if category == "agriculture_general":
        return {
            "fresh": bool(ctx.get("usda_source_promoted")),
            "detail": str(ctx.get("source_detail") or ""),
        }
    return {"fresh": False, "detail": ""}


def _feature_exists(session: Session, ctx: dict[str, Any]) -> bool:
    category = ctx["category"]
    if category == "crypto":
        return _latest_crypto_feature(session, ctx.get("symbol")) is not None
    if category == "weather":
        return _latest_weather_feature(session, ctx.get("location_key")) is not None
    if category == "economic":
        return _latest_economic_feature(session, ctx.get("event_key")) is not None
    if category == "sports":
        return _latest_sports_feature_or_signal(
            session,
            ctx["ticker"],
            ctx.get("league"),
            ctx.get("game_key"),
        )
    if category == "news":
        return _latest_news_feature(session, ctx["ticker"]) is not None
    if category == "agriculture_general":
        return bool(ctx.get("usda_candidate_feature"))
    return False


def _first_blocker(
    *,
    active_current_market: bool,
    verified_kalshi_link: bool,
    source_evidence_fresh: bool,
    snapshot_present: bool,
    snapshot_fresh: bool,
    feature_exists: bool,
    forecast_exists: bool,
    ranking_exists: bool,
    positive_ev: bool,
    executable_ev: bool,
    executable_book: bool,
    liquidity_pass: bool,
    spread_pass: bool,
    settlement_terms_known: bool,
    position_size_pass: bool,
    risk_approved: bool,
    category_specific_blocker: str | None = None,
) -> str:
    if not active_current_market:
        return "ACTIVE_CURRENT_MARKET_MISSING"
    if not verified_kalshi_link:
        return "VERIFIED_LINK_MISSING"
    if not source_evidence_fresh:
        return category_specific_blocker or "SOURCE_MISSING"
    if not snapshot_present:
        return "SNAPSHOT_MISSING"
    if not snapshot_fresh:
        return "SNAPSHOT_STALE"
    if not feature_exists:
        return "FEATURE_MISSING"
    if not forecast_exists:
        return "FORECAST_MISSING"
    if not ranking_exists:
        return "RANKING_MISSING"
    if not positive_ev:
        return "EV_NOT_POSITIVE"
    if not executable_ev:
        return "EXECUTABLE_EV_NOT_POSITIVE"
    if not executable_book:
        return "EXECUTABLE_BOOK_MISSING"
    if not liquidity_pass:
        return "LIQUIDITY_TOO_LOW"
    if not spread_pass:
        return "SPREAD_TOO_WIDE"
    if not settlement_terms_known:
        return "SETTLEMENT_TERMS_UNKNOWN"
    if not position_size_pass:
        return "PHASE_3M_ZERO_SIZE"
    if not risk_approved:
        return "PHASE_3N_RISK_BLOCK"
    return "PAPER_READY"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    blockers = Counter(row["first_blocker"] for row in rows)
    paper_ready = sum(1 for row in rows if _truthy(row["paper_ready"]))
    return {
        "status": "PAPER_READY_OPEN" if paper_ready else "PAPER_READY_CLOSED",
        "categories": sorted({row["category"] for row in rows}),
        "total_rows": len(rows),
        "paper_ready_rows": paper_ready,
        "blocked_rows": len(rows) - paper_ready,
        "positive_ev_rows": sum(1 for row in rows if _truthy(row["positive_ev"])),
        "first_hard_blocker": _dominant_blocker(blockers),
        "blocker_counts": dict(blockers),
        "stale_3ap_only_truth_used": False,
        "paper_trades_created": 0,
        "live_or_demo_orders": 0,
        "db_writes_performed": 0,
    }


def _category_summaries(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    categories = [
        "crypto",
        "weather",
        "economic",
        "sports",
        "agriculture_general",
        "news",
    ]
    for category in categories:
        group = [row for row in rows if row["category"] == category]
        blockers = Counter(row["first_blocker"] for row in group)
        summaries[category] = {
            "category": category,
            "model_name": _category_model(category),
            "current_rows": len(group),
            "paper_ready_rows": sum(1 for row in group if _truthy(row["paper_ready"])),
            "blocked_rows": sum(1 for row in group if not _truthy(row["paper_ready"])),
            "positive_ev_rows": sum(1 for row in group if _truthy(row["positive_ev"])),
            "first_blocker": _dominant_blocker(blockers) if group else "NO_CURRENT_ROWS",
            "blocker_counts": dict(blockers),
            "next_action": _category_next_action(category, blockers, group),
        }
    return summaries


def _category_blockers(summaries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for summary in summaries.values():
        counts = summary.get("blocker_counts") or {"NO_CURRENT_ROWS": 0}
        for blocker, count in sorted(counts.items()):
            rows.append(
                {
                    "category": summary["category"],
                    "model_name": summary["model_name"],
                    "current_rows": summary["current_rows"],
                    "paper_ready_rows": summary["paper_ready_rows"],
                    "blocked_rows": summary["blocked_rows"],
                    "positive_ev_rows": summary["positive_ev_rows"],
                    "first_blocker": summary["first_blocker"],
                    "blocker": blocker,
                    "blocker_count": count,
                    "next_action": summary["next_action"],
                }
            )
    return rows


def _acceptance(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        "current_paper_ready_truth_category_aware": True,
        "no_stale_3ap_only_artifact_drives_status": not summary["stale_3ap_only_truth_used"],
        "every_row_has_exact_first_blocker": all(bool(row.get("first_blocker")) for row in rows),
        "no_paper_orders": summary["paper_trades_created"] == 0,
        "no_live_demo_orders": summary["live_or_demo_orders"] == 0,
        "db_writes_zero": summary["db_writes_performed"] == 0,
    }


def _next_action(
    summary: dict[str, Any],
    category_summaries: dict[str, dict[str, Any]],
) -> str:
    if summary["paper_ready_rows"] > 0:
        return "paper-only operator review; do not auto-create trades"
    ranked = sorted(
        category_summaries.values(),
        key=lambda row: (row["positive_ev_rows"], row["current_rows"]),
        reverse=True,
    )
    if not ranked:
        return (
            "kalshi-bot phase3bb-r3-free-source-inventory "
            "--output-dir reports/phase3bb_r3 --reports-dir reports"
        )
    return str(ranked[0]["next_action"])


def _category_next_action(
    category: str,
    blockers: Counter[str],
    group: list[dict[str, Any]],
) -> str:
    if not group:
        return "refresh active market inventory and rerun R8"
    blocker = _dominant_blocker(blockers)
    if blocker in {"SOURCE_MISSING", "VERIFIED_LINK_MISSING"}:
        if category == "economic":
            return (
                "kalshi-bot phase3bb-r4-economic-parser-backfill "
                "--output-dir reports/phase3bb_r4 --reports-dir reports"
            )
        if category == "news":
            return (
                "kalshi-bot phase3bb-r7-news-event-discovery "
                "--output-dir reports/phase3bb_r7 --reports-dir reports"
            )
        if category == "sports":
            return (
                "kalshi-bot phase3bb-r6-sports-provenance-repair "
                "--output-dir reports/phase3bb_r6 --reports-dir reports"
            )
        if category == "agriculture_general":
            return (
                "kalshi-bot phase3bb-r5-usda-source-activation "
                "--output-dir reports/phase3bb_r5 --reports-dir reports"
            )
    if blocker in {"SNAPSHOT_MISSING", "SNAPSHOT_STALE", "EXECUTABLE_BOOK_MISSING"}:
        return "capture current snapshots/orderbooks for verified linked rows, then rerun R8"
    if blocker in {"FEATURE_MISSING", "FORECAST_MISSING", "RANKING_MISSING"}:
        return f"run the {category} feature/forecast/ranking sprint, then rerun R8"
    if blocker in {"EV_NOT_POSITIVE", "EXECUTABLE_EV_NOT_POSITIVE"}:
        return f"keep {category} watching; paper gate stays closed until executable EV is positive"
    if blocker in {"LIQUIDITY_TOO_LOW", "SPREAD_TOO_WIDE"}:
        return f"watch {category} orderbooks for executable liquidity/spread"
    if blocker in {"PHASE_3M_ZERO_SIZE", "PHASE_3N_RISK_BLOCK"}:
        return "run paper-only operator/risk preflight; do not create trades automatically"
    return "rerun R8 after the next bounded refresh"


def _row_next_action(first_blocker: str, category: str) -> str:
    if first_blocker == "PAPER_READY":
        return "paper-only operator review"
    return _category_next_action(category, Counter({first_blocker: 1}), [{"category": category}])


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        market.ticker: market
        for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    }


def _latest_by_ticker(
    session: Session,
    model: type[Any],
    tickers: list[str],
    timestamp_col: Any,
) -> dict[str, Any]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(model)
        .where(model.ticker.in_(tickers))
        .order_by(model.ticker, desc(timestamp_col), desc(model.id))
    )
    latest: dict[str, Any] = {}
    for row in rows:
        if row.ticker not in latest:
            latest[row.ticker] = row
    return latest


def _latest_forecasts_by_ticker(session: Session, tickers: list[str]) -> dict[str, Forecast]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(Forecast)
        .where(Forecast.ticker.in_(tickers))
        .order_by(Forecast.ticker, desc(Forecast.forecasted_at), desc(Forecast.id))
    )
    latest: dict[str, Forecast] = {}
    for row in rows:
        if row.ticker not in latest:
            latest[row.ticker] = row
    return latest


def _latest_rankings_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketRanking]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(MarketRanking)
        .where(MarketRanking.ticker.in_(tickers))
        .order_by(MarketRanking.ticker, desc(MarketRanking.ranked_at), desc(MarketRanking.id))
    )
    latest: dict[str, MarketRanking] = {}
    for row in rows:
        if row.ticker not in latest:
            latest[row.ticker] = row
    return latest


def _latest_crypto_price(session: Session, symbol: str | None) -> CryptoPrice | None:
    if not symbol:
        return None
    return session.scalar(
        select(CryptoPrice)
        .where(CryptoPrice.symbol == symbol)
        .order_by(desc(CryptoPrice.observed_at), desc(CryptoPrice.id))
        .limit(1)
    )


def _latest_crypto_feature(session: Session, symbol: str | None) -> CryptoFeature | None:
    if not symbol:
        return None
    return session.scalar(
        select(CryptoFeature)
        .where(CryptoFeature.symbol == symbol)
        .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
        .limit(1)
    )


def _latest_weather_forecast(
    session: Session,
    location_key: str | None,
) -> WeatherForecast | None:
    if not location_key:
        return None
    return session.scalar(
        select(WeatherForecast)
        .where(WeatherForecast.location_key == location_key)
        .order_by(desc(WeatherForecast.forecast_generated_at), desc(WeatherForecast.id))
        .limit(1)
    )


def _latest_weather_feature(session: Session, location_key: str | None) -> WeatherFeature | None:
    if not location_key:
        return None
    return session.scalar(
        select(WeatherFeature)
        .where(WeatherFeature.location_key == location_key)
        .order_by(desc(WeatherFeature.generated_at), desc(WeatherFeature.id))
        .limit(1)
    )


def _latest_economic_event(session: Session, event_key: str | None) -> EconomicEvent | None:
    if not event_key:
        return None
    return session.scalar(
        select(EconomicEvent)
        .where(EconomicEvent.event_key == event_key)
        .order_by(desc(EconomicEvent.event_time), desc(EconomicEvent.id))
        .limit(1)
    )


def _latest_economic_feature(session: Session, event_key: str | None) -> EconomicFeature | None:
    if not event_key:
        return None
    return session.scalar(
        select(EconomicFeature)
        .where(EconomicFeature.event_key == event_key)
        .order_by(desc(EconomicFeature.generated_at), desc(EconomicFeature.id))
        .limit(1)
    )


def _sports_game(session: Session, league: str | None, game_key: str | None) -> SportsGame | None:
    if not league or not game_key:
        return None
    return session.scalar(
        select(SportsGame)
        .where(SportsGame.league == league, SportsGame.game_key == game_key)
        .limit(1)
    )


def _latest_sports_feature_or_signal(
    session: Session,
    ticker: str,
    league: str | None,
    game_key: str | None,
) -> bool:
    feature = session.scalar(
        select(SportsFeature)
        .where(SportsFeature.ticker == ticker)
        .order_by(desc(SportsFeature.created_at), desc(SportsFeature.id))
        .limit(1)
    )
    if feature is not None:
        return True
    if not league or not game_key:
        return False
    signal = session.scalar(
        select(SportsSignal)
        .where(
            SportsSignal.league == league,
            SportsSignal.game_key == game_key,
            SportsSignal.ticker == ticker,
        )
        .order_by(desc(SportsSignal.created_at), desc(SportsSignal.id))
        .limit(1)
    )
    return signal is not None


def _latest_news_feature(session: Session, ticker: str) -> NewsFeature | None:
    return session.scalar(
        select(NewsFeature)
        .where(NewsFeature.ticker == ticker)
        .order_by(desc(NewsFeature.created_at), desc(NewsFeature.id))
        .limit(1)
    )


def _active_current_market(market: Market | None, *, now: datetime) -> bool:
    if market is None:
        return False
    if str(market.status or "").lower() not in ACTIVE_MARKET_STATUSES:
        return False
    candidates = [
        parse_datetime(market.close_time),
        parse_datetime(market.expiration_time),
        parse_datetime(market.expected_expiration_time),
    ]
    future_times = [item for item in candidates if item is not None]
    return not future_times or any(item > now for item in future_times)


def _snapshot_has_book(snapshot: MarketSnapshot) -> bool:
    price_fields = (
        snapshot.best_yes_bid,
        snapshot.best_yes_ask,
        snapshot.best_no_bid,
        snapshot.best_no_ask,
        snapshot.yes_bid_dollars,
        snapshot.yes_ask_dollars,
        snapshot.no_bid_dollars,
        snapshot.no_ask_dollars,
    )
    return any(to_decimal(value) is not None for value in price_fields) or bool(
        snapshot.raw_orderbook_json
    )


def _dominant_blocker(blockers: Counter[str]) -> str:
    if not blockers:
        return "NO_CURRENT_ROWS"
    for blocker in (
        "ACTIVE_CURRENT_MARKET_MISSING",
        "VERIFIED_LINK_MISSING",
        "SOURCE_MISSING",
        "SNAPSHOT_MISSING",
        "SNAPSHOT_STALE",
        "FEATURE_MISSING",
        "FORECAST_MISSING",
        "RANKING_MISSING",
        "EV_NOT_POSITIVE",
        "EXECUTABLE_EV_NOT_POSITIVE",
        "EXECUTABLE_BOOK_MISSING",
        "LIQUIDITY_TOO_LOW",
        "SPREAD_TOO_WIDE",
        "SETTLEMENT_TERMS_UNKNOWN",
        "PHASE_3M_ZERO_SIZE",
        "PHASE_3N_RISK_BLOCK",
        "PAPER_READY",
    ):
        if blockers.get(blocker):
            return blocker
    return blockers.most_common(1)[0][0]


def _category_model(category: str) -> str:
    return {
        "crypto": "crypto_v2",
        "weather": "weather_v2",
        "economic": "economic_v1",
        "sports": "sports_v1",
        "agriculture_general": "agriculture_general",
        "news": "news_v1",
    }.get(category, category)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = _metadata_lines(payload, "# Phase 3BB-R8 Unified Paper Gate")
    lines.extend(
        [
            "",
            "## Status",
            "",
            f"- Status: `{summary['status']}`",
            f"- Total current rows: `{summary['total_rows']}`",
            f"- Paper-ready rows: `{summary['paper_ready_rows']}`",
            f"- Positive-EV rows: `{summary['positive_ev_rows']}`",
            f"- First hard blocker: `{summary['first_hard_blocker']}`",
            f"- Stale 3AP-only truth used: `{summary['stale_3ap_only_truth_used']}`",
            "",
            "## Category Status",
            "",
        ]
    )
    for row in payload["category_summaries"].values():
        lines.append(
            "- "
            f"{row['category']}: current=`{row['current_rows']}`, "
            f"paper_ready=`{row['paper_ready_rows']}`, "
            f"positive_ev=`{row['positive_ev_rows']}`, "
            f"first_blocker=`{row['first_blocker']}`"
        )
    lines.extend(
        [
            "",
            "No paper trades, live/demo orders, threshold changes, fake evidence, "
            "or DB writes were run.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R8 Unified Paper Gate")
    lines.extend(
        [
            "",
            "## Funnel",
            "",
            *[f"- {step}" for step in payload["funnel"]],
            "",
            "## Category Blockers",
            "",
            "| Category | Rows | Paper Ready | Positive EV | First Blocker | Next Action |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for row in payload["category_summaries"].values():
        lines.append(
            "| "
            f"{row['category']} | {row['current_rows']} | {row['paper_ready_rows']} | "
            f"{row['positive_ev_rows']} | {row['first_blocker']} | {row['next_action']} |"
        )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- This report is paper/read-only.",
            "- 3AP stale-only truth is not used as the gate.",
            "- A row is paper-ready only if every funnel step passes.",
            "- Positive EV without executable book and risk approval stays blocked.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _age_minutes(value: Any, now: datetime) -> Decimal | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return Decimal(str((now - parsed).total_seconds() / 60))


def _iso(value: Any) -> str:
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed is not None else ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
