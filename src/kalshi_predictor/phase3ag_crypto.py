from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.repository import get_latest_crypto_link_for_ticker
from kalshi_predictor.crypto.semantics import (
    AMBIGUOUS,
    DEFAULT_FEATURE_MAX_AGE_MINUTES,
    EXACT_LINK,
    NOT_CRYPTO,
    UNSUPPORTED,
    parse_crypto_market_terms,
    select_compatible_crypto_feature,
)
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    CryptoFeature,
    CryptoMarketLink,
    Forecast,
    ForecastSkipLog,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
    PositionSizingDecisionLog,
    Settlement,
)
from kalshi_predictor.market_legs import parse_market_legs
from kalshi_predictor.paper.models import ORDER_FILLED, ORDER_OPEN
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
PHASE3AG_CRYPTO_VERSION = "phase3ag_crypto_v1"


@dataclass(frozen=True)
class Phase3AGCryptoArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


def build_phase3ag_crypto_pipeline(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    session.flush()
    markets = _markets(session, limit=limit)
    legs_by_ticker = _legs_by_ticker(session)
    rows = [
        _market_row(session, market, legs_by_ticker.get(market.ticker, []), resolved)
        for market in markets
    ]
    crypto_rows = [row for row in rows if row["is_crypto_candidate"]]
    exact_rows = [row for row in crypto_rows if row["semantic_status"] == EXACT_LINK]
    rejected_rows = [row for row in crypto_rows if row["semantic_status"] != EXACT_LINK]
    latest_forecasts = _latest_crypto_forecasts(session)
    opportunities = _crypto_opportunities(session, {row["ticker"] for row in exact_rows})
    paper_orders = _crypto_paper_orders(session, {row["ticker"] for row in exact_rows})
    paper_summary = _paper_summary(session, paper_orders)
    funnel = {
        "markets_scanned": len(rows),
        "eligible_crypto_markets": len(crypto_rows),
        "successfully_parsed_markets": len(exact_rows),
        "exact_links": sum(1 for row in exact_rows if row["linked"]),
        "ambiguous_matches": sum(1 for row in rejected_rows if row["semantic_status"] == AMBIGUOUS),
        "unsupported_or_rejected_matches": sum(
            1
            for row in rejected_rows
            if row["semantic_status"] in {UNSUPPORTED, NOT_CRYPTO} or row["semantic_status"]
        ),
        "fresh_feature_snapshots": _fresh_feature_count(rows),
        "valid_crypto_v2_forecasts": len(latest_forecasts),
        "positive_ev_opportunities": len(opportunities),
        "paper_decisions": paper_summary["paper_orders"],
        "paper_trades": paper_summary["filled_orders"] + paper_summary["open_orders"],
        "open_trades": paper_summary["open_orders"],
        "resolved_trades": paper_summary["resolved_orders"],
        "voided_trades": paper_summary["voided_orders"],
        "orphaned_trades": paper_summary["orphaned_orders"],
        "realized_pnl": paper_summary["realized_pnl"],
        "roi": paper_summary["roi"],
    }
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AG_CRYPTO",
        "phase_version": PHASE3AG_CRYPTO_VERSION,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "mode": "PAPER_ONLY_DIAGNOSTICS",
        "settings": {
            "crypto_v2_min_link_confidence": str(resolved.crypto_v2_min_link_confidence),
            "crypto_v2_min_history_minutes": resolved.crypto_v2_min_history_minutes,
            "feature_max_age_minutes": DEFAULT_FEATURE_MAX_AGE_MINUTES,
            "execution_enabled": resolved.execution_enabled,
            "execution_dry_run": resolved.execution_dry_run,
        },
        "funnel": funnel,
        "status_counts": _status_counts(crypto_rows),
        "rejected_by_reason": _rejected_by_reason(rejected_rows),
        "feature_counts_by_symbol": _feature_counts_by_symbol(session),
        "latest_forecast_count_by_symbol": _forecast_count_by_symbol(latest_forecasts),
        "skip_reasons": _skip_reasons(session),
        "paper_trade_flow": paper_summary,
        "watermarks": _watermarks(session),
        "top_exact_rows": exact_rows[:50],
        "top_rejected_rows": rejected_rows[:50],
        "recommended_next_action": _recommended_next_action(funnel, exact_rows, rejected_rows),
        "next_commands": [
            "kalshi-bot market-legs-parse --refresh",
            "kalshi-bot ingest-crypto --symbols BTC,ETH,SOL,XRP,DOGE --source coinbase",
            "kalshi-bot build-crypto-features --symbols BTC,ETH,SOL,XRP,DOGE",
            "kalshi-bot link-crypto-markets",
            "kalshi-bot forecast --model crypto_v2",
            "kalshi-bot find-opportunities --model-name crypto_v2 --limit 100",
            (
                "LEARNING_MODE=true EXECUTION_ENABLED=false "
                "kalshi-bot paper-run --model-name crypto_v2"
            ),
            "kalshi-bot phase3ag-crypto-pipeline --output-dir reports/phase3ag_crypto",
        ],
    }


