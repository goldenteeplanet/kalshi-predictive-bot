import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from test_microstructure_research_capture import harness  # noqa: F401

from kalshi_predictor.crypto.doge_range_evidence import (
    PROFILE_AVAILABLE_AT,
    TERMS_SHA256,
    DogeOriginal,
    DogeRangeProof,
    strict_json,
)
from kalshi_predictor.crypto.shared_capture import SharedCryptoInputs
from kalshi_predictor.crypto.shared_capture_manifest import load_crypto_manifest
from kalshi_predictor.forecasting.crypto_v3_independent import (
    CryptoTarget,
    PriceObservation,
    forecast_independent,
)
from kalshi_predictor.microstructure import research_capture as capture

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 11, 6, 40, tzinfo=UTC)


def proof():
    p = FIXTURES / "doge-terms"
    return DogeRangeProof(
        DogeOriginal((p / "series.json").read_bytes(), (p / "series.receipt.json").read_bytes()),
        DogeOriginal(
            (p / "bound-terms.pdf").read_bytes(), (p / "bound-terms.receipt.json").read_bytes()
        ),
    )


def market():
    # Synthetic later event retains actual captured grammar, not an original future capture.
    m = next(
        deepcopy(x)
        for x in json.loads((FIXTURES / "doge-markets-20260911.json").read_bytes())["markets"]
        if x["custom_strike"]["strike_type"] == "between"
    )
    m["ticker"] = m["ticker"].replace("26SEP1102", "26SEP1103")
    m["event_ticker"] = m["event_ticker"].replace("26SEP1102", "26SEP1103")
    m["close_time"] = "2026-09-11T07:00:00Z"
    m["rules_primary"] = m["rules_primary"].replace("2 AM", "3 AM")
    return m


def original(m, **changes):
    raw = json.dumps({"market": m}).encode()
    receipt = dict(
        url="https://api.elections.kalshi.com/trade-api/v2/markets/" + m["ticker"],
        status=200,
        sha256=hashlib.sha256(raw).hexdigest(),
        requested_at=NOW.isoformat(),
        received_at=NOW.isoformat(),
    )
    receipt.update(changes)
    return DogeOriginal(raw, json.dumps(receipt).encode())


def target(m):
    return CryptoTarget(
        "DOGE",
        "RANGE_CLOSED",
        datetime.fromisoformat(m["close_time"]),
        lower=float(m["custom_strike"]["floor_strike"]),
        upper=float(m["custom_strike"]["cap_strike"]),
    )


def test_genuine_reviewed_terms_and_future_market_binding():
    m = market()
    p = proof()
    e = p.bind_market(m, original(m), NOW)
    assert (
        e["terms_sha256"] == TERMS_SHA256 and e["available_at"] == NOW.isoformat()
    )
    assert e["payout_interval"] == "CLOSED_CLOSED" and not e["execution_authority"]
    assert e["rounding_rule"] is None and not e["settlement_reconstructed"]
    assert (
        SharedCryptoInputs(target(m), (), p).bind_market(m, cutoff=NOW, market_original=original(m))
        == e
    )
    with pytest.raises(ValueError):
        SharedCryptoInputs(target(m), (), p).bind_market(m, cutoff=NOW)
    with pytest.raises(ValueError):
        SharedCryptoInputs(replace(target(m), comparator="RANGE"), (), p).bind_market(
            m, cutoff=NOW, market_original=original(m)
        )


@pytest.mark.parametrize(
    "change",
    [
        "pdf",
        "link",
        "series",
        "termsclock",
        "reviewclock",
        "duplicate",
        "overflow",
        "marketclock",
        "marketurl",
        "marketstatus",
        "marketduplicate",
        "marketmismatch",
        "serieshash",
    ],
)
def test_proof_tampering_fail_closed(change):
    p = proof()
    m = market()
    mo = original(m)
    cutoff = NOW
    if change == "pdf":
        p = replace(p, terms=replace(p.terms, raw=p.terms.raw + b"x"))
    elif change in ("link", "series"):
        s = strict_json(p.series.raw)
        s["series"]["contract_terms_url" if change == "link" else "ticker"] = "wrong"
        raw = json.dumps(s).encode()
        receipt = strict_json(p.series.receipt_raw)
        receipt["sha256"] = hashlib.sha256(raw).hexdigest()
        p = replace(p, series=DogeOriginal(raw, json.dumps(receipt).encode()))
    elif change in ("termsclock", "serieshash"):
        r = strict_json(p.terms.receipt_raw)
        r["received_at" if change == "termsclock" else "series_original_sha256"] = (
            "2099-01-01T00:00:00Z" if change == "termsclock" else "a" * 64
        )
        p = replace(p, terms=replace(p.terms, receipt_raw=json.dumps(r).encode()))
    elif change == "reviewclock":
        cutoff = PROFILE_AVAILABLE_AT - timedelta(microseconds=1)
    elif change == "duplicate":
        p = replace(
            p,
            series=replace(
                p.series, receipt_raw=p.series.receipt_raw.rstrip()[:-1] + b',"status":200}'
            ),
        )
    elif change == "overflow":
        p = replace(
            p,
            series=replace(
                p.series, receipt_raw=p.series.receipt_raw.rstrip()[:-1] + b',"ignored":1e999}'
            ),
        )
    elif change == "marketclock":
        mo = original(m, requested_at=(NOW + timedelta(seconds=1)).isoformat())
    elif change == "marketurl":
        mo = original(
            m, url="https://api.elections.kalshi.com:443/trade-api/v2/markets/" + m["ticker"]
        )
    elif change == "marketstatus":
        mo = original(m, status=True)
    elif change == "marketduplicate":
        raw = mo.raw[:-1] + b',"market":' + json.dumps(m).encode() + b"}"
        r = strict_json(mo.receipt_raw)
        r["sha256"] = hashlib.sha256(raw).hexdigest()
        mo = DogeOriginal(raw, json.dumps(r).encode())
    elif change == "marketmismatch":
        m["volume"] = 123456
    with pytest.raises(ValueError):
        p.bind_market(m, mo, cutoff)


