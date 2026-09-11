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
            "valid_until": (
                fixtures.datetime.fromisoformat(inputs["decision_at"]) + timedelta(minutes=2)
            ).isoformat(),
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


@pytest.fixture(params=["windows", "linux"])
def grid_context(context, request):
    from kalshi_predictor.overnight_paper.miami_source_gate import (
        GRID30_LF_REPLAY_CODE_HASHES,
        GRID30_REPLAY_CODE_HASHES,
    )
    from kalshi_predictor.weather.miami_half_hour_forecast import forecast_miami_prior_day_grid30
    from kalshi_predictor.weather.miami_index import decode_miami_index

    code_hashes = (GRID30_REPLAY_CODE_HASHES if request.param == "windows"
                   else GRID30_LF_REPLAY_CODE_HASHES)

    def archived_bytes(path, sha):
        # Reconstruct the two reviewed original encodings for portable fixtures.
        # Production compares supplied raw bytes and never normalizes evidence.
        raw = (Path(__file__).resolve().parents[1] / path).read_bytes()
        if "half_hour" in path:
            raw = raw.replace(b"\r\n", b"\n")
            if request.param == "windows":
                raw = raw.replace(b"\n", b"\r\n")
        assert hashlib.sha256(raw).hexdigest() == sha
        return raw

    delta = timedelta(minutes=30)
    pair = context.captures[0]
    data = pair.index.artifact.decode()
    # Move exact origins and current lag; retain prior-day target endpoints.
    for point in data["timeseries"]:
        minute = (point["t"] // 60000) % 60
        at = fixtures.datetime.fromtimestamp(point["t"] / 1000, fixtures.UTC)
        if at.date() == fixtures.ORIGIN.date() or (minute == 0 and at.hour == 21):
            point["t"] += 30 * 60000
    index = replace(pair.index, artifact=artifact(data), received_at=pair.index.received_at + delta)
    cal = replace(pair.calibrations, received_at=pair.calibrations.received_at + delta)
    capture = decode_miami_index(
        index.artifact.payload,
        cal.artifact.payload,
        index_received_at=index.received_at,
        calibrations_received_at=cal.received_at,
        index_units="fahrenheit",
    )
    saved = context.frozen_prediction.decode()
    for key in ("model_input_as_of", "input_received_at"):
        saved[key] = (fixtures.datetime.fromisoformat(saved[key]) + delta).isoformat()
    saved["prediction"]["forecasts"] = [
        forecast_miami_prior_day_grid30(
            [capture],
            origin_at=fixtures.ORIGIN + delta,
            model_input_as_of=fixtures.CUTOFF + delta,
            horizon_minutes=30,
        )
    ]
    saved["prediction"]["code_proof"]["files"] = [
        {"path": p, "sha256": sha} for p, sha in code_hashes.items()
    ]
    prediction = artifact(saved)
    recording = context.recording_receipt.decode()
    recording["prediction_sha256"] = prediction.sha256
    recording["prediction_recorded_at"] = (
        fixtures.CUTOFF + delta + timedelta(seconds=1)
    ).isoformat()
    market, event, series = [
        replace(o, received_at=o.received_at + delta)
        for o in (context.market, context.event, context.series)
    ]
    return replace(
        context,
        frozen_prediction=prediction,
        recording_receipt=artifact(recording),
        captures=(MiamiCaptureEvidence(index, cal, receipt(index), receipt(cal)),),
        market=market,
        event=event,
        series=series,
        catalog_receipts=tuple(receipt(o) for o in (market, event, series)),
        code_originals=tuple(
            (p, Artifact(sha, archived_bytes(p, sha)))
            for p, sha in code_hashes.items()
        ),
    )


def grid_candidate(context, **changes):
    args = dict(
        model_name="miami_prior_day_increment_grid30_v1",
        origin_at=(fixtures.ORIGIN + timedelta(minutes=30)).isoformat(),
        model_input_as_of=(fixtures.CUTOFF + timedelta(minutes=30)).isoformat(),
        decision_at=(fixtures.CUTOFF + timedelta(minutes=30, seconds=2)).isoformat(),
    )
    args.update(changes)
    return candidate(context, **args)


def test_grid30_original_replay_and_health(grid_context):
    from kalshi_predictor.overnight_paper.miami_source_gate import verify_miami_gate4

    inputs, evidence = grid_candidate(grid_context)
    assert evidence.verified(inputs)
    assert verify_miami_gate4(
        grid_context,
        inputs=inputs,
        sources=tuple((r.sha256, r.payload) for r in evidence.sources),
        now=fixtures.CUTOFF + timedelta(minutes=30, seconds=2),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"model_name": "miami_prior_day_increment_v1"},
        {"model_version": "grid30-v1"},
        {"forecast_probability": ".99"},
        {"origin_at": fixtures.ORIGIN.isoformat()},
        {"model_input_as_of": fixtures.CUTOFF.isoformat()},
    ],
)
def test_grid30_rejects_identity_probability_and_clock_changes(grid_context, changes):
    inputs, evidence = grid_candidate(grid_context, **changes)
    assert not evidence.verified(inputs)


def test_grid30_rejects_hourly_closure(grid_context, context):
    changed = replace(grid_context, code_originals=context.code_originals)
    inputs, evidence = grid_candidate(changed)
    assert not evidence.verified(inputs)