def write_phase3ag_crypto_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ag_crypto"),
    settings: Settings | None = None,
    limit: int | None = None,
) -> Phase3AGCryptoArtifactSet:
    payload = build_phase3ag_crypto_pipeline(session, settings=settings, limit=limit)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ag_crypto_pipeline.json"
    markdown_path = output_dir / "phase3ag_crypto_pipeline.md"
    rows_path = output_dir / "crypto_market_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["top_exact_rows"] + payload["top_rejected_rows"], indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AGCryptoArtifactSet(output_dir, json_path, markdown_path, rows_path)


def _markets(session: Session, *, limit: int | None) -> list[Market]:
    statement = select(Market).order_by(desc(Market.last_seen_at), Market.ticker)
    if limit is not None and limit > 0:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def _legs_by_ticker(session: Session) -> dict[str, list[MarketLeg]]:
    grouped: dict[str, list[MarketLeg]] = {}
    for leg in session.scalars(select(MarketLeg).order_by(MarketLeg.ticker, MarketLeg.leg_index)):
        grouped.setdefault(leg.ticker, []).append(leg)
    return grouped


def _market_row(
    session: Session,
    market: Market,
    persisted_legs: list[MarketLeg],
    settings: Settings,
) -> dict[str, Any]:
    legs = persisted_legs or list(parse_market_legs(market))
    terms = parse_crypto_market_terms(market, legs=legs)
    link = get_latest_crypto_link_for_ticker(session, market.ticker)
    latest_snapshot = _latest_snapshot(session, market.ticker)
    compatible_features = [
        _compatible_feature_payload(session, symbol, terms, latest_snapshot)
        for symbol in terms.component_symbols
    ]
    has_all_features = bool(terms.component_symbols) and all(
        item["ok"] for item in compatible_features
    )
    link_confidence = to_decimal(link.confidence if link else None)
    linked = bool(
        link is not None
        and terms.symbol is not None
        and link.symbol == terms.symbol
        and link_confidence is not None
        and link_confidence >= settings.crypto_v2_min_link_confidence
    )
    forecast = _latest_forecast(session, market.ticker)
    opportunity = _latest_opportunity(session, market.ticker)
    return {
        "ticker": market.ticker,
        "title": market.title,
        "status": market.status,
        "series_ticker": market.series_ticker,
        "event_ticker": market.event_ticker,
        "is_crypto_candidate": terms.is_crypto_candidate,
        "semantic_status": terms.status,
        "symbol": terms.symbol,
        "component_symbols": list(terms.component_symbols),
        "reason_codes": list(terms.reason_codes),
        "reference_price_source": terms.reference_price_source,
        "observation_time": terms.observation_time,
        "expiration_time": terms.expiration_time,
        "settlement_time": terms.settlement_time,
        "settlement_timezone": terms.settlement_timezone,
        "idempotency_key": terms.idempotency_key,
        "linked": linked,
        "link_id": link.id if link else None,
        "link_confidence": link.confidence if link else None,
        "link_reason": link.reason if link else None,
        "latest_snapshot_at": latest_snapshot.captured_at.isoformat()
        if latest_snapshot is not None
        else None,
        "compatible_features": compatible_features,
        "has_all_compatible_features": has_all_features,
        "latest_crypto_v2_forecast_id": forecast.id if forecast else None,
        "latest_crypto_v2_forecast_at": forecast.forecasted_at.isoformat()
        if forecast is not None
        else None,
        "latest_opportunity_id": opportunity.id if opportunity else None,
        "latest_opportunity_score": opportunity.opportunity_score if opportunity else None,
        "next_action": _row_next_action(terms.status, linked, has_all_features, forecast),
    }


