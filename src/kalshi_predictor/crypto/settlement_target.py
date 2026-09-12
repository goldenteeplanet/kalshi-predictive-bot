"""Declared settlement-variable identity, not a rule certificate or a price model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from kalshi_predictor.crypto.cf_settlement_windows import CFWindow, CFWindowRules
from kalshi_predictor.crypto.settlement_rule_version import SOLSettlementRuleVersion


def aware(value: datetime) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_TARGET_CLOCK_REQUIRED")


def _json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_MARKET_FIELD")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("NONFINITE_MARKET_JSON")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject, parse_float=Decimal)

    # JSON Decimal parsing preserves finite overflow for explicit rejection.
    def finite(value):
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("NONFINITE_MARKET_JSON")
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        if isinstance(value, list):
            for child in value:
                finite(child)

    finite(result)
    if type(result) is not dict:
        raise ValueError("MARKET_ENVELOPE_REQUIRED")
    return result


@dataclass(frozen=True)
class SettlementBenchmarkTarget:
    symbol: str
    event_ticker: str
    rules: CFWindowRules
    comparator: str
    threshold: Decimal | None
    lower: Decimal | None
    upper: Decimal | None
    rule_original: bytes
    rule_received_at: datetime
    market_original: bytes
    market_received_at: datetime
    finality_deadline: datetime | None
    finality_basis: str

    def validate(self, *, as_of: datetime, include_sol_rule_binding: bool = False) -> dict:
        aware(as_of)
        aware(self.rule_received_at)
        aware(self.market_received_at)
        if max(self.rule_received_at, self.market_received_at) > as_of:
            raise ValueError("TARGET_ORIGINAL_NOT_VISIBLE")
        if self.symbol not in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
            raise ValueError("UNSUPPORTED_SYMBOL")
        if type(self.rules) is not CFWindowRules:
            raise ValueError("EXACT_CF_RULE_TYPE_REQUIRED")
        r = self.rules
        if type(r.closing) is not CFWindow:
            raise ValueError("EXACT_CF_WINDOW_TYPE_REQUIRED")
        ticks = r.closing.timestamps()
        if r.closing.end_ms - r.closing.start_ms != 60000 or len(ticks) != 60:
            raise ValueError("EXACT_SIXTY_SECOND_AVERAGE_REQUIRED")
        if int(as_of.timestamp() * 1000) >= ticks[0]:
            raise ValueError("ONLY_PREWINDOW_FORECAST_SUPPORTED")
        if r.opening is not None:
            raise ValueError("OPENING_RELATIVE_PAYOFF_NOT_IMPLEMENTED")
        if (
            type(r.decimal_places) is not int
            or not 0 <= r.decimal_places <= 18
            or r.rounding not in {"HALF_UP", "HALF_EVEN", "DOWN", "FLOOR", "CEILING"}
            or r.amendments not in {"REJECT", "LATEST_KNOWN_AS_OF"}
            or type(r.index_id) is not str
            or not r.index_id.strip()
        ):
            raise ValueError("EXPLICIT_BENCHMARK_PRECISION_RULES_REQUIRED")
        for raw in (self.rule_original, self.market_original):
            if type(raw) is not bytes or not 0 < len(raw) <= 3000000:
                raise ValueError("BOUNDED_ORIGINAL_BYTES_REQUIRED")
        url = urlsplit(r.rule_source)
        if (
            url.scheme != "https"
            or url.hostname != "assets.kalshi.com"
            or url.port is not None
            or url.username
            or url.password
            or url.fragment
            or not url.path.startswith("/contract_terms/")
            or url.query
        ):
            raise ValueError("ORIGINAL_RULE_URL_REQUIRED")
        if hashlib.sha256(self.rule_original).hexdigest() != r.rule_sha256:
            raise ValueError("RULE_ORIGINAL_HASH_MISMATCH")
        market = _json(self.market_original).get("market")
        if type(market) is not dict:
            raise ValueError("MARKET_ENVELOPE_REQUIRED")
        try:
            close = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError("ORIGINAL_CLOSE_REQUIRED") from exc
        aware(close)
        if (
            market.get("ticker") != r.market_ticker
            or market.get("event_ticker") != self.event_ticker
            or not self.event_ticker
            or market.get("market_type") != "binary"
            or int(close.timestamp() * 1000) != r.closing.end_ms
        ):
            raise ValueError("MARKET_TARGET_IDENTITY_MISMATCH")
        values: tuple[Decimal | None, ...]
        if self.comparator in {"ABOVE", "AT_OR_ABOVE", "BELOW", "AT_OR_BELOW"}:
            values = (self.threshold,)
            if self.lower is not None or self.upper is not None:
                raise ValueError("CONFLICTING_STRIKES")
        elif self.comparator in {"RANGE", "RANGE_CLOSED"}:
            values = (self.lower, self.upper)
            if self.threshold is not None:
                raise ValueError("CONFLICTING_STRIKES")
        else:
            raise ValueError("UNSUPPORTED_PAYOFF")
        if any(type(x) is not Decimal or not x.is_finite() or x <= 0 for x in values):
            raise ValueError("EXACT_FINITE_DECIMAL_STRIKES_REQUIRED")
        if len(values) == 2:
            assert values[0] is not None and values[1] is not None
            if values[0] >= values[1]:
                raise ValueError("INVALID_RANGE")
        if self.finality_deadline is None:
            if self.finality_basis != "UNRESOLVED":
                raise ValueError("UNKNOWN_FINALITY_MUST_REMAIN_EXPLICIT")
        else:
            aware(self.finality_deadline)
            if self.finality_basis != "DECLARED_UNCERTIFIED" or self.finality_deadline < close:
                raise ValueError("INVALID_DECLARED_FINALITY")
        result = dict(
            schema="settlement-benchmark-target-v1",
            symbol=self.symbol,
            event_ticker=self.event_ticker,
            rules=asdict(r),
            comparator=self.comparator,
            threshold=str(self.threshold) if self.threshold is not None else None,
            lower=str(self.lower) if self.lower is not None else None,
            upper=str(self.upper) if self.upper is not None else None,
            rule_original_sha256=r.rule_sha256,
            market_original_sha256=hashlib.sha256(self.market_original).hexdigest(),
            available_at=max(self.rule_received_at, self.market_received_at).isoformat(),
            finality_deadline=self.finality_deadline.isoformat()
            if self.finality_deadline
            else None,
            finality_basis=self.finality_basis,
            target_variable="CF_ARITHMETIC_WINDOW_AVERAGE",
            authority="DECLARED_ORIGINAL_BOUND_METADATA_NOT_RULE_CERTIFICATION",
            settlement_aligned_forecast=False,
            paper_eligible=False,
        )
        if self.symbol == "SOL" or r.market_ticker.startswith("KXSOLE-"):
            if (
                self.symbol != "SOL"
                or not r.market_ticker.startswith("KXSOLE-")
                or not self.event_ticker.startswith("KXSOLE-")
            ):
                raise ValueError("SOL_EVENT_TARGET_IDENTITY_REQUIRED")
            # These are declared computational assumptions, not interpreted legal
            # evidence. Preserve unknown semantics and attach no field certification.
            rule = SOLSettlementRuleVersion(
                family="KXSOLE",
                benchmark_id=r.index_id,
                sample_frequency_ms=r.closing.cadence_ms,
                sample_count=r.closing.expected_ticks,
                start_offset_ms=r.closing.start_ms - r.closing.end_ms,
                end_offset_ms=0,
                include_start=r.closing.include_start,
                include_end=r.closing.include_end,
                sample_precision=None,
                sample_rounding=None,
                average_precision=None,
                final_precision=format(Decimal((0, (1,), -r.decimal_places)), "f"),
                final_rounding=r.rounding,
                tie_breaking=None,
                missing_sample_behavior=None,
                amendment_handling=r.amendments,
                finality=None,
                effective_from=None,
                effective_until=None,
                authority_version=None,
                field_evidence=(),
            )
            if include_sol_rule_binding:
                result["settlement_rule_binding"] = {
                    "version_id": rule.bind(family="KXSOLE", benchmark_id="SOLUSD_RTI"),
                    "rule": asdict(rule),
                    "status": rule.status,
                    "unresolved_fields": list(rule.unresolved_fields),
                    "evidence_role": "DECLARED_ASSUMPTIONS_NOT_AUTHORITY",
                }
        return result
