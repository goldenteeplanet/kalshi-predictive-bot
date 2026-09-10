import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kalshi_predictor.crypto.research_provenance import freeze_prediction
from kalshi_predictor.weather.miami_evaluation import evaluate_miami_forecast
from kalshi_predictor.weather.miami_index import IndexPoint, MiamiIndexCapture

ORIGIN = datetime(2026, 9, 10, 21, tzinfo=UTC)
ASOF = ORIGIN + timedelta(minutes=1)


def test_real_freeze_prediction_envelope(tmp_path):
    value = payload()
    clocks = iter((ORIGIN + timedelta(minutes=2), ORIGIN + timedelta(minutes=3)))
    freeze_prediction(
        tmp_path / "frozen",
        value["prediction"],
        model_input_as_of=ASOF,
        input_received_at=ORIGIN + timedelta(seconds=30),
        model_committed_at=ORIGIN - timedelta(hours=1),
        target_at=ORIGIN + timedelta(minutes=30),
        clock=lambda: next(clocks),
    )
    result = evaluate_miami_forecast(
        *(
            (tmp_path / "frozen" / name).read_bytes()
            for name in (
                "prediction.json",
                "recording-receipt.json",
                "decision.json",
            )
        ),
        outcome=capture(),
        evaluated_at=ORIGIN + timedelta(minutes=66),
    )
    assert all(row["status"] == "SCORED" for row in result["rows"])


def at(minutes):
    return (ORIGIN + timedelta(minutes=minutes)).isoformat()


def payload():
    models = {
        "persistence": {"mean_f": 85, "samples_f": [85]},
        "fixed_30min_linear_trend": {"mean_f": 87, "samples_f": [87]},
        "prior_day_increment_empirical": {
            "mean_f": 85,
            "samples_f": [84, 86],
            "interval_low_f": 84,
            "interval_high_f": 86,
            "interval_nominal_coverage": 0.8,
        },
    }
    return {
        "schema": "frozen-research-prediction-v1",
        "input_received_at": at(0.5),
        "model_input_as_of": at(1),
        "model_committed_at": at(-60),
        "target_at": at(30),
        "prediction": {
            "code_proof": {
                "source_commit": "a" * 40,
                "commit_recorded_at": at(-60),
                "code_frozen_at": at(-1),
                "files": [{"path": "miami_forecast.py", "sha256": "b" * 64}],
            },
            "forecasts": [
                {
                    "units": "fahrenheit",
                    "origin_at": at(0),
                    "target_at": at(h),
                    "horizon_minutes": h,
                    "model_input_as_of": at(1),
                    "input_received_at": at(0.5),
                    "models": copy.deepcopy(models),
                    "source_hashes": ["c" * 64],
                    "calibration_hashes": ["d" * 64],
                }
                for h in (30, 60)
            ],
        },
    }


def encode(value):
    return json.dumps(value).encode()


def freeze(value):
    raw = encode(value)
    receipt = {
        "schema": "prediction-recording-receipt-v1",
        "prediction_sha256": hashlib.sha256(raw).hexdigest(),
        "prediction_recorded_at": at(2),
    }
    receipt_raw = encode(receipt)
    decision = {
        **receipt,
        "schema": "recorded-research-decision-v1",
        "prediction_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
        "decision_at": at(3),
        **{k: value[k] for k in ("input_received_at", "model_input_as_of", "model_committed_at")},
    }
    return raw, receipt_raw, encode(decision)


def capture(minutes=65):
    received = ORIGIN + timedelta(minutes=minutes)
    points = tuple(
        IndexPoint(ORIGIN + timedelta(minutes=h), Decimal("86.00"), "normal", 5, "v1", False, True)
        for h in (30, 60)
        if h <= minutes
    )
    return MiamiIndexCapture(
        points, "e" * 64, "f" * 64, received, received, received, "v1", (), "fahrenheit", True
    )