def _compatible_feature_payload(
    session: Session,
    symbol: str,
    terms: Any,
    snapshot: MarketSnapshot | None,
) -> dict[str, Any]:
    if snapshot is None:
        return {"symbol": symbol, "ok": False, "reason": "missing_market_snapshot"}
    compatibility = select_compatible_crypto_feature(
        session,
        symbol=symbol,
        terms=terms,
        forecast_cutoff=snapshot.captured_at,
    )
    return {
        "symbol": symbol,
        "ok": compatibility.ok,
        "reason": compatibility.reason,
        "feature_id": compatibility.feature.id if compatibility.feature else None,
        "details": compatibility.details or {},
    }


def _latest_snapshot(session: Session, ticker: str) -> MarketSnapshot | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _latest_forecast(session: Session, ticker: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == "crypto_v2")
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _latest_opportunity(session: Session, ticker: str) -> MarketOpportunity | None:
    return session.scalar(
        select(MarketOpportunity)
        .where(MarketOpportunity.ticker == ticker, MarketOpportunity.model_name == "crypto_v2")
        .order_by(desc(MarketOpportunity.detected_at), desc(MarketOpportunity.id))
        .limit(1)
    )


def _latest_crypto_forecasts(session: Session) -> list[Forecast]:
    return list(
        session.scalars(
            select(Forecast)
            .where(Forecast.model_name == "crypto_v2")
            .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        )
    )


def _crypto_opportunities(session: Session, crypto_tickers: set[str]) -> list[MarketOpportunity]:
    rows = list(
        session.scalars(
            select(MarketOpportunity)
            .where(MarketOpportunity.model_name == "crypto_v2")
            .order_by(desc(MarketOpportunity.detected_at), desc(MarketOpportunity.id))
        )
    )
    return [
        row
        for row in rows
        if row.ticker in crypto_tickers
        and (to_decimal(row.estimated_edge) or Decimal("0")) > Decimal("0")
    ]


def _crypto_paper_orders(session: Session, crypto_tickers: set[str]) -> list[PaperOrder]:
    rows = list(
        session.scalars(
            select(PaperOrder)
            .where(PaperOrder.model_name == "crypto_v2")
            .order_by(desc(PaperOrder.created_at), desc(PaperOrder.id))
        )
    )
    if crypto_tickers:
        rows = [row for row in rows if row.ticker in crypto_tickers]
    return rows


def _paper_summary(session: Session, orders: list[PaperOrder]) -> dict[str, Any]:
    order_ids = {int(order.id) for order in orders if order.id is not None}
    tickers = {order.ticker for order in orders}
    sizing_count = _count_matching_order_ids(session, PositionSizingDecisionLog, order_ids)
    risk_count = _count_matching_order_ids(session, AdvancedRiskDecisionLog, order_ids)
    resolved = 0
    voided = 0
    orphaned = 0
    for order in orders:
        settlement = session.get(Settlement, order.ticker)
        market = session.get(Market, order.ticker)
        result = str(settlement.result or "").lower() if settlement is not None else ""
        if settlement is not None and result in {"yes", "no"}:
            resolved += 1
        elif settlement is not None and result in {"void", "voided", "canceled", "cancelled"}:
            voided += 1
        elif order.status == ORDER_FILLED and market is None:
            orphaned += 1
    pnl_rows = _latest_pnl_rows(session, tickers)
    realized = sum((to_decimal(row.realized_pnl) or Decimal("0") for row in pnl_rows), Decimal("0"))
    exposure = sum(
        (
            (to_decimal(order.market_price) or Decimal("0")) * Decimal(order.quantity)
            for order in orders
        ),
        Decimal("0"),
    )
    roi = None if exposure == 0 else str((realized / exposure).quantize(Decimal("0.0001")))
    return {
        "paper_orders": len(orders),
        "open_orders": sum(1 for order in orders if order.status == ORDER_OPEN),
        "filled_orders": sum(1 for order in orders if order.status == ORDER_FILLED),
        "phase3m_sizing_decisions": sizing_count,
        "phase3n_risk_decisions": risk_count,
        "resolved_orders": resolved,
        "voided_orders": voided,
        "orphaned_orders": orphaned,
        "realized_pnl": str(realized),
        "exposure": str(exposure),
        "roi": roi,
    }


