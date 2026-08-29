"""Offline probability-calibration stress and tail-support proof."""

from __future__ import annotations

import hashlib
import json
import math

SCHEMA = "phase4nc.calibration-stress.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def calibration_report(
    rows: object,
    *,
    bin_count: int = 5,
    minimum_tail_support: int = 3,
    maximum_interval_width: float = 0.8,
    collapse_correlated_groups: bool = False,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(rows, list) or bin_count < 1:
        return _result(["INPUT_INVALID"], [], "REFUSE")
    prepared = []
    group_counts: dict[object, int] = {}
    for row in rows:
        if isinstance(row, dict):
            group_counts[row.get("event_group")] = group_counts.get(row.get("event_group"), 0) + 1
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"ROW_{index}_INVALID")
            continue
        probability, outcome = row.get("probability"), row.get("outcome")
        if (
            not isinstance(probability, (int, float))
            or not 0 <= probability <= 1
            or outcome not in {0, 1}
        ):
            errors.append(f"ROW_{index}_PROBABILITY_OR_OUTCOME_INVALID")
            continue
        weight = float(row.get("weight", 1.0))
        if collapse_correlated_groups:
            weight /= group_counts[row.get("event_group")]
        if weight <= 0:
            errors.append(f"ROW_{index}_WEIGHT_INVALID")
            continue
        prepared.append(
            {**row, "probability": float(probability), "outcome": int(outcome), "_weight": weight}
        )
    prepared.sort(key=lambda row: (row["probability"], str(row.get("sample_id"))))
    bins = _adaptive_bins(prepared, bin_count)
    total_weight = sum(row["_weight"] for row in prepared)
    base_rate = (
        sum(row["outcome"] * row["_weight"] for row in prepared) / total_weight
        if total_weight
        else 0.0
    )
    reliability = resolution = 0.0
    ece = mce = 0.0
    for bin_row in bins:
        fraction = bin_row["weight"] / total_weight if total_weight else 0.0
        gap = abs(bin_row["mean_probability"] - bin_row["observed_rate"])
        reliability += fraction * gap**2
        resolution += fraction * (bin_row["observed_rate"] - base_rate) ** 2
        ece += fraction * gap
        mce = max(mce, gap)
    uncertainty = base_rate * (1 - base_rate)
    brier = (
        sum(row["_weight"] * (row["probability"] - row["outcome"]) ** 2 for row in prepared)
        / total_weight
        if total_weight
        else None
    )
    epsilon = 1e-15
    log_loss = (
        -sum(
            row["_weight"]
            * (
                row["outcome"] * math.log(max(epsilon, min(1 - epsilon, row["probability"])))
                + (1 - row["outcome"])
                * math.log(max(epsilon, min(1 - epsilon, 1 - row["probability"])))
            )
            for row in prepared
        )
        / total_weight
        if total_weight
        else None
    )
    tail_count = sum(row["probability"] <= 0.1 or row["probability"] >= 0.9 for row in prepared)
    widest = max((row["confidence_interval_width"] for row in bins), default=1.0)
    claim_errors = []
    if tail_count < minimum_tail_support:
        claim_errors.append("TAIL_SUPPORT_INADEQUATE")
    if widest > maximum_interval_width:
        claim_errors.append("CONFIDENCE_INTERVAL_TOO_WIDE")
    slope, intercept = _slope_intercept(prepared)
    result = _result(
        sorted(set(errors)), bins, "PASS" if not errors and not claim_errors else "REFUSE"
    )
    result.update(
        {
            "claim_errors": claim_errors,
            "sample_count": len(prepared),
            "effective_weight": total_weight,
            "tail_support": tail_count,
            "brier_score": brier,
            "log_loss": log_loss,
            "reliability": reliability,
            "resolution": resolution,
            "uncertainty": uncertainty,
            "brier_decomposition_check": reliability - resolution + uncertainty,
            "expected_calibration_error": ece,
            "maximum_calibration_error": mce,
            "calibration_slope": slope,
            "calibration_intercept": intercept,
        }
    )
    result["report_sha256"] = _digest(result)
    return result


def fit_platt(
    training: list[dict[str, object]], *, iterations: int = 500, learning_rate: float = 0.05
) -> dict[str, object]:
    intercept = 0.0
    slope = 1.0
    for _ in range(iterations):
        grad_i = grad_s = 0.0
        for row in training:
            x = _logit(float(row["probability"]))
            prediction = _sigmoid(intercept + slope * x)
            error = prediction - int(row["outcome"])
            grad_i += error
            grad_s += error * x
        count = max(1, len(training))
        intercept -= learning_rate * grad_i / count
        slope -= learning_rate * grad_s / count
    body = {
        "method": "PLATT",
        "intercept": intercept,
        "slope": slope,
        "training_sample_ids": [row.get("sample_id") for row in training],
    }
    return {**body, "fit_sha256": _digest(body)}


