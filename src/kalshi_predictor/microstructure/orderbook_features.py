from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import Market, MarketSnapshot
from kalshi_predictor.microstructure.dislocation import detect_dislocation_events, dislocation_score
from kalshi_predictor.microstructure.imbalance import calculate_imbalance, detect_imbalance_events
from kalshi_predictor.microstructure.late_moves import detect_late_move_events, late_move_score
from kalshi_predictor.microstructure.liquidity_tracker import detect_liquidity_events
from kalshi_predictor.microstructure.repository import (
    insert_microstructure_event,
    insert_microstructure_feature,
    insert_orderbook_depth_snapshot,
)
from kalshi_predictor.microstructure.signals import generate_microstructure_signals
from kalshi_predictor.microstructure.smart_money import detect_smart_money_events, smart_money_score
from kalshi_predictor.microstructure.spread_tracker import detect_spread_events
from kalshi_predictor.utils.decimals import decimal_to_str, midpoint, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass(frozen=True)
class MicrostructureBuildResult:
    markets_scanned: int
    features_inserted: int
    events_inserted: int
    signals_inserted: int
    depth_snapshots_inserted: int
    skipped_insufficient_snapshots: int


def parse_orderbook_depth(orderbook_json: dict[str, Any] | None) -> dict[str, Any]:
    orderbook = _orderbook_container(orderbook_json or {})
    yes_levels = _levels(orderbook, "yes_dollars", "yes")
    no_levels = _levels(orderbook, "no_dollars", "no")
    yes_bid_depth = _depth(yes_levels)
    no_bid_depth = _depth(no_levels)
    imbalance = calculate_imbalance(yes_bid_depth, no_bid_depth)
    return {
        "yes_levels": yes_levels,
        "no_levels": no_levels,
        "yes_bid_depth": yes_bid_depth,
        "no_bid_depth": no_bid_depth,
        "top_of_book_depth": (yes_bid_depth or Decimal("0")) + (no_bid_depth or Decimal("0")),
        "total_depth": (yes_bid_depth or Decimal("0")) + (no_bid_depth or Decimal("0")),
        "imbalance": imbalance,
    }


def snapshot_microstructure(snapshot: MarketSnapshot) -> dict[str, Any]:
    raw_market = decode_json(snapshot.raw_market_json)
    raw_orderbook = decode_json(snapshot.raw_orderbook_json)
    depth = parse_orderbook_depth(raw_orderbook)
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    no_bid = to_decimal(snapshot.best_no_bid)
    no_ask = to_decimal(snapshot.best_no_ask)
    spread = to_decimal(snapshot.spread)
    if spread is None and yes_bid is not None and yes_ask is not None:
        spread = yes_ask - yes_bid
    mid = midpoint(yes_bid, yes_ask) if yes_bid is not None and yes_ask is not None else None
    liquidity = (
        to_decimal(raw_market.get("liquidity_dollars"))
        or to_decimal(snapshot.volume_fp)
        or depth["total_depth"]
    )
    return {
        "ticker": snapshot.ticker,
        "captured_at": snapshot.captured_at,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "spread": spread,
        "midpoint": mid,
        "liquidity": liquidity,
        "depth": depth,
        "raw_market": raw_market,
        "raw_orderbook": raw_orderbook,
    }