@pytest.mark.parametrize(
    "lower,upper,expected_closed,expected_legacy",
    [(1.0, 2.0, 0.5, 0), (0.5, 1.0, 0.5, 0.5), (2.01, 3.0, 0, 0), (0.1, 0.5, 0.5, 0)],
)
def test_empirical_exact_endpoint_atoms_preserve_interval_semantics(
    lower, upper, expected_closed, expected_legacy
):
    # Alternating 1/2 prices ending at1: projected one-step atoms exactly .5 and2.
    prices = [
        PriceObservation(
            1.0 if i % 2 == 0 else 2.0,
            NOW - timedelta(minutes=60 - i),
            NOW,
            "test",
            "a" * 64,
            "DOGE",
        )
        for i in range(61)
    ]
    t = CryptoTarget("DOGE", "RANGE_CLOSED", NOW + timedelta(minutes=1), lower=lower, upper=upper)
    a = forecast_independent(prices, t, decision_at=NOW)
    b = forecast_independent(prices, replace(t, comparator="RANGE"), decision_at=NOW)
    assert a["comparisons"]["empirical_matched_horizon"]["probability"] == expected_closed
    assert b["comparisons"]["empirical_matched_horizon"]["probability"] == expected_legacy
    assert a["model_version"] == b["model_version"] == "3-exact-empirical-boundaries"
    for name in ("gaussian_log_returns", "student_t_df3", "existing_distribution_zero_drift"):
        assert a["comparisons"][name]["probability"] == b["comparisons"][name]["probability"]


def test_real_manifest_loader_and_capture_archive_exact_proof(harness, tmp_path):  # noqa: F811
    kwargs, urls, clock_value, _ = harness
    clock_value[0] = NOW
    m = market()
    p = proof()
    rows = [
        [
            int((NOW - timedelta(minutes=300 - i)).timestamp()),
            0.20,
            0.30,
            0.25,
            0.25 + (i % 7) / 10000,
            1,
        ]
        for i in range(300)
    ]
    raw = json.dumps(rows).encode()
    (tmp_path / "candles.json").write_bytes(raw)
    spec = {}
    for role, o in [("series", p.series), ("terms", p.terms)]:
        (tmp_path / (role + ".raw")).write_bytes(o.raw)
        (tmp_path / (role + ".receipt.json")).write_bytes(o.receipt_raw)
        spec[role] = {"path": role + ".raw", "receipt_path": role + ".receipt.json"}
    t = target(m)
    manifest = {
        "schema": "shared-crypto-input-manifest-v1",
        "target": dict(
            symbol=t.symbol,
            comparator=t.comparator,
            observation_at=t.observation_at.isoformat(),
            lower=t.lower,
            upper=t.upper,
        ),
        "doge_range_proof": spec,
        "originals": [
            dict(
                path="candles.json",
                sha256=hashlib.sha256(raw).hexdigest(),
                url="https://api.exchange.coinbase.com/products/DOGE-USD/candles?"
                + urlencode(
                    dict(
                        granularity=60,
                        start=(NOW - timedelta(minutes=300)).isoformat(),
                        end=NOW.isoformat(),
                    )
                ),
                received_at=NOW.isoformat(),
                status=200,
            )
        ],
    }
    mp = tmp_path / "inputs.json"
    mp.write_text(json.dumps(manifest))
    inputs = load_crypto_manifest(mp)
    get_original = kwargs["get"]

    def get(url):
        raw, status = get_original(url)
        j = json.loads(raw)
        if "market" in j:
            j["market"] = m
        return json.dumps(j).encode(), status

    kwargs.update(get=get, crypto_inputs=inputs, ticker=m["ticker"])
    result = capture.run(**kwargs)
    assert result["requests"] == 6
    frozen = json.loads((kwargs["output"] / "frozen/prediction.json").read_bytes())["prediction"]
    assert frozen["shared_crypto"]["model_version"] == "3-exact-empirical-boundaries"
    for i in range(3):
        b = json.loads((kwargs["output"] / f"sample-{i}-doge-range-binding.json").read_bytes())
        assert (
            b["market_sha256"]
            == hashlib.sha256(
                (kwargs["output"] / f"sample-{i}-market.json").read_bytes()
            ).hexdigest()
        )
        assert (
            b["market_receipt_sha256"]
            == hashlib.sha256(
                (kwargs["output"] / f"sample-{i}-market.json.receipt.json").read_bytes()
            ).hexdigest()
        )
        assert f"sample-{i}-doge-range-binding.json" in frozen["protocol_artifact_hashes"]
    assert (kwargs["output"] / "doge-terms.original").read_bytes() == p.terms.raw
    proof_paths = {x["path"] for x in frozen["code_proof"]["files"]}
    assert "src/kalshi_predictor/crypto/doge_range_evidence.py" in proof_paths
