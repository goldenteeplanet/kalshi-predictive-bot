"""Real temporary-ledger weather computation normalized without another forecast."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from test_api_tournament import execution_policy, fee_document, policy
from test_overnight_activation import baseline_template  # noqa: F401
from test_paper_release_preparation_runner import (
    artifact,
    cycle,  # noqa: F401
    frozen_for,
    real_model_bundle,  # noqa: F401
)

from kalshi_predictor.data_sources.tournament import evaluate_tournament
from kalshi_predictor.data_sources.weather_pair import build_weather_pair
from kalshi_predictor.overnight_paper.preparation_runner import run_weather_preparation_live_cycle
from kalshi_predictor.overnight_paper.qualification import EvidenceReference
from kalshi_predictor.utils.time import utc_now


@pytest.fixture
def prepared(cycle, real_model_bundle):  # noqa: F811
    sources = []
    for source in cycle["source_envelopes"]:
        row = json.loads(source.payload)
        if "series" in row["body"]:
            row["body"]["series"].update(fee_type="quadratic", fee_multiplier="1")
        artifact_payload = artifact(row)
        sources.append(
            EvidenceReference(source.artifact, artifact_payload.sha256, artifact_payload.payload)
        )
    cycle = {**cycle, "source_envelopes": tuple(sources)}
    frozen = frozen_for(cycle, real_model_bundle)
    committed = utc_now() - timedelta(seconds=30)
    execution = execution_policy(committed_at=committed.isoformat(), side="BUY_YES")
    frozen_policy = policy(
        kind="weather-paired-source-policy-v1",
        source_id="NWS",
        model_artifact_sha256=frozen.model.sha256,
        procedure_sha256=frozen.procedure.sha256,
        execution_policy_sha256=execution.sha256,
        committed_at=committed.isoformat(),
        holdout_start=(committed + timedelta(seconds=1)).isoformat(),
        holdout_end=(committed + timedelta(days=2)).isoformat(),
    )
    live = run_weather_preparation_live_cycle(**cycle, frozen_execution=frozen)
    now = utc_now()
    return dict(
        cycle=live,
        frozen_execution=frozen,
        policy=frozen_policy,
        execution_policy=execution,
        fee_original=fee_document(),
        decision_at=now,
        recorded_at=now,
        independent_event_id="KNYC-fixture-local-day",
        event_window_start=committed,
    )


def test_actual_receipt_normalizes_to_pending_pair(prepared):
    pair = build_weather_pair(**prepared)
    receipt = prepared["cycle"].record["execution_receipt"]
    assert pair.source_on.decode()["probability"] == receipt["source_on_probability"]
    assert pair.source_off.decode()["probability"] == receipt["source_off_probability"]
    assert pair.features[0].decode()["value"] == receipt["forecast_original"]["feature_json"]
    assert pair.outcome is None
    result = evaluate_tournament(
        policy=prepared["policy"], pairs=(pair,), as_of=prepared["recorded_at"]
    )
    assert result.status == "NOT_ENOUGH_DATA", result
    assert "PAIRED_OUTCOMES_INCOMPLETE" in result.blockers
    assert result.metrics is None and result.actual_paper_pnl is None


@pytest.mark.parametrize("change", ["replay", "receipt", "forecast", "early", "side", "stale"])
def test_refuses_missing_or_inconsistent_actual_evidence(prepared, change):
    original = prepared["cycle"]
    if change == "replay":
        prepared["cycle"] = replace(original, live_result=None)
    elif change in {"receipt", "forecast"}:
        row = deepcopy(original.record)
        if change == "receipt":
            row["execution_receipt"] = None
        else:
            row["execution_receipt"]["forecast_original"]["yes_probability"] = "0.01"
        prepared["cycle"] = replace(original, record=row)
    elif change == "early":
        prepared["decision_at"] -= timedelta(minutes=1)
    elif change == "stale":
        prepared["decision_at"] += timedelta(seconds=61)
        prepared["recorded_at"] += timedelta(seconds=61)
    else:
        row = prepared["execution_policy"].decode()
        row.pop("side")
        prepared["execution_policy"] = artifact(row)
    with pytest.raises(ValueError, match="WEATHER_PAIR_ORIGINALS_INVALID"):
        build_weather_pair(**prepared)
