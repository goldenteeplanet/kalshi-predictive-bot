from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import (
    is_link_deprecated,
    latest_links_for_table,
    market_status_bucket,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.semantics import (
    EXACT_LINK,
    CryptoMarketTerms,
    parse_crypto_market_terms,
    terms_from_link_payload,
)
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
)
from kalshi_predictor.kalshi.orderbook import UsableBidAskBook, usable_bid_ask_book
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.market_legs import parse_market_legs
from kalshi_predictor.opportunities.payout_scoring import (
    is_acceptable_best_payout,
    payout_metrics_from_ranking,
)
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.ui.market_display import summarize_market_title
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE3BC_VERSION = "phase3bc_r2_pure_crypto_parser_hygiene"
MODEL_NAME = "crypto_v2"
MIN_EXECUTABLE_LIQUIDITY_SCORE = Decimal("30")
MIN_EXECUTABLE_CONFIDENCE_SCORE = Decimal("40")


@dataclass(frozen=True)
class Phase3BCArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_phase3bc_crypto_clean_opportunity_router(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Report clean crypto opportunity readiness without creating trades."""
    base_settings = settings or get_settings()
    paper_settings = learning_paper_settings(base_settings)
    links = [
        link
        for link in latest_links_for_table(session, CryptoMarketLink, limit=limit)
        if isinstance(link, CryptoMarketLink)
    ]
    tickers = [link.ticker for link in links]
    markets = _markets_by_ticker(session, tickers)
    legs_by_ticker = _legs_by_ticker(session, tickers)
    snapshots = _latest_snapshots(session, tickers)
    forecasts = _latest_forecasts(session, tickers)
    rankings = _latest_rankings(session, tickers)
    rows = [
        _build_row(
            link=link,
            market=markets.get(link.ticker),
            legs=legs_by_ticker.get(link.ticker, []),
            snapshot=snapshots.get(link.ticker),
            forecast=forecasts.get(link.ticker),
            ranking=rankings.get(link.ticker),
            settings=paper_settings,
            strict_settings=base_settings,
        )
        for link in links
    ]
    rows.sort(key=_row_sort_key, reverse=True)
    status_counts = Counter(row["readiness_status"] for row in rows)
    structure_counts = Counter(row["structure_status"] for row in rows)
    action_counts = Counter(row["final_action"] for row in rows)
    summary = {
        "crypto_links_checked": len(rows),
        "active_crypto_links": sum(1 for row in rows if row["active_market"]),
        "pure_crypto_markets": sum(1 for row in rows if row["structure_status"] == "PURE_CRYPTO"),
        "active_pure_crypto_markets": sum(
            1
            for row in rows
            if row["active_market"] and row["structure_status"] == "PURE_CRYPTO"
        ),
        "mixed_or_cross_category_markets": sum(
            1 for row in rows if row["structure_status"] == "MIXED_CATEGORY"
        ),
        "active_mixed_or_cross_category_markets": sum(
            1
            for row in rows
            if row["active_market"] and row["structure_status"] == "MIXED_CATEGORY"
        ),
        "unsupported_crypto_markets": sum(
            1 for row in rows if row["structure_status"] == "UNSUPPORTED_CRYPTO_TERMS"
        ),
        "paper_ready_candidates": sum(
            1 for row in rows if row["readiness_status"] == "PAPER_READY_CANDIDATE"
        ),
        "strict_turn_on_candidates": sum(
            1 for row in rows if row["strict_turn_on_status"] == "STRICT_READY_CANDIDATE"
        ),
        "watch_only_candidates": sum(1 for row in rows if row["final_action"] == "WATCH_ONLY"),
        "blocked_candidates": sum(1 for row in rows if row["final_action"] == "BLOCKED"),
        "main_blocker": status_counts.most_common(1)[0][0] if status_counts else None,
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3BC",
        "phase_version": PHASE3BC_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_CLEAN_OPPORTUNITY_ROUTER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "model_name": MODEL_NAME,
        "thresholds": {
            "paper_min_edge": str(paper_settings.opportunity_min_edge),
            "paper_min_score": str(paper_settings.opportunity_min_score),
            "paper_max_spread": str(paper_settings.opportunity_max_spread),
            "paper_min_time_to_close_minutes": str(
                paper_settings.opportunity_min_time_to_close_minutes
            ),
            "executable_min_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
            "executable_min_confidence_score": str(MIN_EXECUTABLE_CONFIDENCE_SCORE),
            "strict_min_edge": str(base_settings.opportunity_min_edge),
            "strict_min_score": str(base_settings.opportunity_min_score),
            "strict_max_spread": str(base_settings.opportunity_max_spread),
        },
        "summary": summary,
        "readiness_status_counts": dict(sorted(status_counts.items())),
        "structure_status_counts": dict(sorted(structure_counts.items())),
        "final_action_counts": dict(sorted(action_counts.items())),
        "paper_ready_rows": [
            row for row in rows if row["readiness_status"] == "PAPER_READY_CANDIDATE"
        ][:50],
        "watch_rows": [row for row in rows if row["final_action"] == "WATCH_ONLY"][:50],
        "blocked_examples": [row for row in rows if row["final_action"] == "BLOCKED"][:50],
        "rows": rows,
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": [
            "kalshi-bot ingest-crypto --symbols BTC,ETH,SOL,XRP,DOGE",
            "kalshi-bot build-crypto-features --symbols BTC,ETH,SOL,XRP,DOGE",
            "kalshi-bot link-crypto-markets",
            (
                "kalshi-bot crypto-forecast-doctor --output-dir reports/phase3ar "
                "--limit 500 --repair-snapshots"
            ),
            "kalshi-bot forecast --model crypto_v2 --limit 1000",
            "kalshi-bot find-opportunities --model-name crypto_v2 --limit 150",
            "kalshi-bot phase3bc-crypto-clean-opportunity-router --output-dir reports/phase3bc",
        ],
    }


def write_phase3bc_crypto_clean_opportunity_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc"),
    settings: Settings | None = None,
    limit: int = 500,
) -> Phase3BCArtifactSet:
    payload = build_phase3bc_crypto_clean_opportunity_router(
        session,
        settings=settings,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_crypto_clean_opportunity_router.json"
    markdown_path = output_dir / "phase3bc_crypto_clean_opportunity_router.md"
    rows_path = output_dir / "phase3bc_crypto_clean_opportunity_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BCArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _build_row(
    *,
    link: CryptoMarketLink,
    market: Market | None,
    legs: list[MarketLeg],
    snapshot: MarketSnapshot | None,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    settings: Settings,
    strict_settings: Settings,
) -> dict[str, Any]:
    parsed_legs = legs
    if market is not None and not parsed_legs:
        parsed_legs = [_parsed_leg_to_payload(leg) for leg in parse_market_legs(market)]
    leg_payloads = [_leg_payload(leg) for leg in parsed_legs]
    terms = _terms_for_row(market=market, link=link, legs=parsed_legs)
    market_status = _row_market_status(market=market, snapshot=snapshot)
    active_market = market_status_bucket(market_status) == "active" and not is_link_deprecated(
        link
    )
    structure_status = _structure_status(terms, leg_payloads)
    metrics = payout_metrics_from_ranking(ranking) if ranking is not None else None
    executable_book = _executable_book(snapshot=snapshot, ranking=ranking, settings=settings)
    readiness = _readiness_status(
        structure_status=structure_status,
        active_market=active_market,
        snapshot=snapshot,
        forecast=forecast,
        ranking=ranking,
        metrics=metrics,
        executable_book=executable_book,
        settings=settings,
    )
    strict_status = _strict_turn_on_status(
        readiness_status=readiness,
        ranking=ranking,
        strict_settings=strict_settings,
    )
    blockers = _blockers_for_status(readiness, ranking=ranking, settings=settings)
    non_crypto_legs = [leg for leg in leg_payloads if leg["category"] != "crypto"]
    return {
        "ticker": link.ticker,
        "title": market.title if market is not None else None,
        "clean_title": summarize_market_title(market.title or link.ticker)
        if market is not None
        else link.ticker,
        "series_ticker": market.series_ticker if market is not None else None,
        "event_ticker": market.event_ticker if market is not None else None,
        "market_status": market_status,
        "active_market": active_market,
        "link_id": link.id,
        "link_symbol": link.symbol,
        "link_confidence": link.confidence,
        "link_reason": link.reason,
        "structure_status": structure_status,
        "semantic_status": terms.status if terms is not None else None,
        "component_symbols": list(terms.component_symbols) if terms is not None else [],
        "component_terms": [component.as_payload() for component in terms.components]
        if terms is not None
        else [],
        "leg_count": len(leg_payloads),
        "non_crypto_leg_count": len(non_crypto_legs),
        "non_crypto_leg_categories": sorted({leg["category"] for leg in non_crypto_legs}),
        "component_legs": leg_payloads,
        "latest_snapshot_at": snapshot.captured_at.isoformat() if snapshot else None,
        "latest_forecast_id": forecast.id if forecast is not None else None,
        "latest_forecast_at": forecast.forecasted_at.isoformat() if forecast else None,
        "latest_ranking_at": ranking.ranked_at.isoformat() if ranking else None,
        "model_probability": forecast.yes_probability if forecast is not None else None,
        "best_side": ranking.best_side if ranking is not None else None,
        "best_price": ranking.best_price if ranking is not None else None,
        "estimated_edge": ranking.estimated_edge if ranking is not None else None,
        "expected_value": decimal_to_str(metrics.expected_value) if metrics else None,
        "payout_to_risk_ratio": decimal_to_str(metrics.payout_to_risk_ratio)
        if metrics
        else None,
        "opportunity_score": ranking.opportunity_score if ranking is not None else None,
        "liquidity": ranking.liquidity if ranking is not None else None,
        "liquidity_score": ranking.liquidity_score if ranking is not None else None,
        "spread": ranking.spread if ranking is not None else None,
        "spread_score": ranking.spread_score if ranking is not None else None,
        "book_state": executable_book.state if executable_book is not None else None,
        "book_usable": executable_book.usable if executable_book is not None else False,
        "book_reason": executable_book.reason if executable_book is not None else None,
        "bid_depth": decimal_to_str(executable_book.bid_depth)
        if executable_book is not None
        else None,
        "ask_depth": decimal_to_str(executable_book.ask_depth)
        if executable_book is not None
        else None,
        "book_bid_price": decimal_to_str(executable_book.bid_price)
        if executable_book is not None
        else None,
        "book_ask_price": decimal_to_str(executable_book.ask_price)
        if executable_book is not None
        else None,
        "book_spread": decimal_to_str(executable_book.spread)
        if executable_book is not None
        else None,
        "confidence_score": ranking.model_confidence_score if ranking is not None else None,
        "time_to_close_minutes": ranking.time_to_close_minutes if ranking is not None else None,
        "readiness_status": readiness,
        "strict_turn_on_status": strict_status,
        "final_action": _final_action(readiness),
        "blockers": blockers,
        "what_would_make_tradable": _tradable_actions(readiness, blockers),
        "kalshi_lookup": {
            "copy_ticker": link.ticker,
            "copy_event_ticker": market.event_ticker if market is not None else None,
            "copy_series_ticker": market.series_ticker if market is not None else None,
        },
    }


def _terms_for_row(
    *,
    market: Market | None,
    link: CryptoMarketLink,
    legs: list[MarketLeg],
) -> CryptoMarketTerms | None:
    if market is not None:
        return parse_crypto_market_terms(market, legs=legs)
    return terms_from_link_payload(link.symbol, link.raw_json)


def _structure_status(terms: CryptoMarketTerms | None, legs: list[dict[str, Any]]) -> str:
    if terms is None or terms.status != EXACT_LINK or not terms.component_symbols:
        return "UNSUPPORTED_CRYPTO_TERMS"
    if any(leg["category"] != "crypto" for leg in legs):
        return "MIXED_CATEGORY"
    return "PURE_CRYPTO"


def _readiness_status(
    *,
    structure_status: str,
    active_market: bool,
    snapshot: MarketSnapshot | None,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    metrics: Any,
    executable_book: UsableBidAskBook | None,
    settings: Settings,
) -> str:
    if structure_status == "MIXED_CATEGORY":
        return "BLOCKED_MIXED_CATEGORY"
    if structure_status != "PURE_CRYPTO":
        return "BLOCKED_UNSUPPORTED_CRYPTO_TERMS"
    if not active_market:
        return "BLOCKED_INACTIVE_OR_DEPRECATED_MARKET"
    if snapshot is None:
        return "BLOCKED_MISSING_ACTIVE_SNAPSHOT"
    if forecast is None:
        return "BLOCKED_MISSING_CRYPTO_FORECAST"
    if ranking is None:
        return "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST"
    if ranking.best_side not in {BUY_YES, BUY_NO} or not ranking.best_price:
        return "BLOCKED_MISSING_EXECUTABLE_PRICE"
    if metrics is None or metrics.expected_value is None or metrics.expected_value <= 0:
        return "WATCH_NO_POSITIVE_EXPECTED_VALUE"
    if _decimal(ranking.estimated_edge) < settings.opportunity_min_edge:
        return "WATCH_LOW_EDGE"
    if _decimal(ranking.opportunity_score) < settings.opportunity_min_score:
        return "WATCH_LOW_SCORE"
    if executable_book is None:
        return "BLOCKED_MISSING_EXECUTABLE_PRICE"
    if executable_book.state == "NO_EXECUTABLE_BOOK":
        if (
            executable_book.has_visible_bid_ask
            and executable_book.has_executable_depth
            and _decimal(ranking.liquidity_score) < MIN_EXECUTABLE_LIQUIDITY_SCORE
        ):
            return "BLOCKED_NO_LIQUIDITY"
        return "BLOCKED_NO_EXECUTABLE_BOOK"
    if executable_book.state == "THIN_BOOK":
        return "BLOCKED_NO_LIQUIDITY"
    if executable_book.state == "WIDE_SPREAD":
        return "BLOCKED_WIDE_SPREAD"
    if _decimal(ranking.model_confidence_score) < MIN_EXECUTABLE_CONFIDENCE_SCORE:
        return "WATCH_LOW_CONFIDENCE"
    time_to_close = to_decimal(ranking.time_to_close_minutes)
    if (
        time_to_close is not None
        and time_to_close < settings.opportunity_min_time_to_close_minutes
    ):
        return "WATCH_TOO_CLOSE_TO_SETTLEMENT"
    if not is_acceptable_best_payout(ranking, metrics):
        return "WATCH_PAYOUT_FILTER_NOT_MET"
    return "PAPER_READY_CANDIDATE"


def _strict_turn_on_status(
    *,
    readiness_status: str,
    ranking: MarketRanking | None,
    strict_settings: Settings,
) -> str:
    if readiness_status != "PAPER_READY_CANDIDATE" or ranking is None:
        return "NOT_READY"
    if _decimal(ranking.estimated_edge) < strict_settings.opportunity_min_edge:
        return "STRICT_BLOCK_LOW_EDGE"
    if _decimal(ranking.opportunity_score) < strict_settings.opportunity_min_score:
        return "STRICT_BLOCK_LOW_SCORE"
    spread = to_decimal(ranking.spread)
    if spread is not None and spread > strict_settings.opportunity_max_spread:
        return "STRICT_BLOCK_WIDE_SPREAD"
    return "STRICT_READY_CANDIDATE"


def _blockers_for_status(
    status: str,
    *,
    ranking: MarketRanking | None,
    settings: Settings,
) -> list[str]:
    blockers = {
        "BLOCKED_MIXED_CATEGORY": "Market includes non-crypto component legs.",
        "BLOCKED_UNSUPPORTED_CRYPTO_TERMS": "Crypto settlement terms are not cleanly parsed.",
        "BLOCKED_INACTIVE_OR_DEPRECATED_MARKET": "Market is not active for new paper decisions.",
        "BLOCKED_MISSING_ACTIVE_SNAPSHOT": (
            "No fresh active market snapshot/orderbook is available."
        ),
        "BLOCKED_MISSING_CRYPTO_FORECAST": "No crypto_v2 forecast exists for the exact ticker.",
        "BLOCKED_FORECAST_NOT_RANKED": (
            "Forecast exists but has not been ranked by opportunity scan."
        ),
        "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST": (
            "Current crypto_v2 forecast exists but no current-window ranking was generated."
        ),
        "BLOCKED_MISSING_EXECUTABLE_PRICE": "No executable YES/NO ask price is available.",
        "BLOCKED_NO_EXECUTABLE_BOOK": (
            "No usable bid/ask book with executable best-price depth is available."
        ),
        "BLOCKED_WIDE_SPREAD": "Spread exceeds the configured threshold.",
        "BLOCKED_NO_LIQUIDITY": "Liquidity score is too low for an executable paper candidate.",
    }
    if status in blockers:
        return [blockers[status]]
    if ranking is None:
        return []
    if status == "WATCH_LOW_EDGE":
        return [
            f"Edge {ranking.estimated_edge or 'n/a'} is below {settings.opportunity_min_edge}."
        ]
    if status == "WATCH_LOW_SCORE":
        return [
            "Opportunity score "
            f"{ranking.opportunity_score or 'n/a'} is below {settings.opportunity_min_score}."
        ]
    if status == "WATCH_LOW_CONFIDENCE":
        return [
            "Model confidence score "
            f"{ranking.model_confidence_score or 'n/a'} is below "
            f"{MIN_EXECUTABLE_CONFIDENCE_SCORE}."
        ]
    if status == "WATCH_NO_POSITIVE_EXPECTED_VALUE":
        return ["Expected value is not positive at current ask prices."]
    if status == "WATCH_TOO_CLOSE_TO_SETTLEMENT":
        return [
            "Time to close is below "
            f"{settings.opportunity_min_time_to_close_minutes} minutes."
        ]
    if status == "WATCH_PAYOUT_FILTER_NOT_MET":
        return ["Payout-adjusted filter is not met yet."]
    return []


def _tradable_actions(status: str, blockers: list[str]) -> list[str]:
    if status == "PAPER_READY_CANDIDATE":
        return [
            (
                "Keep paper/read-only mode, run risk gates, and require explicit human "
                "approval before any future execution toggle."
            ),
        ]
    if status == "BLOCKED_MIXED_CATEGORY":
        return [
            (
                "Use only pure crypto target-price rows; keep cross-category bundles "
                "out of crypto trading."
            )
        ]
    if status == "BLOCKED_NO_LIQUIDITY":
        return ["Wait for usable orderbook depth/liquidity before considering paper execution."]
    if status == "BLOCKED_NO_EXECUTABLE_BOOK":
        return ["Wait for visible bid/ask depth at the executable best prices."]
    if status == "BLOCKED_WIDE_SPREAD":
        return ["Wait for spread to tighten below the configured threshold."]
    if status in {"WATCH_LOW_EDGE", "WATCH_LOW_SCORE", "WATCH_NO_POSITIVE_EXPECTED_VALUE"}:
        return ["Wait for price/model movement to create positive tradable EV."]
    if blockers:
        return blockers
    return ["Refresh crypto prices, active market snapshots, forecasts, and rankings."]


def _final_action(status: str) -> str:
    if status == "PAPER_READY_CANDIDATE":
        return "PAPER_READY_CANDIDATE"
    if status.startswith("WATCH_"):
        return "WATCH_ONLY"
    return "BLOCKED"


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    if not tickers:
        return {}
    return {
        row.ticker: row
        for row in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
    }


def _legs_by_ticker(session: Session, tickers: list[str]) -> dict[str, list[MarketLeg]]:
    if not tickers:
        return {}
    grouped: dict[str, list[MarketLeg]] = {}
    statement = (
        select(MarketLeg)
        .where(MarketLeg.ticker.in_(tickers))
        .order_by(MarketLeg.ticker, MarketLeg.leg_index)
    )
    for leg in session.scalars(statement):
        grouped.setdefault(leg.ticker, []).append(leg)
    return grouped


def _latest_snapshots(session: Session, tickers: list[str]) -> dict[str, MarketSnapshot]:
    return _latest_rows_by_ticker(
        session,
        MarketSnapshot,
        tickers,
        time_column=MarketSnapshot.captured_at,
    )


def _latest_forecasts(session: Session, tickers: list[str]) -> dict[str, Forecast]:
    return _latest_rows_by_ticker(
        session,
        Forecast,
        tickers,
        time_column=Forecast.forecasted_at,
        filters=(Forecast.model_name == MODEL_NAME,),
    )


def _latest_rankings(session: Session, tickers: list[str]) -> dict[str, MarketRanking]:
    return _latest_rows_by_ticker(
        session,
        MarketRanking,
        tickers,
        time_column=MarketRanking.ranked_at,
        filters=(MarketRanking.forecast_model == MODEL_NAME,),
    )


def _latest_rows_by_ticker(
    session: Session,
    model: type[MarketSnapshot] | type[Forecast] | type[MarketRanking],
    tickers: list[str],
    *,
    time_column: Any,
    filters: tuple[Any, ...] = (),
) -> dict[str, Any]:
    if not tickers:
        return {}
    row_number = (
        func.row_number()
        .over(partition_by=model.ticker, order_by=[desc(time_column), desc(model.id)])
        .label("row_number")
    )
    subquery = (
        select(model.id.label("id"), model.ticker.label("ticker"), row_number)
        .where(model.ticker.in_(tickers), *filters)
        .subquery()
    )
    rows = session.scalars(
        select(model)
        .join(subquery, model.id == subquery.c.id)
        .where(subquery.c.row_number == 1)
    )
    return {row.ticker: row for row in rows}


def _row_market_status(*, market: Market | None, snapshot: MarketSnapshot | None) -> str | None:
    if market is not None and market.status:
        return market.status
    if snapshot is not None:
        return snapshot.status
    return None


def _executable_book(
    *,
    snapshot: MarketSnapshot | None,
    ranking: MarketRanking | None,
    settings: Settings,
) -> UsableBidAskBook | None:
    if snapshot is None or ranking is None or ranking.best_side not in {BUY_YES, BUY_NO}:
        return None
    return usable_bid_ask_book(
        _decode_orderbook(snapshot.raw_orderbook_json),
        side=ranking.best_side,
        liquidity_score=ranking.liquidity_score,
        min_liquidity_score=MIN_EXECUTABLE_LIQUIDITY_SCORE,
        max_spread=settings.opportunity_max_spread,
    )


def _decode_orderbook(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _leg_payload(leg: Any) -> dict[str, Any]:
    return {
        "leg_index": int(getattr(leg, "leg_index", 0)),
        "side": str(getattr(leg, "side", "")),
        "category": str(getattr(leg, "category", "unknown")).lower(),
        "market_type": str(getattr(leg, "market_type", "UNKNOWN")),
        "entity_name": getattr(leg, "entity_name", None),
        "operator": str(getattr(leg, "operator", "UNKNOWN")),
        "threshold_value": getattr(leg, "threshold_value", None),
        "unit": getattr(leg, "unit", None),
        "confidence": str(getattr(leg, "confidence", "0")),
        "raw_text": str(getattr(leg, "raw_text", "")),
        "reason": str(getattr(leg, "reason", "")),
    }


def _parsed_leg_to_payload(leg: Any) -> MarketLeg:
    return leg  # ParsedMarketLeg and MarketLeg expose the same fields used here.


def _decimal(value: Any) -> Decimal:
    return to_decimal(value) or Decimal("0")


def _row_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    return (
        _decimal(row.get("expected_value")),
        _decimal(row.get("opportunity_score")),
        _decimal(row.get("estimated_edge")),
    )


def _recommended_next_action(summary: dict[str, Any]) -> str:
    if summary["paper_ready_candidates"] > 0:
        return (
            "Crypto has paper-ready candidates. Keep execution blocked, run risk gates, "
            "and inspect rows before any future enablement discussion."
        )
    if summary["watch_only_candidates"] > 0:
        return "Crypto rows are clean but not tradable yet; keep refreshing prices/orderbooks."
    if summary.get("active_pure_crypto_markets", 0) > 0:
        return (
            "Active pure crypto rows exist but are blocked by data/ranking gates; rerun "
            "snapshots, crypto_v2 forecasts, and opportunity scans."
        )
    if summary.get("pure_crypto_markets", 0) > 0:
        return (
            "Pure crypto parsing is repaired, but current pure rows are inactive. Source or "
            "refresh active pure crypto markets before paper decisions."
        )
    if summary["mixed_or_cross_category_markets"] > 0:
        return (
            "Filter mixed crypto/sports bundles out of payout views and wait for pure "
            "crypto rows."
        )
    return "Refresh crypto links, snapshots, forecasts, and rankings."


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3BC Crypto Clean Opportunity Router",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: `{payload['mode']}`",
        f"- Safety: `{payload['paper_only_safety']}`",
        "- Live/demo execution: blocked",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Thresholds",
            "",
        ]
    )
    for key, value in payload["thresholds"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Paper-Ready Candidates",
            "",
            "| Ticker | Market | Side | Price | EV | Edge | Score | Liquidity | Spread | Strict |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    _append_rows(lines, payload["paper_ready_rows"], empty="No paper-ready pure crypto rows.")
    lines.extend(
        [
            "",
            "## Watch Rows",
            "",
            "| Ticker | Market | Status | Side | EV | Edge | Score | Blockers |",
            "|---|---|---|---|---:|---:|---:|---|",
        ]
    )
    _append_watch_rows(lines, payload["watch_rows"], empty="No watch-only pure crypto rows.")
    lines.extend(
        [
            "",
            "## Blocked Examples",
            "",
            "| Ticker | Market | Status | Structure | Blockers |",
            "|---|---|---|---|---|",
        ]
    )
    _append_blocked_rows(lines, payload["blocked_examples"], empty="No blocked examples.")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Next Commands",
            "",
            "```bash",
            *payload["next_commands"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _append_rows(lines: list[str], rows: list[dict[str, Any]], *, empty: str) -> None:
    if not rows:
        lines.append(f"| _{empty}_ |  |  |  |  |  |  |  |  |  |")
        return
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row['clean_title'])} | "
            f"{row['best_side'] or ''} | "
            f"{row['best_price'] or ''} | "
            f"{row['expected_value'] or ''} | "
            f"{row['estimated_edge'] or ''} | "
            f"{row['opportunity_score'] or ''} | "
            f"{row['liquidity_score'] or ''} | "
            f"{row['spread'] or ''} | "
            f"{row['strict_turn_on_status']} |"
        )


def _append_watch_rows(lines: list[str], rows: list[dict[str, Any]], *, empty: str) -> None:
    if not rows:
        lines.append(f"| _{empty}_ |  |  |  |  |  |  |  |")
        return
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row['clean_title'])} | "
            f"{row['readiness_status']} | "
            f"{row['best_side'] or ''} | "
            f"{row['expected_value'] or ''} | "
            f"{row['estimated_edge'] or ''} | "
            f"{row['opportunity_score'] or ''} | "
            f"{_cell('; '.join(row['blockers']))} |"
        )


def _append_blocked_rows(lines: list[str], rows: list[dict[str, Any]], *, empty: str) -> None:
    if not rows:
        lines.append(f"| _{empty}_ |  |  |  |  |")
        return
    for row in rows:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{_cell(row['clean_title'])} | "
            f"{row['readiness_status']} | "
            f"{row['structure_status']} | "
            f"{_cell('; '.join(row['blockers']))} |"
        )


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
