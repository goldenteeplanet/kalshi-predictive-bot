import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from test_microstructure_research_capture import harness  # noqa: F401

from kalshi_predictor.crypto.doge_strikes import parse_doge_strike
from kalshi_predictor.crypto.shared_capture import CandleOriginal, SharedCryptoInputs
from kalshi_predictor.forecasting.crypto_v3_independent import CryptoTarget
from kalshi_predictor.microstructure import research_capture as capture

NOW = datetime(2026, 9, 11, 5, 36, 49, tzinfo=UTC)
RAW = (Path(__file__).parent / "fixtures/doge-markets-20260911.json").read_bytes()
MARKETS = json.loads(RAW)["markets"]


def target(market):
    parsed = parse_doge_strike(market, cutoff=NOW)
    return CryptoTarget(
        "DOGE",
        "ABOVE" if parsed.operator == "greater" else "BELOW",
        parsed.close_time,
        threshold=float(parsed.floor or parsed.cap),
    )


def test_actual_original_all_93_exact_decimal_metadata_and_rule_provenance():
    assert (
        hashlib.sha256(RAW).hexdigest()
        == "350b5359ef24b689f70c5e163b81f092c08a7f0b37f12a18f5875b2abc357c13"
    )
    counts = {"greater": 0, "less": 0, "between": 0}
    for market in MARKETS:
        parsed = parse_doge_strike(market, cutoff=NOW)
        counts[parsed.operator] += 1
        if parsed.operator != "between":
            SharedCryptoInputs(target(market), ()).bind_market(market, cutoff=NOW)
        assert (
            parsed.primary_rule_sha256
            == hashlib.sha256(market["rules_primary"].encode()).hexdigest()
        )
        for field, value in (("floor_strike", parsed.floor), ("cap_strike", parsed.cap)):
            if value is not None:
                assert str(value) == market["custom_strike"][field]
                assert value.as_tuple().exponent == -7
    assert counts == {"greater": 2, "less": 2, "between": 89}


@pytest.mark.parametrize("index", [0, 1, 47, 48])
def test_actual_strict_tail_binds_or_range_remains_diagnostic(index):
    market = MARKETS[index]
    parsed = parse_doge_strike(market, cutoff=NOW)
    if parsed.operator == "between":
        t = CryptoTarget(
            "DOGE", "RANGE", parsed.close_time, lower=float(parsed.floor), upper=float(parsed.cap)
        )
        with pytest.raises(ValueError, match="RANGE_INCLUSIVITY"):
            SharedCryptoInputs(t, ()).bind_market(market, cutoff=NOW)
    else:
        SharedCryptoInputs(target(market), ()).bind_market(market, cutoff=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"floor_strike": True},
        {"floor_strike": "NaN"},
        {"floor_strike": "Infinity"},
        {"floor_strike": "0.2649999junk"},
        {"floor_strike": "2.649999e-1"},
        {"floor_strike": "0.26499990"},
        {"floor_strike": "-0.2649999"},
        {"floor_strike": "0.0000000"},
        {"cap_strike": "0.3000000"},
        {"strike_type": "greater_or_equal"},
        {"unreviewed": "x"},
    ],
)
def test_nested_schema_and_numeric_mutations_refused(change):
    market = deepcopy(MARKETS[0])
    market["custom_strike"].update(change)
    with pytest.raises(ValueError):
        parse_doge_strike(market, cutoff=NOW)


@pytest.mark.parametrize(
    "old,new",
    [
        ("above", "at or above"),
        ("0.2649999", "0.2649998"),
        ("DOGEUSD_RTI", "BTCUSD_RTI"),
        ("60 second", "30 second"),
        ("2 AM", "3 AM"),
        ("EDT", "EST"),
        ("Sep 11", "Sep 12"),
    ],
)
def test_primary_rule_semantic_mutations_refused(old, new):
    market = deepcopy(MARKETS[0])
    market["rules_primary"] = market["rules_primary"].replace(old, new)
    with pytest.raises(ValueError):
        parse_doge_strike(market, cutoff=NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"floor_strike": "0.2649999"},
        {"cap_strike": False},
        {"event_ticker": "KXDOGE-26SEP1103"},
        {"series_ticker": "KXBTC"},
        {"status": "closed"},
        {"market_type": "scalar"},
        {"close_time": "2026-09-11T06:00:00"},
        {"ticker": "KXDOGE-26SEP1103-T0.2649999"},
    ],
)
def test_original_identity_clock_and_top_level_conflicts(change):
    market = dict(MARKETS[0], **change)
    with pytest.raises(ValueError):
        parse_doge_strike(market, cutoff=NOW)


