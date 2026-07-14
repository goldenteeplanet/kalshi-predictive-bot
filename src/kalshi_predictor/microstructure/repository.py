from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    MicrostructureEvent,
    MicrostructureFeature,
    MicrostructureSignal,
    OrderbookDepthSnapshot,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def insert_microstructure_feature(
    session: Session,
    row: Mapping[str, Any],
) -> MicrostructureFeature:
    record = MicrostructureFeature(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        lookback_minutes=int(row.get("lookback_minutes") or 0),
        snapshot_count=int(row.get("snapshot_count") or 0),
        current_yes_bid=decimal_to_str(row.get("current_yes_bid")),
        current_yes_ask=decimal_to_str(row.get("current_yes_ask")),
        current_no_bid=decimal_to_str(row.get("current_no_bid")),
        current_no_ask=decimal_to_str(row.get("current_no_ask")),
        current_spread=decimal_to_str(row.get("current_spread")),
        avg_spread=decimal_to_str(row.get("avg_spread")),
        min_spread=decimal_to_str(row.get("min_spread")),
        max_spread=decimal_to_str(row.get("max_spread")),
        spread_change=decimal_to_str(row.get("spread_change")),
        spread_change_pct=decimal_to_str(row.get("spread_change_pct")),
        current_liquidity=decimal_to_str(row.get("current_liquidity")),
        avg_liquidity=decimal_to_str(row.get("avg_liquidity")),
        liquidity_change=decimal_to_str(row.get("liquidity_change")),
        liquidity_change_pct=decimal_to_str(row.get("liquidity_change_pct")),
        orderbook_imbalance=decimal_to_str(row.get("orderbook_imbalance")),
        yes_bid_depth=decimal_to_str(row.get("yes_bid_depth")),
        no_bid_depth=decimal_to_str(row.get("no_bid_depth")),
        price_velocity=decimal_to_str(row.get("price_velocity")),
        price_acceleration=decimal_to_str(row.get("price_acceleration")),
        late_move_score=decimal_to_str(row.get("late_move_score")),
        dislocation_score=decimal_to_str(row.get("dislocation_score")),
        smart_money_score=decimal_to_str(row.get("smart_money_score")),
        microstructure_confidence=decimal_to_str(row.get("microstructure_confidence")),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_microstructure_event(
    session: Session,
    row: Mapping[str, Any],
) -> MicrostructureEvent:
    record = MicrostructureEvent(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        event_type=str(row["event_type"]),
        severity=str(row.get("severity") or "INFO"),
        score=decimal_to_str(row.get("score")) or "0",
        title=str(row.get("title") or row["event_type"]),
        description=str(row.get("description") or ""),
        evidence_json=encode_json(dict(row.get("evidence") or row.get("evidence_json") or {})),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_microstructure_signal(
    session: Session,
    row: Mapping[str, Any],
) -> MicrostructureSignal:
    record = MicrostructureSignal(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        signal_name=str(row["signal_name"]),
        signal_strength=decimal_to_str(row.get("signal_strength")) or "0",
        signal_direction=(
            str(row.get("signal_direction")) if row.get("signal_direction") is not None else None
        ),
        confidence=decimal_to_str(row.get("confidence")) or "0",
        explanation=str(row.get("explanation") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_orderbook_depth_snapshot(
    session: Session,
    row: Mapping[str, Any],
) -> OrderbookDepthSnapshot:
    record = OrderbookDepthSnapshot(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        yes_bid_depth=decimal_to_str(row.get("yes_bid_depth")),
        no_bid_depth=decimal_to_str(row.get("no_bid_depth")),
        yes_levels_json=encode_json(row.get("yes_levels") or []),
        no_levels_json=encode_json(row.get("no_levels") or []),
        imbalance=decimal_to_str(row.get("imbalance")),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def latest_microstructure_feature(
    session: Session,
    ticker: str,
) -> MicrostructureFeature | None:
    return session.scalar(
        select(MicrostructureFeature)
        .where(MicrostructureFeature.ticker == ticker)
        .order_by(desc(MicrostructureFeature.created_at), desc(MicrostructureFeature.id))
        .limit(1)
    )


def recent_microstructure_features(
    session: Session,
    *,
    ticker: str | None = None,
    limit: int = 100,
) -> list[MicrostructureFeature]:
    statement = select(MicrostructureFeature)
    if ticker:
        statement = statement.where(MicrostructureFeature.ticker == ticker)
    return list(
        session.scalars(
            statement.order_by(
                desc(MicrostructureFeature.created_at),
                desc(MicrostructureFeature.id),
            ).limit(limit)
        )
    )


def recent_microstructure_events(
    session: Session,
    *,
    ticker: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[MicrostructureEvent]:
    statement = select(MicrostructureEvent)
    if ticker:
        statement = statement.where(MicrostructureEvent.ticker == ticker)
    if event_type:
        statement = statement.where(MicrostructureEvent.event_type == event_type)
    return list(
        session.scalars(
            statement.order_by(
                desc(MicrostructureEvent.created_at),
                desc(MicrostructureEvent.id),
            ).limit(limit)
        )
    )


def recent_microstructure_signals(
    session: Session,
    *,
    ticker: str | None = None,
    limit: int = 100,
) -> list[MicrostructureSignal]:
    statement = select(MicrostructureSignal)
    if ticker:
        statement = statement.where(MicrostructureSignal.ticker == ticker)
    return list(
        session.scalars(
            statement.order_by(
                desc(MicrostructureSignal.created_at),
                desc(MicrostructureSignal.id),
            ).limit(limit)
        )
    )


def feature_to_dict(row: MicrostructureFeature) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in row.__mapper__.columns.keys():
        value = getattr(row, key)
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
        elif key == "raw_json":
            data[key] = decode_json(value)
        else:
            data[key] = value
    return data

