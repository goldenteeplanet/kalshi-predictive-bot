from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    CryptoFeatureLineage,
    ExecutableEdgeAttribution,
    Forecast,
    Market,
    MarketSnapshot,
    ReplayDisposition,
    Settlement,
)
from kalshi_predictor.phase4cd.domain import deterministic_id, independent_event_id
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


def build_edge_attribution(
    session: Session,
    *,
    run_id: str,
    slippage: Decimal = Decimal("0"),
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    rows = list(
        session.scalars(
            select(ReplayDisposition)
            .where(ReplayDisposition.run_id == run_id)
            .order_by(ReplayDisposition.forecast_id)
        )
    )
    positive_gross = positive_net = executable = 0
    gross_tickers: Counter[str] = Counter()
    gross_events: Counter[str] = Counter()
    gross_assets: Counter[str] = Counter()
    bands: Counter[str] = Counter()
    feature_after_decision = 0
    max_feature_delay_seconds = Decimal("0")
    for disposition in rows:
        forecast = session.get(Forecast, disposition.forecast_id)
        market = session.get(Market, disposition.market_ticker)
        if forecast is None or market is None:
            continue
        snapshot = session.scalar(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.ticker == forecast.ticker,
                MarketSnapshot.captured_at <= forecast.forecasted_at,
            )
            .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
            .limit(1)
        )
        settlement = session.get(Settlement, forecast.ticker)
        if snapshot is None or settlement is None or settlement.settled_at is None:
            continue
        probability = Decimal(forecast.yes_probability)
        midpoint = to_decimal(forecast.market_mid_probability)
        if midpoint is None:
            midpoint = _snapshot_midpoint(snapshot)
        details = json.loads(disposition.details_json)
        side = str(details.get("side") or _best_side(probability, midpoint))
        price = to_decimal(details.get("price"))
        if price is None:
            price = (
                to_decimal(forecast.best_yes_ask) or to_decimal(snapshot.best_yes_ask)
                if side == "BUY_YES"
                else to_decimal(snapshot.best_no_ask)
            )
        if midpoint is None or price is None:
            continue
        advantage = probability - midpoint if side == "BUY_YES" else midpoint - probability
        midpoint_side = midpoint if side == "BUY_YES" else Decimal("1") - midpoint
        crossing = price - midpoint_side
        gross = advantage - crossing
        fee = resolved.paper_default_fee_per_contract
        net = gross - fee - slippage
        forecast_price = to_decimal(forecast.best_yes_ask) if side == "BUY_YES" else None
        timing_decay = abs(price - forecast_price) if forecast_price is not None else Decimal("0")
        event_id = independent_event_id(
            ticker=market.ticker,
            event_ticker=market.event_ticker,
            series_ticker=market.series_ticker,
        )
        asset = _asset(forecast)
        band = _probability_band(probability)
        bands[band] += 1
        if gross > 0:
            positive_gross += 1
            gross_tickers[forecast.ticker] += 1
            gross_events[event_id] += 1
            gross_assets[asset or "UNKNOWN"] += 1
        if net > 0:
            positive_net += 1
        if disposition.disposition == "EVALUATED_EXECUTABLE":
            executable += 1
        lineage = session.scalar(
            select(CryptoFeatureLineage).where(CryptoFeatureLineage.forecast_id == forecast.id)
        )
        if lineage:
            provenance = json.loads(lineage.provenance_json)
            for feature in provenance.get("canonical_features", []):
                generated = _parse_time(feature.get("generated_at"))
                if generated and generated > _utc(forecast.forecasted_at):
                    feature_after_decision += 1
                    delay = Decimal(str((generated - _utc(forecast.forecasted_at)).total_seconds()))
                    max_feature_delay_seconds = max(max_feature_delay_seconds, delay)
        session.merge(
            ExecutableEdgeAttribution(
                attribution_id=deterministic_id("edge-attribution-v1", run_id, forecast.id),
                run_id=run_id,
                forecast_id=int(forecast.id),
                independent_event_id=event_id,
                asset=asset,
                probability_band=band,
                side=side,
                probability_advantage=str(advantage),
                crossing_spread_cost=str(crossing),
                fees=str(fee),
                slippage=str(slippage),
                timing_decay=str(timing_decay),
                gross_edge=str(gross),
                net_edge=str(net),
                spread=snapshot.spread,
                liquidity=snapshot.open_interest_fp or snapshot.volume_fp,
                forecast_age_seconds=str(
                    max(
                        Decimal("0"),
                        Decimal(
                            str(
                                (
                                    _utc(forecast.forecasted_at) - _utc(snapshot.captured_at)
                                ).total_seconds()
                            )
                        ),
                    )
                ),
                settlement_window_seconds=str(
                    Decimal(
                        str(
                            (
                                _utc(settlement.settled_at) - _utc(forecast.forecasted_at)
                            ).total_seconds()
                        )
                    )
                ),
                terminal_reason=disposition.disposition,
                details_json=json.dumps(
                    {"price": str(price), "midpoint": str(midpoint)}, sort_keys=True
                ),
                created_at=utc_now(),
            )
        )
    session.commit()
    max_ticker_share = _max_share(gross_tickers, positive_gross)
    max_event_share = _max_share(gross_events, positive_gross)
    max_asset_share = _max_share(gross_assets, positive_gross)
    concentration_pass = (
        positive_gross > 0
        and len(gross_events) >= 5
        and max_ticker_share <= Decimal("0.5")
        and max_event_share <= Decimal("0.5")
        and max_asset_share <= Decimal("0.8")
    )
    return {
        "run_id": run_id,
        "attributed": len(rows),
        "positive_gross_rows": positive_gross,
        "positive_net_rows_before_min_edge_gate": positive_net,
        "executable_qualified_rows": executable,
        "hypothetical_pnl_rows": executable,
        "positive_gross_independent_events": len(gross_events),
        "probability_bands": dict(sorted(bands.items())),
        "concentration": {
            "max_ticker_share": str(max_ticker_share),
            "max_event_share": str(max_event_share),
            "max_asset_share": str(max_asset_share),
            "passes_repeatability_gate": concentration_pass,
            "top_tickers": gross_tickers.most_common(5),
            "top_events": gross_events.most_common(5),
            "top_assets": gross_assets.most_common(5),
        },
        "latency": {
            "feature_after_decision_admitted": feature_after_decision,
            "max_feature_delay_seconds": str(max_feature_delay_seconds),
        },
        "performance_claim_authorized": executable > 0 and concentration_pass,
    }


def _snapshot_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    bid = to_decimal(snapshot.best_yes_bid)
    ask = to_decimal(snapshot.best_yes_ask)
    return (bid + ask) / 2 if bid is not None and ask is not None else None


def _best_side(probability: Decimal, midpoint: Decimal | None) -> str:
    return "BUY_YES" if midpoint is None or probability >= midpoint else "BUY_NO"


def _probability_band(probability: Decimal) -> str:
    if probability <= Decimal("0.25"):
        return "0-25"
    if probability <= Decimal("0.50"):
        return "26-50"
    if probability <= Decimal("0.75"):
        return "51-75"
    return "76-100"


def _asset(forecast: Forecast) -> str | None:
    try:
        payload = json.loads(forecast.feature_json or "{}")
    except json.JSONDecodeError:
        return None
    symbols = payload.get("component_symbols") or [payload.get("symbol")]
    return "+".join(str(value) for value in symbols if value) or None


def _max_share(counter: Counter[str], total: int) -> Decimal:
    return Decimal(max(counter.values(), default=0)) / total if total else Decimal("0")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
