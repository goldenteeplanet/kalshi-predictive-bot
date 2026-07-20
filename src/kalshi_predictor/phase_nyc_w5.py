"""NYC-W5 multi-window shadow calibration census."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

MIN_CERTIFIED_WINDOWS = 3
MIN_SETTLED_WINDOWS = 3
MAX_MEAN_ABSOLUTE_DIVERGENCE_F = Decimal("2.0")
MAX_SINGLE_WINDOW_DIVERGENCE_F = Decimal("3.0")


def write_nyc_w5_report(
    *,
    reports_dir: Path,
    output_dir: Path,
    kalshi_client: httpx.Client | None = None,
) -> Path:
    certifications = _load_reports(
        reports_dir, "phase_nyc_w3b*/nyc_w3_live_alignment_preview.json"
    )
    previews = _load_reports(
        reports_dir, "phase_nyc_w4*/nyc_w4_observation_feature_integration_preview.json"
    )
    windows: dict[str, dict[str, Any]] = {}
    for report in certifications:
        for row in report.get("rows", []):
            target = str(row.get("target_utc_time") or "")
            if not target:
                continue
            window = windows.setdefault(target, {"target_utc_time": target})
            window["certification_generated_at"] = report.get("generated_at")
            window["metadata_passed"] = bool(row.get("metadata_passed"))
            window["alignment_passed"] = bool(row.get("alignment_passed"))
            window["observation_at"] = row.get("observation_at")
            window["observation_temperature_f"] = row.get("observation_temperature_f")
            window["offset_seconds"] = row.get("offset_seconds")
            window.setdefault("tickers", []).append(str(row.get("ticker") or ""))

    changes: dict[str, list[Decimal]] = defaultdict(list)
    for report in previews:
        for row in report.get("rows", []):
            target = str(row.get("target_utc_time") or "")
            change = to_decimal(row.get("probability_change"))
            if target and change is not None and row.get("preview_passed"):
                changes[target].append(change)

    owned_client = kalshi_client is None
    active_client = kalshi_client or httpx.Client(
        base_url=PRODUCTION_PUBLIC_REST_URL, timeout=15.0,
    )
    try:
        for target, window in windows.items():
            values = changes.get(target, [])
            window["preview_rows"] = len(values)
            window["minimum_probability_change"] = str(min(values)) if values else None
            window["maximum_probability_change"] = str(max(values)) if values else None
            window["mean_probability_change"] = (
                str(sum(values, Decimal("0")) / len(values)) if values else None
            )
            tickers = [ticker for ticker in window.get("tickers", []) if ticker]
            settlement_value = None
            if tickers:
                response = active_client.get(f"/markets/{tickers[0]}")
                if response.status_code == 200:
                    payload = response.json()
                    market = payload.get("market", payload)
                    settlement_value = to_decimal(market.get("expiration_value"))
                    window["market_status"] = market.get("status")
                    window["market_result"] = market.get("result")
            observation = to_decimal(window.get("observation_temperature_f"))
            divergence = (
                abs(observation - settlement_value)
                if observation is not None and settlement_value is not None else None
            )
            window["settlement_temperature_f"] = (
                str(settlement_value) if settlement_value is not None else None
            )
            window["absolute_observation_settlement_divergence_f"] = (
                str(divergence) if divergence is not None else None
            )
    finally:
        if owned_client:
            active_client.close()

    ordered = [windows[key] for key in sorted(windows)]
    certified = [
        row for row in ordered if row.get("metadata_passed") and row.get("alignment_passed")
    ]
    divergences = [
        to_decimal(row.get("absolute_observation_settlement_divergence_f"))
        for row in certified
    ]
    settled_divergences = [value for value in divergences if value is not None]
    mean_divergence = (
        sum(settled_divergences, Decimal("0")) / len(settled_divergences)
        if settled_divergences else None
    )
    gates = {
        "minimum_certified_windows": len(certified) >= MIN_CERTIFIED_WINDOWS,
        "minimum_settled_windows": len(settled_divergences) >= MIN_SETTLED_WINDOWS,
        "mean_divergence_within_calibration_limit": (
            mean_divergence is not None
            and mean_divergence <= MAX_MEAN_ABSOLUTE_DIVERGENCE_F
        ),
        "single_window_divergence_within_calibration_limit": (
            bool(settled_divergences)
            and max(settled_divergences) <= MAX_SINGLE_WINDOW_DIVERGENCE_F
        ),
    }
    runtime_activation_ready = all(gates.values())
    report = {
        "phase": "NYC-W5",
        "generated_at": utc_now().isoformat(),
        "mode": "MULTI_WINDOW_SHADOW_CALIBRATION_READ_ONLY",
        "database_writes": 0,
        "execution_enabled": False,
        "runtime_weather_v2_changed": False,
        "trading_thresholds_changed": False,
        "calibration_requirements": {
            "minimum_certified_windows": MIN_CERTIFIED_WINDOWS,
            "minimum_settled_windows": MIN_SETTLED_WINDOWS,
            "maximum_mean_absolute_divergence_f": str(MAX_MEAN_ABSOLUTE_DIVERGENCE_F),
            "maximum_single_window_divergence_f": str(MAX_SINGLE_WINDOW_DIVERGENCE_F),
        },
        "windows": ordered,
        "summary": {
            "distinct_windows": len(ordered),
            "certified_windows": len(certified),
            "settled_windows": len(settled_divergences),
            "mean_absolute_divergence_f": (
                str(mean_divergence) if mean_divergence is not None else None
            ),
            "maximum_absolute_divergence_f": (
                str(max(settled_divergences)) if settled_divergences else None
            ),
            "gates": gates,
            "runtime_activation_ready": runtime_activation_ready,
            "status": (
                "READY_FOR_RUNTIME_REVIEW"
                if runtime_activation_ready
                else "COLLECTING_WINDOWS"
            ),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w5_multi_window_shadow_calibration.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _load_reports(reports_dir: Path, pattern: str) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(reports_dir.glob(pattern)):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return reports
