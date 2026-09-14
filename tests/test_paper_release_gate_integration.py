"""Actual rule/provenance dispatch; synthetic policies never enter production registry."""

from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from test_overnight_provenance import artifact
from test_paper_release_provenance import complete_inputs
from test_paper_release_rules_timing import fixture

from kalshi_predictor.overnight_paper import rule_verifier
from kalshi_predictor.overnight_paper.gate_context import QualificationContext
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.qualification import (
    SEMANTIC_VERIFIERS,
    EvidenceReference,
    GateEvidence,
)


def integrated_inputs(tmp_path, monkeypatch):
    args = complete_inputs()
    inputs, provenance = args["decision"], args["context"]
    _, policy, document = fixture()
    policy = replace(
        policy,
        ticker=inputs["ticker"],
        event_id=inputs["event_id"],
        series=inputs["series"],
        observation_time=inputs["close_time"],
    )
    monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", (policy,))
    inputs.update(
        category="Climate and Weather",
        rule_version=policy.version,
        settlement_rule=asdict(policy),
        observation_time=policy.observation_time,
        market_open_time=(args["now"] - timedelta(hours=1)).isoformat(),
        market_close_time=policy.observation_time,
        expected_settlement_time=(args["now"] + timedelta(hours=1, seconds=60)).isoformat(),
        settlement_deadline=(args["now"] + timedelta(hours=1, seconds=120)).isoformat(),
        final_settlement_time=None,
    )
    inputs["settlement_rule"]["amendments"] = []
    forecast = provenance.artifacts["forecast"].decode()
    forecast["rule_version"] = policy.version
    artifacts = dict(provenance.artifacts, forecast=artifact(forecast))
    inputs["forecast_artifact_sha256"] = artifacts["forecast"].sha256
    context = QualificationContext(
        tmp_path,
        (document,),
        replace(provenance, artifacts=artifacts, expected_rule_version=policy.version),
        args["phase3m"],
        args["phase3n"],
    )
    return inputs, context, args["now"]


def evidence(gate, inputs, context, now):
    original = context.provenance.source_artifacts[0]
    source = EvidenceReference("original-source", original.sha256, original.payload)
    report = artifact(
        dict(
            schema="overnight-paper-gate-v1",
            gate=gate,
            decision_id=canonical_hash(inputs),
            ticker=inputs["ticker"],
            category=inputs["category"],
            verifier=SEMANTIC_VERIFIERS[gate],
            verdict="PASS",
            validated_at=now.isoformat(),
            valid_until=(now + timedelta(seconds=60)).isoformat(),
            sources=[source.sha256],
        )
    )
    return GateEvidence(
        gate,
        canonical_hash(inputs),
        inputs["category"],
        inputs["ticker"],
        SEMANTIC_VERIFIERS[gate],
        EvidenceReference("report", report.sha256, report.payload),
        sources=(source,),
        context=context,
    )


@pytest.mark.parametrize("gate", [3, 9])
def test_actual_registered_verifiers_accept_complete_original_fixture(tmp_path, monkeypatch, gate):
    inputs, context, now = integrated_inputs(tmp_path, monkeypatch)
    assert evidence(gate, inputs, context, now).verified(inputs, as_of=now)


@pytest.mark.parametrize("gate", [3, 9, 12])
def test_pass_report_without_original_context_is_not_evidence(tmp_path, monkeypatch, gate):
    inputs, context, now = integrated_inputs(tmp_path, monkeypatch)
    item = evidence(gate, inputs, context, now)
    assert not replace(item, context=None).verified(inputs, as_of=now)


def test_rule_swap_rejected_even_with_new_decision_and_pass_report(tmp_path, monkeypatch):
    inputs, context, now = integrated_inputs(tmp_path, monkeypatch)
    inputs["rule_version"] = "forged"
    assert not evidence(3, inputs, context, now).verified(inputs, as_of=now)
    assert not evidence(9, inputs, context, now).verified(inputs, as_of=now)


def test_rehashed_forecast_swap_is_rejected_by_gate9(tmp_path, monkeypatch):
    inputs, context, now = integrated_inputs(tmp_path, monkeypatch)
    inputs["forecast_probability"] = "0.99"
    assert not evidence(9, inputs, context, now).verified(inputs, as_of=now)


def test_expired_rule_report_is_rejected(tmp_path, monkeypatch):
    inputs, context, now = integrated_inputs(tmp_path, monkeypatch)
    item = evidence(3, inputs, context, now)
    assert not item.verified(inputs, as_of=now + timedelta(seconds=60))
