"""Bounded unauthenticated discovery; archived diagnostics never authorize a trade."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from kalshi_predictor.active_universe import is_active_market_status
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.assets import supported_crypto_asset, symbol_from_event_ticker
from kalshi_predictor.crypto.distribution_model import (
    DistributionInputs,
    inputs_from_features,
    threshold_probability,
)
from kalshi_predictor.crypto.features import calculate_crypto_features
from kalshi_predictor.crypto.semantics import parse_crypto_market_terms
from kalshi_predictor.data.schema import CryptoPrice, Market
from kalshi_predictor.opportunities.scanner import top5_orderbook_notional
from kalshi_predictor.opportunities.scoring import score_liquidity
from kalshi_predictor.overnight_paper.books import qualify_book
from kalshi_predictor.utils.time import parse_datetime

BASE = "https://external-api.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com"
SUPPORTED_CATEGORIES = {"Crypto", "Climate and Weather", "Sports", "Economics"}
BUCKETS = (
    (1, "<=1h"),
    (3, "1-3h"),
    (6, "3-6h"),
    (12, "6-12h"),
    (24, "12-24h"),
    (48, "24-48h"),
    (72, "48-72h"),
)


class PublicArchive:
    """Fixed-origin GET only. No credentials, redirects, retries or exchange SDK."""

    def __init__(self, root: Path, *, max_requests: int = 150, seconds: int = 600):
        root.mkdir(parents=True, exist_ok=False)
        self.root = root
        self.max_requests = max_requests
        self.deadline = time.monotonic() + seconds
        self.receipts: list[dict[str, Any]] = []
        self.rate_limited = False
        self.next_request_at = 0.0

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.rate_limited:
            raise RuntimeError("PUBLIC_RATE_LIMITED_CAPTURE_STOPPED")
        if len(self.receipts) >= self.max_requests or time.monotonic() >= self.deadline:
            raise RuntimeError("PUBLIC_REQUEST_BUDGET_EXHAUSTED")
        kalshi = bool(
            re.fullmatch(
                r"/(?:markets|series|events)(?:/[A-Za-z0-9_.-]+)?"
                r"(?:/orderbook)?",
                path,
            )
        )
        coinbase = bool(
            re.fullmatch(
                r"/products/(?:BTC|ETH|SOL|XRP|DOGE)-USD/"
                r"(?:ticker|candles)",
                path,
            )
        )
        weather = bool(
            re.fullmatch(
                r"https://api\.weather\.gov/(?:stations/KNYC|points/-?\d+\.\d+,-?\d+\.\d+"
                r"|gridpoints/OKX/\d+,\d+/forecast/hourly)",
                path,
            )
        )
        if not kalshi and not coinbase and not weather:
            raise ValueError("Endpoint is not an allowed public evidence GET")
        # Pace acquisition, and never work around a 429 by trying another endpoint.
        delay = max(0.0, self.next_request_at - time.monotonic())
        if time.monotonic() + delay >= self.deadline:
            raise RuntimeError("PUBLIC_REQUEST_BUDGET_EXHAUSTED")
        if delay:
            time.sleep(delay)
        if time.monotonic() >= self.deadline:
            raise RuntimeError("PUBLIC_REQUEST_BUDGET_EXHAUSTED")
        self.next_request_at = time.monotonic() + 0.5
        url = path if weather else (BASE if kalshi else COINBASE) + path
        receipt: dict[str, Any] = {"url": url, "params": params or {}, "method": "GET"}
        self.receipts.append(receipt)
        try:
            remaining = max(0.1, self.deadline - time.monotonic())
            with httpx.Client(
                trust_env=False, follow_redirects=False, timeout=min(15, remaining)
            ) as client:
                with client.stream("GET", url, params=params) as response:
                    if response.status_code == 429:
                        self.rate_limited = True
                        receipt["retry_after"] = response.headers.get("Retry-After")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if time.monotonic() > self.deadline:
                            raise RuntimeError("PUBLIC_DEADLINE_EXCEEDED")
                        data.extend(chunk)
                        if len(data) > 20_000_000:
                            raise RuntimeError("PUBLIC_RESPONSE_TOO_LARGE")
                    receipt["status"] = response.status_code
                    receipt["received_at"] = datetime.now(UTC).isoformat()
                    file = self.root / f"response-{len(self.receipts):03d}.json"
                    file.write_bytes(data)
                    receipt.update(path=str(file), sha256=hashlib.sha256(data).hexdigest())
                    response.raise_for_status()
                    return json.loads(data)
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            (self.root / "requests.json").write_text(
                json.dumps(self.receipts, indent=2), encoding="utf-8"
            )


def settlement_bucket(hours: float) -> str | None:
    if hours <= 0:
        return None
    return next((label for upper, label in BUCKETS if hours <= upper), None)


def run_discovery(
    archive_root: Path,
    *,
    max_pages: int = 40,
    max_book_requests: int = 24,
    timeout_seconds: int = 600,
    resume_from: Path | None = None,
) -> dict[str, Any]:
    if not 1 <= max_pages <= 100 or not 1 <= max_book_requests <= 100:
        raise ValueError("Discovery budgets must be 1..100")
    public = PublicArchive(
        archive_root, max_requests=max_pages + 2 * max_book_requests + 80, seconds=timeout_seconds
    )
    now = datetime.now(UTC)
    # BaseSettings accepts this runtime override; the generated field-only mypy
    # constructor signature does not include inherited settings control keywords.
    settings_overrides: dict[str, Any] = {"_env_file": None}
    settings = Settings(**settings_overrides)
    payload: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "archive_root": str(archive_root),
        "mode": "OBSERVATION_ONLY",
        "orders_created": 0,
        "eligible_candidates": [],
        "rows": [],
        "errors": [],
        "coverage": {},
        "documentation": "https://docs.kalshi.com/api-reference/market/get-markets",
    }
    cursor = ""
    pages = 0
    markets: dict[str, dict[str, Any]] = {}
    catalog: dict[str, dict[str, Any]] = {}
    complete = False
    window_start = now
    inherited_receipts = []
    if resume_from:
        prior = json.loads((resume_from / "universe.json").read_text(encoding="utf-8"))
        window_start = _required_time(
            prior.get("coverage", {}).get("window_start") or prior["generated_at"],
            "resume_window_start",
        )
        cursor = str(prior["coverage"].get("resume_cursor") or "")
        complete = prior["coverage"]["pagination_complete"]
        pages = prior["coverage"]["pages"]
        inherited_receipts = prior.get("inherited_market_receipts", []) + json.loads(
            (resume_from / "requests.json").read_text(encoding="utf-8")
        )
        inherited_receipts = [
            item
            for item in inherited_receipts
            if item["url"] == BASE + "/markets" and item.get("status") == 200
        ]
        for receipt in inherited_receipts:
            data = Path(receipt["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != receipt["sha256"]:
                raise ValueError("RESUME_EVIDENCE_HASH_MISMATCH")
            for market in json.loads(data).get("markets", []):
                markets[market["ticker"]] = market
    payload["inherited_market_receipts"] = inherited_receipts
    try:
        series = public.get("/series")
        catalog = {row["ticker"]: row for row in series.get("series", [])}
        for _ in range(max_pages):
            if complete:
                break
            params = {
                "limit": 1000,
                "mve_filter": "exclude",
                "min_close_ts": int(window_start.timestamp()),
                "max_close_ts": int((window_start + timedelta(hours=72)).timestamp()),
            }
            if cursor:
                params["cursor"] = cursor
            page = public.get("/markets", params)
            pages += 1
            for market in page.get("markets", []):
                markets[market["ticker"]] = market
            next_cursor = str(page.get("cursor") or "")
            if not next_cursor:
                complete = True
                cursor = ""
                break
            if next_cursor == cursor:
                raise RuntimeError("REPEATED_PUBLIC_CURSOR")
            cursor = next_cursor
    except Exception as exc:
        payload["errors"].append(str(exc))
    fast_series = [
        s
        for s in catalog.values()
        if s.get("category") == "Crypto"
        and s.get("frequency") in {"fifteen_min", "hourly"}
        and symbol_from_event_ticker(s["ticker"])
    ]
    fast_coverage = []
    for series_meta in sorted(
        fast_series, key=lambda s: (s.get("frequency") != "fifteen_min", s["ticker"])
    )[:40]:
        try:
            page = public.get(
                "/markets",
                {"series_ticker": series_meta["ticker"], "status": "open", "limit": 1000},
            )
            fast_coverage.append(
                {
                    "series": series_meta["ticker"],
                    "count": len(page.get("markets", [])),
                    "resume_cursor": page.get("cursor") or None,
                }
            )
            for raw in page.get("markets", []):
                markets[raw["ticker"]] = raw
        except Exception as exc:
            payload["errors"].append(f"FAST_SERIES:{series_meta['ticker']}:{exc}")
            break
    event_rows: dict[str, dict[str, Any]] = {}
    for raw in markets.values():
        if not is_active_market_status(raw.get("status")):
            continue
        event = str(raw.get("event_ticker") or "")
        # Catalog prefix is only a discovery hint; exact event metadata is checked below.
        series_hint = next(
            (key for key in (event.rsplit("-", 1)[0], event.split("-", 1)[0]) if key in catalog), ""
        )
        series_meta = catalog.get(series_hint, {})
        if series_meta.get("category") not in SUPPORTED_CATEGORIES:
            continue
        close = parse_datetime(raw.get("close_time"))
        expiration = parse_datetime(raw.get("expected_expiration_time"))
        if close is None or close <= now:
            continue
        eta = expiration or close
        bucket = settlement_bucket((eta - now).total_seconds() / 3600)
        if not bucket or raw.get("mve_selected_legs") or raw.get("market_type") != "binary":
            continue
        row = candidate_row(raw, series_meta, now)
        previous = event_rows.get(event)
        # Keep one representative threshold per event, prefer observed activity, never invented EV.
        if previous is None or Decimal(str(raw.get("volume_fp") or 0)) > Decimal(
            str(previous["raw_market"].get("volume_fp") or 0)
        ):
            event_rows[event] = row
    rows = sorted(
        event_rows.values(),
        key=lambda row: (row["settlement_eta_hours"], row["category"] != "Crypto", row["ticker"]),
    )
    # Round-robin categories gives crypto/weather/sports/economics a fair bounded book sample.
    queues = {
        category: [
            row
            for row in rows
            if row["category"] == category and row.get("research_supported", True)
        ]
        for category in sorted(SUPPORTED_CATEGORIES)
    }
    selected: list[dict[str, Any]] = []
    while any(queues.values()) and len(selected) < max_book_requests:
        for queue in queues.values():
            if queue and len(selected) < max_book_requests:
                selected.append(queue.pop(0))
    crypto_sources: dict[str, Any] = {}
    for row in selected:
        try:
            if _required_time(row["raw_market"].get("close_time"), "close_time") <= datetime.now(
                UTC
            ):
                row["first_blocker"] = "MARKET_CLOSED_DURING_SCAN"
                continue
            event_data = public.get("/events/" + quote(row["event"], safe=""))
            verified_event = event_data.get("event", {})
            series_ticker = verified_event.get("series_ticker")
            row["identity_verified"] = bool(
                verified_event.get("event_ticker") == row["event"]
                and series_ticker == row["series"]
                and series_ticker in catalog
            )
            row["identity_evidence"] = public.receipts[-1]
            raw_book = public.get(
                "/markets/" + quote(row["ticker"], safe="") + "/orderbook", {"depth": 5}
            )
            receipt = public.receipts[-1]
            raw = row["raw_market"]
            depth = top5_orderbook_notional(
                ticker=row["ticker"], raw_orderbook_json=json.dumps(raw_book)
            )
            liquidity = score_liquidity(
                volume=raw.get("volume_fp"),
                open_interest=raw.get("open_interest_fp"),
                liquidity=max(Decimal(str(raw.get("liquidity_dollars") or 0)), depth),
            )
            book = qualify_book(
                raw_book,
                received_at=datetime.fromisoformat(receipt["received_at"]),
                now=datetime.now(UTC),
                max_spread=settings.opportunity_max_spread,
                liquidity_score=liquidity,
                price_ranges=raw.get("price_ranges"),
            )
            row.update(
                book=book,
                book_evidence=receipt,
                executable_price=book["yes_ask"],
                spread=book["sides"]["YES"]["spread"],
                depth_liquidity=str(depth),
            )
            row["first_blocker"] = (
                "UNVERIFIED_MARKET_IDENTITY"
                if not row["identity_verified"]
                else "SETTLEMENT_RULE_UNCERTIFIED"
                if book["executable"]
                else book["sides"]["YES"]["first_blocker"]
            )
            if row.get("semantic_conflicts"):
                row["first_blocker"] = "SEMANTIC_PARSER_CONFLICT"
            symbol = row.get("crypto_terms", {}).get("symbol")
            if row["category"] == "Crypto" and supported_crypto_asset(symbol or ""):
                if symbol not in crypto_sources:
                    crypto_sources[symbol] = collect_crypto_source(public, symbol)
                row["external_source"] = crypto_sources[symbol]
                add_crypto_research(row, crypto_sources[symbol], datetime.now(UTC))
        except Exception as exc:
            row["first_blocker"] = "PUBLIC_EVIDENCE_ERROR"
            row["error"] = str(exc)
    payload["rows"] = rows
    payload["external_sources"] = crypto_sources
    payload["coverage"] = {
        "scope": "All non-MVE public markets with close time in the next 72h",
        "window_start": window_start.isoformat(),
        "window_end": (window_start + timedelta(hours=72)).isoformat(),
        "fast_series_supplement": fast_coverage,
        "category_scope": sorted(SUPPORTED_CATEGORIES),
        "catalog_series": len(catalog),
        "supported_series": sum(
            s.get("category") in SUPPORTED_CATEGORIES for s in catalog.values()
        ),
        "pages": pages,
        "markets_scanned": len(markets),
        "pagination_complete": complete,
        "resume_cursor": cursor or None,
        "events_represented": len(rows),
        "book_requests_attempted": len(selected),
        "executable_books": sum(bool(row.get("book", {}).get("executable")) for row in rows),
        "note": "Category membership is discovery scope, not certified model/settlement support. "
        "Close time is only an ETA fallback, not guaranteed settlement time. "
        "One threshold per event; other thresholds and unprobed books are not qualified.",
        "buckets": dict(Counter(row["settlement_bucket"] for row in rows)),
    }
    payload["finished_at"] = datetime.now(UTC).isoformat()
    (archive_root / "universe.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    return payload


def _required_time(value: Any, field: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"MISSING_OR_INVALID_TIMESTAMP:{field}")
    return parsed


def candidate_row(raw: dict[str, Any], series: dict[str, Any], now: datetime) -> dict[str, Any]:
    eta = _required_time(
        raw.get("expected_expiration_time") or raw.get("close_time"), "settlement_eta"
    )
    hours = (eta - now).total_seconds() / 3600
    row = {
        "ticker": raw["ticker"],
        "event": raw.get("event_ticker"),
        "series": series["ticker"],
        "category": series["category"],
        "settlement_eta": eta.isoformat(),
        "settlement_eta_source": (
            "expected_expiration_time" if raw.get("expected_expiration_time") else "close_time"
        ),
        "settlement_eta_certified": False,
        "close_time": raw.get("close_time"),
        "latest_expiration_time": raw.get("latest_expiration_time"),
        "max_settlement_delay_hours": (
            (
                _required_time(raw["latest_expiration_time"], "latest_expiration_time") - now
            ).total_seconds()
            / 3600
            if raw.get("latest_expiration_time")
            else None
        ),
        "settlement_eta_hours": hours,
        "settlement_bucket": settlement_bucket(hours),
        "model": None,
        "forecast": None,
        "executable_price": None,
        "spread": None,
        "depth_liquidity": None,
        "gross_ev": None,
        "fees": None,
        "slippage_estimate": None,
        "net_ev": None,
        "source_freshness": "UNVERIFIED",
        "settlement_rule_status": "UNCERTIFIED",
        "paper_readiness": "PAPER_NOT_READY",
        "first_blocker": "BOOK_NOT_SAMPLED",
        "settlement_sources": series.get("settlement_sources"),
        "contract_terms_url": series.get("contract_terms_url"),
        "raw_market": raw,
    }
    if series["category"] == "Crypto":
        market = Market(
            ticker=raw["ticker"],
            event_ticker=raw.get("event_ticker"),
            series_ticker=series["ticker"],
            title=raw.get("title"),
            rules_primary=raw.get("rules_primary"),
            raw_json=json.dumps(raw),
        )
        row["crypto_terms"] = parse_crypto_market_terms(market).as_payload()
        terms = row["crypto_terms"]
        row["research_supported"] = bool(
            terms["status"] == "EXACT_LINK"
            and supported_crypto_asset(terms["symbol"] or "")
            and len(terms["components"]) == 1
            and symbol_from_event_ticker(series["ticker"]) == terms["symbol"]
        )
        row["semantic_conflicts"] = []
        if any(s.get("name") == "CF Benchmarks" for s in series.get("settlement_sources", [])):
            if terms["reference_price_source"] == "coinbase":
                row["semantic_conflicts"].append("CF_SETTLEMENT_MISCLASSIFIED_AS_COINBASE")
        if raw.get("strike_type") == "greater_or_equal" and terms["components"]:
            if terms["components"][0]["comparator"] == "ABOVE":
                row["semantic_conflicts"].append("INCLUSIVE_COMPARATOR_MISCLASSIFIED_AS_STRICT")
        if not row["research_supported"]:
            row["first_blocker"] = "UNSUPPORTED_CRYPTO_SEMANTICS_OR_ASSET"
    return row


def collect_crypto_source(public: PublicArchive, symbol: str) -> dict[str, Any]:
    """Closed Coinbase candles are analytical evidence, never CF settlement truth."""
    ticker = public.get(f"/products/{symbol}-USD/ticker")
    ticker_receipt = dict(public.receipts[-1])
    candles = public.get(f"/products/{symbol}-USD/candles", {"granularity": 60})
    captured = datetime.now(UTC)
    analytical_source = None
    analytical_inputs = None
    feature_candles = candles
    if symbol == "BTC":
        from .crypto_source import MAX_ORIGINAL_BYTES, build_coinbase_source, verify_coinbase_source

        with Path(ticker_receipt["path"]).open("rb") as original:
            ticker_raw = original.read(MAX_ORIGINAL_BYTES + 1)
        candle_receipt = dict(public.receipts[-1])
        with Path(candle_receipt["path"]).open("rb") as original:
            candle_raw = original.read(MAX_ORIGINAL_BYTES + 1)
        analytical_source = build_coinbase_source(
            ticker_payload=ticker_raw,
            ticker_receipt=ticker_receipt,
            candle_payload=candle_raw,
            candle_receipt=candle_receipt,
            decision_at=captured,
        )
        analytical_inputs = verify_coinbase_source(
            analytical_source,
            decision_at=captured,
            now=captured,
        )
        feature_candles = analytical_inputs["inputs"]["closed_candles"]
    prices = [
        CryptoPrice(
            symbol=symbol,
            source="coinbase_closed_1m_candles",
            observed_at=datetime.fromtimestamp(float(item[0]) + 60, UTC),
            price_usd=str(item[4]),
            raw_json=json.dumps(item),
        )
        for item in feature_candles
        if float(item[0]) + 60 <= captured.timestamp()
    ]
    features = calculate_crypto_features(prices, window_minutes=1440)
    latest = max((price.observed_at for price in prices), default=None)
    return {
        "role": "ANALYTICAL_SOURCE",
        "analytical_source": analytical_source,
        "analytical_inputs": analytical_inputs,
        "symbol": symbol,
        "ticker": ticker,
        "ticker_evidence": ticker_receipt,
        "candles_evidence": dict(public.receipts[-1]),
        "features": features,
        "closed_candle_count": len(prices),
        "latest_closed_candle_at": latest.isoformat() if latest else None,
        "collected_at": captured.isoformat(),
        "settlement_truth": False,
    }


CRYPTO_DIAGNOSTIC_TICKER_MAX_AGE_SECONDS = 60
CRYPTO_DIAGNOSTIC_CANDLE_MAX_AGE_SECONDS = 1800


def _crypto_aware_time(value: Any, label: str) -> datetime:
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("CRYPTO_DIAGNOSTIC_INVALID_CLOCK:" + label) from exc


def _crypto_diagnostic_inputs(source: dict[str, Any], close: Any, now: datetime):
    """Bind current spot and horizon to one provider clock; candle history stays separate."""
    decision_at = _crypto_aware_time(now, "decision_at")
    ticker = source["ticker"]
    if source.get("symbol") == "BTC" or source.get("analytical_source") is not None:
        from .btc_fixed_model import verified_btc_features

        verified, checked_features = verified_btc_features(
            source["analytical_source"],
            computed_at=decision_at,
        )
        view = verified["inputs"]
        if (
            source["features"] != checked_features
            or Decimal(str(ticker["price"])) != Decimal(view["spot"])
            or _crypto_aware_time(ticker["time"], "ticker_provider_at")
            != _crypto_aware_time(view["trade_at"], "original_trade_at")
            or source.get("analytical_inputs") != verified
        ):
            raise ValueError("COINBASE_DIAGNOSTIC_ORIGINAL_INPUT_MISMATCH")
    origin = _crypto_aware_time(ticker.get("time"), "ticker_provider_at")
    receipt = _crypto_aware_time(source["ticker_evidence"].get("received_at"), "ticker_received_at")
    cutoff = _crypto_aware_time(source.get("latest_closed_candle_at"), "candle_cutoff_at")
    candles_received = _crypto_aware_time(
        source["candles_evidence"].get("received_at"), "candles_received_at"
    )
    collected = _crypto_aware_time(source.get("collected_at"), "collected_at")
    close_at = _crypto_aware_time(close, "close_time")
    if not origin <= receipt <= decision_at:
        raise ValueError("CRYPTO_DIAGNOSTIC_TICKER_VISIBILITY_INVALID")
    if (decision_at - origin).total_seconds() > CRYPTO_DIAGNOSTIC_TICKER_MAX_AGE_SECONDS:
        raise ValueError("CRYPTO_DIAGNOSTIC_TICKER_STALE")
    if (
        not cutoff <= origin
        or not cutoff <= candles_received <= collected <= decision_at
        or receipt > collected
    ):
        raise ValueError("CRYPTO_DIAGNOSTIC_CANDLE_VISIBILITY_INVALID")
    if (decision_at - cutoff).total_seconds() > CRYPTO_DIAGNOSTIC_CANDLE_MAX_AGE_SECONDS:
        raise ValueError("CRYPTO_DIAGNOSTIC_CANDLE_HISTORY_STALE")
    if type(source.get("closed_candle_count")) is not int or source["closed_candle_count"] < 3:
        raise ValueError("CRYPTO_DIAGNOSTIC_INSUFFICIENT_CANDLE_HISTORY")
    if isinstance(ticker.get("price"), bool):
        raise ValueError("CRYPTO_DIAGNOSTIC_INVALID_TICKER_PRICE")
    try:
        spot = float(ticker["price"])
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError("CRYPTO_DIAGNOSTIC_INVALID_TICKER_PRICE") from exc
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError("CRYPTO_DIAGNOSTIC_INVALID_TICKER_PRICE")
    if close_at <= decision_at:
        raise ValueError("CRYPTO_DIAGNOSTIC_MARKET_CLOSED")
    features = dict(source["features"])
    features["price"] = spot
    # Reject nonfinite supplied historical inputs before distribution clamping.
    for name in ("volatility_1h", "volatility_4h", "volatility_24h", "return_1h"):
        value = features.get(name)
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
            raise ValueError("CRYPTO_DIAGNOSTIC_INVALID_HISTORICAL_FEATURE:" + name)
    horizon = (close_at - origin).total_seconds() / 60
    inputs: DistributionInputs | None
    if source.get("symbol") == "BTC" or source.get("analytical_source") is not None:
        from .btc_fixed_model import prepare_btc_inputs

        inputs = prepare_btc_inputs(
            source["analytical_source"],
            horizon_end_at=close_at,
            computed_at=decision_at,
        ).distribution
    else:
        inputs = inputs_from_features(features, horizon_minutes=horizon)
    if inputs is None:
        raise ValueError("CRYPTO_DIAGNOSTIC_INSUFFICIENT_CANDLE_HISTORY")
    return inputs, {
        "spot": str(ticker["price"]),
        "spot_basis": "COINBASE_TICKER_PROVIDER_TIME",
        "spot_provider_at": origin.isoformat(),
        "spot_received_at": receipt.isoformat(),
        "decision_at": decision_at.isoformat(),
        "horizon_start_at": origin.isoformat(),
        "horizon_minutes": horizon,
        "candle_feature_cutoff_at": cutoff.isoformat(),
        "candle_features_received_at": candles_received.isoformat(),
        "historical_candle_spot": source["features"].get("price"),
        "volatility_per_minute": inputs.volatility_per_minute,
        "drift_per_minute": inputs.drift_per_minute,
        "historical_inputs": {
            key: source["features"].get(key)
            for key in ("volatility_1h", "volatility_4h", "volatility_24h", "return_1h")
        },
        "ticker_evidence": dict(source["ticker_evidence"]),
        "candles_evidence": dict(source["candles_evidence"]),
        "execution_authority": False,
    }


def add_crypto_research(row: dict[str, Any], source: dict[str, Any], now: datetime) -> None:
    terms = row["crypto_terms"]
    raw = row["raw_market"]
    observed: datetime | None
    latest: datetime | None
    diagnostic_now: datetime | None
    try:
        observed = _crypto_aware_time(source["ticker"].get("time"), "ticker_provider_at")
        latest = _crypto_aware_time(source.get("latest_closed_candle_at"), "candle_cutoff_at")
        diagnostic_now = _crypto_aware_time(now, "decision_at")
    except (ValueError, TypeError, KeyError):
        observed = latest = diagnostic_now = None
    # Existing execution/source gates still required; these are diagnostic source clocks.
    row["source_freshness"] = {
        "ticker_provider_time": observed.isoformat() if observed else None,
        "ticker_age_seconds": (diagnostic_now - observed).total_seconds()
        if observed and diagnostic_now
        else None,
        "latest_closed_candle_at": source.get("latest_closed_candle_at"),
        "candle_age_seconds": (diagnostic_now - latest).total_seconds()
        if latest and diagnostic_now
        else None,
        "gate_status": "NOT_CERTIFIED_FOR_SETTLEMENT",
    }
    inputs = None
    diagnostic_blocker = None
    row["forecast_inputs"] = None
    try:
        inputs, row["forecast_inputs"] = _crypto_diagnostic_inputs(
            source, raw.get("close_time"), now
        )
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        diagnostic_blocker = str(exc)
    row["forecast_diagnostic_blocker"] = diagnostic_blocker
    comparator = {
        "greater": "ABOVE",
        "less": "BELOW",
        "between": "RANGE",
        "greater_or_equal": "AT_OR_ABOVE",
        "less_or_equal": "AT_OR_BELOW",
    }.get(str(raw.get("strike_type")))
    floor, cap = raw.get("floor_strike"), raw.get("cap_strike")
    threshold = floor if comparator in {"ABOVE", "AT_OR_ABOVE"} else cap
    probability = (
        threshold_probability(
            inputs,
            comparator=comparator,
            threshold=float(threshold) if threshold is not None else None,
            lower=float(floor) if floor is not None else None,
            upper=float(cap) if cap is not None else None,
        )
        if inputs
        and comparator
        and terms.get("status") == "EXACT_LINK"
        and not row.get("semantic_conflicts")
        else None
    )
    row["model"] = "crypto.distribution_model (research only; uncalibrated for this contract)"
    row["forecast"] = probability
    row["research_forecast_artifact"] = None
    if probability is not None and source.get("analytical_source") is not None:
        from .btc_fixed_model import model_specification
        from .crypto_source import bind_coinbase_forecast_inputs

        original_source = source["analytical_source"]
        source_hash = hashlib.sha256(
            json.dumps(
                original_source, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        row["research_forecast_artifact"] = bind_coinbase_forecast_inputs(
            {
                "kind": "crypto-research-diagnostic-v1",
                "model_specification": model_specification(),
                "execution_entrypoint": (
                    "kalshi_predictor.overnight_paper.discovery:add_crypto_research"
                ),
                "horizon_role": "MARKET_CLOSE_RESEARCH_PROXY_NOT_VERIFIED_PAYOFF_TIME",
                "probability": str(probability),
                "generated_at": _crypto_aware_time(now, "decision_at").isoformat(),
                "source_hashes": [source_hash],
                "forecast_inputs": row["forecast_inputs"],
                "scope": "ANALYTICAL_DIAGNOSTIC_NOT_MODEL_RELEASE",
            },
            original_source,
            decision_at=now,
        )
    row["model_readiness"] = (
        "CONTRACT_SPECIFIC_NO_LEAKAGE_CALIBRATION_MISSING"
        if probability is not None
        else diagnostic_blocker or "EXPLICIT_THRESHOLD_OR_SUFFICIENT_HISTORY_MISSING"
    )
    row["model_scope_warning"] = (
        "Terminal spot distribution is not validated for CF benchmark averaging or directional "
        "opening-reference contracts. Diagnostic probability cannot authorize paper activation."
    )
