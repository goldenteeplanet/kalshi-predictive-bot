"""Pure scoring of frozen public-index forecasts, never contract settlement."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshi_predictor.weather.miami_index import MiamiIndexCapture

MODELS = {"persistence", "fixed_30min_linear_trend", "prior_day_increment_empirical"}


def _clock(value: str | datetime) -> datetime:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("MIAMI_EVALUATION_AWARE_CLOCK_REQUIRED")
    return at.astimezone(UTC)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash(value: str) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _load(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not 0 < len(raw) <= 8_000_000:
        raise ValueError("MIAMI_EVALUATION_BYTES_BUDGET")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("MIAMI_EVALUATION_DUPLICATE_KEY")
            data[key] = value
        return data

    def reject(value: str) -> None:
        raise ValueError("MIAMI_EVALUATION_NONFINITE_JSON")

    result = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)
    if not isinstance(result, dict):
        raise ValueError("MIAMI_EVALUATION_OBJECT_REQUIRED")
    return result


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1000


def evaluate_miami_forecast(
    prediction_raw: bytes,
    recording_receipt_raw: bytes,
    decision_raw: bytes,
    *,
    outcome: MiamiIndexCapture,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Validate prospective receipts, then score exact available target minutes.

    Pending rows have no zero-filled metrics. Local receipts and git metadata
    are not external timestamp attestations; supplied code hashes are preserved,
    not independently reverified against a repository by this pure helper.
    """
    prediction, receipt, decision = map(
        _load, (prediction_raw, recording_receipt_raw, decision_raw)
    )
    if (
        prediction.get("schema") != "frozen-research-prediction-v1"
        or receipt.get("schema") != "prediction-recording-receipt-v1"
        or decision.get("schema") != "recorded-research-decision-v1"
        or receipt.get("prediction_sha256") != _digest(prediction_raw)
        or decision.get("prediction_sha256") != _digest(prediction_raw)
        or decision.get("prediction_receipt_sha256") != _digest(recording_receipt_raw)
    ):
        raise ValueError("MIAMI_FROZEN_HASH_OR_SCHEMA_MISMATCH")
    for key in ("input_received_at", "model_input_as_of"):
        if _clock(prediction[key]) != _clock(decision[key]):
            raise ValueError("MIAMI_DECISION_CLOCK_MISMATCH")
    recorded = _clock(receipt["prediction_recorded_at"])
    asof, received = (
        _clock(prediction["model_input_as_of"]),
        _clock(prediction["input_received_at"]),
    )
    committed = _clock(prediction["model_committed_at"])
    if "model_committed_at" in decision and _clock(decision["model_committed_at"]) != committed:
        raise ValueError("MIAMI_DECISION_CLOCK_MISMATCH")
    decided, evaluated = _clock(decision["decision_at"]), _clock(evaluated_at)
    earliest = _clock(prediction["target_at"])
    if (
        _clock(decision["prediction_recorded_at"]) != recorded
        or not received <= asof <= recorded <= decided < earliest
        or committed > asof
        or evaluated < decided
    ):
        raise ValueError("MIAMI_PROSPECTIVE_CHRONOLOGY_INVALID")
    full = prediction["prediction"]
    proof = full["code_proof"]
    if (
        _clock(proof["commit_recorded_at"]) != committed
        or not committed <= _clock(proof["code_frozen_at"]) <= asof
        or not isinstance(proof.get("source_commit"), str)
        or len(proof["source_commit"]) != 40
        or any(c not in "0123456789abcdef" for c in proof["source_commit"])
        or not proof.get("files")
        or any(not item.get("path") or not _hash(item.get("sha256")) for item in proof["files"])
    ):
        raise ValueError("MIAMI_COMMITTED_SOURCE_PROOF_REQUIRED")
    forecasts = full["forecasts"]
    if len(forecasts) != 2 or {f["horizon_minutes"] for f in forecasts} != {30, 60}:
        raise ValueError("MIAMI_FIXED_TWO_HORIZONS_REQUIRED")
    targets = [_clock(f["target_at"]) for f in forecasts]
    if min(targets) != earliest:
        raise ValueError("MIAMI_EARLIEST_TARGET_MISMATCH")
    if len({_clock(f["origin_at"]) for f in forecasts}) != 1:
        raise ValueError("MIAMI_ORIGIN_MISMATCH")
    index_receipt = _clock(outcome.index_received_at)
    calibration_receipt = _clock(outcome.calibrations_received_at)
    if (
        outcome.index_units != "fahrenheit"
        or _clock(outcome.available_at) != max(index_receipt, calibration_receipt)
        or max(index_receipt, calibration_receipt) > evaluated
        or not _hash(outcome.index_sha256)
        or not _hash(outcome.calibrations_sha256)
        or len(outcome.points) > 11000
    ):
        raise ValueError("MIAMI_OUTCOME_UNITS_RECEIPT_OR_HASH_INVALID")
    points = {}
    previous = None
    for observed_point in outcome.points:
        at = _clock(observed_point.event_at)
        if at.second or at.microsecond or at > index_receipt or (previous and at <= previous):
            raise ValueError("MIAMI_OUTCOME_POINT_CLOCK_INVALID")
        points[at], previous = observed_point, at
    rows = []
    for forecast, target in zip(forecasts, targets, strict=True):
        if (
            forecast["units"] != "fahrenheit"
            or _clock(forecast["model_input_as_of"]) != asof
            or _clock(forecast["input_received_at"]) != received
            or target
            != _clock(forecast["origin_at"]) + timedelta(minutes=forecast["horizon_minutes"])
            or target.second
            or target.microsecond
            or set(forecast["models"]) != MODELS
            or not forecast["source_hashes"]
            or not forecast["calibration_hashes"]
            or any(not _hash(h) for h in forecast["source_hashes"] + forecast["calibration_hashes"])
        ):
            raise ValueError("MIAMI_FORECAST_IDENTITY_OR_PROVENANCE_INVALID")
        models = forecast["models"]
        for model in models.values():
            samples = model["samples_f"]
            if (
                not 1 <= len(samples) <= 1000
                or any(not _number(x) for x in samples)
                or not _number(model["mean_f"])
                or not math.isclose(model["mean_f"], statistics.mean(samples), abs_tol=1e-10)
            ):
                raise ValueError("MIAMI_FORECAST_SAMPLES_INVALID")
            lo, hi = model.get("interval_low_f"), model.get("interval_high_f")
            if (lo is None) != (hi is None) or (
                lo is not None and (not _number(lo) or not _number(hi) or lo > hi)
            ):
                raise ValueError("MIAMI_FORECAST_INTERVAL_INVALID")
        row = {
            "target_at": target.isoformat(),
            "horizon_minutes": forecast["horizon_minutes"],
            "source_hashes": forecast["source_hashes"],
            "calibration_hashes": forecast["calibration_hashes"],
        }
        if min(index_receipt, evaluated) < target + timedelta(minutes=5):
            rows.append({**row, "status": "PENDING_TARGET_NOT_DUE", "metrics": None})
            continue
        point = points.get(target)
        if point is None or point.value_f is None or point.status not in {"normal", "degraded"}:
            rows.append({**row, "status": "PENDING_EXACT_TARGET_UNAVAILABLE", "metrics": None})
            continue
        if (
            not isinstance(point.value_f, Decimal)
            or not point.value_f.is_finite()
            or not Decimal(-40) <= point.value_f <= Decimal(122)
            or point.value_f != point.value_f.quantize(Decimal(".01"))
            or type(point.contributors) is not int
            or not 4 <= point.contributors <= 5
            or (point.status == "normal" and point.contributors != 5)
            or not point.config_version
            or not point.configuration_published_by_event
        ):
            raise ValueError("MIAMI_CANONICAL_TARGET_INVALID")
        actual = float(point.value_f)
        metrics = {}
        for name, model in models.items():
            samples = sorted(model["samples_f"])
            n = len(samples)
            # Half the mean pairwise distance, computed in O(n log n), not O(n^2).
            half_pair = sum((2 * i - n + 1) * v for i, v in enumerate(samples)) / (n * n)
            error = model["mean_f"] - actual
            lo, hi = model.get("interval_low_f"), model.get("interval_high_f")
            metrics[name] = {
                "absolute_error_f": abs(error),
                "squared_error_f2": error * error,
                "crps_f": sum(abs(x - actual) for x in samples) / n - half_pair,
                "interval_covered": lo <= actual <= hi if lo is not None else None,
                "interval_nominal_coverage": model.get("interval_nominal_coverage"),
            }
        rows.append(
            {
                **row,
                "status": "SCORED",
                "actual_f": actual,
                "target_config_version": point.config_version,
                "metrics": metrics,
            }
        )
    return {
        "schema": "miami-index-evaluation-v1",
        "evaluated_at": evaluated.isoformat(),
        "prediction_sha256": _digest(prediction_raw),
        "recording_receipt_sha256": _digest(recording_receipt_raw),
        "decision_sha256": _digest(decision_raw),
        "source_code_proof": proof,
        "outcome_index_sha256": outcome.index_sha256,
        "outcome_calibrations_sha256": outcome.calibrations_sha256,
        "outcome_index_received_at": index_receipt.isoformat(),
        "outcome_calibrations_received_at": calibration_receipt.isoformat(),
        "rows": rows,
        "settlement_certified": False,
        "execution_authority": False,
        "paper_orders": 0,
        "scope": "PUBLIC_INDEX_RESEARCH_NOT_CONTRACT_SETTLEMENT",
    }
