"""NYC-W3 read-only live point-temperature alignment preview."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from kalshi_predictor.phase_gh1h import PRODUCTION_PUBLIC_REST_URL
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.station_observations import (
    align_point_observation,
    fetch_nws_station_observations,
)
from kalshi_predictor.weather.temperature_contracts import (
    MarketMetadataValidation,
    parse_point_temperature_ticker,
    validate_point_temperature_market,
)


def write_nyc_w3_report(
    *,
    output_dir: Path,
    user_agent: str,
    market_limit: int = 20,
    tolerance_minutes: int = 15,
    exact_tickers: list[str] | None = None,
    kalshi_client: httpx.Client | None = None,
    nws_client: httpx.Client | None = None,
) -> Path:
    if market_limit < 1:
        raise ValueError("market_limit must be positive")
    if tolerance_minutes < 0:
        raise ValueError("tolerance_minutes must be non-negative")
    owned_kalshi = kalshi_client is None
    active_kalshi = kalshi_client or httpx.Client(
        base_url=PRODUCTION_PUBLIC_REST_URL, timeout=15.0,
    )
    try:
        if exact_tickers:
            markets = []
            for ticker in exact_tickers[:market_limit]:
                response = active_kalshi.get(f"/markets/{ticker}")
                response.raise_for_status()
                payload = response.json()
                markets.append(payload.get("market", payload))
        else:
            response = active_kalshi.get(
                "/markets",
                params={
                    "limit": market_limit, "status": "open",
                    "series_ticker": "KXTEMPNYCH",
                },
            )
            response.raise_for_status()
            markets = response.json().get("markets", [])
    finally:
        if owned_kalshi:
            active_kalshi.close()

    validations: list[tuple[dict[str, Any], MarketMetadataValidation]] = []
    rows: list[dict[str, Any]] = []
    for market in markets[:market_limit]:
        ticker = str(market.get("ticker") or "")
        contract = parse_point_temperature_ticker(ticker)
        if contract is None:
            rows.append({
                "ticker": ticker, "metadata_passed": False,
                "metadata_blockers": ["TICKER_PARSE_FAILED"], "alignment_passed": False,
                "alignment_blocker": "MARKET_METADATA_NOT_VERIFIED",
            })
            continue
        validations.append((
            market,
            validate_point_temperature_market(
                contract, market, series_scope="KXTEMPNYCH"
            ),
        ))

    observations_by_date: dict[date, list[Any]] = {}
    fetch_errors: dict[str, str] = {}
    for _, validation in validations:
        if not validation.passed:
            continue
        contract = validation.contract
        target_date = contract.target_local_time.date()
        if target_date in observations_by_date:
            continue
        try:
            dates = {target_date}
            local_start = contract.target_local_time - timedelta(minutes=tolerance_minutes)
            local_end = contract.target_local_time + timedelta(minutes=tolerance_minutes)
            dates.update({local_start.date(), local_end.date()})
            observations_by_date[target_date] = []
            for scoped_date in sorted(dates):
                observations_by_date[target_date].extend(fetch_nws_station_observations(
                    station_id=contract.station_id,
                    target_local_date=scoped_date,
                    timezone=contract.timezone,
                    user_agent=user_agent,
                    client=nws_client,
                ))
        except Exception as exc:
            observations_by_date[target_date] = []
            fetch_errors[target_date.isoformat()] = str(exc)

    for market, validation in validations:
        contract = validation.contract
        observations = observations_by_date.get(contract.target_local_time.date(), [])
        alignment = align_point_observation(
            validation, observations, tolerance_minutes=tolerance_minutes,
        )
        rows.append({
            "ticker": contract.ticker,
            "target_local_time": contract.target_local_time.isoformat(),
            "target_utc_time": contract.target_utc_time.isoformat(),
            "station_id": contract.station_id,
            "settlement_source": contract.settlement_source,
            "evidence_source": "noaa_nws_observation_non_settlement_evidence",
            "metadata_passed": validation.passed,
            "metadata_blockers": list(validation.blockers),
            "alignment_passed": alignment.passed,
            "alignment_blocker": alignment.blocker,
            "observation_count_for_local_date": len(observations),
            "observation_at": (
                alignment.observation.observed_at.isoformat() if alignment.observation else None
            ),
            "observation_temperature_f": (
                str(alignment.observation.temperature_f) if alignment.observation else None
            ),
            "offset_seconds": alignment.offset_seconds,
            "market_status": market.get("status"),
        })

    metadata_blockers = Counter(
        blocker for row in rows for blocker in row.get("metadata_blockers", [])
    )
    alignment_blockers = Counter(
        str(row["alignment_blocker"])
        for row in rows if row.get("alignment_blocker") is not None
    )
    offsets = [int(row["offset_seconds"]) for row in rows if row.get("offset_seconds") is not None]
    report = {
        "phase": "NYC-W3",
        "generated_at": utc_now().isoformat(),
        "mode": "READ_ONLY_PUBLIC_ALIGNMENT_PREVIEW",
        "database_writes": 0,
        "execution_enabled": False,
        "weather_v2_connected": False,
        "thresholds_changed": False,
        "series_ticker": "KXTEMPNYCH",
        "exact_ticker_certification": bool(exact_tickers),
        "station_id": "KNYC",
        "tolerance_minutes": tolerance_minutes,
        "rows": rows,
        "summary": {
            "markets_returned": len(markets),
            "markets_evaluated": len(rows),
            "metadata_passed": sum(bool(row.get("metadata_passed")) for row in rows),
            "metadata_failed": sum(not bool(row.get("metadata_passed")) for row in rows),
            "alignment_passed": sum(bool(row.get("alignment_passed")) for row in rows),
            "alignment_failed": sum(not bool(row.get("alignment_passed")) for row in rows),
            "metadata_blocker_counts": dict(sorted(metadata_blockers.items())),
            "alignment_blocker_counts": dict(sorted(alignment_blockers.items())),
            "minimum_offset_seconds": min(offsets) if offsets else None,
            "maximum_offset_seconds": max(offsets) if offsets else None,
            "nws_fetch_errors": fetch_errors,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "nyc_w3_live_alignment_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
