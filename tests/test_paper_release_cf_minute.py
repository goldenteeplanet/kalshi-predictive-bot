import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import pytest
from test_paper_release_rules_timing import fixture

from kalshi_predictor.overnight_paper.rule_verifier import (
    CF_MINUTE_METHOD,
    evaluate_cf_minute,
    verify_settlement_rule,
)

END = datetime(2026, 9, 8, 7, 15, tzinfo=UTC)


def points():
    return tuple((END - timedelta(seconds=60 - i), "100.00") for i in range(60))


def test_unrounded_mean_is_exact_and_not_default_rounded():
    rows = list(points())
    rows[0] = (rows[0][0], "100.01")
    result = evaluate_cf_minute(tuple(rows), boundary=END)
    assert result.unrounded_mean == Fraction(600001, 6000)
    assert result.rounded_value is None


@pytest.mark.parametrize(
    "rounding,expected", [("ROUND_HALF_UP", "100.01"), ("ROUND_HALF_EVEN", "100.00")]
)
def test_half_cent_requires_explicit_choice_independent_of_decimal_context(rounding, expected):
    rows = tuple((t, "100.01" if i < 30 else v) for i, (t, v) in enumerate(points()))
    with localcontext() as ctx:
        ctx.prec = 2
        result = evaluate_cf_minute(rows, boundary=END, rounding=rounding)
    assert result.unrounded_mean == Fraction(20001, 200)
    assert result.rounded_value == Decimal(expected)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "reverse", "subsecond", "extra", "outside"]
)
def test_incomplete_or_ambiguous_window_rejected(mutation):
    rows = list(points())
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[1] = rows[0]
    elif mutation == "reverse":
        rows.reverse()
    elif mutation == "subsecond":
        rows[0] = (rows[0][0] + timedelta(milliseconds=200), rows[0][1])
    elif mutation == "extra":
        rows.append((END, "100.00"))
    else:
        rows[-1] = (END, "100.00")
    with pytest.raises(ValueError):
        evaluate_cf_minute(tuple(rows), boundary=END)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", "100.001", "bad", 100.0, True])
def test_invalid_original_values_rejected(value):
    rows = list(points())
    rows[0] = (rows[0][0], value)
    with pytest.raises(ValueError):
        evaluate_cf_minute(tuple(rows), boundary=END)


def test_numerical_rule_requires_explicit_reviewed_rounding():
    decision, policy, document = fixture()
    policy = replace(
        policy,
        methodology=CF_MINUTE_METHOD,
        provider="CF Benchmarks",
        source_identity="BRTI",
        selection="60 standard top-of-second points in [observation-60s,observation)",
        conversion="identity USD",
        precision="input USD 0.01; arithmetic mean; output USD 0.01",
        rounding="unknown",
    )

    def verify(p):
        decision.update(rule_version=p.version, settlement_rule={**asdict(p), "amendments": []})
        return verify_settlement_rule(decision=decision, documents=(document,), registry=(p,))

    assert not verify(policy).passed
    assert verify(replace(policy, rounding="ROUND_HALF_EVEN")).passed
    # An injected synthetic policy is never installed into the production registry.
    assert not verify_settlement_rule(decision=decision, documents=(document,)).passed


def test_three_retrospective_public_examples_do_not_establish_tie_policy():
    source = json.loads(
        (Path(__file__).parent / "fixtures/paper_release_cf_examples.json").read_text()
    )
    assert source["scope"] == "RETROSPECTIVE_PUBLIC_SETTLEMENT_REPRODUCTION_ONLY"
    assert source["index_identity"] == "BRTI"
    assert source["source_url"] == "https://www.cfbenchmarks.com/data/indices/BRTI"
    assert source["source_response_sha256"] == (
        "14b47e1ad2df6dcc606c9e2efa5c1ae76df11272600679573f6d988fda7fbc33"
    )
    assert source["exchange_source_url"].startswith(
        "https://external-api.kalshi.com/trade-api/v2/markets?"
    )
    assert len(source["exchange_response_sha256"]) == 64
    source_receipt = datetime.fromisoformat(source["source_received_at"])
    exchange_receipt = datetime.fromisoformat(source["exchange_received_at"])
    assert len(source["examples"]) == 3
    for market in source["examples"]:
        assert market["ticker"].startswith(market["event_ticker"] + "-")
        assert [w["role"] for w in market["windows"]] == ["opening", "closing"]
        values = []
        for captured in market["windows"]:
            end = datetime.fromisoformat(captured["boundary"])
            assert source_receipt > end and exchange_receipt > end
            window = tuple((p["timestamp"], p["value"]) for p in captured["points"])
            assert len(window) == 60
            up = evaluate_cf_minute(window, boundary=end, rounding="ROUND_HALF_UP")
            even = evaluate_cf_minute(window, boundary=end, rounding="ROUND_HALF_EVEN")
            assert up.rounded_value == even.rounded_value == Decimal(captured["expected_value"])
            assert (up.unrounded_mean * 100).denominator != 2
            values.append(up.rounded_value)
        assert ("yes" if values[1] >= values[0] else "no") == market["result"]
