from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from test_cf_process_inputs import encode, fixture
from test_settlement_target_research_router import NOW, target

from kalshi_predictor.crypto.cf_process_inputs import digest
from kalshi_predictor.crypto.settlement_target import _json
from kalshi_predictor.overnight_paper.cf_source import (
    CFSourceContext,
    build_cf_source,
    verify_cf_source,
)


def originals():
    declared = target()
    market = _json(declared.market_original)
    market["market"].update(ticker="KXSOLE-E-T100", event_ticker="KXSOLE-E")
    declared = replace(
        declared, symbol="SOL", event_ticker="KXSOLE-E", market_original=encode(market),
        rules=replace(declared.rules, index_id="SOLUSD_RTI", market_ticker="KXSOLE-E-T100"),
    )
    body, receipt = fixture()
    raw = encode(body)
    receipt.update(
        index_id="SOLUSD_RTI", url=receipt["url"].replace("BRTI", "SOLUSD_RTI"),
        source_sha256=digest(raw),
    )
    return raw, encode(receipt), declared


def test_sol_originals_reconstruct_without_forecasting_or_rule_certification(monkeypatch):
    import kalshi_predictor.crypto.settlement_average_model as model

    def forbidden(*args, **kwargs):
        raise AssertionError("source bridge must not forecast")

    monkeypatch.setattr(model, "forecast_benchmark_average", forbidden)
    raw, receipt, declared = originals()
    source = build_cf_source(raw=raw, receipt=receipt, target=declared, decision_at=NOW)
    checked = verify_cf_source(source, target=declared, decision_at=NOW, now=NOW)
    assert checked["inputs"]["index_id"] == "SOLUSD_RTI"
    assert checked["inputs"]["level"] == "101.00"
    assert checked["target"]["paper_eligible"] is False
    assert checked["target"]["settlement_rule_binding"]["unresolved_fields"]
    assert checked["execution_authority"] is False
    assert source["provider_generated_at"] is None
    assert source["provider_updated_at"] is None
    assert bytes.fromhex(source["body"]["cf"]["payload_hex"]) == raw


@pytest.mark.parametrize("field,value", [
    ("provider_generated_at", NOW.isoformat()),
    ("provider_updated_at", NOW.isoformat()),
    ("settlement_truth", True),
    ("clock_basis", "coinbase-btc-trade-closed-candles-v1"),
    ("available_at", (NOW - timedelta(seconds=1)).isoformat()),
])
def test_relabelled_bundle_rejected(field, value):
    raw, receipt, declared = originals()
    source = build_cf_source(raw=raw, receipt=receipt, target=declared, decision_at=NOW)
    source[field] = value
    with pytest.raises(ValueError, match="RECONSTRUCTION"):
        verify_cf_source(source, target=declared, decision_at=NOW, now=NOW)


def test_changed_inputs_and_originals_rejected():
    raw, receipt, declared = originals()
    source = build_cf_source(raw=raw, receipt=receipt, target=declared, decision_at=NOW)
    altered = deepcopy(source)
    altered["inputs"]["level"] = "1000000"
    with pytest.raises(ValueError, match="RECONSTRUCTION"):
        verify_cf_source(altered, target=declared, decision_at=NOW, now=NOW)
    source["body"]["cf"]["payload_hex"] += "20"
    with pytest.raises(ValueError, match="ORIGINAL_HASH"):
        verify_cf_source(source, target=declared, decision_at=NOW, now=NOW)


def test_freshness_rechecked_at_use_and_no_future_visibility():
    raw, receipt, declared = originals()
    source = build_cf_source(raw=raw, receipt=receipt, target=declared, decision_at=NOW)
    with pytest.raises(ValueError, match="STALE"):
        verify_cf_source(source, target=declared, decision_at=NOW, now=NOW + timedelta(seconds=61))
    with pytest.raises(ValueError):
        build_cf_source(
            raw=raw, receipt=receipt, target=declared, decision_at=NOW - timedelta(seconds=1),
        )


def test_wrong_asset_and_target_window_rejected():
    raw, receipt, declared = originals()
    source = build_cf_source(raw=raw, receipt=receipt, target=declared, decision_at=NOW)
    with pytest.raises(ValueError, match="SOL_TARGET"):
        verify_cf_source(source, target=target(), decision_at=NOW, now=NOW)
    changed = replace(declared, rules=replace(declared.rules, decimal_places=3))
    with pytest.raises(ValueError, match="RECONSTRUCTION"):
        verify_cf_source(source, target=changed, decision_at=NOW, now=NOW)


def test_target_context_round_trip_preserves_originals_and_unknown_rule():
    import json

    raw, receipt, target = originals()
    record = CFSourceContext(target).to_record(decision_at=NOW)
    restored = CFSourceContext.from_record(json.loads(json.dumps(record)), decision_at=NOW)
    assert restored.target == target
    assert restored.target.rule_original == target.rule_original
    assert restored.target.market_original == target.market_original
    assert record["target"]["settlement_rule_binding"]["unresolved_fields"]
    source = build_cf_source(raw=raw, receipt=receipt, target=target, decision_at=NOW)
    assert verify_cf_source(source, target=restored.target, decision_at=NOW, now=NOW)


@pytest.mark.parametrize("change", ["rule", "market", "status", "extra"])
def test_target_context_rejects_lost_or_relabelled_originals(change):
    _, _, target = originals()
    record = CFSourceContext(target).to_record(decision_at=NOW)
    if change in {"rule", "market"}:
        record[change + "_original"]["payload_hex"] += "20"
    elif change == "status":
        record["target"]["settlement_rule_binding"]["status"] = "CERTIFIED"
    else:
        record["approved"] = True
    with pytest.raises(ValueError):
        CFSourceContext.from_record(record, decision_at=NOW)