def _count_matching_order_ids(session: Session, table: Any, order_ids: set[int]) -> int:
    if not order_ids:
        return 0
    return int(
        session.scalar(
            select(func.count()).select_from(table).where(table.paper_order_id.in_(order_ids))
        )
        or 0
    )


def _latest_pnl_rows(session: Session, tickers: set[str]) -> list[PaperPnl]:
    if not tickers:
        return []
    rows = list(
        session.scalars(
            select(PaperPnl)
            .where(PaperPnl.ticker.in_(tickers))
            .order_by(PaperPnl.ticker, desc(PaperPnl.calculated_at), desc(PaperPnl.id))
        )
    )
    latest: dict[str, PaperPnl] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return list(latest.values())


def _fresh_feature_count(rows: list[dict[str, Any]]) -> int:
    feature_ids = {
        item["feature_id"]
        for row in rows
        for item in row.get("compatible_features", [])
        if item.get("ok") and item.get("feature_id") is not None
    }
    return len(feature_ids)


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row["semantic_status"])
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _rejected_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.get("reason_codes", []) or ["unknown"]:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _feature_counts_by_symbol(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(CryptoFeature.symbol, func.count(CryptoFeature.id)).group_by(CryptoFeature.symbol)
    ).all()
    return {str(symbol): int(count) for symbol, count in rows}


