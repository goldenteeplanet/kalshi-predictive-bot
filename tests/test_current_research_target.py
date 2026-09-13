import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_predictor.crypto.current_research_target import (
    target_assumptions,
    target_from_discovery,
)

NOW = datetime(2026, 9, 11, 5, 36, 49, tzinfo=UTC)
RULE_URL = "https://assets.kalshi.com/contract_terms/CRYPTO.pdf"


def build(market, asset="BTC", **changes):
    args = dict(
        market=market,
        asset=asset,
        market_received_at=NOW,
        rule_original=b"test-rule-original",
        rule_url="https://assets.kalshi.com/contract_terms/DOGE.pdf"
        if asset == "DOGE"
        else RULE_URL,
        rule_received_at=NOW,
        as_of=NOW,
    )
    args.update(changes)
    return target_from_discovery(**args)


def market(**changes):
    value = dict(
        ticker="KXBTC-E-B100",
        event_ticker="KXBTC-E",
        market_type="binary",
        status="open",
        close_time=(NOW.replace(second=0) + timedelta(hours=1)).isoformat(),
        rules_primary="CF Bitcoin Real-Time Index (BRTI)",
        strike_type="between",
        floor_strike="100.01",
        cap_strike="109.99",
    )
    value.update(changes)
    return value


def test_range_target_retains_exact_endpoints_and_explicit_assumptions():
    target = build(market())
    assert target.comparator == "RANGE_CLOSED"
    assert (target.lower, target.upper) == (Decimal("100.01"), Decimal("109.99"))
    assert target.threshold is None
    assert len(target.rules.closing.timestamps()) == 60
    assert target.finality_deadline is None
    assert target_assumptions(target)["paper_eligible"] is False
    assert target.validate(as_of=NOW)["settlement_aligned_forecast"] is False


@pytest.mark.parametrize(
    "operator,floor,cap,comparator",
    [("greater", "100", None, "ABOVE"), ("less", None, "100", "BELOW")],
)
def test_tail_targets(operator, floor, cap, comparator):
    target = build(market(strike_type=operator, floor_strike=floor, cap_strike=cap))
    assert target.comparator == comparator
    assert target.threshold == Decimal(100)
    assert target.lower is None and target.upper is None


@pytest.mark.parametrize(
    "change",
    [
        dict(cap_strike="100.01"),
        dict(floor_strike="NaN"),
        dict(floor_strike=True),
        dict(strike_type="greater"),
        dict(rules_primary="ETHUSD_RTI"),
        dict(status="closed"),
        dict(event_ticker="KXETH-E"),
        dict(custom_strike={"strike_type": "less"}),
    ],
)
def test_invalid_or_conflicting_market_rejected(change):
    with pytest.raises(ValueError):
        build(market(**change))


def test_stale_market_rejected():
    with pytest.raises(ValueError, match="FRESH_DISCOVERY"):
        build(market(), market_received_at=NOW - timedelta(seconds=301))


def test_exact_market_original_preserved():
    raw = json.dumps({"market": market()}, indent=2).encode()
    assert build(raw).market_original == raw


def test_real_doge_original_all_strike_types_preserved_without_rule_promotion():
    rows = json.loads((Path(__file__).parent / "fixtures/doge-markets-20260911.json").read_bytes())[
        "markets"
    ]
    counts = {"ABOVE": 0, "BELOW": 0, "RANGE_CLOSED": 0}
    for row in rows:
        target = build(row, "DOGE")
        counts[target.comparator] += 1
        assert target.rules.decimal_places == 7
        assert target_assumptions(target)["rule_certified"] is False
    assert counts == {"ABOVE": 2, "BELOW": 2, "RANGE_CLOSED": 89}


def test_ethereum_alias_is_explicit_uncertified_research_scenario():
    row = market(
        ticker="KXETH-E-B100",
        event_ticker="KXETH-E",
        rules_primary="CF Benchmarks Ethereum Real-Time Index (ERTI)",
    )
    target = build(row, "ETH")
    assumptions = target_assumptions(target)
    assert target.rules.index_id == "ETHUSD_RTI"
    assert assumptions["market_index_label"] == "ERTI"
    assert assumptions["alias_status"] == "UNVERIFIED"
    assert assumptions["rule_certified"] is False
    assert assumptions["paper_eligible"] is False


@pytest.mark.parametrize(
    "primary",
    ["ERTI", "Bitcoin Real-Time Index (ERTI)", "Ethereum Real-Time Index (ERTI) BTCUSD_RTI"],
)
def test_alias_requires_exact_ethereum_identity_without_conflicting_index(primary):
    with pytest.raises(ValueError, match="MARKET_INDEX_MISMATCH"):
        build(market(ticker="KXETH-E-B100", event_ticker="KXETH-E", rules_primary=primary), "ETH")


def test_doge_rejects_generic_crypto_terms_attachment():
    rows = json.loads((Path(__file__).parent / "fixtures/doge-markets-20260911.json").read_bytes())[
        "markets"
    ]
    with pytest.raises(ValueError, match="FAMILY_TERMS_URL_MISMATCH"):
        build(rows[0], "DOGE", rule_url=RULE_URL)


def test_non_doge_rejects_doge_terms_attachment():
    with pytest.raises(ValueError, match="FAMILY_TERMS_URL_MISMATCH"):
        build(market(), rule_url="https://assets.kalshi.com/contract_terms/DOGE.pdf")
