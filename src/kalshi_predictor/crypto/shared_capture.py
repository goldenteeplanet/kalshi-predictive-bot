"""Immutable candle input for opt-in shared-cutoff research; no acquisition or authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

from kalshi_predictor.crypto.doge_strikes import parse_doge_strike
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    forecast_independent,
)
from kalshi_predictor.microstructure.provenance import aware, bound_close_time


@dataclass(frozen=True)
class CandleOriginal:
    raw: bytes
    sha256: str
    url: str
    received_at: datetime
    status: int = 200


@dataclass(frozen=True)
class SharedCryptoInputs:
    target: CryptoTarget
    originals: tuple[CandleOriginal, ...]

    def prices(self, cutoff: datetime) -> tuple[PriceObservation, ...]:
        cutoff = aware(cutoff)
        if type(self.originals) is not tuple or not 1 <= len(self.originals) <= 4:
            raise ValueError("BOUNDED_CANDLE_ORIGINALS_REQUIRED")
        rows = []
        for original in self.originals:
            if (
                type(original.status) is not int
                or original.status != 200
                or not isinstance(original.raw, bytes)
                or not 0 < len(original.raw) <= 1_000_000
                or hashlib.sha256(original.raw).hexdigest() != original.sha256
                or aware(original.received_at) > cutoff
            ):
                raise ValueError("CANDLE_ORIGINAL_HASH_STATUS_OR_CLOCK")
            url = urlparse(original.url)
            query = parse_qs(url.query, strict_parsing=True)
            if (
                url.scheme != "https"
                or url.netloc != "api.exchange.coinbase.com"
                or url.path != f"/products/{self.target.symbol}-USD/candles"
                or url.fragment
                or set(query) != {"granularity", "start", "end"}
                or any(len(v) != 1 for v in query.values())
                or query["granularity"] != ["60"]
            ):
                raise ValueError("EXACT_COINBASE_CANDLE_REQUEST_REQUIRED")
            start = aware(datetime.fromisoformat(query["start"][0]))
            end = aware(datetime.fromisoformat(query["end"][0]))
            if not 0 < (end - start).total_seconds() <= 300 * 60:
                raise ValueError("BOUNDED_CANDLE_WINDOW_REQUIRED")
            values = json.loads(original.raw)
            if not isinstance(values, list) or len(values) > 301:
                raise ValueError("BOUNDED_CANDLE_ROWS_REQUIRED")
            for row in values:
                if not isinstance(row, list) or len(row) != 6:
                    raise ValueError("CANDLE_SHAPE")
                if type(row[0]) is not int or row[0] % 60:
                    raise ValueError("CANDLE_MINUTE_REQUIRED")
                at = datetime.fromtimestamp(row[0], UTC)
                if not start <= at < end:
                    # Retain only the declared half-open request window.
                    continue
                closed = datetime.fromtimestamp(row[0] + 60, UTC)
                if closed > aware(original.received_at):
                    raise ValueError("UNCLOSED_CANDLE")
                price = Decimal(str(row[4]))
                if isinstance(row[4], bool) or not price.is_finite() or price <= 0:
                    raise ValueError("INVALID_CANDLE_PRICE")
                rows.append(
                    PriceObservation(
                        float(price),
                        closed,
                        original.received_at,
                        "coinbase_closed_1m_candles",
                        original.sha256,
                        self.target.symbol,
                    )
                )
        rows.sort(key=lambda p: p.observed_at)
        # Invoke the real independent model for its full cadence/freshness/target checks.
        forecast_independent(rows, self.target, decision_at=cutoff)
        return tuple(rows)

    def bind_market(self, market: dict, *, cutoff: datetime | None = None) -> None:
        if self.target.symbol == "DOGE":
            if not isinstance(cutoff, datetime):
                raise ValueError("DOGE_EXPLICIT_INPUT_CUTOFF_REQUIRED")
            parsed = parse_doge_strike(market, cutoff=cutoff)
            if parsed.operator == "between":
                raise ValueError("DOGE_RANGE_INCLUSIVITY_AND_CF_PRECISION_UNCERTIFIED")
            doge_comparator = "ABOVE" if parsed.operator == "greater" else "BELOW"
            strike = parsed.floor if parsed.floor is not None else parsed.cap
            if type(self.target.threshold) not in (int, float):
                raise ValueError("DOGE_TARGET_METADATA_MISMATCH")
            try:
                threshold = Decimal(str(self.target.threshold))
            except (InvalidOperation, ValueError):
                raise ValueError("DOGE_TARGET_METADATA_MISMATCH") from None
            if (
                self.target.comparator != doge_comparator
                or self.target.observation_at != parsed.close_time
                or self.target.lower is not None
                or self.target.upper is not None
                or isinstance(self.target.threshold, bool)
                or self.target.threshold is None
                or not threshold.is_finite()
                or threshold != strike
            ):
                raise ValueError("DOGE_TARGET_METADATA_MISMATCH")
            return
        prefix = {
            "BTC": "KXBTC",
            "ETH": "KXETH",
            "SOL": "KXSOLE",
            "XRP": "KXXRP",
            "DOGE": "KXDOGE",
        }.get(self.target.symbol)
        if not prefix or not str(market.get("ticker", "")).startswith(prefix + "-"):
            raise ValueError("CRYPTO_MARKET_SYMBOL_MISMATCH")
        comparator = {
            "greater": "ABOVE",
            "greater_or_equal": "AT_OR_ABOVE",
            "less": "BELOW",
            "less_or_equal": "AT_OR_BELOW",
            "between": "RANGE",
        }.get(str(market.get("strike_type")))
        if (
            comparator != self.target.comparator
            or bound_close_time(market) != self.target.observation_at
        ):
            raise ValueError("CRYPTO_TARGET_METADATA_MISMATCH")
        floor, cap = market.get("floor_strike"), market.get("cap_strike")
        if comparator == "RANGE":
            if (
                floor is None
                or cap is None
                or Decimal(str(floor)) != Decimal(str(self.target.lower))
                or Decimal(str(cap)) != Decimal(str(self.target.upper))
            ):
                raise ValueError("CRYPTO_TARGET_STRIKE_MISMATCH")
        else:
            if floor is not None and cap is not None:
                raise ValueError("CONFLICTING_CRYPTO_STRIKES")
            strike = floor if floor is not None else cap
            if strike is None or Decimal(str(strike)) != Decimal(str(self.target.threshold)):
                raise ValueError("CRYPTO_TARGET_STRIKE_MISMATCH")
