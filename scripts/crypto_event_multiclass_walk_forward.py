#!/usr/bin/env python3
"""Event-weighted multiclass walk-forward for mutually exclusive crypto buckets."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from kalshi_predictor.research.probability_polytope import polytope_information


def _walk_forward_module():
    path = Path(__file__).with_name("crypto_distribution_walk_forward.py")
    spec = importlib.util.spec_from_file_location("crypto_distribution_walk_forward", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WF = _walk_forward_module()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-train-fraction", type=float, default=0.60)
    parser.add_argument("--liquidity-policy", type=Path)
    parser.add_argument("--event-manifest-cache", type=Path)
    parser.add_argument("--alignment-manifest", type=Path)
    args = parser.parse_args()
    liquidity_policy = (
        json.loads(args.liquidity_policy.read_text(encoding="utf-8"))
        if args.liquidity_policy
        else {}
    )
    allow_point_market = liquidity_policy.get("market_comparison_mode") != (
        "GATED_BID_ASK_PROBABILITY_BOUNDS"
    )

    manifest_source = None
    if args.alignment_manifest and args.alignment_manifest.exists():
        alignment_payload = json.loads(
            args.alignment_manifest.read_text(encoding="utf-8")
        )
        alignment_rows = [
            row for row in alignment_payload.get("aligned_events", []) if row.get("aligned")
        ]
        forecast_ids = [int(row["forecast_id"]) for row in alignment_rows]
        raw_rows = WF._load_rows_for_forecast_ids(args.database, forecast_ids)
        audits = [WF._contract_audit(row) for row in raw_rows]
        prepared = [
            item
            for raw, audit in zip(raw_rows, audits, strict=True)
            if audit["eligible"] and (item := WF._prepare(raw, audit)) is not None
        ]
        representatives = {int(row["forecast_id"]): row for row in prepared}
        connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=120)
        connection.row_factory = sqlite3.Row
        event_rows = _load_aligned_events(connection, representatives, alignment_rows)
        connection.close()
        manifest_source = str(args.alignment_manifest)
    elif args.event_manifest_cache and args.event_manifest_cache.exists():
        cached = json.loads(args.event_manifest_cache.read_text(encoding="utf-8"))
        event_rows = cached.get("event_audit_rows", [])
        if not event_rows:
            parser.error("event manifest cache contains no event_audit_rows")
        manifest_source = str(args.event_manifest_cache)
    else:
        raw_rows = WF._load_rows(args.database)
        audits = [WF._contract_audit(row) for row in raw_rows]
        prepared = [
            item
            for raw, audit in zip(raw_rows, audits, strict=True)
            if audit["eligible"] and (item := WF._prepare(raw, audit)) is not None
        ]
        representatives = _event_representatives(prepared)
        connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=120)
        connection.row_factory = sqlite3.Row
        event_rows = _load_events(connection, representatives)
        connection.close()
    complete = [event for event in event_rows if event["eligible"]]
    cut = max(1, int(len(complete) * args.initial_train_fraction))
    predictions = []
    for index, event in enumerate(complete[cut:], start=cut):
        history = [
            prior["representative"]
            for prior in complete[:index]
            if prior["representative"]["settlement_jd"]
            <= event["representative"]["forecast_jd"]
        ]
        calibration, level = WF._calibration_history(history, event["representative"])
        bias, drift = WF._regularized_location_bias(calibration)
        scale, volatility = WF._regularized_volatility_scale(calibration, bias)
        df, tail = WF._regularized_student_df(calibration, bias)
        mixture = WF._regularized_gaussian_mixture(calibration, bias)
        probabilities = {
            "gaussian": _normalized_bucket_probabilities(
                event,
                lambda row, scale=scale, bias=bias: WF._distribution_probability(
                    row, scale, bias
                ),
            ),
            "student_t": _normalized_bucket_probabilities(
                event,
                lambda row, scale=scale, df=df, bias=bias: WF._student_t_probability(
                    row, scale, df, bias
                ),
            ),
            "gaussian_mixture": _normalized_bucket_probabilities(
                event,
                lambda row, mixture=mixture, bias=bias: (
                    WF._gaussian_mixture_probability(row, mixture, bias)
                ),
            ),
            "market_implied": (
                _normalize([bucket["market_mid"] for bucket in event["buckets"]])
                if allow_point_market and event["market_quote_complete"]
                else None
            ),
        }
        market_bounds = _market_probability_bounds(event)
        predictions.append(
            {
                "event_ticker": event["event_ticker"],
                "symbol": event["representative"]["symbol"],
                "horizon_band": event["representative"]["horizon_band"],
                "bucket_count": len(event["buckets"]),
                "true_bucket_index": event["true_bucket_index"],
                "calibration_event_n": len(calibration),
                "calibration_level": level,
                "location_bias_z": bias,
                "drift_calibration": drift,
                "volatility_calibration": volatility,
                "student_t_df": df,
                "tail_calibration": tail,
                "mixture_parameters": mixture,
                "probabilities": probabilities,
                "market_probability_bounds": market_bounds,
                "scores": {
                    model: _multiclass_scores(values, event["true_bucket_index"])
                    for model, values in probabilities.items()
                    if values is not None
                },
            }
        )

    metrics = {
        model: _aggregate_scores(predictions, model)
        for model in ("gaussian", "student_t", "gaussian_mixture", "market_implied")
    }
    interval_gates = {
        model: _interval_frozen_gate(predictions, model)
        for model in ("gaussian", "student_t", "gaussian_mixture")
    }
    shadow_permitted = any(row["passed"] for row in interval_gates.values())
    payload = {
        "policy": "EVENT_WEIGHTED_EXPANDING_WINDOW_NO_FUTURE_SETTLEMENTS",
        "events_reviewed": len(event_rows),
        "complete_events": len(complete),
        "excluded_events": len(event_rows) - len(complete),
        "exclusion_reason_counts": _reason_counts(event_rows),
        "warning_counts": _warning_counts(event_rows),
        "initial_train_events": cut,
        "validation_events": len(predictions),
        "effective_sample_unit": "UNIQUE_EVENT_TICKER",
        "event_manifest_policy": "INDEXED_MATERIALIZED_AUDITED_EVENT_VECTORS",
        "event_manifest_cache_source": manifest_source,
        "forecast_capture_alignment_required": bool(args.alignment_manifest),
        "market_comparison_mode": (
            "POINT_PROBABILITIES"
            if allow_point_market
            else "GATED_BID_ASK_PROBABILITY_BOUNDS"
        ),
        "missing_market_side_policy": "BOUND_TO_ZERO_OR_ONE_NEVER_IMPUTE_A_POINT",
        "metrics": metrics,
        "frozen_gate_comparison": {
            model: _frozen_gate(metrics[model], metrics["market_implied"], len(predictions))
            for model in ("gaussian", "student_t", "gaussian_mixture")
        },
        "interval_frozen_gate_comparison": interval_gates,
        "shadow_activation_permitted": shadow_permitted,
        "continuous_shadow_enabled": False,
        "decision": (
            "ELIGIBLE_FOR_SEPARATE_SHADOW_ACTIVATION_REVIEW"
            if shadow_permitted
            else "DO_NOT_ENABLE_CONTINUOUS_SHADOW"
        ),
        "event_audit_rows": event_rows,
        "rows": predictions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    summary = {
        key: value
        for key, value in payload.items()
        if key not in {"rows", "event_audit_rows"}
    }
    print(json.dumps(summary))
    return 0


def _event_representatives(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_event: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = row["event_ticker"]
        if event and (event not in by_event or row["forecast_jd"] < by_event[event]["forecast_jd"]):
            by_event[event] = row
    return sorted(by_event.values(), key=lambda row: (row["forecast_jd"], row["event_ticker"]))


def _load_aligned_events(
    connection: sqlite3.Connection,
    representatives: dict[int, dict[str, Any]],
    alignment_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events = []
    for alignment in alignment_rows:
        representative = representatives.get(int(alignment["forecast_id"]))
        if representative is None:
            continue
        captured_bounds = {
            str(row["ticker"]): row for row in alignment.get("bounds", [])
        }
        if not captured_bounds:
            continue
        placeholders = ",".join("?" for _ in captured_bounds)
        market_rows = list(
            connection.execute(
                f"""SELECT m.ticker,m.raw_json,COALESCE(s.result,m.result) result
                    FROM markets m LEFT JOIN settlements s ON s.ticker=m.ticker
                    WHERE m.ticker IN ({placeholders})""",
                list(captured_bounds),
            )
        )
        buckets = []
        reasons = []
        for market in market_rows:
            interval = _interval(json.loads(market["raw_json"] or "{}"))
            if interval is None:
                reasons.append("UNPARSEABLE_BUCKET")
                continue
            captured = captured_bounds[market["ticker"]]
            has_bid = bool(
                captured.get("has_bid", float(captured.get("lower", 0.0)) > 0.0)
            )
            has_ask = bool(
                captured.get("has_ask", float(captured.get("upper", 1.0)) < 1.0)
            )
            buckets.append(
                {
                    "ticker": market["ticker"],
                    "result": market["result"],
                    "market_mid": None,
                    "market_bid": float(captured["lower"]) if has_bid else None,
                    "market_ask": float(captured["upper"]) if has_ask else None,
                    "model_row": {**representative, **interval},
                    **interval,
                }
            )
        buckets.sort(
            key=lambda bucket: (
                float("-inf") if bucket["lower"] is None else bucket["lower"]
            )
        )
        yes_indexes = [
            index for index, bucket in enumerate(buckets) if bucket["result"] == "yes"
        ]
        if len(market_rows) != len(captured_bounds):
            reasons.append("CAPTURED_SIBLING_MARKET_MISSING")
        if any(bucket["result"] not in {"yes", "no"} for bucket in buckets):
            reasons.append("SIBLING_OUTCOME_NOT_SETTLED")
        if len(yes_indexes) != 1:
            reasons.append("TRUE_BUCKET_NOT_UNIQUE")
        reasons.extend(_coverage_reasons(buckets))
        events.append(
            {
                "event_ticker": alignment["event_ticker"],
                "representative": representative,
                "eligible": not reasons,
                "exclusion_reasons": sorted(set(reasons)),
                "warnings": ["POINT_MARKET_VECTOR_DISABLED_USE_CAPTURED_POLYTOPE"],
                "market_quote_complete": False,
                "bucket_count": len(buckets),
                "true_bucket_index": yes_indexes[0] if len(yes_indexes) == 1 else None,
                "buckets": buckets,
                "alignment": alignment,
            }
        )
    return sorted(
        events, key=lambda row: row["representative"]["forecast_jd"]
    )


def _load_events(
    connection: sqlite3.Connection, representatives: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    connection.execute(
        "CREATE TEMP TABLE requested_events (event_ticker TEXT PRIMARY KEY, forecasted_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO requested_events VALUES (?,?)",
        [(row["event_ticker"], row["forecasted_at"]) for row in representatives],
    )
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute(_SIBLING_SQL):
        grouped.setdefault(row["requested_event_ticker"], []).append(row)
    return [
        _build_event(row, grouped.get(row["event_ticker"], []))
        for row in representatives
    ]


def _build_event(
    representative: dict[str, Any], rows: list[sqlite3.Row]
) -> dict[str, Any]:
    buckets = []
    reasons = []
    terminal_values = set()
    event_terminal = representative.get("terminal_spot")
    for row in rows:
        raw = json.loads(row["raw_json"] or "{}")
        interval = _interval(raw)
        terminal = WF._number(raw.get("expiration_value")) or event_terminal
        if terminal is not None:
            terminal_values.add(terminal)
        if interval is None:
            reasons.append("UNPARSEABLE_BUCKET")
            continue
        result = row["result"]
        if result not in {"yes", "no"} and terminal is not None:
            result = "yes" if _contains(interval, terminal) else "no"
        if result not in {"yes", "no"}:
            reasons.append("SIBLING_OUTCOME_NOT_RECONSTRUCTABLE")
        bucket_row = {**representative, **interval}
        buckets.append(
            {
                "ticker": row["ticker"],
                "result": result,
                "market_mid": (
                    float(row["market_mid"]) if row["market_mid"] is not None else None
                ),
                "market_bid": (
                    float(row["market_bid"]) if row["market_bid"] is not None else None
                ),
                "market_ask": (
                    float(row["market_ask"]) if row["market_ask"] is not None else None
                ),
                "model_row": bucket_row,
                **interval,
            }
        )
    buckets.sort(key=lambda bucket: (float("-inf") if bucket["lower"] is None else bucket["lower"]))
    reasons.extend(_coverage_reasons(buckets))
    if len(terminal_values) != 1:
        reasons.append("TERMINAL_REFERENCE_NOT_UNIQUE")
    yes_indexes = [index for index, bucket in enumerate(buckets) if bucket["result"] == "yes"]
    if len(yes_indexes) != 1:
        reasons.append("TRUE_BUCKET_NOT_UNIQUE")
    return {
        "event_ticker": representative["event_ticker"],
        "representative": representative,
        "eligible": not reasons,
        "exclusion_reasons": sorted(set(reasons)),
        "warnings": (
            ["POINT_IN_TIME_QUOTE_VECTOR_INCOMPLETE"]
            if any(bucket["market_mid"] is None for bucket in buckets)
            else []
        ),
        "market_quote_complete": all(
            bucket["market_mid"] is not None for bucket in buckets
        ),
        "bucket_count": len(buckets),
        "true_bucket_index": yes_indexes[0] if len(yes_indexes) == 1 else None,
        "buckets": buckets,
    }


def _interval(raw: dict[str, Any]) -> dict[str, Any] | None:
    strike_type = str(raw.get("strike_type") or "").lower()
    floor = WF._number(raw.get("floor_strike"))
    cap = WF._number(raw.get("cap_strike"))
    if strike_type == "between" and floor is not None and cap is not None:
        return {"comparator": "RANGE", "lower": floor, "upper": cap, "threshold": floor}
    if strike_type == "less" and cap is not None:
        return {"comparator": "BELOW", "lower": None, "upper": cap, "threshold": cap}
    if strike_type == "greater" and floor is not None:
        return {"comparator": "ABOVE", "lower": floor, "upper": None, "threshold": floor}
    return None


def _coverage_reasons(buckets: list[dict[str, Any]]) -> list[str]:
    if not buckets:
        return ["NO_BUCKETS"]
    reasons = []
    if buckets[0]["lower"] is not None:
        reasons.append("LOWER_TAIL_MISSING")
    if buckets[-1]["upper"] is not None:
        reasons.append("UPPER_TAIL_MISSING")
    for left, right in zip(buckets, buckets[1:], strict=False):
        if left["upper"] is None or right["lower"] is None:
            continue
        if abs(left["upper"] - right["lower"]) > 0.011:
            reasons.append("BUCKET_GAP_OR_OVERLAP")
    return reasons


def _contains(interval: dict[str, Any], value: float) -> bool:
    if interval["comparator"] == "RANGE":
        return interval["lower"] <= value <= interval["upper"]
    if interval["comparator"] == "BELOW":
        return value < interval["threshold"]
    return value > interval["threshold"]


def _normalized_bucket_probabilities(event: dict[str, Any], scorer: Any) -> list[float]:
    return _normalize([scorer(bucket["model_row"]) for bucket in event["buckets"]])


def _market_probability_bounds(event: dict[str, Any]) -> dict[str, Any]:
    bounds = []
    two_sided = 0
    for bucket in event["buckets"]:
        bid = bucket.get("market_bid")
        ask = bucket.get("market_ask")
        if bid is not None and ask is not None:
            two_sided += 1
        lower = max(0.0, min(1.0, bid if bid is not None else 0.0))
        upper = max(0.0, min(1.0, ask if ask is not None else 1.0))
        bounds.append(
            {
                "ticker": bucket["ticker"],
                "lower": lower,
                "upper": upper,
            }
        )
    coverage = two_sided / len(bounds) if bounds else 0.0
    sum_lower = sum(row["lower"] for row in bounds)
    sum_upper = sum(row["upper"] for row in bounds)
    feasible = (
        all(row["lower"] <= row["upper"] for row in bounds)
        and sum_lower <= 1.0 + 1e-9
        and sum_upper >= 1.0 - 1e-9
    )
    score_bounds = (
        _interval_score_bounds(bounds, event["true_bucket_index"]) if feasible else None
    )
    information = polytope_information(bounds)
    return {
        "bounds": bounds,
        "two_sided_coverage": coverage,
        "simplex_feasible": feasible,
        "polytope_information": information,
        "gate_policy": "TIGHTENED_WIDTH_AND_SIMPLEX_VOLUME_NOT_TWO_SIDED_COVERAGE",
        "gate_passed": information["gate_passed"],
        "score_bounds": score_bounds,
        "missing_side_policy": "ZERO_OR_ONE_BOUND_NOT_POINT_IMPUTATION",
    }


def _interval_score_bounds(
    bounds: list[dict[str, Any]], true_index: int
) -> dict[str, float]:
    lower = [float(row["lower"]) for row in bounds]
    upper = [float(row["upper"]) for row in bounds]
    tight_lower = []
    tight_upper = []
    for index in range(len(bounds)):
        other_upper = sum(upper) - upper[index]
        other_lower = sum(lower) - lower[index]
        tight_lower.append(max(lower[index], 1.0 - other_upper))
        tight_upper.append(min(upper[index], 1.0 - other_lower))
    target = [1.0 if index == true_index else 0.0 for index in range(len(bounds))]
    optimistic_point = _bounded_simplex_projection(target, tight_lower, tight_upper)
    optimistic_brier = sum(
        (value - target[index]) ** 2 for index, value in enumerate(optimistic_point)
    )
    # Coordinate extrema need not be jointly attainable. Summing their worst
    # losses is therefore a conservative valid upper bound, capped by the
    # multiclass Brier maximum of two.
    pessimistic_brier = min(
        2.0,
        sum(
            max(
                (tight_lower[index] - target[index]) ** 2,
                (tight_upper[index] - target[index]) ** 2,
            )
            for index in range(len(bounds))
        ),
    )
    true_lower = max(tight_lower[true_index], 0.001)
    true_upper = max(tight_upper[true_index], 0.001)
    return {
        "optimistic_brier": optimistic_brier,
        "pessimistic_brier": pessimistic_brier,
        "optimistic_log_loss": -math.log(true_upper),
        "pessimistic_log_loss": -math.log(true_lower),
    }


def _bounded_simplex_projection(
    target: list[float], lower: list[float], upper: list[float]
) -> list[float]:
    low_lambda = min(t - u for t, u in zip(target, upper, strict=True)) - 1.0
    high_lambda = max(t - lo for t, lo in zip(target, lower, strict=True)) + 1.0
    for _ in range(100):
        midpoint = (low_lambda + high_lambda) / 2.0
        values = [
            min(u, max(lo, t - midpoint))
            for t, lo, u in zip(target, lower, upper, strict=True)
        ]
        if sum(values) > 1.0:
            low_lambda = midpoint
        else:
            high_lambda = midpoint
    midpoint = (low_lambda + high_lambda) / 2.0
    return [
        min(u, max(lo, t - midpoint))
        for t, lo, u in zip(target, lower, upper, strict=True)
    ]


def _normalize(values: list[float]) -> list[float]:
    total = sum(max(0.0, value) for value in values)
    if total <= 0:
        return [1.0 / len(values)] * len(values)
    return [max(0.0, value) / total for value in values]


def _multiclass_scores(probabilities: list[float], true_index: int) -> dict[str, float]:
    brier = sum(
        (probability - (1.0 if index == true_index else 0.0)) ** 2
        for index, probability in enumerate(probabilities)
    )
    return {"brier": brier, "log_loss": -math.log(max(probabilities[true_index], 0.001))}


def _aggregate_scores(rows: list[dict[str, Any]], model: str) -> dict[str, float | None]:
    scored = [row for row in rows if model in row["scores"]]
    if not scored:
        return {"multiclass_brier": None, "log_loss": None}
    return {
        "multiclass_brier": sum(row["scores"][model]["brier"] for row in scored)
        / len(scored),
        "log_loss": sum(row["scores"][model]["log_loss"] for row in scored)
        / len(scored),
        "events_scored": len(scored),
    }


def _frozen_gate(model: dict[str, Any], market: dict[str, Any], n: int) -> dict[str, Any]:
    advantage = (
        market["multiclass_brier"] - model["multiclass_brier"]
        if model["multiclass_brier"] is not None and market["multiclass_brier"] is not None
        else None
    )
    checks = {
        "cohort_n": n >= WF.MIN_POLICY_COHORT_N,
        "market_advantage": advantage is not None and advantage >= WF.MIN_MARKET_BRIER_ADVANTAGE,
        "separation": advantage is not None and abs(advantage) >= WF.MIN_SCALE_BRIER_GAP,
    }
    return {"passed": all(checks.values()), "checks": checks, "market_brier_advantage": advantage}


def _interval_frozen_gate(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if row["market_probability_bounds"]["gate_passed"]
        and row["market_probability_bounds"]["score_bounds"] is not None
        and model in row["scores"]
    ]
    if not eligible:
        return {
            "passed": False,
            "eligible_events": 0,
            "checks": {
                "cohort_n": False,
                "pessimistic_brier_advantage": False,
                "pessimistic_log_loss_advantage": False,
            },
            "pessimistic_model_brier_advantage": None,
            "pessimistic_model_log_loss_advantage": None,
        }
    count = len(eligible)
    model_brier = sum(row["scores"][model]["brier"] for row in eligible) / count
    model_log = sum(row["scores"][model]["log_loss"] for row in eligible) / count
    # Strict/model-pessimistic comparison: the model must beat the market even
    # when the market is assigned its best feasible score inside the polytope.
    market_best_brier = sum(
        row["market_probability_bounds"]["score_bounds"]["optimistic_brier"]
        for row in eligible
    ) / count
    market_best_log = sum(
        row["market_probability_bounds"]["score_bounds"]["optimistic_log_loss"]
        for row in eligible
    ) / count
    brier_advantage = market_best_brier - model_brier
    log_advantage = market_best_log - model_log
    checks = {
        "cohort_n": count >= WF.MIN_POLICY_COHORT_N,
        "pessimistic_brier_advantage": brier_advantage
        >= WF.MIN_MARKET_BRIER_ADVANTAGE,
        "pessimistic_log_loss_advantage": log_advantage > 0.0,
    }
    return {
        "passed": all(checks.values()),
        "eligible_events": count,
        "checks": checks,
        "model_brier": model_brier,
        "model_log_loss": model_log,
        "market_optimistic_brier": market_best_brier,
        "market_optimistic_log_loss": market_best_log,
        "pessimistic_model_brier_advantage": brier_advantage,
        "pessimistic_model_log_loss_advantage": log_advantage,
    }


def _reason_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        for reason in event["exclusion_reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _warning_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        for warning in event["warnings"]:
            counts[warning] = counts.get(warning, 0) + 1
    return dict(sorted(counts.items()))


_SIBLING_SQL = """
SELECT m.ticker,m.raw_json,COALESCE(s.result,m.result) result,
  e.event_ticker requested_event_ticker,
  CAST((SELECT (CAST(ms.best_yes_bid AS REAL)+CAST(ms.best_yes_ask AS REAL))/2.0
    FROM market_snapshots ms WHERE ms.ticker=m.ticker AND ms.captured_at<=e.forecasted_at
    AND ms.best_yes_bid IS NOT NULL AND ms.best_yes_ask IS NOT NULL
    ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1) AS REAL) market_mid,
  CAST((SELECT ms.best_yes_bid FROM market_snapshots ms
    WHERE ms.ticker=m.ticker AND ms.captured_at<=e.forecasted_at
    ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1) AS REAL) market_bid,
  CAST((SELECT ms.best_yes_ask FROM market_snapshots ms
    WHERE ms.ticker=m.ticker AND ms.captured_at<=e.forecasted_at
    ORDER BY ms.captured_at DESC,ms.id DESC LIMIT 1) AS REAL) market_ask
FROM requested_events e JOIN markets m ON m.event_ticker=e.event_ticker
LEFT JOIN settlements s ON s.ticker=m.ticker
ORDER BY m.ticker
"""


if __name__ == "__main__":
    raise SystemExit(main())
