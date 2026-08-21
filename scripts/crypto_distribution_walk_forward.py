#!/usr/bin/env python3
"""Chronological comparison of distribution, crypto_v2, and market probabilities."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from kalshi_predictor.crypto.distribution_model import (
    inputs_from_features,
    threshold_probability,
)
from kalshi_predictor.kalshi.protocol_math import fee_adjusted_expected_value, trading_fee

SCALES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
VOLATILITY_PRIOR_WEIGHT = 30.0
MIN_VOLATILITY_SCALE = 0.5
MAX_VOLATILITY_SCALE = 2.0
MIN_SEGMENT_TRAIN = 10
MIN_ACTIVATION_OBSERVATIONS = 100
MIN_POLICY_COHORT_N = 10
MIN_CONFIDENCE_TRAIN_N = 20
MIN_MARKET_BRIER_ADVANTAGE = 0.005
MIN_SCALE_BRIER_GAP = 0.0001
TAIL_PRIOR_WEIGHT = 40.0
TAIL_PRIOR_DF = 30.0
MIN_STUDENT_DF = 4.5
MAX_STUDENT_DF = 50.0
DRIFT_PRIOR_WEIGHT = 30.0
MAX_ABS_LOCATION_BIAS_Z = 1.0
MIXTURE_PRIOR_WEIGHT = 10.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-train-fraction", type=float, default=0.60)
    args = parser.parse_args()

    rows = _load_rows(args.database)
    contract_audits = [_contract_audit(raw) for raw in rows]
    prepared = [
        row
        for raw, audit in zip(rows, contract_audits, strict=True)
        if audit["eligible"] and (row := _prepare(raw, audit)) is not None
    ]
    cut = max(1, int(len(prepared) * args.initial_train_fraction))
    validation = prepared[cut:]
    predictions: list[dict[str, Any]] = []
    for index, row in enumerate(validation, start=cut):
        history = _settled_history(prepared[:index], row)
        calibration_history, calibration_level = _calibration_history(history, row)
        location_bias_z, drift_calibration = _regularized_location_bias(
            calibration_history
        )
        scale, volatility_calibration = _regularized_volatility_scale(
            calibration_history, location_bias_z
        )
        distribution = _distribution_probability(row, scale, location_bias_z)
        confidence = _calibration_confidence(
            calibration_history, scale, location_bias_z
        )
        student_df, tail_calibration = _regularized_student_df(
            calibration_history, location_bias_z
        )
        student_t = _student_t_probability(
            row, scale, student_df, location_bias_z
        )
        student_confidence = _student_t_confidence(
            calibration_history, scale, student_df, location_bias_z
        )
        mixture_parameters = _regularized_gaussian_mixture(
            calibration_history, location_bias_z
        )
        gaussian_mixture = _gaussian_mixture_probability(
            row, mixture_parameters, location_bias_z
        )
        mixture_confidence = _mixture_confidence(
            calibration_history, mixture_parameters, location_bias_z
        )
        predictions.append(
            {
                **row,
                "distribution": distribution,
                "scale": scale,
                "calibration_level": calibration_level,
                "calibration_train_n": len(calibration_history),
                "calibration_confidence": confidence,
                "location_bias_z": location_bias_z,
                "drift_calibration": drift_calibration,
                "volatility_calibration": volatility_calibration,
                "student_t": student_t,
                "student_t_df": student_df,
                "tail_calibration": tail_calibration,
                "student_t_calibration_confidence": student_confidence,
                "gaussian_mixture": gaussian_mixture,
                "gaussian_mixture_parameters": mixture_parameters,
                "gaussian_mixture_calibration_confidence": mixture_confidence,
            }
        )

    metrics = {
        "distribution_v1": _metrics(predictions, "distribution"),
        "student_t_v1": _metrics(predictions, "student_t"),
        "gaussian_mixture_v1": _metrics(predictions, "gaussian_mixture"),
        "crypto_v2": _metrics(predictions, "crypto_v2"),
        "market_implied": _metrics(predictions, "market_implied"),
    }
    payload = {
        "status": "EXPERIMENTAL" if predictions else "INSUFFICIENT_COVERAGE",
        "policy": "EXPANDING_WINDOW_NO_FUTURE_ROWS",
        "models": [
            "distribution_v1",
            "student_t_v1",
            "gaussian_mixture_v1",
            "crypto_v2",
            "market_implied",
        ],
        "observations": len(prepared),
        "initial_train_n": cut,
        "validation_n": len(predictions),
        "volatility_calibration_policy": {
            "method": "EXPANDING_WINDOW_REALIZED_MOVE_RMS_LOG_SHRINKAGE",
            "prior_scale": 1.0,
            "prior_weight": VOLATILITY_PRIOR_WEIGHT,
            "minimum_scale": MIN_VOLATILITY_SCALE,
            "maximum_scale": MAX_VOLATILITY_SCALE,
            "requires_prior_settlement": True,
        },
        "volatility_diagnosis": _volatility_diagnosis(prepared),
        "standardized_tail_diagnosis": _tail_diagnosis(prepared),
        "contract_reference_audit": _contract_audit_summary(contract_audits),
        "metrics": metrics,
        "calibration": {
            "by_symbol": _group_metrics(predictions, "symbol"),
            "by_horizon": _group_metrics(predictions, "horizon_band"),
            "by_probability_band": _group_metrics(predictions, "probability_band"),
        },
        "winner_by_brier": min(
            (name for name, value in metrics.items() if value["brier_score"] is not None),
            key=lambda name: metrics[name]["brier_score"],
            default=None,
        ),
        "promotion_allowed": False,
        "activation_gate": {
            "minimum_unique_settled_markets": MIN_ACTIVATION_OBSERVATIONS,
            "sample_requirement_met": len(prepared) >= MIN_ACTIVATION_OBSERVATIONS,
            "decision": "REMAIN_RESEARCH_ONLY",
        },
        "forensics": {
            "btc": _forensic_summary(
                [row for row in predictions if row["symbol"] == "BTC"]
            ),
            "sub_20_percent": _forensic_summary(
                [row for row in predictions if row["market_implied"] < 0.20]
            ),
        },
        "restricted_shadow_policy": _restricted_shadow_policy(predictions),
        "restricted_shadow_policy_comparison": {
            "gaussian": _restricted_shadow_policy(predictions),
            "student_t": _restricted_shadow_policy(
                predictions,
                field="student_t",
                confidence_field="student_t_calibration_confidence",
            ),
            "gaussian_mixture": _restricted_shadow_policy(
                predictions,
                field="gaussian_mixture",
                confidence_field="gaussian_mixture_calibration_confidence",
            ),
        },
        "rows": predictions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {key: value for key, value in payload.items() if key != "rows"}
    print(json.dumps(summary, sort_keys=True))
    return 0


def _load_rows(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    rows = list(connection.execute(_SQL))
    connection.close()
    return rows


def _load_rows_for_forecast_ids(
    database: Path, forecast_ids: list[int]
) -> list[sqlite3.Row]:
    if not forecast_ids:
        return []
    placeholders = ",".join("?" for _ in forecast_ids)
    target = "WHERE f.model_name='crypto_v2' AND s.result IN ('yes','no')"
    replacement = target + f" AND f.id IN ({placeholders})"
    sql = _SQL.replace(target, replacement, 1)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    rows = list(connection.execute(sql, forecast_ids))
    connection.close()
    return rows


def _prepare(
    row: sqlite3.Row, audit: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    audit = audit or _contract_audit(row)
    if not audit["eligible"]:
        return None
    feature_json = json.loads(row["feature_json"] or "{}")
    structured = feature_json.get("structured_terms") or {}
    components = structured.get("components") or []
    component = components[0] if len(components) == 1 else None
    if not isinstance(component, dict):
        return None
    features = {
        "price": row["spot"],
        "return_1h": row["return_1h"],
        "volatility_1h": row["volatility_1h"],
        "volatility_4h": row["volatility_4h"],
        "volatility_24h": row["volatility_24h"],
    }
    horizon = max(1.0, float(row["horizon_minutes"] or 0))
    if inputs_from_features(features, horizon_minutes=horizon) is None:
        return None
    market_raw = json.loads(row["market_raw_json"] or "{}")
    threshold = _number(component.get("threshold_value"))
    lower = _number(market_raw.get("floor_strike"))
    upper = _number(market_raw.get("cap_strike"))
    prepared = dict(row)
    prepared.update(
        features=features,
        comparator=str(component.get("comparator") or ""),
        threshold=threshold,
        lower=lower,
        upper=upper,
        outcome=float(row["outcome"]),
        crypto_v2=float(row["crypto_v2"]),
        market_implied=float(row["market_implied"]),
        event_ticker=str(market_raw.get("event_ticker") or ""),
        symbol=str(row["symbol"] or "UNKNOWN").upper(),
        horizon_band=_horizon_band(horizon),
        probability_band=_probability_band(float(row["market_implied"])),
        settlement_time=row["settlement_time"],
        forecast_jd=float(row["forecast_jd"]),
        settlement_jd=float(row["settlement_jd"]),
        terminal_spot=audit["kalshi_terminal_reference"],
        terminal_observed_at=row["terminal_observed_at"],
        harvested_terminal_spot=_number(row["terminal_spot"]),
        terminal_reference_basis_difference=audit["harvested_spot_basis_difference"],
    )
    inputs = inputs_from_features(features, horizon_minutes=horizon)
    assert inputs is not None
    prepared["forecast_sigma"] = (
        inputs.volatility_per_minute * math.sqrt(horizon)
    )
    prepared["forecast_drift"] = inputs.drift_per_minute * horizon
    terminal_spot = prepared["terminal_spot"]
    prepared["realized_log_move"] = (
        math.log(terminal_spot / inputs.spot)
        if terminal_spot is not None and terminal_spot > 0 and inputs.spot > 0
        else None
    )
    return prepared


def _contract_audit(row: sqlite3.Row) -> dict[str, Any]:
    market_raw = json.loads(row["market_raw_json"] or "{}")
    floor = _number(market_raw.get("floor_strike"))
    cap = _number(market_raw.get("cap_strike"))
    terminal = _number(market_raw.get("expiration_value"))
    rules = str(market_raw.get("rules_primary") or "")
    rules_secondary = str(market_raw.get("rules_secondary") or "")
    result = "yes" if float(row["outcome"]) == 1.0 else "no"
    reasons: list[str] = []
    feature_raw_value = _row_value(row, "crypto_feature_raw_json")
    if feature_raw_value is not None:
        feature_raw = json.loads(feature_raw_value or "{}")
        if feature_raw.get("feature_version") != "crypto_features_v3_interval_normalized":
            reasons.append("VOLATILITY_FEATURE_UNIT_NOT_REBUILT")
    rule_bounds = _parse_rule_bounds(rules)
    if terminal is None or terminal <= 0:
        reasons.append("KALSHI_TERMINAL_REFERENCE_MISSING")
    if floor is None or cap is None or cap < floor:
        reasons.append("RAW_BUCKET_BOUNDS_INVALID")
    if rule_bounds is None:
        reasons.append("RULE_BUCKET_BOUNDS_UNPARSEABLE")
    elif floor is not None and cap is not None and (
        abs(rule_bounds[0] - floor) > 0.001 or abs(rule_bounds[1] - cap) > 0.001
    ):
        reasons.append("RULE_AND_RAW_BUCKET_BOUNDS_MISMATCH")
    reference_text = f"{rules} {rules_secondary}".lower()
    if "cf benchmarks" not in reference_text or "real-time index" not in reference_text:
        reasons.append("SETTLEMENT_REFERENCE_SOURCE_AMBIGUOUS")
    if terminal is not None and floor is not None and cap is not None:
        reconstructed = "yes" if floor <= terminal <= cap else "no"
        if reconstructed != result:
            reasons.append("TERMINAL_REFERENCE_RESULT_MISMATCH")
    harvested = _number(row["terminal_spot"])
    return {
        "ticker": row["ticker"],
        "eligible": not reasons,
        "exclusion_reasons": reasons,
        "kalshi_terminal_reference": terminal,
        "floor": floor,
        "cap": cap,
        "rule_floor": rule_bounds[0] if rule_bounds else None,
        "rule_cap": rule_bounds[1] if rule_bounds else None,
        "result": result,
        "harvested_terminal_spot": harvested,
        "harvested_spot_basis_difference": (
            harvested - terminal
            if harvested is not None and terminal is not None
            else None
        ),
    }


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[key] if key in row.keys() else None


def _parse_rule_bounds(rules: str) -> tuple[float, float] | None:
    match = re.search(
        r"\bbetween\s+\$?([\d,]+(?:\.\d+)?)\s*-\s*\$?([\d,]+(?:\.\d+)?)",
        rules,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return float(match.group(1).replace(",", "")), float(
        match.group(2).replace(",", "")
    )


def _contract_audit_summary(audits: list[dict[str, Any]]) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    basis = [
        abs(audit["harvested_spot_basis_difference"])
        for audit in audits
        if audit["harvested_spot_basis_difference"] is not None
    ]
    basis_bps = [
        abs(audit["harvested_spot_basis_difference"])
        / audit["kalshi_terminal_reference"]
        * 10_000
        for audit in audits
        if audit["harvested_spot_basis_difference"] is not None
        and audit["kalshi_terminal_reference"]
    ]
    for audit in audits:
        for reason in audit["exclusion_reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "reviewed": len(audits),
        "eligible": sum(1 for audit in audits if audit["eligible"]),
        "excluded": sum(1 for audit in audits if not audit["eligible"]),
        "exclusion_reason_counts": dict(sorted(reason_counts.items())),
        "harvested_spot_comparisons": len(basis),
        "harvested_spot_mean_absolute_basis_difference": (
            sum(basis) / len(basis) if basis else None
        ),
        "harvested_spot_mean_absolute_basis_bps": (
            sum(basis_bps) / len(basis_bps) if basis_bps else None
        ),
        "rows": audits,
    }


def _settled_history(
    history: list[dict[str, Any]], row: dict[str, Any]
) -> list[dict[str, Any]]:
    return [item for item in history if item["settlement_jd"] <= row["forecast_jd"]]


def _regularized_location_bias(
    history: list[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    residuals = [
        value for row in history if (value := _standardized_residual(row)) is not None
    ]
    if not residuals:
        return 0.0, {"residual_n": 0, "raw_bias_z": None, "bias_z": 0.0}
    raw_bias = sum(residuals) / len(residuals)
    weight = len(residuals) / (len(residuals) + DRIFT_PRIOR_WEIGHT)
    bias = max(
        -MAX_ABS_LOCATION_BIAS_Z,
        min(MAX_ABS_LOCATION_BIAS_Z, raw_bias * weight),
    )
    return bias, {
        "residual_n": len(residuals),
        "raw_bias_z": raw_bias,
        "shrinkage_weight": weight,
        "bias_z": bias,
    }


def _regularized_volatility_scale(
    history: list[dict[str, Any]], location_bias_z: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    usable = [
        row
        for row in history
        if row.get("realized_log_move") is not None and row["forecast_sigma"] > 0
    ]
    if not usable:
        return 1.0, {"realized_move_n": 0, "raw_scale": None, "scale": 1.0}
    numerator = sum(
        (
            row["realized_log_move"]
            - row["forecast_drift"]
            - location_bias_z * row["forecast_sigma"]
        )
        ** 2
        for row in usable
    )
    denominator = sum(row["forecast_sigma"] ** 2 for row in usable)
    raw_scale = math.sqrt(numerator / denominator) if denominator > 0 else 1.0
    weight = len(usable) / (len(usable) + VOLATILITY_PRIOR_WEIGHT)
    shrunk_log_scale = weight * math.log(max(raw_scale, 1e-9))
    scale = max(
        MIN_VOLATILITY_SCALE,
        min(MAX_VOLATILITY_SCALE, math.exp(shrunk_log_scale)),
    )
    return scale, {
        "realized_move_n": len(usable),
        "raw_scale": raw_scale,
        "shrinkage_weight": weight,
        "scale": scale,
    }


def _volatility_diagnosis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "policy": "EXACT_KALSHI_EXPIRATION_VALUE_AFTER_CONTRACT_AUDIT",
        "overall": _volatility_group(rows),
        "by_symbol": _volatility_groups(rows, "symbol"),
        "by_horizon": _volatility_groups(rows, "horizon_band"),
    }


def _volatility_groups(
    rows: list[dict[str, Any]], field: str
) -> dict[str, dict[str, Any]]:
    keys = sorted({str(row[field]) for row in rows})
    return {
        key: _volatility_group([row for row in rows if str(row[field]) == key])
        for key in keys
    }


def _volatility_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in rows if row.get("realized_log_move") is not None]
    if not usable:
        return {"n": len(rows), "terminal_move_n": 0, "realized_to_forecast_rms": None}
    realized_rms = math.sqrt(
        sum((row["realized_log_move"] - row["forecast_drift"]) ** 2 for row in usable)
        / len(usable)
    )
    forecast_rms = math.sqrt(
        sum(row["forecast_sigma"] ** 2 for row in usable) / len(usable)
    )
    return {
        "n": len(rows),
        "terminal_move_n": len(usable),
        "realized_move_rms": realized_rms,
        "forecast_sigma_rms": forecast_rms,
        "realized_to_forecast_rms": (
            realized_rms / forecast_rms if forecast_rms > 0 else None
        ),
    }


def _standardized_residual(row: dict[str, Any]) -> float | None:
    if row["forecast_sigma"] <= 0 or row.get("realized_log_move") is None:
        return None
    return (row["realized_log_move"] - row["forecast_drift"]) / row["forecast_sigma"]


def _tail_diagnosis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "policy": "EXACT_KALSHI_TERMINAL_RETURN_DIVIDED_BY_FORECAST_SIGMA",
        "overall": _tail_group(rows),
        "by_symbol": {
            key: _tail_group([row for row in rows if row["symbol"] == key])
            for key in sorted({row["symbol"] for row in rows})
        },
        "by_horizon": {
            key: _tail_group([row for row in rows if row["horizon_band"] == key])
            for key in sorted({row["horizon_band"] for row in rows})
        },
    }


def _tail_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [value for row in rows if (value := _standardized_residual(row)) is not None]
    if len(values) < 2:
        return {"n": len(values), "skew": None, "excess_kurtosis": None}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    standard_deviation = math.sqrt(variance)
    skew = (
        sum((value - mean) ** 3 for value in values)
        / len(values)
        / standard_deviation**3
        if standard_deviation > 0
        else 0.0
    )
    excess = (
        sum((value - mean) ** 4 for value in values)
        / len(values)
        / standard_deviation**4
        - 3.0
        if standard_deviation > 0
        else 0.0
    )
    return {
        "n": len(values),
        "mean": mean,
        "standard_deviation": standard_deviation,
        "skew": skew,
        "excess_kurtosis": excess,
        "absolute_over_2_sigma_rate": sum(abs(value) > 2 for value in values) / len(values),
        "absolute_over_3_sigma_rate": sum(abs(value) > 3 for value in values) / len(values),
    }


def _horizon_band(minutes: float) -> str:
    if minutes <= 60:
        return "00-01h"
    if minutes <= 360:
        return "01-06h"
    if minutes <= 1440:
        return "06-24h"
    if minutes <= 4320:
        return "01-03d"
    return "03d+"


def _probability_band(probability: float) -> str:
    edges = ((0.05, "00-05%"), (0.20, "05-20%"), (0.40, "20-40%"),
             (0.60, "40-60%"), (0.80, "60-80%"), (0.95, "80-95%"))
    for upper, label in edges:
        if probability < upper:
            return label
    return "95-100%"


def _calibration_history(
    history: list[dict[str, Any]], row: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    symbol_horizon = [
        item for item in history
        if item["symbol"] == row["symbol"] and item["horizon_band"] == row["horizon_band"]
    ]
    if len(symbol_horizon) >= MIN_SEGMENT_TRAIN:
        return symbol_horizon, "SYMBOL_HORIZON"
    symbol = [item for item in history if item["symbol"] == row["symbol"]]
    if len(symbol) >= MIN_SEGMENT_TRAIN:
        return symbol, "SYMBOL"
    return history, "GLOBAL"


def _calibration_confidence(
    history: list[dict[str, Any]], selected_scale: float, location_bias_z: float = 0.0
) -> dict[str, Any]:
    selected_brier = _distribution_brier(history, selected_scale, location_bias_z)
    alternatives = {
        max(MIN_VOLATILITY_SCALE, selected_scale * 0.9),
        min(MAX_VOLATILITY_SCALE, selected_scale * 1.1),
    }
    runner_up_gap = min(
        (
            _distribution_brier(history, scale, location_bias_z) - selected_brier
            for scale in alternatives
        ),
        default=0.0,
    )
    market_brier = _field_brier(history, "market_implied")
    market_advantage = (
        market_brier - selected_brier if market_brier is not None else None
    )
    checks = {
        "train_n": len(history) >= MIN_CONFIDENCE_TRAIN_N,
        "market_advantage": (
            market_advantage is not None
            and market_advantage >= MIN_MARKET_BRIER_ADVANTAGE
        ),
        "scale_separation": runner_up_gap >= MIN_SCALE_BRIER_GAP,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "selected_brier": selected_brier,
        "market_brier": market_brier,
        "market_brier_advantage": market_advantage,
        "runner_up_scale_brier_gap": runner_up_gap,
    }


def _field_brier(rows: list[dict[str, Any]], field: str) -> float | None:
    if not rows:
        return None
    return sum((float(row[field]) - row["outcome"]) ** 2 for row in rows) / len(rows)


def _trade_result(row: dict[str, Any], field: str = "distribution") -> dict[str, Any] | None:
    probability = max(0.001, min(0.999, float(row[field])))
    options = [
        (
            "yes",
            fee_adjusted_expected_value(probability=probability, price=row["yes_ask"]),
            row["yes_ask"],
        ),
        (
            "no",
            fee_adjusted_expected_value(probability=1 - probability, price=row["no_ask"]),
            row["no_ask"],
        ),
    ]
    side, ev, price = max(
        options, key=lambda item: item[1] if item[1] is not None else -999
    )
    if ev is None or ev <= 0:
        return None
    realized = row["outcome"] if side == "yes" else 1 - row["outcome"]
    pnl = realized - price - float(trading_fee(price=price) or 0)
    return {
        "side": side,
        "expected_value": float(ev),
        "price": price,
        "pnl": pnl,
    }


def _forensic_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [(row, trade) for row in rows if (trade := _trade_result(row)) is not None]
    return {
        "rows": len(rows),
        "trades": len(trades),
        "wins": sum(1 for _, trade in trades if trade["pnl"] > 0),
        "net_pnl": sum(trade["pnl"] for _, trade in trades),
        "scale_counts": _counts(rows, "scale"),
        "probability_band_counts": _counts(rows, "probability_band"),
        "calibration_level_counts": _counts(rows, "calibration_level"),
        "trade_rows": [
            {
                "ticker": row["ticker"],
                "symbol": row["symbol"],
                "market_implied": row["market_implied"],
                "distribution": row["distribution"],
                "scale": row["scale"],
                **trade,
            }
            for row, trade in trades
        ],
    }


def _counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row[field])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _restricted_shadow_policy(
    rows: list[dict[str, Any]],
    *,
    field: str = "distribution",
    confidence_field: str = "calibration_confidence",
) -> dict[str, Any]:
    cohort = [
        row
        for row in rows
        if row["symbol"] == "ETH" and 0.20 <= row["market_implied"] < 0.80
    ]
    cohort_size_ok = len(cohort) >= MIN_POLICY_COHORT_N
    accepted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejection_counts: dict[str, int] = {}
    for row in cohort:
        reasons = []
        if not cohort_size_ok:
            reasons.append("COHORT_N_BELOW_MINIMUM")
        confidence = row[confidence_field]
        if not confidence["passed"]:
            reasons.append("CALIBRATION_CONFIDENCE_FAILED")
        trade = _trade_result(row, field)
        if trade is None:
            reasons.append("EV_NOT_POSITIVE_AFTER_FEES")
        if reasons:
            for reason in reasons:
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        elif trade is not None:
            accepted.append((row, trade))
    return {
        "mode": "OFFLINE_RESTRICTED_SHADOW_SIMULATION",
        "model_field": field,
        "continuous_shadow_enabled": False,
        "policy": "ETH_AND_MARKET_PROBABILITY_20_TO_80_PERCENT",
        "minimum_cohort_n": MIN_POLICY_COHORT_N,
        "cohort_n": len(cohort),
        "cohort_size_gate_passed": cohort_size_ok,
        "accepted_trades": len(accepted),
        "net_pnl": sum(trade["pnl"] for _, trade in accepted),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "decision": "DO_NOT_ENABLE_CONTINUOUS_SHADOW",
    }


def _group_metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row[field]), []).append(row)
    return {
        key: {
            "n": len(group),
            "distribution_v1": _metrics(group, "distribution"),
            "student_t_v1": _metrics(group, "student_t"),
            "gaussian_mixture_v1": _metrics(group, "gaussian_mixture"),
            "crypto_v2": _metrics(group, "crypto_v2"),
            "market_implied": _metrics(group, "market_implied"),
        }
        for key, group in sorted(groups.items())
    }


def _distribution_probability(
    row: dict[str, Any], scale: float, location_bias_z: float = 0.0
) -> float:
    if location_bias_z == 0.0:
        return _base_distribution_probability(row, scale)
    return _gaussian_probability(row, scale, location_bias_z)


def _base_distribution_probability(row: dict[str, Any], scale: float) -> float:
    inputs = inputs_from_features(
        row["features"],
        horizon_minutes=float(row["horizon_minutes"]),
        volatility_scale=scale,
    )
    assert inputs is not None
    return threshold_probability(
        inputs,
        comparator=row["comparator"],
        threshold=row["threshold"],
        lower=row["lower"],
        upper=row["upper"],
    ) or 0.5


def _distribution_brier(
    rows: list[dict[str, Any]], scale: float, location_bias_z: float = 0.0
) -> float:
    if not rows:
        return math.inf
    squared_errors = [
        (_distribution_probability(row, scale, location_bias_z) - row["outcome"]) ** 2
        for row in rows
    ]
    return sum(squared_errors) / len(rows)


def _gaussian_probability(
    row: dict[str, Any], scale: float, location_bias_z: float
) -> float:
    inputs = inputs_from_features(
        row["features"], horizon_minutes=float(row["horizon_minutes"])
    )
    assert inputs is not None
    base_sigma = inputs.volatility_per_minute * math.sqrt(inputs.horizon_minutes)
    location = inputs.drift_per_minute * inputs.horizon_minutes + location_bias_z * base_sigma
    sigma = max(1e-9, base_sigma * scale)
    return _probability_from_cdf(
        row,
        lambda strike: 0.5
        * (1.0 + math.erf((math.log(strike / inputs.spot) - location) / sigma / math.sqrt(2.0))),
    )


def _regularized_gaussian_mixture(
    history: list[dict[str, Any]], location_bias_z: float
) -> dict[str, Any]:
    values = [
        value - location_bias_z
        for row in history
        if (value := _standardized_residual(row)) is not None
    ]
    if len(values) < 4:
        return {
            "residual_n": len(values),
            "weight": 0.5,
            "mean_1": 0.0,
            "sigma_1": 1.0,
            "mean_2": 0.0,
            "sigma_2": 1.0,
        }
    ordered = sorted(values)
    mean_1 = ordered[len(ordered) // 4]
    mean_2 = ordered[(3 * len(ordered)) // 4]
    sigma_1 = sigma_2 = 1.0
    weight = 0.5
    prior_each = MIXTURE_PRIOR_WEIGHT / 2.0
    for _ in range(30):
        responsibilities = []
        for value in values:
            first = weight * _normal_pdf(value, mean_1, sigma_1)
            second = (1.0 - weight) * _normal_pdf(value, mean_2, sigma_2)
            responsibilities.append(first / max(first + second, 1e-300))
        count_1 = sum(responsibilities)
        count_2 = len(values) - count_1
        weight = (count_1 + prior_each) / (len(values) + MIXTURE_PRIOR_WEIGHT)
        paired = list(zip(responsibilities, values, strict=True))
        mean_1 = sum(r * value for r, value in paired) / max(count_1, 1e-9)
        mean_2 = sum((1 - r) * value for r, value in paired) / max(count_2, 1e-9)
        variance_1 = (
            sum(r * (value - mean_1) ** 2 for r, value in paired)
            + prior_each
        ) / (count_1 + prior_each)
        variance_2 = (
            sum((1 - r) * (value - mean_2) ** 2 for r, value in paired)
            + prior_each
        ) / (count_2 + prior_each)
        sigma_1 = max(0.1, min(3.0, math.sqrt(variance_1)))
        sigma_2 = max(0.1, min(3.0, math.sqrt(variance_2)))
    if mean_1 > mean_2:
        mean_1, mean_2 = mean_2, mean_1
        sigma_1, sigma_2 = sigma_2, sigma_1
        weight = 1.0 - weight
    return {
        "residual_n": len(values),
        "weight": weight,
        "mean_1": mean_1,
        "sigma_1": sigma_1,
        "mean_2": mean_2,
        "sigma_2": sigma_2,
    }


def _gaussian_mixture_probability(
    row: dict[str, Any], parameters: dict[str, Any], location_bias_z: float
) -> float:
    inputs = inputs_from_features(
        row["features"], horizon_minutes=float(row["horizon_minutes"])
    )
    assert inputs is not None
    base_sigma = inputs.volatility_per_minute * math.sqrt(inputs.horizon_minutes)
    base_location = (
        inputs.drift_per_minute * inputs.horizon_minutes
        + location_bias_z * base_sigma
    )

    def cdf(strike: float) -> float:
        standardized = (math.log(strike / inputs.spot) - base_location) / max(base_sigma, 1e-9)
        first = _normal_cdf(standardized, parameters["mean_1"], parameters["sigma_1"])
        second = _normal_cdf(standardized, parameters["mean_2"], parameters["sigma_2"])
        return parameters["weight"] * first + (1 - parameters["weight"]) * second

    return _probability_from_cdf(row, cdf)


def _mixture_brier(
    rows: list[dict[str, Any]], parameters: dict[str, Any], location_bias_z: float
) -> float:
    if not rows:
        return math.inf
    return sum(
        (_gaussian_mixture_probability(row, parameters, location_bias_z) - row["outcome"]) ** 2
        for row in rows
    ) / len(rows)


def _mixture_confidence(
    history: list[dict[str, Any]], parameters: dict[str, Any], location_bias_z: float
) -> dict[str, Any]:
    selected_brier = _mixture_brier(history, parameters, location_bias_z)
    symmetric = {
        "residual_n": parameters["residual_n"],
        "weight": 0.5,
        "mean_1": 0.0,
        "sigma_1": 1.0,
        "mean_2": 0.0,
        "sigma_2": 1.0,
    }
    runner_up_gap = _mixture_brier(history, symmetric, location_bias_z) - selected_brier
    market_brier = _field_brier(history, "market_implied")
    market_advantage = market_brier - selected_brier if market_brier is not None else None
    checks = {
        "train_n": len(history) >= MIN_CONFIDENCE_TRAIN_N,
        "market_advantage": (
            market_advantage is not None
            and market_advantage >= MIN_MARKET_BRIER_ADVANTAGE
        ),
        "scale_separation": runner_up_gap >= MIN_SCALE_BRIER_GAP,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "selected_brier": selected_brier,
        "market_brier": market_brier,
        "market_brier_advantage": market_advantage,
        "runner_up_scale_brier_gap": runner_up_gap,
    }


def _probability_from_cdf(row: dict[str, Any], cdf: Any) -> float:
    comparator = row["comparator"].strip().upper()
    if comparator == "RANGE" and row["lower"] is not None and row["upper"] is not None:
        probability = cdf(row["upper"]) - cdf(row["lower"])
    elif comparator in {"ABOVE", "GREATER_THAN", "AT_OR_ABOVE"}:
        probability = 1.0 - cdf(row["threshold"])
    elif comparator in {"BELOW", "LESS_THAN", "AT_OR_BELOW"}:
        probability = cdf(row["threshold"])
    else:
        probability = 0.5
    return max(0.001, min(0.999, probability))


def _normal_pdf(value: float, mean: float, sigma: float) -> float:
    z = (value - mean) / sigma
    return math.exp(-0.5 * z * z) / sigma / math.sqrt(2.0 * math.pi)


def _normal_cdf(value: float, mean: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((value - mean) / sigma / math.sqrt(2.0)))


def _regularized_student_df(
    history: list[dict[str, Any]], location_bias_z: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    residuals = [
        value - location_bias_z
        for row in history
        if (value := _standardized_residual(row)) is not None
    ]
    if len(residuals) < 4:
        return TAIL_PRIOR_DF, {
            "residual_n": len(residuals),
            "sample_excess_kurtosis": None,
            "regularized_excess_kurtosis": 6.0 / (TAIL_PRIOR_DF - 4.0),
            "degrees_of_freedom": TAIL_PRIOR_DF,
        }
    mean = sum(residuals) / len(residuals)
    variance = sum((value - mean) ** 2 for value in residuals) / len(residuals)
    sample_excess = (
        sum((value - mean) ** 4 for value in residuals)
        / len(residuals)
        / variance**2
        - 3.0
        if variance > 0
        else 0.0
    )
    sample_excess = max(0.0, sample_excess)
    weight = len(residuals) / (len(residuals) + TAIL_PRIOR_WEIGHT)
    prior_excess = 6.0 / (TAIL_PRIOR_DF - 4.0)
    regularized_excess = weight * sample_excess + (1 - weight) * prior_excess
    degrees_of_freedom = (
        4.0 + 6.0 / regularized_excess
        if regularized_excess > 0
        else MAX_STUDENT_DF
    )
    degrees_of_freedom = max(
        MIN_STUDENT_DF, min(MAX_STUDENT_DF, degrees_of_freedom)
    )
    return degrees_of_freedom, {
        "residual_n": len(residuals),
        "sample_excess_kurtosis": sample_excess,
        "regularized_excess_kurtosis": regularized_excess,
        "shrinkage_weight": weight,
        "degrees_of_freedom": degrees_of_freedom,
    }


def _student_t_probability(
    row: dict[str, Any], scale: float, df: float, location_bias_z: float = 0.0
) -> float:
    inputs = inputs_from_features(
        row["features"],
        horizon_minutes=float(row["horizon_minutes"]),
        volatility_scale=scale,
    )
    assert inputs is not None
    sigma = inputs.volatility_per_minute * math.sqrt(inputs.horizon_minutes) * scale
    base_sigma = inputs.volatility_per_minute * math.sqrt(inputs.horizon_minutes)
    location = (
        inputs.drift_per_minute * inputs.horizon_minutes
        + location_bias_z * base_sigma
    )
    t_scale = max(1e-9, sigma * math.sqrt((df - 2.0) / df))

    def below(value: float) -> float:
        if value <= 0:
            return 0.0
        return _student_t_cdf((math.log(value / inputs.spot) - location) / t_scale, df)

    comparator = row["comparator"].strip().upper()
    if comparator == "RANGE" and row["lower"] is not None and row["upper"] is not None:
        probability = below(row["upper"]) - below(row["lower"])
    elif comparator in {"ABOVE", "GREATER_THAN", "AT_OR_ABOVE"}:
        probability = 1.0 - below(row["threshold"])
    elif comparator in {"BELOW", "LESS_THAN", "AT_OR_BELOW"}:
        probability = below(row["threshold"])
    else:
        probability = 0.5
    return max(0.001, min(0.999, probability))


def _student_t_brier(
    rows: list[dict[str, Any]],
    scale: float,
    df: float,
    location_bias_z: float = 0.0,
) -> float:
    if not rows:
        return math.inf
    return sum(
        (_student_t_probability(row, scale, df, location_bias_z) - row["outcome"]) ** 2
        for row in rows
    ) / len(rows)


def _student_t_confidence(
    history: list[dict[str, Any]],
    scale: float,
    df: float,
    location_bias_z: float = 0.0,
) -> dict[str, Any]:
    selected_brier = _student_t_brier(history, scale, df, location_bias_z)
    alternatives = {
        max(MIN_STUDENT_DF, df * 0.8),
        min(MAX_STUDENT_DF, df * 1.2),
    }
    runner_up_gap = min(
        (
            _student_t_brier(history, scale, candidate, location_bias_z)
            for candidate in alternatives
        ),
        default=0.0,
    )
    market_brier = _field_brier(history, "market_implied")
    market_advantage = market_brier - selected_brier if market_brier is not None else None
    checks = {
        "train_n": len(history) >= MIN_CONFIDENCE_TRAIN_N,
        "market_advantage": (
            market_advantage is not None
            and market_advantage >= MIN_MARKET_BRIER_ADVANTAGE
        ),
        "scale_separation": runner_up_gap >= MIN_SCALE_BRIER_GAP,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "selected_brier": selected_brier,
        "market_brier": market_brier,
        "market_brier_advantage": market_advantage,
        "runner_up_scale_brier_gap": runner_up_gap,
    }


def _student_t_cdf(value: float, df: float) -> float:
    x = df / (df + value * value)
    beta = _regularized_incomplete_beta(x, df / 2.0, 0.5)
    return 1.0 - 0.5 * beta if value >= 0 else 0.5 * beta


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / max(tiny, abs(d)) * (1 if d >= 0 else -1)
    result = d
    for iteration in range(1, 201):
        twice = 2 * iteration
        numerator = iteration * (b - iteration) * x / ((qam + twice) * (a + twice))
        d = 1.0 + numerator * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + numerator / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        result *= d * c
        numerator = -(a + iteration) * (qab + iteration) * x / (
            (a + twice) * (qap + twice)
        )
        d = 1.0 + numerator * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + numerator / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return result


def _metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    if not rows:
        return {"brier_score": None, "log_loss": None, "net_positive_trades": 0, "net_pnl": "0"}
    probabilities = [max(0.001, min(0.999, float(row[field]))) for row in rows]
    paired = list(zip(probabilities, rows, strict=True))
    brier = sum((probability - row["outcome"]) ** 2 for probability, row in paired) / len(rows)
    log_loss = -sum(
        row["outcome"] * math.log(probability)
        + (1 - row["outcome"]) * math.log(1 - probability)
        for probability, row in paired
    ) / len(rows)
    trades = [
        trade["pnl"]
        for row in rows
        if (trade := _trade_result(row, field)) is not None
    ]
    return {
        "brier_score": brier,
        "log_loss": log_loss,
        "net_positive_trades": len(trades),
        "net_pnl": str(sum(trades)),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_SQL = """