def test_binding_requires_actual_cutoff_future_target_and_exact_comparator():
    market = MARKETS[0]
    t = target(market)
    inputs = SharedCryptoInputs(t, ())
    with pytest.raises(ValueError, match="EXPLICIT_INPUT_CUTOFF"):
        inputs.bind_market(market)
    for cutoff in (
        NOW.replace(tzinfo=None),
        t.observation_at,
        t.observation_at + timedelta(seconds=1),
    ):
        with pytest.raises(ValueError):
            inputs.bind_market(market, cutoff=cutoff)
    for changed in (
        replace(t, comparator="AT_OR_ABOVE"),
        replace(t, threshold=True),
        replace(t, threshold=0.265),
        replace(t, threshold=float("nan")),
        replace(t, threshold=float("inf")),
        replace(t, threshold="invalid"),
        replace(t, threshold="0.2649999"),
        replace(t, threshold=None),
        replace(t, observation_at=t.observation_at + timedelta(hours=1)),
    ):
        with pytest.raises(ValueError):
            SharedCryptoInputs(changed, ()).bind_market(market, cutoff=NOW)


def test_existing_non_doge_binding_unchanged():
    t = CryptoTarget("BTC", "ABOVE", NOW + timedelta(hours=1), threshold=100)
    SharedCryptoInputs(t, ()).bind_market(
        dict(
            ticker="KXBTC-E-T100",
            strike_type="greater",
            floor_strike=100,
            close_time=t.observation_at.isoformat(),
        )
    )


@pytest.mark.parametrize("change_metadata", [False, True])
def test_real_shared_caller_receipt_cutoff_and_custom_stability(
    harness, monkeypatch, change_metadata  # noqa: F811
):
    kwargs, urls, clock_value, _ = harness
    clock_value[0] = NOW
    rows = [
        [
            int((NOW.replace(second=0) - timedelta(minutes=300 - i)).timestamp()),
            0.25,
            0.28,
            0.26,
            0.26 + (i % 7) / 10000,
            1,
        ]
        for i in range(300)
    ]
    raw = json.dumps(rows).encode()
    end = NOW.replace(second=0)
    url = "https://api.exchange.coinbase.com/products/DOGE-USD/candles?" + urlencode(
        dict(granularity=60, start=(end - timedelta(minutes=300)).isoformat(), end=end.isoformat())
    )
    genuine_inputs = SharedCryptoInputs(
        target(MARKETS[0]), (CandleOriginal(raw, hashlib.sha256(raw).hexdigest(), url, NOW),)
    )
    original_get = kwargs["get"]
    original_bind = SharedCryptoInputs.bind_market
    observed_cutoffs = []

    def get(url):
        original_raw, status = original_get(url)
        value = json.loads(original_raw)
        if "market" in value:
            value["market"] = deepcopy(MARKETS[0])
            if change_metadata and len(urls) == 3:
                value["market"]["custom_strike"]["floor_strike"] = "0.2649998"
        return json.dumps(value).encode(), status

    def observed_bind(self, market, *, cutoff=None):
        observed_cutoffs.append(cutoff)
        return original_bind(self, market, cutoff=cutoff)

    monkeypatch.setattr(SharedCryptoInputs, "bind_market", observed_bind)
    kwargs.update(get=get, crypto_inputs=genuine_inputs, ticker=MARKETS[0]["ticker"])
    if change_metadata:
        with pytest.raises(ValueError, match="CONTRACT_METADATA_CHANGED"):
            capture.run(**kwargs)
        assert len(urls) == 3 and len(observed_cutoffs) == 1
        assert not (kwargs["output"] / "frozen").exists()
    else:
        result = capture.run(**kwargs)
        assert result["requests"] == 6 and len(observed_cutoffs) == 3
        proof = json.loads((kwargs["output"] / "code-proof.json").read_bytes())
        assert "src/kalshi_predictor/crypto/doge_strikes.py" in {r["path"] for r in proof["files"]}
    for i, cutoff in enumerate(observed_cutoffs):
        receipt = json.loads(
            (kwargs["output"] / f"sample-{i}-market.json.receipt.json").read_bytes()
        )
        assert cutoff == datetime.fromisoformat(receipt["received_at"])
