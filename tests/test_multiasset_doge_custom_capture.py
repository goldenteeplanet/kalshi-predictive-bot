import json
from datetime import timedelta
from decimal import Decimal

import pytest
from test_multiasset_capture import NOW, TARGET, setup

from kalshi_predictor.crypto import multiasset_capture as C
from kalshi_predictor.crypto import multiasset_outcomes as O


def doge_fixture():
    plan, original_transport, calls = setup()
    plan.update(symbol="DOGE", benchmark="DOGEUSD_RTI", event="KXDOGE-26SEP1123")
    for rule in plan["rule_hypotheses"]:
        rule.update(family="KXDOGE", benchmark_id="DOGEUSD_RTI", final_precision=".00001")
    markets = []
    for index, (lo, hi) in enumerate([("0.1000000", "0.1049999"), ("0.1050000", "0.1099999")]):
        markets.append(
            {
                "ticker": plan["event"] + "-B" + str(index),
                "event_ticker": plan["event"],
                "market_type": "binary",
                "status": "active",
                "close_time": TARGET.isoformat(),
                "strike_type": "custom",
                "floor_strike": None,
                "cap_strike": None,
                "custom_strike": {"strike_type": "between", "floor_strike": lo, "cap_strike": hi},
                "rules_primary": (
                    "If there is a 60 second average of CF Benchmarks' "
                    "Dogecoin Real-Time Index (DOGEUSD_RTI) before 11 PM EDT is between "
                    + lo
                    + "-"
                    + hi
                    + " at 11 PM EDT on Sep 11, 2026, then the market resolves to Yes."
                ),
            }
        )

    def transport(url, timeout):
        status, raw = original_transport(url, timeout)
        if "/cfbenchmarks/" in url:
            data = json.loads(raw)
            for i, row in enumerate(data["data"]["payload"]):
                row["value"] = "0.1049" if i % 2 else "0.1051"
            return status, C.encode(data)
        if "/orderbook" not in url:
            return status, C.encode({"markets": markets, "cursor": ""})
        return status, raw

    return plan, transport, calls, markets


def test_custom_range_catalog_produces_research_only_capture(tmp_path):
    plan, transport, calls, _ = doge_fixture()
    result = C.capture(tmp_path / "capture", plan, transport, clock=lambda: NOW)
    assert result["decisions"] == 4 and len(calls) == 6
    for p in (tmp_path / "capture").glob("decision-*.json"):
        decision = json.loads(p.read_bytes())
        assert decision["rule_status"] == "UNCERTIFIED"
        assert decision["paper_eligible"] is False


def test_custom_rule_mismatch_is_rejected_before_cf_request(tmp_path):
    plan, transport, calls, markets = doge_fixture()
    markets[0]["custom_strike"]["floor_strike"] = "0.0900000"
    with pytest.raises(ValueError, match="RULE_METADATA_MISMATCH"):
        C.capture(tmp_path / "capture", plan, transport, clock=lambda: NOW)
    assert len(calls) == 1


def test_conflicting_top_level_custom_bounds_are_not_silently_normalized(tmp_path):
    plan, transport, calls, markets = doge_fixture()
    markets[0]["floor_strike"] = "0.1000000"
    with pytest.raises(ValueError, match="CONTRADICTORY_TOP_LEVEL"):
        C.capture(tmp_path / "capture", plan, transport, clock=lambda: NOW)
    assert len(calls) == 1


def test_custom_bounds_remain_bound_through_official_outcome(tmp_path):
    plan, transport, _, markets = doge_fixture()
    root = tmp_path / "capture"
    C.capture(root, plan, transport, clock=lambda: NOW)
    plan_sha = C.digest((root / "protocol.json").read_bytes())
    pin = C.encode(
        {
            "event": plan["event"],
            "target_at": TARGET.isoformat(),
            "at": NOW.isoformat(),
            "protocol_sha256": plan_sha,
            "completion_sha256": C.digest((root / "completion.json").read_bytes()),
        }
    )
    receipt = C.encode(
        {"at": (NOW + timedelta(seconds=1)).isoformat(), "pin_sha256": C.digest(pin)}
    )

    def official(url, timeout):
        market = next(m for m in markets if url.endswith("/" + m["ticker"]))
        strike = market["custom_strike"]
        yes = Decimal(strike["floor_strike"]) <= Decimal(".1055") <= Decimal(strike["cap_strike"])
        return 200, C.encode(
            {
                "market": {
                    **market,
                    "status": "finalized",
                    "result": "yes" if yes else "no",
                    "settlement_value_dollars": "1" if yes else "0",
                    "settlement_ts": (TARGET + timedelta(minutes=2)).isoformat(),
                }
            }
        )

    result = O.collect(
        tmp_path / "outcome",
        root,
        pin,
        receipt,
        plan_sha,
        official,
        clock=lambda: TARGET + timedelta(minutes=10),
    )
    assert result == {"rows": 20, "requests": 2}
