#!/usr/bin/env python3
"""Build fail-closed CLIAUS monthly-rain evidence from NWS CLI and NOAA QPF."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import WeatherForecast, WeatherMarketLink, WeatherObservation
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.weather.monthly_rain import (
    calibrate_monthly_rain,
    parse_cli_month_to_date,
)
from kalshi_predictor.weather.repository import (
    insert_weather_features,
    insert_weather_forecast_if_missing,
)

CLI_URL = "https://forecast.weather.gov/product.php?site=EWX&issuedby=AUS&product=CLI&format=txt"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-calibration-samples", type=int, default=12)
    parser.add_argument("--historical-calibration", type=Path)
    args = parser.parse_args()
    with urllib.request.urlopen(CLI_URL, timeout=20) as response:
        cli_text = response.read().decode("utf-8", "replace")
    month_to_date = parse_cli_month_to_date(cli_text)
    engine = init_db()
    with get_session_factory(engine)() as session:
        qpf_rows = list(
            session.scalars(select(WeatherForecast).where(WeatherForecast.location_key == "austin"))
        )
        samples = _calibration_samples(session)
    historical = {}
    if args.historical_calibration and args.historical_calibration.exists():
        historical = json.loads(args.historical_calibration.read_text(encoding="utf-8"))
        samples.extend(
            (Decimal(row["predicted_total_inches"]), Decimal(row["actual_total_inches"]))
            for row in historical.get("samples", [])
        )
    calibration = calibrate_monthly_rain(samples, minimum_samples=args.minimum_calibration_samples)
    qpf_available = sum(row.precipitation_inches is not None for row in qpf_rows)
    blockers = []
    if month_to_date is None:
        blockers.append("CLIAUS_MONTH_TO_DATE_MISSING")
    amount_model = historical.get("current_remaining_amount_model") or {}
    monotonic_recalibration = historical.get("monotonic_recalibration") or {}
    calibrated_amount_available = (
        to_decimal(amount_model.get("expected_remaining_inches")) is not None and calibration.passed
    )
    if qpf_available == 0 and not calibrated_amount_available:
        blockers.append("NOAA_QUANTITATIVE_PRECIPITATION_MISSING")
    if not calibration.passed:
        blockers.append(calibration.blocker)
    predicted_total = (
        month_to_date + to_decimal(amount_model.get("expected_remaining_inches"))
        if month_to_date is not None
        and to_decimal(amount_model.get("expected_remaining_inches")) is not None
        else None
    )
    artifact_hash = hashlib.sha256(
        (json.dumps(historical, sort_keys=True) + cli_text).encode("utf-8")
    ).hexdigest()
    source_rows = 0
    feature_rows = 0
    if not blockers and predicted_total is not None:
        generated_at = datetime.now(UTC)
        with get_session_factory(engine)() as session:
            targets = list(
                session.scalars(
                    select(WeatherMarketLink.target_time)
                    .where(
                        WeatherMarketLink.location_key == "austin",
                        WeatherMarketLink.weather_metric == "RAIN",
                        WeatherMarketLink.target_time.is_not(None),
                        WeatherMarketLink.target_time > generated_at,
                    )
                    .distinct()
                )
            )
            for target in targets:
                _, inserted = insert_weather_forecast_if_missing(
                    session,
                    location_key="austin",
                    source="cliaus_monthly_calibrated_v1",
                    forecast_generated_at=generated_at,
                    forecast_time=target,
                    precipitation_inches=predicted_total,
                    raw_json={
                        "station_id": "CLIAUS",
                        "source_url": CLI_URL,
                        "artifact_sha256": artifact_hash,
                        "month_to_date_inches": str(month_to_date),
                        "predicted_total_inches": str(predicted_total),
                        "calibration_sample_count": calibration.sample_count,
                    },
                )
                source_rows += int(inserted)
                insert_weather_features(
                    session,
                    location_key="austin",
                    source="cliaus_monthly_calibrated_v1",
                    generated_at=generated_at,
                    target_time=target,
                    features={
                        "expected_precipitation_inches": predicted_total,
                        "rain_risk_score": "0.5",
                        "weather_confidence_score": "0.8",
                    },
                    raw_json={
                        "forecast_generated_at": generated_at.isoformat(),
                        "forecast_age_hours": "0",
                        "station_id": "CLIAUS",
                        "source_url": CLI_URL,
                        "artifact_sha256": artifact_hash,
                        "predicted_total_inches": str(predicted_total),
                        "calibration_sample_count": calibration.sample_count,
                        "monotonic_recalibration": monotonic_recalibration,
                    },
                )
                feature_rows += 1
            session.commit()
    engine.dispose()
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "station_id": "CLIAUS",
        "source_url": CLI_URL,
        "month_to_date_inches": str(month_to_date) if month_to_date is not None else None,
        "noaa_forecast_rows": len(qpf_rows),
        "noaa_qpf_rows": qpf_available,
        "remaining_amount_model": amount_model,
        "predicted_total_inches": str(predicted_total) if predicted_total is not None else None,
        "monotonic_recalibration": monotonic_recalibration,
        "artifact_sha256": artifact_hash,
        "first_class_source_rows_inserted": source_rows,
        "first_class_feature_rows_inserted": feature_rows,
        "calibration": {
            "sample_count": calibration.sample_count,
            "minimum_samples": args.minimum_calibration_samples,
            "bias_inches": str(calibration.bias_inches),
            "residual_sigma_inches": (
                str(calibration.residual_sigma_inches)
                if calibration.residual_sigma_inches is not None
                else None
            ),
            "passed": calibration.passed,
            "historical_artifact": (
                str(args.historical_calibration) if args.historical_calibration else None
            ),
        },
        "activation_permitted": not blockers,
        "blockers": blockers,
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload))
    return 0


def _calibration_samples(session) -> list[tuple]:
    samples = []
    for row in session.scalars(
        select(WeatherObservation).where(WeatherObservation.location_key == "austin")
    ):
        raw = decode_json(row.raw_json)
        predicted = to_decimal(raw.get("monthly_rain_predicted_inches"))
        actual = to_decimal(raw.get("settled_cliaus_monthly_total_inches"))
        if predicted is not None and actual is not None:
            samples.append((predicted, actual))
    return samples


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
