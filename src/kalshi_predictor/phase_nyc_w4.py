"""NYC-W4 no-write preview of certified observation input to weather_v2."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.utils.decimals import midpoint, to_decimal
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.temperature_contracts import parse_point_temperature_ticker


def write_nyc_w4_report(
    *,
    certification_path: Path,
    output_dir: Path,
    max_adjustment: Decimal,
    market_limit: int = 20,
    kalshi_client: httpx.Client | None = None,
) -> Path:
    if market_limit < 1:
        raise ValueError("market_limit must be positive")
    certification = json.loads(certification_path.read_text(encoding="utf-8"))
    if not certification.get("exact_ticker_certification"):
        raise ValueError("NYC-W4 requires exact-ticker NYC-W3B certification")
    certified_rows = [
        row for row in certification.get("rows", [])
        if row.get("metadata_passed") and row.get("alignment_passed")
    ][:market_limit]

    owned_client = kalshi_client is None
    active_client = kalshi_client or httpx.Client(
        base_url=PRODUCTION_PUBLIC_REST_URL, timeout=15.0,
    )
    rows: list[dict[str, Any]] = []
    try:
        for certified in certified_rows:
            ticker = str(certified.get("ticker") or "")
            contract = parse_point_temperature_ticker(ticker)
            if contract is None or contract.contract_kind != "ABOVE":
                rows.append({"ticker": ticker, "preview_passed": False,
                             "blocker": "UNSUPPORTED_CERTIFIED_CONTRACT"})
                continue
            response = active_client.get(f"/markets/{ticker}")
            response.raise_for_status()
            payload = response.json()
            market = payload.get("market", payload)
            baseline = _market_probability(market)
            observation = to_decimal(certified.get("observation_temperature_f"))
            if baseline is None or observation is None:
                rows.append({"ticker": ticker, "preview_passed": False,
                             "blocker": "BASELINE_OR_OBSERVATION_MISSING"})
                continue
            signal = _clamp_signal((observation - contract.raw_strike) / Decimal("20"))
            adjustment = signal * max_adjustment
            preview_probability = _clamp_probability(baseline + adjustment)
            rows.append({
                "ticker": ticker,
                "preview_passed": True,
                "blocker": None,
                "target_utc_time": certified.get("target_utc_time"),
                "observation_at": certified.get("observation_at"),
                "observation_offset_seconds": certified.get("offset_seconds"),
                "observation_temperature_f": str(observation),
                "evidence_source": certified.get("evidence_source"),
                "settlement_source": certified.get("settlement_source"),
                "baseline_probability_without_observation": str(baseline),
                "weather_v2_temperature_signal_preview": str(signal),
                "weather_v2_adjustment_preview": str(adjustment),
                "probability_with_observation_preview": str(preview_probability),
                "probability_change": str(preview_probability - baseline),
                "runtime_weather_v2_changed": False,
            })
    finally:
        if owned_client:
            active_client.close()

    report = {
        "phase": "NYC-W4",
        "generated_at": utc_now().isoformat(),
        "mode": "NON_SETTLEMENT_OBSERVATION_FEATURE_INTEGRATION_PREVIEW",
        "certification_path": str(certification_path),
        "database_writes": 0,
        "execution_enabled": False,
        "runtime_weather_v2_changed": False,
        "thresholds_changed": False,
        "max_adjustment_unchanged": str(max_adjustment),
        "evidence_policy": (
            "NOAA KNYC observations are non-settlement evidence and are never represented "
            "as The Weather Company settlement truth."
        ),
        "rows": rows,
        "summary": {
            "certified_rows_available": len(certified_rows),
            "rows_previewed": sum(bool(row.get("preview_passed")) for row in rows),
            "rows_blocked": sum(not bool(row.get("preview_passed")) for row in rows),
            "probability_increased": sum(
                (to_decimal(row.get("probability_change")) or Decimal("0")) > 0
                for row in rows
            ),
            "probability_decreased": sum(
                (to_decimal(row.get("probability_change")) or Decimal("0")) < 0
                for row in rows
            ),
            "probability_unchanged": sum(
                row.get("preview_passed")
                and (to_decimal(row.get("probability_change")) or Decimal("0")) == 0
                for row in rows
            ),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w4_observation_feature_integration_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _market_probability(market: dict[str, Any]) -> Decimal | None:
    yes_bid = to_decimal(market.get("yes_bid_dollars"))
    yes_ask = to_decimal(market.get("yes_ask_dollars"))
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(market.get("last_price_dollars"))


def _clamp_signal(value: Decimal) -> Decimal:
    return max(Decimal("-1"), min(Decimal("1"), value))


def _clamp_probability(value: Decimal) -> Decimal:
    return max(Decimal("0.01"), min(Decimal("0.99"), value))
