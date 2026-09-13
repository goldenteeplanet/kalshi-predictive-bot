import json
import sqlite3
from dataclasses import asdict, replace

import pytest
from test_settlement_average_model import process
from test_settlement_target_research_router import NOW, target

from kalshi_predictor.crypto.settlement_average_model import forecast_benchmark_average
from kalshi_predictor.crypto.settlement_rule_version import SOLSettlementRuleVersion


def sol_target():
    original = target()
    market = json.loads(original.market_original)
    market["market"].update(ticker="KXSOLE-E-T100", event_ticker="KXSOLE-E")
    return replace(
        original,
        symbol="SOL",
        event_ticker="KXSOLE-E",
        rules=replace(original.rules, market_ticker="KXSOLE-E-T100", index_id="SOLUSD_RTI"),
        market_original=json.dumps(market).encode(),
    )


def test_actual_average_forecast_carries_uncertified_sol_semantics():
    candidate = sol_target()
    result = forecast_benchmark_average(
        candidate, replace(process(), symbol="SOL", index_id="SOLUSD_RTI"), as_of=NOW
    )
    binding = result["target"]["settlement_rule_binding"]
    rule = SOLSettlementRuleVersion(**binding["rule"])
    assert binding["version_id"] == rule.version_id
    assert binding["status"] == "UNCERTIFIED"
    assert rule.field_evidence == ()
    assert rule.sample_precision is None and rule.missing_sample_behavior is None
    assert rule.effective_from is None and rule.authority_version is None
    assert "include_start" in binding["unresolved_fields"]
    assert not result["paper_eligible"] and not result["execution_authority"]
    assert not result["settlement_alignment_certified"]
    assert asdict(candidate) == asdict(sol_target())


def test_sol_rule_id_changes_with_declared_membership_and_rounding():
    candidate = sol_target()
    first = candidate.validate(as_of=NOW, include_sol_rule_binding=True)[
        "settlement_rule_binding"
    ]["version_id"]
    changed = replace(
        candidate,
        rules=replace(
            candidate.rules,
            closing=replace(candidate.rules.closing, include_start=False, include_end=True),
        ),
    )
    assert changed.validate(as_of=NOW, include_sol_rule_binding=True)[
        "settlement_rule_binding"
    ]["version_id"] != first
    changed = replace(candidate, rules=replace(candidate.rules, rounding="HALF_EVEN"))
    assert changed.validate(as_of=NOW, include_sol_rule_binding=True)[
        "settlement_rule_binding"
    ]["version_id"] != first


@pytest.mark.parametrize("index", ["BRTI", "SOLPERP", "SOLUSD_RTI_OTHER"])
def test_sol_cannot_bind_a_different_benchmark(index):
    candidate = sol_target()
    with pytest.raises(ValueError, match="SOL_EVENT_RULE_IDENTITY_REQUIRED"):
        replace(candidate, rules=replace(candidate.rules, index_id=index)).validate(as_of=NOW)


def test_sol_event_cannot_be_labeled_as_another_symbol():
    with pytest.raises(ValueError, match="SOL_EVENT_TARGET_IDENTITY_REQUIRED"):
        replace(sol_target(), symbol="BTC").validate(as_of=NOW)


def test_non_sol_target_does_not_acquire_sol_binding():
    assert "settlement_rule_binding" not in target().validate(as_of=NOW)


def test_existing_target_fingerprint_shape_is_preserved_for_frozen_replay():
    candidate = sol_target()
    legacy = candidate.validate(as_of=NOW)
    current = candidate.validate(as_of=NOW, include_sol_rule_binding=True)
    assert "settlement_rule_binding" not in legacy
    current.pop("settlement_rule_binding")
    assert current == legacy


def test_actual_sol_capture_persists_rule_binding_and_its_source(tmp_path, monkeypatch):
    from test_cf_average_shadow_capture import harness

    capture, out, *_ = harness(tmp_path, monkeypatch)
    capture()
    with sqlite3.connect(out / "research.db") as db:
        rows = db.execute("SELECT payload FROM research_shadow").fetchall()
    assert len(rows) == 4
    for (raw,) in rows:
        stored = json.loads(raw)
        decision = stored["decision"]
        binding = decision["forecast"]["target"]["settlement_rule_binding"]
        assert binding["rule"]["family"] == "KXSOLE"
        assert binding["rule"]["benchmark_id"] == "SOLUSD_RTI"
        assert binding["status"] == "UNCERTIFIED"
        assert "crypto.settlement_rule_version" in stored["source_originals"]
        assert not decision["paper_eligible"] and not decision["execution_authority"]
