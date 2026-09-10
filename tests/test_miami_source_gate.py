import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from kalshi_predictor.overnight_paper.miami_source_gate import (
    REPLAY_CODE_HASHES,
    SOURCE_KIND,
    VERIFIER,
    MiamiCaptureEvidence,
    MiamiGateContext,
)
from kalshi_predictor.overnight_paper.provenance import Artifact
from kalshi_predictor.overnight_paper.qualification import (
    EvidenceReference,
    GateEvidence,
    Readiness,
    decision_fingerprint,
    qualify_candidate,
)

spec = importlib.util.spec_from_file_location(
    "binding_fixtures", Path(__file__).with_name("test_miami_binding.py")
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def artifact(value):
    raw = json.dumps(value, sort_keys=True).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


def receipt(original):
    return artifact(
        {
            "url": original.url,
            "status": 200,
            "sha256": original.artifact.sha256,
            "received_at": original.received_at.isoformat(),
        }
    )


@pytest.fixture
def context():
    values = fixtures.inputs.__wrapped__()
    market_data = values["market"].artifact.decode()
    market_data["market"]["status"] = "open"
    market = replace(values["market"], artifact=artifact(market_data))
    series_data = values["series"].artifact.decode()
    series_data["series"]["category"] = "Climate and Weather"
    series = replace(values["series"], artifact=artifact(series_data))
    saved = values["frozen_prediction"].decode()
    saved["prediction"]["code_proof"] = {
        "commit_recorded_at": saved["model_committed_at"],
        "code_frozen_at": saved["model_committed_at"],
        "files": [{"path": path, "sha256": sha} for path, sha in REPLAY_CODE_HASHES.items()],
    }
    prediction = artifact(saved)
    recording = values["recording_receipt"].decode()
    recording["prediction_sha256"] = prediction.sha256
    repo = Path(__file__).resolve().parents[1]
    code = tuple(
        (path, Artifact(sha, (repo / path).read_bytes()))
        for path, sha in REPLAY_CODE_HASHES.items()
    )
    index, calibration = values["captures"][0]
    index = replace(index, url=index.url + "?last_sec=7200")
    return MiamiGateContext(
        prediction,
        artifact(recording),
        (MiamiCaptureEvidence(index, calibration, receipt(index), receipt(calibration)),),
        0,
        market,
        values["event"],
        series,
        (receipt(market), receipt(values["event"]), receipt(series)),
        values["rule_documents"],
        code,
    )


def candidate(context, **changes):
    originals = sorted(set((a.sha256, a.payload) for a in context.artifacts()))
    references = tuple(
        EvidenceReference(str(i), sha, raw) for i, (sha, raw) in enumerate(originals)
    )
    inputs = {
        "ticker": "KXTEMPMIAH-26SEP1018-T81.99",
        "event_id": "KXTEMPMIAH-26SEP1018",
        "series": "KXTEMPMIAH",
        "category": "Climate and Weather",
        "source_kind": SOURCE_KIND,
        "model_name": "miami_prior_day_increment_v1",
        "model_version": "1",
        "historical_public_availability": "UNKNOWN",
        "miami_context_sha256": context.fingerprint(),
        "forecast_id": 123,
        "frozen_prediction_sha256": context.frozen_prediction.sha256,
        "forecast_probability": str(2 / 3),
        "origin_at": fixtures.ORIGIN.isoformat(),
        "observation_time": (fixtures.ORIGIN + timedelta(hours=1)).isoformat(),
        "model_input_as_of": fixtures.CUTOFF.isoformat(),
        "decision_at": (fixtures.CUTOFF + timedelta(seconds=2)).isoformat(),
        "source_hashes": [r.sha256 for r in references],
    }
    inputs.update(changes)
    decision_id = decision_fingerprint(inputs)
    report = artifact(
        {
            "schema": "overnight-paper-gate-v1",
            "gate": 4,
            "decision_id": decision_id,
            "ticker": inputs["ticker"],
            "category": inputs["category"],
            "verifier": VERIFIER,
            "verdict": "PASS",
            "sources": [r.sha256 for r in references],
            "validated_at": inputs["decision_at"],
            "valid_until": (fixtures.CUTOFF + timedelta(minutes=2)).isoformat(),
        }
    )
    evidence = GateEvidence(
        4,
        decision_id,
        inputs["category"],
        inputs["ticker"],
        VERIFIER,
        EvidenceReference("gate4", report.sha256, report.payload),
        sources=references,
        context=context,
    )
    return inputs, evidence


def test_exact_original_replay_passes_only_source_gate(context):
    from decimal import Decimal

    inputs, evidence = candidate(context)
    assert evidence.verified(inputs)
    result = qualify_candidate(
        ticker=inputs["ticker"],
        category=inputs["category"],
        decision_inputs=inputs,
        decision_id=evidence.decision_id,
        evidence=(evidence,),
        minimum_net_ev=Decimal(".01"),
    )
    assert result.status == Readiness.PAPER_NOT_READY
    assert dict(result.gates)["FRESH_ANALYTICAL_SOURCE"]
    assert not dict(result.gates)["CERTIFIED_SETTLEMENT_RULE"]


@pytest.mark.parametrize(
    "changes",
    [
        {"source_kind": "nws"},
        {"event_id": "another-event"},
        {"model_name": "weather_v2"},
        {"forecast_probability": ".99"},
        {"frozen_prediction_sha256": "other-frozen-forecast"},
        {"origin_at": "2026-09-10T20:00:00Z"},
        {"model_input_as_of": "2026-09-10T21:04:00Z"},
        {"observation_time": "2026-09-10T23:00:00Z"},
        {"historical_public_availability": "PROVEN"},
    ],
)
def test_resigned_pass_cannot_rebind_model_or_source(context, changes):
    inputs, evidence = candidate(context, **changes)
    assert not evidence.verified(inputs)


def test_forged_pass_without_typed_context_or_originals_fails(context):
    inputs, evidence = candidate(context)
    assert not replace(evidence, context={"source_healthy": True}).verified(inputs)
    assert not replace(evidence, sources=evidence.sources[:-1]).verified(inputs)


def test_source_freshness_rechecked_at_activation(context):
    inputs, evidence = candidate(context)
    assert not evidence.verified(inputs, as_of=fixtures.CUTOFF + timedelta(seconds=61))


def test_changed_receipt_and_code_closure_fail(context):
    pair = context.captures[0]
    altered = pair.index_receipt.decode()
    altered["sha256"] = "0" * 64
    ctx = replace(context, captures=(replace(pair, index_receipt=artifact(altered)),))
    inputs, evidence = candidate(ctx)
    assert not evidence.verified(inputs)
    path, original = context.code_originals[0]
    ctx = replace(
        context,
        code_originals=((path, Artifact(original.sha256, original.payload + b" ")),)
        + context.code_originals[1:],
    )
    inputs, evidence = candidate(ctx)
    assert not evidence.verified(inputs)


def test_refreshed_current_pair_cannot_replace_frozen_original(context):
    pair = context.captures[0]
    changed_index = replace(pair.index, received_at=pair.index.received_at + timedelta(seconds=1))
    ctx = replace(
        context,
        captures=(replace(pair, index=changed_index, index_receipt=receipt(changed_index)),),
    )
    inputs, evidence = candidate(ctx)
    assert not evidence.verified(inputs)