def fit_isotonic(training: list[dict[str, object]]) -> dict[str, object]:
    ordered = sorted(
        training, key=lambda row: (float(row["probability"]), str(row.get("sample_id")))
    )
    blocks = [
        {
            "min": float(row["probability"]),
            "max": float(row["probability"]),
            "sum": float(row["outcome"]),
            "count": 1,
        }
        for row in ordered
    ]
    index = 0
    while index < len(blocks) - 1:
        if (
            blocks[index]["sum"] / blocks[index]["count"]
            > blocks[index + 1]["sum"] / blocks[index + 1]["count"]
        ):
            left, right = blocks[index], blocks.pop(index + 1)
            left["max"] = right["max"]
            left["sum"] += right["sum"]
            left["count"] += right["count"]
            index = max(0, index - 1)
        else:
            index += 1
    public = [
        {
            "min": row["min"],
            "max": row["max"],
            "value": row["sum"] / row["count"],
            "count": row["count"],
        }
        for row in blocks
    ]
    body = {
        "method": "ISOTONIC",
        "blocks": public,
        "training_sample_ids": [row.get("sample_id") for row in ordered],
    }
    return {**body, "fit_sha256": _digest(body)}


def apply_calibrator(
    rows: list[dict[str, object]], fit: dict[str, object]
) -> list[dict[str, object]]:
    output = []
    for row in rows:
        probability = float(row["probability"])
        if fit["method"] == "PLATT":
            calibrated = _sigmoid(
                float(fit["intercept"]) + float(fit["slope"]) * _logit(probability)
            )
        else:
            blocks = fit["blocks"]
            block = min(
                blocks,
                key=lambda value: (
                    0
                    if value["min"] <= probability <= value["max"]
                    else min(abs(probability - value["min"]), abs(probability - value["max"]))
                ),
            )
            calibrated = float(block["value"])
        output.append({**row, "probability": calibrated})
    return output


def compare_calibrators(
    training: list[dict[str, object]], test: list[dict[str, object]]
) -> dict[str, object]:
    platt = fit_platt(training)
    isotonic = fit_isotonic(training)
    leaky = fit_isotonic(test)
    reports = {
        "uncalibrated": calibration_report(
            test, minimum_tail_support=0, maximum_interval_width=1.0
        ),
        "platt_training_only": calibration_report(
            apply_calibrator(test, platt), minimum_tail_support=0, maximum_interval_width=1.0
        ),
        "isotonic_training_only": calibration_report(
            apply_calibrator(test, isotonic), minimum_tail_support=0, maximum_interval_width=1.0
        ),
        "isotonic_leaky_control": calibration_report(
            apply_calibrator(test, leaky), minimum_tail_support=0, maximum_interval_width=1.0
        ),
    }
    result = {
        "schema": SCHEMA,
        "reports": reports,
        "platt_fit": platt,
        "isotonic_fit": isotonic,
        "leaky_fit": leaky,
        "leaky_control_accepted": False,
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def _adaptive_bins(rows, count):
    bins = []
    for index in range(count):
        start, end = index * len(rows) // count, (index + 1) * len(rows) // count
        chunk = rows[start:end]
        if not chunk:
            continue
        weight = sum(row["_weight"] for row in chunk)
        observed = sum(row["outcome"] * row["_weight"] for row in chunk) / weight
        mean_probability = sum(row["probability"] * row["_weight"] for row in chunk) / weight
        low, high = _wilson(observed, weight)
        bins.append(
            {
                "count": len(chunk),
                "weight": weight,
                "minimum_probability": chunk[0]["probability"],
                "maximum_probability": chunk[-1]["probability"],
                "mean_probability": mean_probability,
                "observed_rate": observed,
                "confidence_low": low,
                "confidence_high": high,
                "confidence_interval_width": high - low,
            }
        )
    return bins


def _wilson(rate, count):
    z = 1.96
    denominator = 1 + z * z / count
    center = (rate + z * z / (2 * count)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def _slope_intercept(rows):
    if len(rows) < 2:
        return None, None
    xs = [_logit(row["probability"]) for row in rows]
    ys = [row["outcome"] for row in rows]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / variance
        if variance
        else 0.0
    )
    return slope, mean_y - slope * mean_x


def _logit(value):
    clipped = max(1e-6, min(1 - 1e-6, value))
    return math.log(clipped / (1 - clipped))


def _sigmoid(value):
    return 1 / (1 + math.exp(-max(-30, min(30, value))))


def _result(errors, bins, claim):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "calibration_claim": claim,
        "bins": bins,
        "safety": _safety(),
    }


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