@pytest.mark.parametrize("grid", [60, True, 30.0, 15, None])
def test_grid30_health_requires_explicit_integer_grid(grid_context, grid):
    from kalshi_predictor.overnight_paper.miami_source import verify_miami_source

    pair = grid_context.captures[0]
    health = verify_miami_source(
        index=pair.index,
        calibrations=pair.calibrations,
        index_receipt=pair.index_receipt,
        calibrations_receipt=pair.calibrations_receipt,
        origin_at=fixtures.ORIGIN + timedelta(minutes=30),
        target_at=fixtures.ORIGIN + timedelta(hours=1),
        model_input_as_of=fixtures.CUTOFF + timedelta(minutes=30),
        decision_at=fixtures.CUTOFF + timedelta(minutes=30, seconds=2),
        now=fixtures.CUTOFF + timedelta(minutes=30, seconds=2),
        origin_grid_minutes=grid,
    )
    assert not health.source_healthy


@pytest.mark.parametrize("mutation", ["schema", "mixed", "samples", "origin", "code"])
def test_grid30_rehashed_prediction_tamper_rejected(grid_context, mutation):
    saved = grid_context.frozen_prediction.decode()
    f = saved["prediction"]["forecasts"][0]
    if mutation == "schema":
        f["schema"] = "miami-prior-day-prospective-v1"
    elif mutation == "mixed":
        other = dict(f, model="miami_prior_day_increment_v1")
        saved["prediction"]["forecasts"].append(other)
    elif mutation == "samples":
        f["models"]["prior_day_increment_empirical"]["samples_f"][0] += 1
    elif mutation == "origin":
        f["origin_at"] = fixtures.ORIGIN.isoformat()
    else:
        saved["prediction"]["code_proof"]["files"][0]["sha256"] = "f" * 64
    prediction = artifact(saved)
    rec = grid_context.recording_receipt.decode()
    rec["prediction_sha256"] = prediction.sha256
    changed = replace(grid_context, frozen_prediction=prediction, recording_receipt=artifact(rec))
    inputs, evidence = grid_candidate(changed)
    assert not evidence.verified(inputs)


@pytest.mark.parametrize("change", ["stale", "future", "offgrid", "date"])
def test_grid30_health_preserves_clock_and_date_checks(grid_context, change):
    from kalshi_predictor.overnight_paper.miami_source import verify_miami_source

    pair = grid_context.captures[0]
    kwargs = dict(
        index=pair.index,
        calibrations=pair.calibrations,
        index_receipt=pair.index_receipt,
        calibrations_receipt=pair.calibrations_receipt,
        origin_at=fixtures.ORIGIN + timedelta(minutes=30),
        target_at=fixtures.ORIGIN + timedelta(hours=1),
        model_input_as_of=fixtures.CUTOFF + timedelta(minutes=30),
        decision_at=fixtures.CUTOFF + timedelta(minutes=30, seconds=2),
        now=fixtures.CUTOFF + timedelta(minutes=30, seconds=2),
        origin_grid_minutes=30,
    )
    if change == "stale":
        kwargs["now"] += timedelta(minutes=2)
    elif change == "future":
        kwargs["model_input_as_of"] -= timedelta(minutes=1)
    elif change == "offgrid":
        kwargs["origin_at"] += timedelta(minutes=1)
    else:
        kwargs["target_at"] += timedelta(days=1)
    assert not verify_miami_source(**kwargs).source_healthy

@pytest.mark.parametrize('mixed_proof', [False, True])
def test_grid30_rejects_mixed_encoding_closure_even_with_matching_proof(grid_context, mixed_proof):
    path = 'scripts/positive_ev_miami_half_hour_research.py'
    changed_code = []
    for name, source in grid_context.code_originals:
        if name == path:
            raw = source.payload
            raw = raw.replace(b'\r\n', b'\n') if b'\r\n' in raw else raw.replace(b'\n', b'\r\n')
            source = Artifact(hashlib.sha256(raw).hexdigest(), raw)
        changed_code.append((name, source))
    changed = replace(grid_context, code_originals=tuple(changed_code))
    if mixed_proof:
        saved = changed.frozen_prediction.decode()
        saved['prediction']['code_proof']['files'] = [
            {'path': p, 'sha256': a.sha256} for p, a in changed_code]
        prediction = artifact(saved)
        rec = changed.recording_receipt.decode()
        rec['prediction_sha256'] = prediction.sha256
        changed = replace(changed, frozen_prediction=prediction, recording_receipt=artifact(rec))
    inputs, evidence = grid_candidate(changed)
    assert not evidence.verified(inputs)


def test_grid30_rejects_other_complete_profile_proof(grid_context):
    from kalshi_predictor.overnight_paper.miami_source_gate import (
        GRID30_LF_REPLAY_CODE_HASHES,
        GRID30_REPLAY_CODE_HASHES,
    )
    current = {p: a.sha256 for p, a in grid_context.code_originals}
    other = (GRID30_REPLAY_CODE_HASHES if current == GRID30_LF_REPLAY_CODE_HASHES
             else GRID30_LF_REPLAY_CODE_HASHES)
    saved = grid_context.frozen_prediction.decode()
    saved['prediction']['code_proof']['files'] = [
        {'path': p, 'sha256': sha} for p, sha in other.items()]
    prediction = artifact(saved)
    rec = grid_context.recording_receipt.decode()
    rec['prediction_sha256'] = prediction.sha256
    changed = replace(grid_context, frozen_prediction=prediction, recording_receipt=artifact(rec))
    inputs, evidence = grid_candidate(changed)
    assert not evidence.verified(inputs)
