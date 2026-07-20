import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import Forecast, Market, MarketSnapshot, Settlement
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now


def encode_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def decode_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


def upsert_market(session: Session, market_json: Mapping[str, Any]) -> Market:
    ticker = _required_str(market_json, "ticker")
    now = utc_now()
    market = _pending_market(session, ticker) or session.get(Market, ticker)
    is_new = market is None

    if market is None:
        market = Market(ticker=ticker, first_seen_at=now, last_seen_at=now, raw_json="{}")
        session.add(market)

    market.event_ticker = _str_or_none(market_json.get("event_ticker"))
    market.series_ticker = _str_or_none(market_json.get("series_ticker"))
    market.title = _str_or_none(market_json.get("title"))
    market.subtitle = _str_or_none(market_json.get("subtitle"))
    market.market_type = _str_or_none(_first_present(market_json, "market_type", "type"))
    market.status = _str_or_none(market_json.get("status"))
    market.result = _str_or_none(market_json.get("result"))
    market.open_time = parse_datetime(market_json.get("open_time"))
    market.close_time = parse_datetime(market_json.get("close_time"))
    market.expected_expiration_time = parse_datetime(market_json.get("expected_expiration_time"))
    market.expiration_time = parse_datetime(market_json.get("expiration_time"))
    market.settlement_ts = parse_datetime(
        _first_present(market_json, "settlement_ts", "settled_time", "settled_at")
    )
    market.settlement_value_dollars = _decimal_string(
        _first_present(market_json, "settlement_value_dollars", "settlement_value")
    )
    market.volume_fp = _decimal_string(market_json.get("volume_fp"))
    market.open_interest_fp = _decimal_string(market_json.get("open_interest_fp"))
    market.liquidity_dollars = _decimal_string(market_json.get("liquidity_dollars"))
    market.rules_primary = _str_or_none(_first_present(market_json, "rules_primary", "rules"))
    market.rules_secondary = _str_or_none(market_json.get("rules_secondary"))
    market.raw_json = encode_json(dict(market_json))
    market.last_seen_at = now
    if is_new:
        market.first_seen_at = now

    return market


def insert_market_snapshot(
    session: Session,
    market_json: Mapping[str, Any],
    orderbook_json: Mapping[str, Any] | None,
    captured_at: datetime,
) -> MarketSnapshot:
    market_payload = dict(market_json)
    if "close_time" not in market_payload:
        existing_market = _pending_market(session, _required_str(market_payload, "ticker"))
        if existing_market is None:
            existing_market = session.get(Market, _required_str(market_payload, "ticker"))
        if existing_market is not None and existing_market.close_time is not None:
            # Snapshot payloads can be intentionally partial. Preserve the exact known
            # close time only when the source omitted the field; explicit values retain
            # normal upsert semantics.
            market_payload["close_time"] = existing_market.close_time.isoformat()

    market = upsert_market(session, market_payload)
    best_prices = parse_orderbook(dict(orderbook_json) if orderbook_json is not None else None)

    yes_bid = _decimal_string(market_json.get("yes_bid_dollars"))
    yes_ask = _decimal_string(market_json.get("yes_ask_dollars"))
    no_bid = _decimal_string(market_json.get("no_bid_dollars"))
    no_ask = _decimal_string(market_json.get("no_ask_dollars"))

    snapshot = MarketSnapshot(
        ticker=market.ticker,
        captured_at=captured_at,
        status=_str_or_none(market_json.get("status")),
        yes_bid_dollars=yes_bid,
        yes_ask_dollars=yes_ask,
        no_bid_dollars=no_bid,
        no_ask_dollars=no_ask,
        best_yes_bid=decimal_to_str(best_prices.best_yes_bid) or yes_bid,
        best_yes_ask=decimal_to_str(best_prices.best_yes_ask) or yes_ask,
        best_no_bid=decimal_to_str(best_prices.best_no_bid) or no_bid,
        best_no_ask=decimal_to_str(best_prices.best_no_ask) or no_ask,
        spread=decimal_to_str(best_prices.spread) or _spread_string(yes_bid, yes_ask),
        last_price_dollars=_decimal_string(market_json.get("last_price_dollars")),
        volume_fp=_decimal_string(market_json.get("volume_fp")),
        volume_24h_fp=_decimal_string(market_json.get("volume_24h_fp")),
        open_interest_fp=_decimal_string(market_json.get("open_interest_fp")),
        raw_market_json=encode_json(dict(market_json)),
        raw_orderbook_json=(
            encode_json(dict(orderbook_json)) if orderbook_json is not None else None
        ),
    )
    session.add(snapshot)
    session.flush()
    from kalshi_predictor.memory.capture import capture_market_snapshot

    capture_market_snapshot(session, snapshot)
    return snapshot