WITH ranked AS (
  SELECT f.*, row_number() OVER (PARTITION BY f.ticker ORDER BY f.forecasted_at, f.id) rn
  FROM forecasts f JOIN settlements s ON s.ticker=f.ticker
  WHERE f.model_name='crypto_v2' AND s.result IN ('yes','no')
)
SELECT f.id forecast_id, f.ticker, f.forecasted_at, f.feature_json, m.raw_json market_raw_json,
  cf.symbol symbol, cf.raw_json crypto_feature_raw_json,
  COALESCE(s.settled_at,m.settlement_ts,m.expiration_time,m.close_time) settlement_time,
  julianday(f.forecasted_at) forecast_jd,
  julianday(COALESCE(s.settled_at,m.settlement_ts,m.expiration_time,m.close_time)) settlement_jd,
  CAST(f.yes_probability AS REAL) crypto_v2,
  CAST(f.market_mid_probability AS REAL) market_implied,
  CAST(cf.price AS REAL) spot, CAST(cf.return_1h AS REAL) return_1h,
  CAST(cf.volatility_1h AS REAL) volatility_1h,
  CAST(cf.volatility_4h AS REAL) volatility_4h,
  CAST(cf.volatility_24h AS REAL) volatility_24h,
  CAST((SELECT cp.price_usd FROM crypto_prices cp
    WHERE cp.symbol=cf.symbol
      AND cp.observed_at<=COALESCE(s.settled_at,m.settlement_ts,m.expiration_time,m.close_time)
      AND cp.observed_at>=datetime(
        COALESCE(s.settled_at,m.settlement_ts,m.expiration_time,m.close_time),
        '-15 minutes'
      )
    ORDER BY cp.observed_at DESC,cp.id DESC LIMIT 1) AS REAL) terminal_spot,
  (SELECT cp.observed_at FROM crypto_prices cp
    WHERE cp.symbol=cf.symbol
      AND cp.observed_at<=COALESCE(s.settled_at,m.settlement_ts,m.expiration_time,m.close_time)
      AND cp.observed_at>=datetime(
        COALESCE(s.settled_at,m.settlement_ts,m.expiration_time,m.close_time),
        '-15 minutes'
      )
    ORDER BY cp.observed_at DESC,cp.id DESC LIMIT 1) terminal_observed_at,
  MAX(1.0, (
    julianday(COALESCE(m.settlement_ts,m.expiration_time,m.close_time))
    - julianday(f.forecasted_at)
  )*1440) horizon_minutes,
  CAST((SELECT ms.best_yes_ask FROM market_snapshots ms
    WHERE ms.ticker=f.ticker AND ms.captured_at<=f.forecasted_at
    ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1) AS REAL) yes_ask,
  CAST((SELECT ms.best_no_ask FROM market_snapshots ms
    WHERE ms.ticker=f.ticker AND ms.captured_at<=f.forecasted_at
    ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1) AS REAL) no_ask,
  CASE s.result WHEN 'yes' THEN 1.0 ELSE 0.0 END outcome
FROM ranked f JOIN markets m ON m.ticker=f.ticker JOIN settlements s ON s.ticker=f.ticker
JOIN crypto_features cf ON cf.id=CAST(json_extract(f.feature_json,'$.crypto_feature_id') AS INTEGER)
WHERE f.rn=1 AND f.market_mid_probability IS NOT NULL
ORDER BY f.forecasted_at,f.ticker
"""


if __name__ == "__main__":
    raise SystemExit(main())
