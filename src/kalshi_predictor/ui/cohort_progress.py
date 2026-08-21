"""Read-only crypto cohort dashboard adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_STATUS_PATH = Path("reports/crypto_event_vectors/liquidity_coverage_status.json")
DEFAULT_RESULT_PATH = Path("reports/crypto_event_vectors/multiclass_interval_scoring.json")
DEFAULT_STATE_PATH = Path("reports/crypto_event_vectors/cohort_gate_state.json")
DEFAULT_WINDOW_PATH = Path("reports/crypto_event_vectors/liquidity_window_diagnosis.json")
DEFAULT_COLLECTOR_PATH = Path("reports/crypto_event_vectors/status.json")
DEFAULT_ALIGNMENT_PATH = Path(
    "reports/crypto_event_vectors/forecast_polytope_alignment.json"
)
DEFAULT_TELEMETRY_PATH = Path(
    "reports/crypto_event_vectors/targeted_forecast_telemetry.json"
)


def load_cohort_progress(
    status_path: Path = DEFAULT_STATUS_PATH,
    result_path: Path = DEFAULT_RESULT_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    window_path: Path = DEFAULT_WINDOW_PATH,
    collector_path: Path = DEFAULT_COLLECTOR_PATH,
    alignment_path: Path = DEFAULT_ALIGNMENT_PATH,
    telemetry_path: Path = DEFAULT_TELEMETRY_PATH,
) -> dict[str, Any]:
    status = _read(status_path)
    collector = _read(collector_path)
    alignment = _read(alignment_path)
    target = int(status.get("minimum_settled_interval_cohort", 10))
    settled = int(status.get("settled_interval_eligible_events", 0))
    return {
        "available": bool(status),
        "settled": settled,
        "target": target,
        "remaining": max(0, target - settled),
        "progress_percent": min(100.0, 100.0 * settled / target) if target else 100.0,
        "measured": int(status.get("unique_events_measured", 0)),
        "interval_eligible": int(status.get("interval_eligible_events", 0)),
        "aligned_events": int(status.get("forecast_polytope_aligned_events", 0)),
        "alignment_policy": status.get(
            "alignment_policy", "MISSING_ALIGNMENT_MANIFEST_FAIL_CLOSED"
        ),
        "alignment_rejections": status.get("alignment_rejection_reason_counts", {}),
        "statistically_valid": bool(status.get("statistically_valid_cohort", False)),
        "comparison_mode": status.get("market_comparison_mode", "UNAVAILABLE"),
        "eta": status.get(
            "time_to_target_estimate",
            {
                "status": "UNAVAILABLE",
                "estimated_days": None,
                "estimated_completion_at": None,
                "confidence": "UNAVAILABLE",
            },
        ),
        "families": status.get("families", []),
        "liquidity_window": _read(window_path),
        "rerun_state": _read(state_path),
        "comparison": _comparison_summary(_read(result_path)),
        "targeted_forecast_telemetry": _rolling_telemetry(_read(telemetry_path))
        or _targeted_forecast_telemetry(collector, alignment),
    }


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _comparison_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": bool(payload),
        "decision": payload.get("decision", "NOT_YET_RUN"),
        "shadow_activation_permitted": bool(
            payload.get("shadow_activation_permitted", False)
        ),
        "gates": payload.get("interval_frozen_gate_comparison", {}),
    }


def _targeted_forecast_telemetry(
    collector: dict[str, Any], alignment: dict[str, Any]
) -> dict[str, Any]:
    refresh = collector.get("targeted_forecast_refresh", {})
    rows = refresh.get("rows", []) if isinstance(refresh, dict) else []
    rows = [row for row in rows if isinstance(row, dict)]
    targeted_events = {str(row.get("event_ticker")) for row in rows}
    audits = [
        row
        for row in alignment.get("alignment_audit_rows", [])
        if isinstance(row, dict)
    ]
    latest: dict[str, dict[str, Any]] = {}
    for row in audits:
        latest.setdefault(str(row.get("event_ticker")), row)

    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = str(row.get("family") or "UNKNOWN")
        item = families.setdefault(
            family,
            {
                "family": family,
                "attempted": 0,
                "forecasted": 0,
                "midpoint_probe_failures": 0,
                "one_sided_bound_uses": 0,
                "targeted_aligned": 0,
                "targeted_alignment_observations": 0,
                "latencies": [],
            },
        )
        item["attempted"] += 1
        item["forecasted"] += row.get("status") == "FORECASTED"
        item["midpoint_probe_failures"] += int(
            row.get("midpoint_probe_failures", 0)
        )
        item["one_sided_bound_uses"] += int(row.get("one_sided_bound_uses", 0))
        audit = latest.get(str(row.get("event_ticker")))
        if audit:
            item["targeted_alignment_observations"] += 1
            item["targeted_aligned"] += bool(audit.get("aligned"))
            lag = audit.get("forecast_capture_lag_seconds")
            if lag is not None:
                item["latencies"].append(float(lag))

    baselines: dict[str, list[bool]] = {}
    for event, row in latest.items():
        if event not in targeted_events:
            baselines.setdefault(str(row.get("family") or "UNKNOWN"), []).append(
                bool(row.get("aligned"))
            )

    family_rows = []
    for family, item in sorted(families.items()):
        targeted_n = item.pop("targeted_alignment_observations")
        targeted_rate = item["targeted_aligned"] / targeted_n if targeted_n else None
        baseline = baselines.get(family, [])
        baseline_rate = sum(baseline) / len(baseline) if baseline else None
        latencies = item.pop("latencies")
        family_rows.append(
            {
                **item,
                "success_rate": item["forecasted"] / item["attempted"],
                "mean_forecast_capture_lag_seconds": (
                    sum(latencies) / len(latencies) if latencies else None
                ),
                "maximum_forecast_capture_lag_seconds": max(latencies, default=None),
                "targeted_alignment_rate": targeted_rate,
                "historical_alignment_rate": baseline_rate,
                "aligned_yield_improvement_percentage_points": (
                    100.0 * (targeted_rate - baseline_rate)
                    if targeted_rate is not None and baseline_rate is not None
                    else None
                ),
            }
        )
    attempted = len(rows)
    forecasted = sum(row.get("status") == "FORECASTED" for row in rows)
    all_latencies = [
        float(latest[event]["forecast_capture_lag_seconds"])
        for event in targeted_events
        if event in latest and latest[event].get("forecast_capture_lag_seconds") is not None
    ]
    return {
        "available": bool(refresh),
        "policy": (
            refresh.get("policy", "UNAVAILABLE")
            if isinstance(refresh, dict)
            else "UNAVAILABLE"
        ),
        "attempted": attempted,
        "forecasted": forecasted,
        "success_rate": forecasted / attempted if attempted else None,
        "midpoint_probe_failures": sum(
            int(row.get("midpoint_probe_failures", 0)) for row in rows
        ),
        "one_sided_bound_uses": sum(
            int(row.get("one_sided_bound_uses", 0)) for row in rows
        ),
        "mean_forecast_capture_lag_seconds": (
            sum(all_latencies) / len(all_latencies) if all_latencies else None
        ),
        "families": family_rows,
    }


def _rolling_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return {}

    def adapt(row: dict[str, Any]) -> dict[str, Any]:
        forecast = row.get("forecast_success", {})
        targeted = row.get("targeted_alignment", {})
        historical = row.get("historical_alignment", {})
        improvement = row.get("aligned_yield_improvement")
        improvement_interval = row.get("aligned_yield_improvement_interval", {})
        return {
            "family": row.get("family"),
            "attempted": int(row.get("targeted_observations", 0)),
            "forecasted": int(forecast.get("successes", 0)),
            "success_rate": forecast.get("rate"),
            "success_interval": forecast,
            "midpoint_probe_failures": int(row.get("midpoint_probe_failures", 0)),
            "one_sided_bound_uses": int(row.get("one_sided_bound_uses", 0)),
            "immediate_captures_attempted": int(
                row.get("immediate_captures_attempted", 0)
            ),
            "immediate_captures_within_budget": int(
                row.get("immediate_captures_within_budget", 0)
            ),
            "latency_sample_size": int(row.get("latency_sample_size", 0)),
            "mean_forecast_capture_lag_seconds": row.get(
                "mean_forecast_capture_lag_seconds"
            ),
            "maximum_forecast_capture_lag_seconds": row.get(
                "maximum_forecast_capture_lag_seconds"
            ),
            "targeted_alignment_rate": targeted.get("rate"),
            "targeted_alignment_interval": targeted,
            "historical_alignment_rate": historical.get("rate"),
            "historical_alignment_interval": historical,
            "aligned_yield_improvement_percentage_points": (
                100.0 * float(improvement) if improvement is not None else None
            ),
            "aligned_yield_improvement_interval_percentage_points": {
                "lower": (
                    100.0 * float(improvement_interval["lower"])
                    if improvement_interval.get("lower") is not None
                    else None
                ),
                "upper": (
                    100.0 * float(improvement_interval["upper"])
                    if improvement_interval.get("upper") is not None
                    else None
                ),
            },
        }

    overall = adapt(summary)
    return {
        "available": True,
        "policy": payload.get("policy", "ROLLING_EVENT_LEVEL_TELEMETRY"),
        **{key: value for key, value in overall.items() if key != "family"},
        "families": [
            adapt(row)
            for row in summary.get("families", [])
            if isinstance(row, dict)
        ],
        "history_observations": len(payload.get("observations", [])),
        "generated_at": payload.get("generated_at"),
    }