def insert_forecast(
    session: Session, forecast: Any, *, market_snapshot_id: int | None = None,
    attribution_enabled: bool | None = None,
) -> Forecast:
    payload = _to_mapping(forecast)
    record = Forecast(
        ticker=_required_str(payload, "ticker"),
        forecasted_at=_required_datetime(payload, "forecasted_at"),
        model_name=_required_str(payload, "model_name"),
        yes_probability=_required_decimal_string(payload, "yes_probability"),
        market_mid_probability=_decimal_string(payload.get("market_mid_probability")),
        best_yes_bid=_decimal_string(payload.get("best_yes_bid")),
        best_yes_ask=_decimal_string(payload.get("best_yes_ask")),
        feature_json=encode_json(payload.get("feature_json", {})),
        notes=_str_or_none(payload.get("notes")),
    )
    session.add(record)
    session.flush()
    if attribution_enabled is None:
        from kalshi_predictor.config import get_settings

        attribution_enabled = get_settings().runtime_provenance_dual_write_enabled
    from kalshi_predictor.provenance.dual_write import capture_forecast_provenance

    capture_forecast_provenance(
        session, record, payload, market_snapshot_id=market_snapshot_id,
        enabled=attribution_enabled,
    )
    from kalshi_predictor.memory.capture import capture_forecast_created

    capture_forecast_created(session, record)
    return record


def upsert_settlement(session: Session, market_json: Mapping[str, Any]) -> Settlement:
    ticker = _required_str(market_json, "ticker")
    now = utc_now()
    settlement = _pending_settlement(session, ticker) or session.get(Settlement, ticker)
    if settlement is None:
        settlement = Settlement(ticker=ticker, raw_json="{}", updated_at=now)
        session.add(settlement)

    result = _str_or_none(market_json.get("result"))
    yes_settlement_value = _settlement_value(market_json, result)
    settlement.settled_at = parse_datetime(
        _first_present(market_json, "settlement_ts", "settled_time", "settled_at")
    )
    settlement.result = result
    settlement.yes_settlement_value = yes_settlement_value
    settlement.raw_json = encode_json(dict(market_json))
    settlement.updated_at = now
    session.flush()
    from kalshi_predictor.memory.capture import capture_settlement_outcomes

    capture_settlement_outcomes(session, settlement)
    return settlement


def get_recent_snapshots(
    session: Session,
    *,
    ticker: str | None = None,
    limit: int = 100,
    since: datetime | None = None,
) -> list[MarketSnapshot]:
    statement: Select[tuple[MarketSnapshot]] = select(MarketSnapshot)
    if ticker:
        statement = statement.where(MarketSnapshot.ticker == ticker)
    if since:
        statement = statement.where(MarketSnapshot.captured_at >= since)
    statement = statement.order_by(desc(MarketSnapshot.captured_at)).limit(limit)
    return list(session.scalars(statement))


def get_forecasts_with_settlements(
    model_name: str,
    *,
    session: Session | None = None,
) -> list[tuple[Forecast, Settlement]]:
    if session is None:
        engine = init_db()
        session_factory = get_session_factory(engine)
        with session_factory() as owned_session:
            return get_forecasts_with_settlements(model_name, session=owned_session)

    rows = session.execute(
        select(Forecast, Settlement)
        .join(Settlement, Forecast.ticker == Settlement.ticker)
        .where(Forecast.model_name == model_name)
        .order_by(Forecast.forecasted_at)
    ).all()
    return [(forecast, settlement) for forecast, settlement in rows]


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return cast(Mapping[str, Any], asdict(value))
    if isinstance(value, Mapping):
        return value
    attrs = {
        key: getattr(value, key)
        for key in dir(value)
        if not key.startswith("_") and not callable(getattr(value, key))
    }
    return attrs


def _pending_market(session: Session, ticker: str) -> Market | None:
    for item in session.new:
        if isinstance(item, Market) and item.ticker == ticker:
            return item
    return None


def _pending_settlement(session: Session, ticker: str) -> Settlement | None:
    for item in session.new:
        if isinstance(item, Settlement) and item.ticker == ticker:
            return item
    return None


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _required_str(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if value is None or str(value) == "":
        raise ValueError(f"Missing required field: {key}")
    return str(value)


def _required_datetime(mapping: Mapping[str, Any], key: str) -> datetime:
    value = mapping.get(key)
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Missing required datetime field: {key}")
    return parsed


def _required_decimal_string(mapping: Mapping[str, Any], key: str) -> str:
    value = _decimal_string(mapping.get(key))
    if value is None:
        raise ValueError(f"Missing required decimal field: {key}")
    return value


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _decimal_string(value: Any) -> str | None:
    return decimal_to_str(value)


def _spread_string(yes_bid: str | None, yes_ask: str | None) -> str | None:
    bid = to_decimal(yes_bid)
    ask = to_decimal(yes_ask)
    if bid is None or ask is None:
        return None
    return decimal_to_str(ask - bid)


def _settlement_value(market_json: Mapping[str, Any], result: str | None) -> str | None:
    explicit_value = _decimal_string(
        _first_present(
            market_json,
            "yes_settlement_value",
            "settlement_value_dollars",
            "settlement_value",
        )
    )
    if explicit_value is not None:
        return explicit_value
    if result is None:
        return None
    normalized = result.lower()
    if normalized in {"yes", "y", "1", "true"}:
        return "1"
    if normalized in {"no", "n", "0", "false"}:
        return "0"
    return None
