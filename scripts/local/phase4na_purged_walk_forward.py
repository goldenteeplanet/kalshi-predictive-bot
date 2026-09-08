"""Offline purged/embargoed walk-forward evaluation proof."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

SCHEMA = "phase4na.purged-walk-forward.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def build_purged_folds(
    samples: object,
    *,
    embargo_seconds: int,
    mode: str = "expanding",
    rolling_train_limit: int | None = None,
    minimum_train_size: int = 2,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(samples, list) or embargo_seconds < 0 or mode not in {"expanding", "rolling"}:
        return _result(["INPUT_INVALID"], [])
    normalized = []
    seen_ids: set[str] = set()
    for index, row in enumerate(samples):
        if not isinstance(row, dict):
            errors.append(f"SAMPLE_{index}_INVALID")
            continue
        start, end, available = (
            _time(row.get(key)) for key in ("label_start", "label_end", "available_at")
        )
        sample_id = str(row.get("sample_id"))
        if start is None or end is None or available is None or start >= end:
            errors.append(f"SAMPLE_{index}_TIME_INVALID")
        elif sample_id in seen_ids:
            errors.append(f"SAMPLE_{index}_DUPLICATE_ID")
        else:
            seen_ids.add(sample_id)
            normalized.append({**row, "_start": start, "_end": end, "_available": available})
    normalized.sort(
        key=lambda row: (row["_start"], str(row.get("event_group")), str(row.get("sample_id")))
    )
    groups: list[tuple[object, list[dict[str, object]]]] = []
    for group in sorted(
        {row.get("event_group") for row in normalized},
        key=lambda value: min(r["_start"] for r in normalized if r.get("event_group") == value),
    ):
        groups.append((group, [row for row in normalized if row.get("event_group") == group]))
    folds = []
    for group, test_rows in groups:
        test_start = min(row["_start"] for row in test_rows)
        cutoff = test_start - timedelta(seconds=embargo_seconds)
        train = [
            row
            for row in normalized
            if row.get("event_group") != group
            and row["_end"] <= cutoff
            and row["_available"] <= cutoff
            and not any(
                row["_start"] < test["_end"] and row["_end"] > test["_start"] for test in test_rows
            )
        ]
        if mode == "rolling" and rolling_train_limit is not None:
            train = train[-rolling_train_limit:]
        status = "READY" if len(train) >= minimum_train_size else "SPARSE_REFUSE"
        public_train = [_public(row) for row in train]
        public_test = [_public(row) for row in test_rows]
        fold_body = {
            "test_event_group": group,
            "test_start": test_start.isoformat(),
            "embargo_cutoff": cutoff.isoformat(),
            "train_sample_ids": [row["sample_id"] for row in public_train],
            "test_sample_ids": [row["sample_id"] for row in public_test],
            "status": status,
            "mode": mode,
        }
        folds.append(
            {
                **fold_body,
                "fold_sha256": _digest(fold_body),
                "train": public_train,
                "test": public_test,
            }
        )
    result = _result(sorted(set(errors)), folds)
    result["source_sha256"] = _digest(samples)
    result["fold_set_sha256"] = _digest(folds)
    return result


def evaluate_walk_forward(
    folds: object, *, feature_field: str = "feature_value"
) -> dict[str, object]:
    errors: list[str] = []
    predictions = []
    if not isinstance(folds, list):
        return _evaluation(["FOLDS_INVALID"], [])
    for fold in folds:
        if fold.get("status") != "READY":
            continue
        train, test = fold.get("train", []), fold.get("test", [])
        if not train or any(feature_field not in row for row in train + test):
            errors.append(f"FOLD_{fold.get('fold_sha256')}_FEATURE_INVALID")
            continue
        values = [float(row[feature_field]) for row in train]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        scale = math.sqrt(variance) or 1.0
        positive = [float(row[feature_field]) for row in train if int(row["outcome"]) == 1]
        negative = [float(row[feature_field]) for row in train if int(row["outcome"]) == 0]
        threshold = (
            ((sum(positive) / len(positive)) + (sum(negative) / len(negative))) / 2
            if positive and negative
            else mean
        )
        for row in test:
            z = (float(row[feature_field]) - threshold) / scale
            probability = 1 / (1 + math.exp(-max(-20, min(20, z))))
            predictions.append(
                {
                    "sample_id": row["sample_id"],
                    "fold_sha256": fold["fold_sha256"],
                    "probability": probability,
                    "outcome": int(row["outcome"]),
                    "train_mean": mean,
                    "train_scale": scale,
                    "train_threshold": threshold,
                }
            )
    return _evaluation(sorted(set(errors)), predictions)


def compare_ordinary_to_purged(
    samples: list[dict[str, object]], *, embargo_seconds: int
) -> dict[str, object]:
    purged_folds = build_purged_folds(samples, embargo_seconds=embargo_seconds)
    purged = evaluate_walk_forward(purged_folds["folds"], feature_field="feature_value")
    ordinary_folds = []
    for test in samples:
        train = [row for row in samples if row.get("sample_id") != test.get("sample_id")]
        body = {
            "test_sample_ids": [test["sample_id"]],
            "train_sample_ids": [row["sample_id"] for row in train],
            "status": "READY",
        }
        ordinary_folds.append(
            {**body, "fold_sha256": _digest(body), "train": train, "test": [test]}
        )
    ordinary = evaluate_walk_forward(ordinary_folds, feature_field="revision_value")
    inflation = None
    if purged["brier_score"] is not None and ordinary["brier_score"] is not None:
        inflation = purged["brier_score"] - ordinary["brier_score"]
    result = {
        "schema": SCHEMA,
        "purged": purged,
        "ordinary": ordinary,
        "brier_inflation_from_leakage": inflation,
        "ordinary_uses_future_revision_control": True,
        "safety": _safety(),
    }
    result["comparison_sha256"] = _digest(result)
    return result


def audit_fold_leakage(folds: list[dict[str, object]]) -> dict[str, object]:
    violations = []
    for fold in folds:
        cutoff = _time(fold.get("embargo_cutoff"))
        test_groups = {row.get("event_group") for row in fold.get("test", [])}
        for row in fold.get("train", []):
            if (
                _time(row.get("label_end")) > cutoff
                or _time(row.get("available_at")) > cutoff
                or row.get("event_group") in test_groups
            ):
                violations.append(
                    {"fold_sha256": fold.get("fold_sha256"), "sample_id": row.get("sample_id")}
                )
    result = {
        "verdict": "PASS" if not violations else "REFUSE",
        "violations": violations,
        "safety": _safety(),
    }
    result["audit_sha256"] = _digest(result)
    return result


def _public(row):
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _result(errors, folds):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "folds": folds,
        "ready_fold_count": sum(fold["status"] == "READY" for fold in folds),
        "sparse_fold_count": sum(fold["status"] != "READY" for fold in folds),
        "safety": _safety(),
    }


def _evaluation(errors, predictions):
    brier = (
        sum((row["probability"] - row["outcome"]) ** 2 for row in predictions) / len(predictions)
        if predictions
        else None
    )
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "predictions": predictions,
        "prediction_count": len(predictions),
        "brier_score": brier,
        "safety": _safety(),
    }
    result["evaluation_sha256"] = _digest(result)
    return result


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
