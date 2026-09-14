"""Pure prospective event selection; no acquisition, forecast, or execution authority."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from kalshi_predictor.crypto.cf_process_inputs import (
    INDEX,
    _json,
    _time,
    decode_cf_original,
    digest,
)
from kalshi_predictor.crypto.cost_evidence import OriginalBook, _unique
from kalshi_predictor.crypto.current_research_target import _bound
from kalshi_predictor.crypto.doge_strikes import parse_doge_strike

API = "https://external-api.kalshi.com/trade-api/v2"
SERIES = {"BTC": "KXBTC", "ETH": "KXETH", "SOL": "KXSOLE", "XRP": "KXXRP", "DOGE": "KXDOGE"}
POLICY = "NEAREST_FOUR_THEN_NEAREST_AND_METADATA_LIQUIDITY_V1"


@dataclass(frozen=True)
class CurrentEventDiscovery:
    original: OriginalBook
    receipt: bytes
    event_ticker: str


@dataclass(frozen=True)
class CurrentEventSelectionInputs:
    discovery: CurrentEventDiscovery
    cf_original: bytes
    cf_receipt: bytes
    protocol_original: bytes
    protocol_sha256: str
    selected_at: datetime


def seed_discovery_url(series: str, *, not_before: datetime, not_after: datetime) -> str:
    """Documented close filters omit status; returned status is checked separately."""
    if (
        series not in SERIES.values()
        or not_before.utcoffset() is None
        or not_after.utcoffset() is None
        or not not_before < not_after
        or not_before.microsecond != 0
        or not_after.microsecond != 0
    ):
        raise ValueError("EVENT_SEED_WINDOW_INVALID")
    return (
        API
        + "/markets?"
        + urlencode(
            dict(
                series_ticker=series,
                limit=1,
                min_close_ts=int(not_before.timestamp()),
                max_close_ts=int(not_after.timestamp()),
            )
        )
    )


def seed_event(
    original: OriginalBook,
    receipt_raw: bytes,
    *,
    series: str,
    not_before: datetime,
    not_after: datetime,
    assessed_at: datetime,
) -> str | None:
    """One observed candidate event; never claims global chronological coverage."""
    if (
        original.url != seed_discovery_url(series, not_before=not_before, not_after=not_after)
        or assessed_at.utcoffset() is None
        or original.received_at.utcoffset() is None
        or not 0 <= (assessed_at - original.received_at).total_seconds() <= 60
        or not 0 < len(original.payload) <= 1_000_000
        or len(receipt_raw) > 16_000
    ):
        raise ValueError("EVENT_SEED_ORIGINAL_INVALID")
    receipt = _json(receipt_raw, digest(receipt_raw))
    if (
        receipt.get("url") != original.url
        or receipt.get("method") != "GET"
        or type(receipt.get("http_status")) is not int
        or receipt["http_status"] != 200
        or receipt.get("original_complete") is not True
        or receipt.get("source_sha256") != original.sha256
        or _time(receipt["received_at"]) != original.received_at
        or not _time(receipt["requested_at"]) <= original.received_at
    ):
        raise ValueError("EVENT_SEED_RECEIPT_INVALID")
    body = json.loads(original.payload, object_pairs_hook=_unique)
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("markets"), list)
        or len(body["markets"]) > 1
    ):
        raise ValueError("EVENT_SEED_ROW_BOUND")
    if not body["markets"]:
        return None
    market = body["markets"][0]
    if not isinstance(market, dict):
        raise ValueError("EVENT_SEED_MARKET_OBJECT")
    event = market.get("event_ticker")
    if not isinstance(event, str):
        raise ValueError("EVENT_SEED_EVENT_IDENTITY")
    event_discovery_url(series, event)
    if (
        market.get("status") not in ("open", "active")
        or not isinstance(market.get("ticker"), str)
        or not market["ticker"].startswith(event + "-")
        or not not_before <= _time(market.get("close_time")) <= not_after
        or not 1200 <= (_time(market.get("close_time")) - assessed_at).total_seconds() <= 4800
    ):
        raise ValueError("EVENT_SEED_TARGET_NOT_ELIGIBLE")
    return event


def event_discovery_url(series: str, event: str) -> str:
    if (
        series not in SERIES.values()
        or not isinstance(event, str)
        or not re.fullmatch(re.escape(series) + r"-[A-Z0-9]+", event)
    ):
        raise ValueError("EVENT_DISCOVERY_SCOPE_INVALID")
    return API + "/markets?" + urlencode(dict(event_ticker=event, status="open", limit=200))


def event_discovery_rows(
    evidence: CurrentEventDiscovery,
    *,
    series: str,
    assessed_at: datetime,
) -> dict[str, Any]:
    """Only complete, exact-event originals enter the universe; no cursor fallback."""
    if type(evidence) is not CurrentEventDiscovery or assessed_at.utcoffset() is None:
        raise ValueError("EVENT_DISCOVERY_ORIGINAL_REQUIRED")
    original, event = evidence.original, evidence.event_ticker
    if (
        original.url != event_discovery_url(series, event)
        or original.received_at.utcoffset() is None
        or not 0 <= (assessed_at - original.received_at).total_seconds() <= 300
        or not 0 < len(original.payload) <= 2_000_000
        or len(evidence.receipt) > 16_000
    ):
        raise ValueError("EVENT_DISCOVERY_ORIGINAL_FRESHNESS_OR_BOUND")
    receipt = _json(evidence.receipt, digest(evidence.receipt))
    if (
        receipt.get("method") != "GET"
        or type(receipt.get("http_status")) is not int
        or receipt["http_status"] != 200
        or receipt.get("original_complete") is not True
        or receipt.get("source_sha256") != original.sha256
        or receipt.get("url") != original.url
        or _time(receipt["received_at"]) != original.received_at
        or not _time(receipt["requested_at"]) <= original.received_at
    ):
        raise ValueError("EVENT_DISCOVERY_RECEIPT_INVALID")
    body = json.loads(original.payload, object_pairs_hook=_unique)
    if not isinstance(body, dict) or body.get("cursor", "") != "":
        raise ValueError("EVENT_DISCOVERY_INCOMPLETE")
    markets = body.get("markets")
    if not isinstance(markets, list) or len(markets) > 200:
        raise ValueError("EVENT_DISCOVERY_ROW_BOUND")
    rows, seen, closes = [], set(), set()
    source = {
        "url": original.url,
        "sha256": original.sha256,
        "receipt_sha256": digest(evidence.receipt),
        "received_at": original.received_at.isoformat(),
        "page_index": 0,
    }
    for market in markets:
        if not isinstance(market, dict):
            raise ValueError("EVENT_DISCOVERY_MARKET_OBJECT")
        ticker = market.get("ticker")
        if (
            not isinstance(ticker, str)
            or not ticker.startswith(event + "-")
            or ticker in seen
            or market.get("event_ticker") != event
            or market.get("series_ticker", series) != series
            or market.get("market_type") != "binary"
            or market.get("status") not in ("active", "open")
        ):
            raise ValueError("EVENT_DISCOVERY_IDENTITY_OR_DUPLICATE")
        close = _time(market.get("close_time"))
        if not 600 < (close - assessed_at).total_seconds() <= 72 * 3600:
            raise ValueError("EVENT_DISCOVERY_OBSERVATION_HORIZON")
        seen.add(ticker)
        closes.add(close)
        rows.append({"market": market, "sources": [source], "conflicting_sightings": False})
    if len(closes) > 1:
        raise ValueError("EVENT_DISCOVERY_MIXED_CLOSE_TIMES")
    return {
        "rows": rows,
        "pages": [source | {"rows_returned": len(rows), "has_next_page": False}],
        "next_cursor": "",
        "pagination_incomplete": False,
        "stop_reason": "COMPLETE_EXACT_EVENT",
        "raw_rows_returned": len(rows),
        "unique_markets": len(rows),
        "universe_scope": "EXACT_OBSERVED_EVENT_NOT_ALL_FAMILY_EVENTS",
    }


def _number(value: Any) -> Decimal:
    if type(value) not in (str, int, float) or len(str(value)) > 100:
        raise ValueError("SELECTION_FINITE_DECIMAL_REQUIRED")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("SELECTION_FINITE_DECIMAL_REQUIRED") from exc
    if not result.is_finite():
        raise ValueError("SELECTION_FINITE_DECIMAL_REQUIRED")
    return result


def _geometry(
    market: dict,
    level: Decimal,
    *,
    asset: str,
    as_of: datetime,
) -> tuple[Decimal, Decimal]:
    metadata = json.loads(json.dumps(market), parse_float=Decimal)
    if asset == "DOGE":
        parsed = parse_doge_strike(metadata, cutoff=as_of)
        kind, lower, upper = parsed.operator, parsed.floor, parsed.cap
    else:
        if metadata.get("custom_strike") not in (None, {}):
            raise ValueError("UNSUPPORTED_CUSTOM_STRIKE")
        kind = metadata.get("strike_type")
        lower, upper = (_bound(metadata.get(k)) for k in ("floor_strike", "cap_strike"))
    if kind == "between":
        if lower is None or upper is None or not 0 < lower < upper:
            raise ValueError("SELECTION_INVALID_INTERVAL")
        return max(lower - level, level - upper, Decimal(0)) / level, abs(
            (lower + upper) / 2 - level
        ) / level
    if kind in ("greater", "less"):
        strike = lower if kind == "greater" else upper
        other = upper if kind == "greater" else lower
        if strike is None or strike <= 0 or other is not None:
            raise ValueError("SELECTION_INVALID_THRESHOLD")
        distance = abs(strike - level) / level
        return distance, distance
    raise ValueError("SELECTION_UNSUPPORTED_GEOMETRY")


def select_near_benchmark_contracts(
    *,
    discovery: CurrentEventDiscovery,
    asset: str,
    cf_original: bytes,
    cf_receipt: bytes,
    protocol_original: bytes,
    protocol_sha256: str,
    selected_at: datetime,
) -> dict[str, Any]:
    """Freeze a deterministic shortlist before books/target-bound forecast requests.

    Caller must durably record protocol before acquisition and this result before
    books. Local receipt clocks are bindings, not independent attestations.
    """
    if asset not in SERIES or selected_at.utcoffset() is None:
        raise ValueError("SELECTION_ASSET_OR_CLOCK")
    plan = _json(protocol_original, protocol_sha256)
    if (
        set(plan)
        != {
            "schema",
            "asset",
            "event_ticker",
            "declared_at",
            "not_before",
            "not_after",
            "policy",
            "max_contracts",
            "shortlist_size",
        }
        or plan["schema"] != "current-event-selection-protocol-v1"
        or plan["asset"] != asset
        or plan["event_ticker"] != discovery.event_ticker
        or plan["policy"] != POLICY
        or type(plan["max_contracts"]) is not int
        or plan["max_contracts"] != 2
        or type(plan["shortlist_size"]) is not int
        or plan["shortlist_size"] != 4
    ):
        raise ValueError("SELECTION_PREDECLARED_POLICY_REQUIRED")
    declared, start, end = (_time(plan[k]) for k in ("declared_at", "not_before", "not_after"))
    if not declared <= start <= selected_at < end or not 0 < (end - start).total_seconds() <= 180:
        raise ValueError("SELECTION_PROTOCOL_CLOCK")
    universe = event_discovery_rows(discovery, series=SERIES[asset], assessed_at=selected_at)
    dr = _json(discovery.receipt, digest(discovery.receipt))
    receipt = _json(cf_receipt, digest(cf_receipt))
    if (
        len(cf_original) > 500_000
        or len(cf_receipt) > 16_000
        or receipt.get("schema") != "cf-response-receipt-v1"
        or receipt.get("method") != "GET"
        or type(receipt.get("http_status")) is not int
        or receipt["http_status"] != 200
        or receipt.get("profile") != "LATEST_1HZ"
        or receipt.get("index_id") != INDEX[asset]
        or receipt.get("source_sha256") != digest(cf_original)
    ):
        raise ValueError("SELECTION_CF_RECEIPT")
    requested, received, recorded = (
        _time(receipt[k]) for k in ("requested_at", "received_at", "recorded_at")
    )
    if not (
        start
        <= _time(dr["requested_at"])
        <= discovery.original.received_at
        <= requested
        <= received
        <= recorded
        <= selected_at
    ):
        raise ValueError("SELECTION_ACQUISITION_CHRONOLOGY")
    decoded = decode_cf_original(
        cf_original,
        sha256=digest(cf_original),
        request_url=receipt["url"],
        index_id=INDEX[asset],
        profile="LATEST_1HZ",
    )
    observed = datetime.fromtimestamp(decoded.timestamps_ms[-1] / 1000, UTC)
    if (
        not observed <= decoded.server_time <= received
        or not 0 <= (selected_at - observed).total_seconds() <= 60
    ):
        raise ValueError("SELECTION_CF_FUTURE_OR_STALE")
    level = decoded.values[-1]
    candidates, exclusions = [], []
    for row in universe["rows"]:
        market = row["market"]
        try:
            distance, center = _geometry(market, level, asset=asset, as_of=selected_at)
        except ValueError as exc:
            exclusions.append({"ticker": market["ticker"], "reason": str(exc)})
            continue
        bid, ask = None, None
        try:
            bid, ask = (
                _number(market.get("yes_bid_dollars")),
                _number(market.get("yes_ask_dollars")),
            )
        except ValueError:
            pass
        two_sided = bid is not None and ask is not None and 0 < bid < ask < 1
        spread = ask - bid if two_sided and ask is not None and bid is not None else Decimal(1)
        candidates.append(
            {
                "ticker": market["ticker"],
                "distance": str(distance),
                "center_distance": str(center),
                "metadata_two_sided": two_sided,
                "metadata_spread": str(spread) if two_sided else None,
            }
        )
    candidates.sort(
        key=lambda r: (Decimal(r["distance"]), Decimal(r["center_distance"]), r["ticker"])
    )
    shortlist = candidates[:4]
    chosen = shortlist[:1]
    if len(shortlist) > 1:
        chosen.append(
            min(
                shortlist[1:],
                key=lambda r: (
                    not r["metadata_two_sided"],
                    Decimal(r["metadata_spread"] or "1"),
                    Decimal(r["distance"]),
                    Decimal(r["center_distance"]),
                    r["ticker"],
                ),
            )
        )
    return {
        "schema": "current-event-selection-manifest-v1",
        "selected_at": selected_at.isoformat(),
        "protocol_original_json": protocol_original.decode(),
        "protocol_sha256": protocol_sha256,
        "event_original_json": discovery.original.payload.decode(),
        "event_receipt_original_json": discovery.receipt.decode(),
        "event_sha256": discovery.original.sha256,
        "event_receipt_sha256": digest(discovery.receipt),
        "cf_sha256": digest(cf_original),
        "cf_receipt_sha256": digest(cf_receipt),
        "benchmark_level": str(level),
        "benchmark_observed_at": observed.isoformat(),
        "universe_count": len(universe["rows"]),
        "scores": candidates,
        "exclusions": sorted(exclusions, key=lambda r: r["ticker"]),
        "shortlist": [r["ticker"] for r in shortlist],
        "selected": [r["ticker"] for r in chosen],
        "book_selection_frozen": True,
        "liquidity_verified": False,
        "forecast_cf_requires_new_target_bound_request": True,
        "paper_eligible": False,
        "execution_authority": False,
        "external_timestamp_attestation": False,
    }


def selected_event_rows(
    inputs: CurrentEventSelectionInputs,
    *,
    asset: str,
    assessed_at: datetime,
) -> dict[str, Any]:
    """Replayed fixed selection subset plus complete immutable universe manifest."""
    if (
        type(inputs) is not CurrentEventSelectionInputs
        or assessed_at.utcoffset() is None
        or inputs.selected_at.utcoffset() is None
        or inputs.selected_at > assessed_at
    ):
        raise ValueError("EVENT_SELECTION_INPUTS_OR_CLOCK")
    manifest = select_near_benchmark_contracts(
        discovery=inputs.discovery,
        asset=asset,
        cf_original=inputs.cf_original,
        cf_receipt=inputs.cf_receipt,
        protocol_original=inputs.protocol_original,
        protocol_sha256=inputs.protocol_sha256,
        selected_at=inputs.selected_at,
    )
    universe = event_discovery_rows(inputs.discovery, series=SERIES[asset], assessed_at=assessed_at)
    selected = set(manifest["selected"])
    return {
        **universe,
        "rows": [row for row in universe["rows"] if row["market"]["ticker"] in selected],
        "selection_manifest": manifest,
        "selection_universe_count": universe["unique_markets"],
        "assessment_scope": "PREDECLARED_SELECTED_SUBSET_OF_COMPLETE_EVENT",
        "selected_markets": len(selected),
    }
