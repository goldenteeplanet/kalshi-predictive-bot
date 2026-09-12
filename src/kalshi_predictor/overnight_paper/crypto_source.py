"""Pure BTC Coinbase analytical inputs. Never CF observations or settlement truth."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from .source_health import aware

CLOCK_BASIS = "coinbase-btc-trade-closed-candles-v1"
VERIFIER = "coinbase-btc-analytical-v1"
BUNDLE_URL = "urn:kalshi-paper:coinbase-btc-analytical-v1"
TICKER_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
MAX_ORIGINAL_BYTES = 1_000_000
BTC_SERIES = frozenset({"KXBTC", "KXBTCD", "KXBTC15M"})


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _number(value: Any, *, positive: bool = True) -> Decimal:
    _need(
        not isinstance(value, bool) and isinstance(value, str | int | float), "COINBASE_NUMBER_TYPE"
    )
    result = Decimal(str(value))
    _need(
        result.is_finite() and (result > 0 if positive else result >= 0), "COINBASE_NUMBER_INVALID"
    )
    return result


def _original(
    item: dict[str, Any], url: str, params: dict[str, Any], at: datetime, now: datetime
) -> Any:
    _need(
        isinstance(item, dict)
        and item.get("url") == url
        and item.get("params") == params
        and item.get("method") == "GET"
        and item.get("status") == 200,
        "COINBASE_EXACT_ORIGINAL_ENDPOINT_REQUIRED",
    )
    encoded = item.get("payload_hex")
    if not isinstance(encoded, str):
        raise ValueError("COINBASE_ORIGINAL_SIZE")
    _need(
        0 < len(encoded) <= MAX_ORIGINAL_BYTES * 2,
        "COINBASE_ORIGINAL_SIZE",
    )
    raw = bytes.fromhex(encoded)
    _need(hashlib.sha256(raw).hexdigest() == item.get("sha256"), "COINBASE_ORIGINAL_HASH")
    received = aware(item["received_at"])
    _need(
        received <= at <= now and 0 <= (now - received).total_seconds() <= 60,
        "COINBASE_RECEIPT_VISIBILITY_OR_STALE",
    )

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, "COINBASE_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("COINBASE_NONFINITE_JSON")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def coinbase_candle_window(at: datetime, *, minutes: int = 180) -> dict[str, Any]:
    """Explicit closed-minute request range; does not relax response validation."""
    _need(type(minutes) is int and 3 <= minutes <= 300, "COINBASE_WINDOW_MINUTES")
    end = aware(at).astimezone(UTC).replace(second=0, microsecond=0)
    return {
        "granularity": 60,
        "start": (end - timedelta(minutes=minutes)).isoformat(),
        "end": end.isoformat(),
    }


def _candle_window(item: dict[str, Any]) -> tuple[dict[str, Any], datetime | None, datetime | None]:
    _need(isinstance(item, dict), "COINBASE_EXACT_ORIGINAL_ENDPOINT_REQUIRED")
    params = item.get("params")
    if not isinstance(params, dict):
        raise ValueError("COINBASE_EXACT_ORIGINAL_ENDPOINT_REQUIRED")
    if (
        isinstance(params, dict)
        and params == {"granularity": 60}
        and type(params["granularity"]) is int
    ):
        return params, None, None
    _need(
        isinstance(params, dict)
        and set(params) == {"granularity", "start", "end"}
        and type(params["granularity"]) is int
        and params["granularity"] == 60,
        "COINBASE_EXACT_ORIGINAL_ENDPOINT_REQUIRED",
    )
    _need(
        all(isinstance(params[k], str) and len(params[k]) <= 64 for k in ("start", "end")),
        "COINBASE_CANDLE_WINDOW_CLOCK",
    )
    start, end = aware(params["start"]), aware(params["end"])
    _need(
        all(
            t.utcoffset() == timedelta(0) and t.second == 0 and t.microsecond == 0
            for t in (start, end)
        ),
        "COINBASE_CANDLE_WINDOW_GRID",
    )
    _need(
        timedelta(minutes=3) <= end - start <= timedelta(minutes=300)
        and end <= aware(item["received_at"]),
        "COINBASE_CANDLE_WINDOW_BOUNDS",
    )
    return params, start, end


def verify_coinbase_source(
    source: dict[str, Any], *, decision_at: datetime, now: datetime
) -> dict[str, Any]:
    """Recompute exact inputs from originals at decision and activation clocks.

    Candle starts/end boundaries are interval timestamps, not publication clocks.
    Receipt proves collector availability only. Missing intervals are never filled.
    """
    at, current = aware(decision_at), aware(now)
    _need(
        source.get("clock_basis") == CLOCK_BASIS
        and source.get("url") == BUNDLE_URL
        and source.get("role") == "ANALYTICAL_SOURCE"
        and source.get("settlement_truth") is False
        and source.get("product_id") == "BTC-USD",
        "COINBASE_ANALYTICAL_SCOPE_REQUIRED",
    )
    _need(
        source.get("provider_generated_at") is None and source.get("provider_updated_at") is None,
        "COINBASE_INVENTED_PROVIDER_CLOCK",
    )
    body = source["body"]
    _need(
        isinstance(body, dict) and set(body) == {"ticker", "candles"},
        "COINBASE_ORIGINAL_PAIR_REQUIRED",
    )
    ticker = _original(body["ticker"], TICKER_URL, {}, at, current)
    candle_params, requested_start, requested_end = _candle_window(body["candles"])
    candles = _original(body["candles"], CANDLES_URL, candle_params, at, current)
    received = max(aware(body[key]["received_at"]) for key in ("ticker", "candles"))
    _need(
        aware(source["received_at"]) == aware(source["available_at"]) == received,
        "COINBASE_BUNDLE_RECEIPT_MISMATCH",
    )
    _need(isinstance(ticker, dict), "COINBASE_TICKER_BODY")
    trade_at = aware(ticker["time"])
    _need(
        trade_at <= aware(body["ticker"]["received_at"])
        and 0 <= (current - trade_at).total_seconds() <= 60,
        "COINBASE_TRADE_CLOCK_STALE_OR_FUTURE",
    )
    spot = _number(ticker["price"])
    _need(isinstance(candles, list) and 3 <= len(candles) <= 300, "COINBASE_CANDLE_COUNT")
    seen = set()
    selected = []
    excluded = []
    excluded_outside_window = []
    for index, row in enumerate(candles):
        _need(
            isinstance(row, list)
            and len(row) == 6
            and type(row[0]) is int
            and row[0] >= 0
            and row[0] % 60 == 0,
            "COINBASE_CANDLE_SHAPE_OR_GRID",
        )
        _need(row[0] not in seen, "COINBASE_DUPLICATE_CANDLE")
        seen.add(row[0])
        low, high, opened, close = (_number(v) for v in row[1:5])
        _number(row[5], positive=False)
        _need(low <= opened <= high and low <= close <= high, "COINBASE_CANDLE_OHLC")
        start = datetime.fromtimestamp(row[0], tz=trade_at.tzinfo)
        end = start + timedelta(seconds=60)
        _need(start <= aware(body["candles"]["received_at"]), "COINBASE_FUTURE_CANDLE")
        if (
            requested_start is not None
            and requested_end is not None
            and not (requested_start <= start and end <= requested_end)
        ):
            excluded_outside_window.append(index)
        elif end <= trade_at and end <= aware(body["candles"]["received_at"]):
            selected.append(row)
        else:
            excluded.append(index)
    selected.sort(key=lambda row: row[0])
    _need(len(selected) >= 3, "COINBASE_INSUFFICIENT_CLOSED_CANDLES")
    last_end = datetime.fromtimestamp(selected[-1][0], tz=trade_at.tzinfo) + timedelta(seconds=60)
    _need(0 <= (current - last_end).total_seconds() <= 1800, "COINBASE_CANDLE_HISTORY_STALE")
    view = dict(
        product_id="BTC-USD",
        spot=str(spot),
        trade_at=trade_at.isoformat(),
        candle_cutoff_at=last_end.isoformat(),
        closed_candles=selected,
        excluded_unclosed_row_indices=excluded,
        missing_minutes=sum(
            (b[0] - a[0]) // 60 - 1 for a, b in zip(selected, selected[1:], strict=False)
        ),
        original_hashes=[body[key]["sha256"] for key in ("ticker", "candles")],
    )
    if requested_start is not None and requested_end is not None:
        view.update(
            requested_start_at=requested_start.isoformat(),
            requested_end_at=requested_end.isoformat(),
            excluded_outside_request_row_indices=excluded_outside_window,
        )
    return {"inputs": view, "input_sha256": _hash(view), "available_at": received.isoformat()}


def build_coinbase_source(
    *,
    ticker_payload: bytes,
    ticker_receipt: dict[str, Any],
    candle_payload: bytes,
    candle_receipt: dict[str, Any],
    decision_at: datetime,
) -> dict[str, Any]:
    """Collector bridge using existing archived bytes/receipts; performs no I/O."""
    originals = {}
    for name, payload, receipt in (
        ("ticker", ticker_payload, ticker_receipt),
        ("candles", candle_payload, candle_receipt),
    ):
        _need(
            isinstance(payload, bytes) and 0 < len(payload) <= MAX_ORIGINAL_BYTES,
            "COINBASE_ORIGINAL_SIZE",
        )
        originals[name] = {
            key: receipt[key]
            for key in ("url", "params", "method", "status", "received_at", "sha256")
        }
        originals[name]["payload_hex"] = payload.hex()
    received = max(aware(item["received_at"]) for item in originals.values()).isoformat()
    source = dict(
        url=BUNDLE_URL,
        clock_basis=CLOCK_BASIS,
        role="ANALYTICAL_SOURCE",
        product_id="BTC-USD",
        settlement_truth=False,
        provider_generated_at=None,
        provider_updated_at=None,
        received_at=received,
        available_at=received,
        body=originals,
    )
    verify_coinbase_source(source, decision_at=decision_at, now=decision_at)
    return source


def verify_coinbase_binding(
    source: dict[str, Any],
    *,
    decision: dict[str, Any],
    now: datetime,
    forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Require exact BTC identity and bind recomputed inputs into actual artifacts."""
    series = decision.get("series")
    _need(
        decision.get("category") == "Crypto"
        and series in BTC_SERIES
        and isinstance(decision.get("event_id"), str)
        and decision["event_id"].startswith(series + "-")
        and isinstance(decision.get("ticker"), str)
        and decision["ticker"].startswith(decision["event_id"] + "-"),
        "COINBASE_EXACT_BTC_MARKET_REQUIRED",
    )
    verified = verify_coinbase_source(source, decision_at=aware(decision["decision_at"]), now=now)
    _need(
        decision.get("coinbase_input_sha256") == verified["input_sha256"],
        "COINBASE_DECISION_INPUT_BINDING",
    )
    if forecast is not None:
        _need(
            forecast.get("coinbase_input_sha256") == verified["input_sha256"]
            and aware(verified["available_at"]) <= aware(forecast["generated_at"]),
            "COINBASE_FORECAST_INPUT_BINDING",
        )
    return verified


def coinbase_feature_record(
    source: dict[str, Any], *, decision_at: datetime, now: datetime
) -> dict[str, Any]:
    """Build a raw analytical feature record; computes no forecast or calibration."""
    verified = verify_coinbase_source(source, decision_at=decision_at, now=now)
    return dict(
        name="coinbase_btc_inputs",
        value=verified["inputs"],
        source_sha256=_hash(source),
        observed_at=verified["inputs"]["trade_at"],
        available_at=verified["available_at"],
    )


def bind_coinbase_forecast_inputs(
    forecast: dict[str, Any], source: dict[str, Any], *, decision_at: datetime
) -> dict[str, Any]:
    """Bind an already computed forecast; never create a probability or model proof."""
    verified = verify_coinbase_source(source, decision_at=decision_at, now=decision_at)
    _need(
        _hash(source) in forecast.get("source_hashes", [])
        and aware(verified["available_at"])
        <= aware(forecast["generated_at"])
        <= aware(decision_at),
        "COINBASE_FORECAST_ORIGINAL_VISIBILITY",
    )
    return forecast | {"coinbase_input_sha256": verified["input_sha256"]}