def build_microstructure_features(
    session: Session,
    *,
    lookback_minutes: int | None = None,
    settings: Settings | None = None,
    persist: bool = True,
) -> MicrostructureBuildResult:
    resolved_settings = settings or get_settings()
    lookback = lookback_minutes or resolved_settings.microstructure_lookback_minutes
    since = utc_now() - timedelta(minutes=lookback)
    snapshots = list(
        session.scalars(
            select(MarketSnapshot)
            .where(MarketSnapshot.captured_at >= since)
            .order_by(MarketSnapshot.ticker, MarketSnapshot.captured_at, MarketSnapshot.id)
        )
    )
    by_ticker: dict[str, list[MarketSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_ticker[snapshot.ticker].append(snapshot)

    features_inserted = 0
    events_inserted = 0
    signals_inserted = 0
    depth_inserted = 0
    skipped = 0

    for ticker, ticker_snapshots in by_ticker.items():
        if len(ticker_snapshots) < resolved_settings.microstructure_min_snapshots:
            skipped += 1
            continue
        feature = compute_microstructure_feature(
            session,
            ticker_snapshots,
            lookback_minutes=lookback,
            settings=resolved_settings,
        )
        events = detect_microstructure_events(
            session,
            feature,
            settings=resolved_settings,
        )
        if persist:
            insert_microstructure_feature(session, feature)
            features_inserted += 1
            depth = feature["raw_json"]["current_depth"]
            insert_orderbook_depth_snapshot(
                session,
                {
                    "created_at": feature["created_at"],
                    "ticker": ticker,
                    **depth,
                    "raw_json": depth,
                },
            )
            depth_inserted += 1
            for event in events:
                insert_microstructure_event(session, {**event, "created_at": feature["created_at"]})
            events_inserted += len(events)
            signals = generate_microstructure_signals(
                session,
                feature,
                events,
                settings=resolved_settings,
            )
            signals_inserted += len(signals)

    return MicrostructureBuildResult(
        markets_scanned=len(by_ticker),
        features_inserted=features_inserted,
        events_inserted=events_inserted,
        signals_inserted=signals_inserted,
        depth_snapshots_inserted=depth_inserted,
        skipped_insufficient_snapshots=skipped,
    )


def compute_microstructure_feature(
    session: Session,
    snapshots: list[MarketSnapshot],
    *,
    lookback_minutes: int,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    rows = [snapshot_microstructure(snapshot) for snapshot in snapshots]
    current = rows[-1]
    first = rows[0]
    spreads = [row["spread"] for row in rows if row["spread"] is not None]
    liquidities = [row["liquidity"] for row in rows if row["liquidity"] is not None]
    midpoints = [row["midpoint"] for row in rows if row["midpoint"] is not None]
    spread_change = _change(current["spread"], first["spread"])
    liquidity_change = _change(current["liquidity"], first["liquidity"])
    velocity = _change(midpoints[-1], midpoints[0]) if len(midpoints) >= 2 else None
    acceleration = _acceleration(midpoints)
    minutes_to_close = _minutes_to_close(session, current["ticker"], current["raw_market"])
    feature: dict[str, Any] = {
        "created_at": utc_now(),
        "ticker": current["ticker"],
        "lookback_minutes": lookback_minutes,
        "snapshot_count": len(rows),
        "current_yes_bid": current["yes_bid"],
        "current_yes_ask": current["yes_ask"],
        "current_no_bid": current["no_bid"],
        "current_no_ask": current["no_ask"],
        "current_spread": current["spread"],
        "avg_spread": _average(spreads),
        "min_spread": min(spreads) if spreads else None,
        "max_spread": max(spreads) if spreads else None,
        "spread_change": spread_change,
        "spread_change_pct": _change_pct(spread_change, first["spread"]),
        "current_liquidity": current["liquidity"],
        "avg_liquidity": _average(liquidities),
        "liquidity_change": liquidity_change,
        "liquidity_change_pct": _change_pct(liquidity_change, first["liquidity"]),
        "orderbook_imbalance": current["depth"]["imbalance"],
        "yes_bid_depth": current["depth"]["yes_bid_depth"],
        "no_bid_depth": current["depth"]["no_bid_depth"],
        "price_velocity": velocity,
        "price_acceleration": acceleration,
        "late_move_score": Decimal("0"),
        "dislocation_score": Decimal("0"),
        "smart_money_score": Decimal("0"),
        "microstructure_confidence": Decimal("0"),
        "raw_json": {
            "minutes_to_close": decimal_to_str(minutes_to_close),
            "current_depth": current["depth"],
            "first_imbalance": decimal_to_str(first["depth"]["imbalance"]),
            "midpoints": [decimal_to_str(value) for value in midpoints],
            "spreads": [decimal_to_str(value) for value in spreads],
            "liquidities": [decimal_to_str(value) for value in liquidities],
        },
    }
    feature["late_move_score"] = late_move_score(feature, minutes_to_close=minutes_to_close)
    feature["dislocation_score"] = dislocation_score(
        market_midpoint=current["midpoint"],
        model_probability=_latest_probability(session, current["ticker"], "ensemble_v2"),
        recent_velocity=velocity,
    )
    feature["smart_money_score"] = smart_money_score(feature)
    feature["microstructure_confidence"] = _confidence(feature, resolved_settings)
    feature["raw_json"]["minutes_to_close"] = decimal_to_str(minutes_to_close)
    return feature


def detect_microstructure_events(
    session: Session,
    feature: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    minutes_to_close = to_decimal(feature.get("raw_json", {}).get("minutes_to_close"))
    events = []
    events.extend(detect_spread_events(feature, settings=resolved_settings))
    events.extend(detect_liquidity_events(feature, settings=resolved_settings))
    previous_feature = {
        "orderbook_imbalance": feature.get("raw_json", {}).get("first_imbalance"),
    }
    events.extend(
        detect_imbalance_events(
            feature,
            previous_feature=previous_feature,
            settings=resolved_settings,
        )
    )
    events.extend(detect_dislocation_events(session, feature, settings=resolved_settings))
    events.extend(
        detect_late_move_events(
            feature,
            minutes_to_close=minutes_to_close,
            settings=resolved_settings,
        )
    )
    events.extend(detect_smart_money_events(feature, settings=resolved_settings))
    return events


def _levels(orderbook: dict[str, Any], dollars_key: str, cents_key: str) -> list[Any]:
    levels = orderbook.get(dollars_key)
    if levels is None:
        levels = orderbook.get(cents_key)
    return levels if isinstance(levels, list) else []


def _orderbook_container(orderbook_json: dict[str, Any]) -> dict[str, Any]:
    for key in ("orderbook_fp", "orderbook"):
        value = orderbook_json.get(key)
        if isinstance(value, dict):
            return value
    return orderbook_json


def _depth(levels: list[Any]) -> Decimal | None:
    total = Decimal("0")
    for level in levels:
        quantity = None
        if isinstance(level, dict):
            quantity = (
                level.get("quantity")
                or level.get("count")
                or level.get("size")
                or level.get("contracts")
            )
        elif isinstance(level, (list, tuple)) and len(level) > 1:
            quantity = level[1]
        amount = to_decimal(quantity)
        if amount is not None:
            total += amount
    return total if total > 0 else None


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _change(current: Decimal | None, first: Decimal | None) -> Decimal | None:
    if current is None or first is None:
        return None
    return current - first


def _change_pct(change: Decimal | None, first: Decimal | None) -> Decimal | None:
    if change is None or first is None or first == 0:
        return None
    return change / first


def _acceleration(values: list[Decimal]) -> Decimal | None:
    if len(values) < 3:
        return None
    first_move = values[-2] - values[0]
    last_move = values[-1] - values[-2]
    return last_move - first_move


def _minutes_to_close(
    session: Session,
    ticker: str,
    raw_market: dict[str, Any],
) -> Decimal | None:
    close_time = parse_datetime(raw_market.get("close_time"))
    if close_time is None:
        market = session.get(Market, ticker)
        close_time = market.close_time if market is not None else None
    if close_time is None:
        return None
    return Decimal(str((close_time - utc_now()).total_seconds() / 60))


def _latest_probability(session: Session, ticker: str, model_name: str) -> Decimal | None:
    from kalshi_predictor.data.schema import Forecast

    forecast = session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == model_name)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )
    return to_decimal(forecast.yes_probability if forecast else None)


def _confidence(feature: dict[str, Any], settings: Settings) -> Decimal:
    snapshots = Decimal(int(feature.get("snapshot_count") or 0))
    sample = min(snapshots / Decimal(max(1, settings.microstructure_min_snapshots)), Decimal("1"))
    has_depth = (
        Decimal("1")
        if feature.get("yes_bid_depth") and feature.get("no_bid_depth")
        else Decimal("0")
    )
    has_prices = (
        Decimal("1")
        if feature.get("current_yes_bid") and feature.get("current_yes_ask")
        else Decimal("0")
    )
    signal_strength = max(
        to_decimal(feature.get("late_move_score")) or Decimal("0"),
        to_decimal(feature.get("dislocation_score")) or Decimal("0"),
        to_decimal(feature.get("smart_money_score")) or Decimal("0"),
    )
    confidence = (
        sample * Decimal("45")
        + has_depth * Decimal("20")
        + has_prices * Decimal("20")
        + signal_strength * Decimal("15")
    )
    return min(confidence, Decimal("100"))