def _forecast_count_by_symbol(forecasts: list[Forecast]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for forecast in forecasts:
        raw = decode_json(forecast.feature_json)
        symbols = raw.get("component_symbols") or [raw.get("symbol")]
        for symbol in symbols:
            if not symbol:
                continue
            key = str(symbol)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _skip_reasons(session: Session) -> dict[str, int]:
    rows = list(
        session.scalars(
            select(ForecastSkipLog).where(ForecastSkipLog.model_name == "crypto_v2")
        )
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.reason] = counts.get(row.reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _watermarks(session: Session) -> dict[str, Any]:
    latest_link = session.scalar(select(func.max(CryptoMarketLink.detected_at)))
    latest_feature = session.scalar(select(func.max(CryptoFeature.generated_at)))
    latest_forecast = session.scalar(
        select(func.max(Forecast.forecasted_at)).where(Forecast.model_name == "crypto_v2")
    )
    latest_settlement = session.scalar(select(func.max(Settlement.updated_at)))
    return {
        "latest_crypto_link_at": latest_link.isoformat() if latest_link else None,
        "latest_crypto_feature_at": latest_feature.isoformat() if latest_feature else None,
        "latest_crypto_v2_forecast_at": latest_forecast.isoformat()
        if latest_forecast
        else None,
        "latest_settlement_updated_at": latest_settlement.isoformat()
        if latest_settlement
        else None,
    }


def _row_next_action(
    status: str,
    linked: bool,
    has_all_features: bool,
    forecast: Forecast | None,
) -> str:
    if status != EXACT_LINK:
        return "Review semantic reject reason; unsupported or ambiguous markets stay excluded."
    if not linked:
        return "Run kalshi-bot link-crypto-markets."
    if not has_all_features:
        return "Run ingest-crypto and build-crypto-features for the linked symbols."
    if forecast is None:
        return "Run kalshi-bot forecast --model crypto_v2."
    return "Ready for opportunity scoring and paper-only gates."


def _recommended_next_action(
    funnel: dict[str, Any],
    exact_rows: list[dict[str, Any]],
    rejected_rows: list[dict[str, Any]],
) -> str:
    if funnel["exact_links"] == 0 and funnel["eligible_crypto_markets"] > 0:
        return "Run link-crypto-markets after refreshing market legs."
    missing_feature_rows = [
        row for row in exact_rows if row["linked"] and not row["has_all_compatible_features"]
    ]
    if missing_feature_rows:
        return (
            "Build fresh point-in-time crypto features for every linked component symbol, "
            "then rerun crypto_v2 forecasts."
        )
    if funnel["fresh_feature_snapshots"] == 0 and funnel["exact_links"] > 0:
        return "Ingest crypto prices and rebuild canonical point-in-time crypto features."
    if funnel["valid_crypto_v2_forecasts"] == 0 and funnel["fresh_feature_snapshots"] > 0:
        return "Run forecast --model crypto_v2 and inspect skip reasons if no rows appear."
    if funnel["positive_ev_opportunities"] == 0 and funnel["valid_crypto_v2_forecasts"] > 0:
        return (
            "Run opportunity scoring for crypto_v2. If it still finds zero opportunities, "
            "the EV/score/risk gates are correctly blocking paper trades."
        )
    if funnel["paper_decisions"] == 0 and funnel["positive_ev_opportunities"] > 0:
        return "Run paper-run for crypto_v2 with Learning Mode paper-only settings."
    if rejected_rows:
        return "Review rejected crypto semantics; keep ambiguous/unsupported markets excluded."
    return "Crypto linkage proof is healthy; continue through existing paper-only gates."


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AG Crypto Market Linkage and Paper-Trade Settlement",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked; this command does not submit exchange orders.",
        "",
        "## Crypto Funnel",
        "",
        "| Step | Count / Value |",
        "| --- | ---: |",
    ]
    for key, value in payload["funnel"].items():
        lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## Semantic Status Counts",
            "",
            "| Status | Count |",
            "| --- | ---: |",
        ]
    )
    if payload["status_counts"]:
        for key, value in payload["status_counts"].items():
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Rejected Matches By Reason",
            "",
            "| Reason | Count |",
            "| --- | ---: |",
        ]
    )
    if payload["rejected_by_reason"]:
        for key, value in payload["rejected_by_reason"].items():
            lines.append(f"| {key} | {value} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Paper Trade Flow",
            "",
        ]
    )
    for key, value in payload["paper_trade_flow"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Latest Watermarks",
            "",
        ]
    )
    for key, value in payload["watermarks"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Example Exact Links",
            "",
            "| Ticker | Symbol | Linked | Features | Forecast | Next action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["top_exact_rows"][:20]:
        features = ",".join(
            item["symbol"] for item in row["compatible_features"] if item.get("ok")
        )
        lines.append(
            f"| {row['ticker']} | {row['symbol']} | {row['linked']} | "
            f"{features or 'none'} | {row['latest_crypto_v2_forecast_id'] or 'none'} | "
            f"{_md(row['next_action'])} |"
        )
    if not payload["top_exact_rows"]:
        lines.append("| none |  |  |  |  | No exact crypto links found. |")
    lines.extend(
        [
            "",
            "## Example Rejections",
            "",
            "| Ticker | Status | Reasons | Next action |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["top_rejected_rows"][:20]:
        lines.append(
            f"| {row['ticker']} | {row['semantic_status']} | "
            f"{_md(', '.join(row['reason_codes']))} | {_md(row['next_action'])} |"
        )
    if not payload["top_rejected_rows"]:
        lines.append("| none |  |  | No rejected crypto-looking markets. |")
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
        ]
    )
    lines.extend(payload["next_commands"])
    lines.append("```")
    return "\n".join(lines) + "\n"


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|")
