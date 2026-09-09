"""Synthetic policies exercise mechanics; no production fee certification."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.paper import fees

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


def synthetic_evidence(monkeypatch, now=NOW, ticker="M"):
    original = b"SYNTHETIC reviewed rate .07, no settlement fee; not official evidence"
    sha = hashlib.sha256(original).hexdigest()
    policy = fees.CertifiedFeePolicy(
        "synthetic-only",
        (now - timedelta(days=1)).isoformat(),
        (now + timedelta(days=1)).isoformat(),
        "0.07",
        ((fees.ROUNDING_URL, sha),),
        sha,
        sha,
        sha,
        "0",
    )
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (policy,))
    bodies = {
        f"/markets/{ticker}": {
            "market": {
                "ticker": ticker,
                "event_ticker": "E",
                "status": "open",
                "close_time": (now + timedelta(hours=1)).isoformat(),
            }
        },
        "/events/E": {
            "event": {
                "event_ticker": "E",
                "series_ticker": "S",
                "fee_type_override": None,
                "fee_multiplier_override": None,
            }
        },
        "/series/S": {"series": {"ticker": "S", "fee_type": "quadratic", "fee_multiplier": "1"}},
    }
    captures = []
    for path, body in bodies.items():
        raw = json.dumps(
            {"url": fees.PUBLIC_BASE + path, "received_at": now.isoformat(), "body": body}
        ).encode()
        captures.append({"payload_hex": raw.hex(), "sha256": hashlib.sha256(raw).hexdigest()})
    return {
        "policy_version": policy.version,
        "documents": [
            {
                "url": fees.ROUNDING_URL,
                "received_at": now.isoformat(),
                "payload_hex": original.hex(),
            }
        ],
        "captures": captures,
    }


@pytest.fixture
def evidence(monkeypatch):
    return synthetic_evidence(monkeypatch)


def quote(evidence, floor="0"):
    return fees.build_fee_quote(
        evidence=evidence,
        ticker="M",
        side="BUY_YES",
        price=Decimal(".30"),
        simulator_floor=Decimal(floor),
        now=NOW,
    )


def test_charge_floor_and_exact_cost(evidence):
    q = quote(evidence)
    assert q.charge == Decimal(".02")
    assert q.decode()["fee_decomposition"]["trade_fee"] == "0.014700"
    assert Decimal(1) - Decimal(".30") - q.charge == Decimal(".68")
    assert -Decimal(".30") - q.charge == Decimal("-.32")
    assert quote(evidence, ".03").charge == Decimal(".03")


def test_no_production_policy(evidence, monkeypatch):
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", ())
    with pytest.raises(ValueError, match="REVIEWED_FEE_POLICY_REQUIRED"):
        quote(evidence)


def test_missing_override_is_unknown(evidence):
    capture = evidence["captures"][1]
    row = json.loads(bytes.fromhex(capture["payload_hex"]))
    del row["body"]["event"]["fee_multiplier_override"]
    raw = json.dumps(row).encode()
    capture.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="OVERRIDE_INVALID"):
        quote(evidence)


def test_activation_clock_and_historical_lineage(evidence):
    q = quote(evidence)
    args = dict(ticker="M", side="BUY_YES", quantity=1, price=Decimal(".30"))
    assert fees.historical_fee_quote(q.decode(), **args) == q
    with pytest.raises(ValueError, match="STALE"):
        fees.verify_fee_quote(
            q.decode(), **args, simulator_floor=Decimal(0), now=NOW + timedelta(seconds=61)
        )
    payload = q.decode()
    payload["simulated_charge"] = "0"
    with pytest.raises(ValueError, match="RECOMPUTATION"):
        fees.historical_fee_quote(payload, **args)


def test_fresh_quote_does_not_extend_original_clock(evidence):
    late = NOW + timedelta(seconds=40)
    q = fees.build_fee_quote(
        evidence=evidence,
        ticker="M",
        side="BUY_YES",
        price=Decimal(".30"),
        simulator_floor=Decimal(0),
        now=late,
    )
    with pytest.raises(ValueError, match="CAPTURE_AMBIGUOUS_OR_STALE"):
        fees.verify_fee_quote(
            q.decode(),
            ticker="M",
            side="BUY_YES",
            quantity=1,
            price=Decimal(".30"),
            simulator_floor=Decimal(0),
            now=NOW + timedelta(seconds=70),
        )


@pytest.mark.parametrize(
    "field,value",
    [("ticker", "OTHER"), ("side", "SELL_YES"), ("quantity", 2), ("price", Decimal(".31"))],
)
def test_quote_cannot_move_to_other_order(evidence, field, value):
    args = dict(ticker="M", side="BUY_YES", quantity=1, price=Decimal(".30"))
    args[field] = value
    with pytest.raises(ValueError, match="MISMATCH"):
        fees.historical_fee_quote(quote(evidence).decode(), **args)


def test_new_guarded_missing_contract_rejected():
    with pytest.raises(ValueError, match="EVIDENCE_REQUIRED"):
        fees.decision_fee_quote(
            {},
            ticker="M",
            side="BUY_YES",
            quantity=1,
            price=Decimal(".30"),
            simulator_floor=Decimal(0),
            now=NOW,
            required=True,
        )


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("bad", ["-0.01", "NaN", "Infinity", "-Infinity"])
def test_nonfinite_and_negative_math_inputs_rejected(index, bad):
    values = [Decimal(".30"), Decimal("1"), Decimal(".07")]
    values[index] = Decimal(bad)
    with pytest.raises(ValueError, match="FINITE_NONNEGATIVE"):
        fees.single_buy_fees(*values)


@pytest.mark.parametrize(
    "price,rate,trade,debit",
    [
        ("0", ".07", "0.000000", "0.00"),
        ("1", ".07", "0.000000", "1.00"),
        (".5", ".04", "0.010000", "0.51"),
        (".5", ".0400001", "0.010001", "0.52"),
        (".3001", "0", "0.000000", "0.31"),
    ],
)
def test_rounding_discontinuities_are_explicit(price, rate, trade, debit):
    result = fees.single_buy_fees(Decimal(price), Decimal(1), Decimal(rate))
    assert result["trade_fee"] == Decimal(trade)
    assert result["total_debit"] == Decimal(debit)
    assert result["estimated_fee"] == Decimal(debit) - Decimal(price)


def test_out_of_range_price_rejected():
    with pytest.raises(ValueError, match="PRICE_OUT_OF_RANGE"):
        fees.single_buy_fees(Decimal("1.01"), Decimal(1), Decimal(".07"))


def test_tampered_document_original_rejected(evidence):
    evidence["documents"][0]["payload_hex"] = b"changed original".hex()
    with pytest.raises(ValueError, match="ORIGINAL_DOCUMENT_MISMATCH"):
        quote(evidence)


def test_tampered_catalog_original_rejected(evidence):
    evidence["captures"][0]["payload_hex"] = b"changed original".hex()
    with pytest.raises(ValueError, match="CAPTURE_HASH_MISMATCH"):
        quote(evidence)


def test_unregistered_rate_change_cannot_reuse_policy_identity(evidence, monkeypatch):
    from dataclasses import replace

    changed = replace(fees.CERTIFIED_FEE_POLICIES[0], taker_rate="0")
    monkeypatch.setattr(fees, "CERTIFIED_FEE_POLICIES", (changed,))
    with pytest.raises(ValueError, match="REVIEWED_FEE_POLICY_REQUIRED"):
        quote(evidence)


def test_later_validation_cannot_admit_original_received_after_quote(evidence):
    q = quote(evidence).decode()
    capture = q["evidence"]["captures"][0]
    row = json.loads(bytes.fromhex(capture["payload_hex"]))
    row["received_at"] = (NOW + timedelta(seconds=10)).isoformat()
    raw = json.dumps(row).encode()
    capture.update(payload_hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="CAPTURE_AMBIGUOUS_OR_STALE"):
        fees.verify_fee_quote(
            q,
            ticker="M",
            side="BUY_YES",
            quantity=1,
            price=Decimal(".30"),
            simulator_floor=Decimal(0),
            now=NOW + timedelta(seconds=20),
        )
