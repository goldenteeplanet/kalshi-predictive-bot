"""Current discovery to explicit, uncertified settlement computation scenarios.

Precision and endpoint conventions below are predeclared research choices, not
interpretations that close unresolved exchange rule questions. Never certify a
target or paper admission using this adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal

from kalshi_predictor.crypto.cf_process_inputs import INDEX
from kalshi_predictor.crypto.cf_settlement_windows import CFWindow, CFWindowRules
from kalshi_predictor.crypto.doge_strikes import parse_doge_strike
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget, _json, aware

FAMILIES = {"BTC": "KXBTC", "ETH": "KXETH", "SOL": "KXSOLE", "XRP": "KXXRP", "DOGE": "KXDOGE"}
RESEARCH_PLACES = {"BTC": 2, "ETH": 2, "SOL": 2, "XRP": 5, "DOGE": 7}
TERMS_URLS = {
    asset: "https://assets.kalshi.com/contract_terms/"
    + ("DOGE.pdf" if asset == "DOGE" else "CRYPTO.pdf")
    for asset in FAMILIES
}


def target_from_discovery(
    market: dict | bytes,
    asset: str,
    market_received_at: datetime,
    rule_original: bytes,
    rule_url: str,
    rule_received_at: datetime,
    as_of: datetime,
) -> SettlementBenchmarkTarget:
    """Accept exact market-envelope bytes or a derived discovery-row dictionary.

    Dict input is canonicalized as a derived record; caller must retain the real
    discovery response and receipt. Decimal values retain their exact digits.
    """
    aware(as_of)
    aware(market_received_at)
    aware(rule_received_at)
    if asset not in FAMILIES:
        raise ValueError("SUPPORTED_RESEARCH_ASSET_REQUIRED")
    if rule_url != TERMS_URLS[asset]:
        raise ValueError("FAMILY_TERMS_URL_MISMATCH")
    if not 0 <= (as_of - market_received_at).total_seconds() <= 300:
        raise ValueError("FRESH_DISCOVERY_REQUIRED")
    if rule_received_at > as_of:
        raise ValueError("RULE_NOT_AVAILABLE")
    if isinstance(market, bytes):
        raw = market
        metadata = _json(raw).get("market")
    else:
        raw = json.dumps(
            {"market": market},
            sort_keys=True,
            separators=(",", ":"),
            default=lambda v: str(v) if isinstance(v, Decimal) else _reject(v),
            allow_nan=False,
        ).encode()
        metadata = _json(raw)["market"]
    if not isinstance(metadata, dict):
        raise ValueError("MARKET_ENVELOPE_REQUIRED")
    family = FAMILIES[asset]
    event = metadata.get("event_ticker")
    ticker = metadata.get("ticker")
    if (
        not isinstance(event, str)
        or not event.startswith(family + "-")
        or not isinstance(ticker, str)
        or not ticker.startswith(event + "-")
        or metadata.get("series_ticker", family) != family
        or metadata.get("market_type") != "binary"
        or metadata.get("status") not in {"open", "active"}
    ):
        raise ValueError("ACTIVE_MARKET_ASSET_IDENTITY_REQUIRED")
    close = datetime.fromisoformat(metadata["close_time"].replace("Z", "+00:00"))
    aware(close)
    if (close - as_of).total_seconds() <= 60:
        raise ValueError("CURRENT_PREWINDOW_MARKET_REQUIRED")
    primary = metadata.get("rules_primary")
    if not isinstance(primary, str):
        raise ValueError("BENCHMARK_RULE_TEXT_REQUIRED")
    mentioned = set(re.findall(r"\b(?:BRTI|ERTI|[A-Z]+USD_RTI)\b", primary))
    eth_alias = (
        asset == "ETH" and mentioned == {"ERTI"} and "Ethereum Real-Time Index (ERTI)" in primary
    )
    if mentioned != {INDEX[asset]} and not eth_alias:
        raise ValueError("MARKET_INDEX_MISMATCH")
    operator: str | None
    if asset == "DOGE":
        # Existing exact grammar checks custom strikes, local event/close time,
        # primary rule bounds and absent top-level conflicting strikes.
        parsed = parse_doge_strike(metadata, cutoff=as_of)
        operator, lower, upper = parsed.operator, parsed.floor, parsed.cap
    else:
        if metadata.get("custom_strike") not in (None, {}):
            raise ValueError("UNSUPPORTED_CUSTOM_STRIKE")
        operator = metadata.get("strike_type")
        lower, upper = (_bound(metadata.get(key)) for key in ("floor_strike", "cap_strike"))
    if operator == "between" and lower is not None and upper is not None and lower < upper:
        comparator, threshold = "RANGE_CLOSED", None
    elif operator == "greater" and lower is not None and upper is None:
        comparator, threshold, lower = "ABOVE", lower, None
    elif operator == "less" and lower is None and upper is not None:
        comparator, threshold, upper = "BELOW", upper, None
    else:
        raise ValueError("CONFLICTING_OR_UNSUPPORTED_STRIKES")
    end = int(close.timestamp() * 1000)
    rules = CFWindowRules(
        ticker,
        INDEX[asset],
        CFWindow(end - 60000, end, True, False, 1000, 60),
        None,
        RESEARCH_PLACES[asset],
        "HALF_UP",
        "REJECT",
        rule_url,
        hashlib.sha256(rule_original).hexdigest(),
    )
    target = SettlementBenchmarkTarget(
        asset,
        event,
        rules,
        comparator,
        threshold,
        lower,
        upper,
        rule_original,
        rule_received_at,
        raw,
        market_received_at,
        None,
        "UNRESOLVED",
    )
    target.validate(as_of=as_of)
    return target


def _reject(value):
    raise ValueError("JSON_METADATA_TYPE_REQUIRED")


def _bound(value) -> Decimal | None:
    if value is None:
        return None
    if type(value) not in (str, int, Decimal):
        raise ValueError("EXACT_DECIMAL_STRIKE_REQUIRED")
    result = Decimal(value)
    if not result.is_finite() or result <= 0:
        raise ValueError("FINITE_POSITIVE_STRIKE_REQUIRED")
    return result


def target_assumptions(target: SettlementBenchmarkTarget) -> dict:
    """Attach these explicit assumptions to the predeclared capture protocol."""
    primary = _json(target.market_original)["market"].get("rules_primary", "")
    eth_alias = target.symbol == "ETH" and "Ethereum Real-Time Index (ERTI)" in primary
    return dict(
        schema="current-research-target-assumptions-v1",
        status="DECLARED_UNCERTIFIED_COMPUTATIONAL_SCENARIO",
        window="[close-60s,close)",
        frequency_ms=1000,
        samples=60,
        decimal_places=target.rules.decimal_places,
        rounding=target.rules.rounding,
        amendments=target.rules.amendments,
        comparator=target.comparator,
        precision_authority="RESEARCH_CHOICE_NOT_RULE_AUTHORITY",
        endpoint_authority="RESEARCH_CHOICE_NOT_RULE_AUTHORITY",
        market_index_label="ERTI" if eth_alias else target.rules.index_id,
        source_index=target.rules.index_id,
        alias_status="UNVERIFIED" if eth_alias else "NO_ALIAS_USED",
        alias_basis=(
            "DECLARED_RESEARCH_INFERENCE_ETHEREUM_IDENTITY_NOT_PROVEN_INDEX_EQUIVALENCE"
            if eth_alias
            else None
        ),
        discovery_dict_binding="DERIVED_ROW_REQUIRES_ORIGINAL_DISCOVERY_RECEIPT",
        finality="UNRESOLVED",
        rule_certified=False,
        paper_eligible=False,
        execution_authority=False,
    )
