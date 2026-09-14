import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_cf_guarded_source_bridge import NOW, originals

from kalshi_predictor.overnight_paper.cf_source import (
    CLOCK_BASIS,
    VERIFIER,
    CFSourceContext,
    build_cf_source,
    verify_cf_source,
)
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.qualification import (
    EvidenceReference,
    GateEvidence,
    decision_fingerprint,
)


def reference(body):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return EvidenceReference("fixture-original", canonical_hash(body), raw)


def gate_fixture(change=None):
    raw, receipt, target = originals()
    source = build_cf_source(raw=raw, receipt=receipt, target=target, decision_at=NOW)
    checked = verify_cf_source(source, target=target, decision_at=NOW, now=NOW)
    decision = dict(
        category="Crypto", series="KXSOLE", ticker=target.rules.market_ticker,
        event_id=target.event_ticker, source_kind=CLOCK_BASIS, decision_at=NOW.isoformat(),
        source_hashes=[canonical_hash(source)], cf_input_sha256=checked["input_sha256"],
        cf_target_sha256=canonical_hash(checked["target"]),
    )
    if change:
        decision.update(change)
    identity = decision_fingerprint(decision)
    report = dict(
        schema="overnight-paper-gate-v1", gate=4, decision_id=identity,
        ticker=decision["ticker"], category="Crypto", verifier=VERIFIER, verdict="PASS",
        sources=[canonical_hash(source)], validated_at=NOW.isoformat(),
        valid_until=(NOW + timedelta(seconds=120)).isoformat(),
    )
    gate = GateEvidence(
        4, identity, "Crypto", decision["ticker"], VERIFIER, reference(report),
        sources=(reference(source),), context=CFSourceContext(target),
    )
    return gate, decision


def test_actual_source_gate_replays_cf_and_does_not_certify_other_gates():
    gate, decision = gate_fixture()
    assert gate.verified(decision, as_of=NOW)
    assert not replace(gate, gate=3).verified(decision, as_of=NOW)
    assert not replace(gate, gate=9).verified(decision, as_of=NOW)


@pytest.mark.parametrize("change", [
    {"ticker": "KXSOLE-OTHER-T100"}, {"event_id": "KXSOLE-OTHER"},
    {"series": "KXSOLPERP"}, {"cf_input_sha256": "0" * 64},
    {"cf_target_sha256": "0" * 64}, {"source_hashes": []},
    {"source_kind": "coinbase-btc-trade-closed-candles-v1"},
])
def test_recomputed_pass_report_cannot_override_source_semantics(change):
    gate, decision = gate_fixture(change)
    assert not gate.verified(decision, as_of=NOW)


def test_gate_rechecks_age_despite_unexpired_supplied_report():
    gate, decision = gate_fixture()
    assert not gate.verified(decision, as_of=NOW + timedelta(seconds=61))


def test_context_and_exact_original_are_required():
    gate, decision = gate_fixture()
    assert not replace(gate, context=None).verified(decision, as_of=NOW)
    assert not replace(gate, sources=()).verified(decision, as_of=NOW)
    changed = replace(gate.sources[0], payload=gate.sources[0].payload + b" ")
    assert not replace(gate, sources=(changed,)).verified(decision, as_of=NOW)