def evaluate(value=None, outcome=None):
    return evaluate_miami_forecast(
        *freeze(value or payload()),
        outcome=outcome or capture(),
        evaluated_at=ORIGIN + timedelta(minutes=66),
    )


def test_exact_scores_crps_and_intervals_preserve_receipts():
    result = evaluate()
    assert [r["status"] for r in result["rows"]] == ["SCORED", "SCORED"]
    metrics = result["rows"][0]["metrics"]
    assert metrics["persistence"]["absolute_error_f"] == 1
    assert metrics["persistence"]["squared_error_f2"] == 1
    assert metrics["persistence"]["crps_f"] == 1
    assert metrics["persistence"]["interval_covered"] is None
    assert metrics["prior_day_increment_empirical"]["crps_f"] == 0.5
    assert metrics["prior_day_increment_empirical"]["interval_covered"]
    assert result["outcome_index_sha256"] == "e" * 64
    assert not result["settlement_certified"] and result["paper_orders"] == 0


@pytest.mark.parametrize("index", [0, 1, 2])
def test_changed_bytes_reject(index):
    parts = list(freeze(payload()))
    if index < 2:
        parts[index] += b" "
    else:
        decision = json.loads(parts[2])
        decision["prediction_sha256"] = "0" * 64
        parts[2] = encode(decision)
    with pytest.raises(ValueError, match="HASH"):
        evaluate_miami_forecast(
            *parts, outcome=capture(), evaluated_at=ORIGIN + timedelta(minutes=66)
        )


def test_receipt_before_target_plus_five_stays_pending_even_when_evaluated_later():
    result = evaluate(outcome=capture(34))
    assert all(r["status"] == "PENDING_TARGET_NOT_DUE" for r in result["rows"])
    assert all(r["metrics"] is None for r in result["rows"])
    ready = evaluate(outcome=capture(35))
    assert ready["rows"][0]["status"] == "SCORED"
    assert ready["rows"][1]["metrics"] is None


def test_no_nearest_minute_or_incomplete_value_substitution():
    original = capture()
    shifted = replace(original.points[0], event_at=ORIGIN + timedelta(minutes=29))
    incomplete = replace(original.points[1], value_f=None, status="incomplete", contributors=3)
    result = evaluate(outcome=replace(original, points=(shifted, incomplete)))
    assert all(r["status"] == "PENDING_EXACT_TARGET_UNAVAILABLE" for r in result["rows"])


@pytest.mark.parametrize("failure", ["units", "hash", "duplicate", "future_receipt", "quorum"])
def test_bad_outcome_fails(failure):
    outcome = capture()
    if failure == "units":
        outcome = replace(outcome, index_units="celsius")
    elif failure == "hash":
        outcome = replace(outcome, index_sha256="")
    elif failure == "duplicate":
        outcome = replace(outcome, points=outcome.points * 2)
    elif failure == "future_receipt":
        outcome = replace(outcome, index_received_at=ORIGIN + timedelta(days=1))
    else:
        outcome = replace(outcome, points=(replace(outcome.points[0], contributors=3),))
    with pytest.raises(ValueError):
        evaluate(outcome=outcome)


@pytest.mark.parametrize("failure", ["late_model", "late_freeze", "late_input", "units", "samples"])
def test_bad_prediction_fails_even_if_hashes_match(failure):
    value = payload()
    if failure == "late_model":
        value["prediction"]["code_proof"]["commit_recorded_at"] = at(2)
    elif failure == "late_freeze":
        value["prediction"]["code_proof"]["code_frozen_at"] = at(2)
    elif failure == "late_input":
        value["input_received_at"] = at(2)
    elif failure == "units":
        value["prediction"]["forecasts"][0]["units"] = "celsius"
    else:
        value["prediction"]["forecasts"][0]["models"]["persistence"]["samples_f"] = []
    with pytest.raises(ValueError):
        evaluate(value)
