"""Offline multiple-testing and strategy-selection-bias audit."""

from __future__ import annotations

import hashlib
import json
import math

SCHEMA = "phase4nb.strategy-selection-bias-audit.v1"
FIELDS = {
    "attempt_id",
    "strategy",
    "parameter_sha256",
    "feature_family",
    "correlation_family",
    "threshold",
    "market_subset",
    "evaluation_id",
    "status",
    "metric",
    "seed",
    "p_value",
    "effect",
    "standard_error",
    "holdout_id",
    "stage",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def audit_attempts(
    attempts: object,
    *,
    declared_attempt_count: int,
    alpha: float = 0.05,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(attempts, list) or declared_attempt_count < 0 or not 0 < alpha < 1:
        return _result(["INPUT_INVALID"], [])
    if len(attempts) != declared_attempt_count:
        errors.append("HIDDEN_OR_MISSING_ATTEMPTS")
    valid = []
    ids: set[str] = set()
    semantic: dict[str, str] = {}
    metrics: dict[tuple[object, object], set[object]] = {}
    for index, row in enumerate(attempts):
        row_errors = []
        if not isinstance(row, dict) or set(row) != FIELDS:
            errors.append(f"ATTEMPT_{index}_FIELD_SET_INVALID")
            continue
        attempt_id = str(row["attempt_id"])
        if attempt_id in ids:
            row_errors.append("DUPLICATE_ATTEMPT_ID")
        ids.add(attempt_id)
        if row["status"] not in {"PASSED", "FAILED", "ABANDONED"}:
            row_errors.append("STATUS_INVALID")
        if not isinstance(row["p_value"], (int, float)) or not 0 <= row["p_value"] <= 1:
            row_errors.append("P_VALUE_INVALID")
        if not isinstance(row["standard_error"], (int, float)) or row["standard_error"] <= 0:
            row_errors.append("STANDARD_ERROR_INVALID")
        identity = _digest(
            {
                key: row[key]
                for key in sorted(
                    FIELDS
                    - {
                        "attempt_id",
                        "evaluation_id",
                        "status",
                        "p_value",
                        "effect",
                        "standard_error",
                    }
                )
            }
        )
        if identity in semantic:
            row_errors.append("DUPLICATE_STRATEGY_ATTEMPT")
        semantic[identity] = attempt_id
        metrics.setdefault((row["strategy"], row["parameter_sha256"]), set()).add(row["metric"])
        if row_errors:
            errors.extend(f"ATTEMPT_{index}_{error}" for error in sorted(set(row_errors)))
        else:
            valid.append(row)
    if any(len(values) > 1 for values in metrics.values()):
        errors.append("POST_HOC_METRIC_SWITCHING")
    corrections = _corrections(valid, alpha)
    result = _result(sorted(set(errors)), corrections)
    result.update(
        {
            "declared_attempt_count": declared_attempt_count,
            "registered_attempt_count": len(attempts),
            "failed_or_abandoned_count": sum(
                row.get("status") != "PASSED" for row in attempts if isinstance(row, dict)
            ),
            "seed_count": len({row.get("seed") for row in attempts if isinstance(row, dict)}),
            "market_subset_count": len(
                {row.get("market_subset") for row in attempts if isinstance(row, dict)}
            ),
            "correlation_family_count": len(
                {row.get("correlation_family") for row in attempts if isinstance(row, dict)}
            ),
            "selection_history_sha256": _digest(attempts),
        }
    )
    result["audit_sha256"] = _digest(result)
    return result


def _corrections(rows: list[dict[str, object]], alpha: float) -> list[dict[str, object]]:
    count = len(rows)
    if not count:
        return []
    ordered = sorted(
        enumerate(rows), key=lambda item: (float(item[1]["p_value"]), str(item[1]["attempt_id"]))
    )
    holm_running = 0.0
    holm = {}
    for rank, (index, row) in enumerate(ordered):
        holm_running = max(holm_running, min(1.0, float(row["p_value"]) * (count - rank)))
        holm[index] = holm_running
    bh = {}
    bh_running = 1.0
    for reverse_rank in range(count - 1, -1, -1):
        index, row = ordered[reverse_rank]
        bh_running = min(bh_running, float(row["p_value"]) * count / (reverse_rank + 1))
        bh[index] = min(1.0, bh_running)
    results = []
    for index, row in enumerate(rows):
        bonferroni = min(1.0, float(row["p_value"]) * count)
        deflation_hurdle = float(row["standard_error"]) * math.sqrt(2 * math.log(max(2, count)))
        results.append(
            {
                "attempt_id": row["attempt_id"],
                "raw_p_value": row["p_value"],
                "bonferroni_p_value": bonferroni,
                "holm_p_value": holm[index],
                "benjamini_hochberg_q_value": bh[index],
                "raw_significant": float(row["p_value"]) <= alpha,
                "survives_all_corrections": max(bonferroni, holm[index], bh[index]) <= alpha
                and float(row["effect"]) > deflation_hurdle,
                "effect": row["effect"],
                "deflated_effect_hurdle": deflation_hurdle,
            }
        )
    return results


def validate_nested_selection(
    attempts: object,
    *,
    selected_attempt_id: str,
    final_holdout_id: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(attempts, list):
        return _nested(["ATTEMPTS_INVALID"], None)
    selected = next((row for row in attempts if row.get("attempt_id") == selected_attempt_id), None)
    if selected is None or selected.get("stage") != "INNER_SELECTION":
        errors.append("SELECTED_ATTEMPT_NOT_INNER_SELECTED")
    if any(
        row.get("holdout_id") == final_holdout_id and row.get("stage") != "FINAL_HOLDOUT"
        for row in attempts
    ):
        errors.append("HOLDOUT_PEEK_OR_REUSE")
    finals = [
        row
        for row in attempts
        if row.get("holdout_id") == final_holdout_id and row.get("stage") == "FINAL_HOLDOUT"
    ]
    if len(finals) != 1:
        errors.append("FINAL_HOLDOUT_MUST_RUN_EXACTLY_ONCE")
        final = None
    else:
        final = finals[0]
    if selected is not None and final is not None:
        identity_fields = (
            "strategy",
            "parameter_sha256",
            "feature_family",
            "threshold",
            "market_subset",
        )
        if any(selected.get(field) != final.get(field) for field in identity_fields):
            errors.append("FINAL_STRATEGY_SUBSTITUTION")
    return _nested(sorted(set(errors)), final)


def _nested(errors, final):
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "final_attempt_id": final.get("attempt_id") if final else None,
        "holdout_accepted": not errors,
        "safety": _safety(),
    }
    result["nested_selection_sha256"] = _digest(result)
    return result


def _result(errors, corrections):
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "corrections": corrections,
        "readiness": "PASS"
        if corrections
        and any(row["survives_all_corrections"] for row in corrections)
        and not errors
        else "REFUSE",
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
