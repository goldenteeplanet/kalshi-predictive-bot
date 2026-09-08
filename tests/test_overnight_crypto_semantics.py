"""Regression derived from public KXBTC15M-26SEP072215-15, captured 2026-09-08."""

import json
from datetime import UTC, datetime

from kalshi_predictor.crypto.semantics import parse_crypto_market_terms, validate_crypto_feature
from kalshi_predictor.data.schema import CryptoFeature, Market


def cf_market():
    raw = {
        "ticker": "KXBTC15M-26SEP072215-15",
        "event_ticker": "KXBTC15M-26SEP072215",
        "title": "BTC price up in next 15 mins?",
        "strike_type": "greater_or_equal",
        "floor_strike": 79421.7,
        "yes_sub_title": "Target Price: $79,421.70",
        "rules_primary": (
            "The CF Benchmarks BRTI sixty-second average is at least its opening average."
        ),
        "rules_secondary": (
            "Google or Coinbase may guide analysis; CF Benchmarks determines settlement."
        ),
    }
    return Market(
        ticker=raw["ticker"],
        event_ticker=raw["event_ticker"],
        series_ticker="KXBTC15M",
        title=raw["title"],
        raw_json=json.dumps(raw),
    )


def test_cf_primary_rule_beats_coinbase_disclaimer_and_inclusive_beats_up_title():
    terms = parse_crypto_market_terms(cf_market())
    assert terms.reference_price_source == "cf_benchmarks"
    assert terms.components[0].reference_price_source == "cf_benchmarks"
    assert terms.components[0].comparator == "AT_OR_ABOVE"
    assert terms.components[0].threshold_value == "79421.70"


def test_analytical_coinbase_feature_cannot_impersonate_cf_settlement_input():
    now = datetime(2026, 9, 8, 2, 7, tzinfo=UTC)
    feature = CryptoFeature(symbol="BTC", source="coinbase", generated_at=now, raw_json="{}")
    result = validate_crypto_feature(
        feature, terms=parse_crypto_market_terms(cf_market()), forecast_cutoff=now
    )
    assert not result.ok
    assert result.reason == "incompatible_reference_price_source"


def test_legacy_coinbase_explicit_source_preserved():
    market = Market(
        ticker="KXBTC-EXAMPLE",
        event_ticker="KXBTC-EVENT",
        title="BTC above $60000?",
        raw_json=json.dumps({"rules_primary": "Coinbase BTC spot price above $60000"}),
    )
    terms = parse_crypto_market_terms(market)
    assert terms.reference_price_source == "coinbase"
    assert terms.components[0].comparator == "ABOVE"
