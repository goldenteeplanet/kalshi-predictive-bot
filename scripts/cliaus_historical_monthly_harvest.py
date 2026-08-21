#!/usr/bin/env python3
"""Harvest CLIAUS daily rain and build expanding, no-leakage monthly samples."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.weather.monthly_rain import calibrate_monthly_rain, fit_isotonic

NCEI_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
STATION = "USW00013904"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--cutoff-day", type=int, default=20)
    parser.add_argument("--minimum-training-months", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = fetch_daily(args.start, args.end)
    samples = expanding_samples(
        rows,
        cutoff_day=args.cutoff_day,
        minimum_training_months=args.minimum_training_months,
    )
    complete = _complete_months(rows)
    current_month = date.today().month
    remaining_history = []
    for key in sorted(complete):
        daily = complete[key]
        total = sum((amount for _, amount in daily), Decimal("0"))
        mtd = sum((amount for day, amount in daily if day.day <= args.cutoff_day), Decimal("0"))
        remaining_history.append({"month": key[1], "remaining": total - mtd})
    current_remaining = _predict_remaining(remaining_history, current_month)
    monotonic = _monotonic_mappings(samples)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "NOAA_NCEI_DAILY_SUMMARIES",
        "station_id": "CLIAUS",
        "ncei_station": STATION,
        "start": args.start,
        "end": args.end,
        "cutoff_day": args.cutoff_day,
        "daily_rows": len(rows),
        "complete_months": len(_complete_months(rows)),
        "calibration_sample_count": len(samples),
        "no_leakage": True,
        "model": "EXPANDING_SEASONAL_REMAINING_AMOUNT_SHRINKAGE_V1",
        "current_remaining_amount_model": {
            "calendar_month": current_month,
            "expected_remaining_inches": str(current_remaining),
            "training_months": len(remaining_history),
        },
        "monotonic_recalibration": monotonic,
        "samples": samples,
    }
    _write_atomic(args.output, payload)
    print(json.dumps({key: value for key, value in payload.items() if key != "samples"}))
    return 0 if len(samples) >= 12 else 1


def fetch_daily(start: str, end: str) -> list[tuple[date, Decimal]]:
    query = urllib.parse.urlencode(
        {
            "dataset": "daily-summaries",
            "stations": STATION,
            "startDate": start,
            "endDate": end,
            "format": "csv",
            "includeAttributes": "false",
            "includeStationName": "false",
            "units": "standard",
        }
    )
    with urllib.request.urlopen(f"{NCEI_URL}?{query}", timeout=60) as response:
        text = response.read().decode("utf-8-sig", "replace")
    result = []
    for row in csv.DictReader(io.StringIO(text)):
        value = str(row.get("PRCP") or "").strip()
        if not value:
            continue
        result.append((date.fromisoformat(row["DATE"][:10]), Decimal(value)))
    return result


def expanding_samples(
    rows: list[tuple[date, Decimal]], *, cutoff_day: int, minimum_training_months: int
) -> list[dict[str, str]]:
    months = _complete_months(rows)
    history: list[dict[str, object]] = []
    samples: list[dict[str, str]] = []
    for key in sorted(months):
        daily = months[key]
        mtd = sum((amount for day, amount in daily if day.day <= cutoff_day), Decimal("0"))
        actual = sum((amount for _, amount in daily), Decimal("0"))
        remaining = actual - mtd
        if len(history) >= minimum_training_months:
            predicted_remaining = _predict_remaining(history, key[1])
            samples.append(
                {
                    "month": f"{key[0]:04d}-{key[1]:02d}",
                    "cutoff_day": str(cutoff_day),
                    "training_months": str(len(history)),
                    "month_to_date_inches": str(mtd),
                    "predicted_remaining_inches": str(predicted_remaining),
                    "predicted_total_inches": str(mtd + predicted_remaining),
                    "actual_total_inches": str(actual),
                }
            )
        history.append({"month": key[1], "remaining": remaining})
    return samples


def _predict_remaining(history: list[dict[str, object]], calendar_month: int) -> Decimal:
    prior = [item["remaining"] for item in history]
    same_month = [item["remaining"] for item in history if item["month"] == calendar_month]
    global_mean = sum(prior, Decimal("0")) / len(prior)
    seasonal_mean = sum(same_month, Decimal("0")) / len(same_month) if same_month else global_mean
    weight = Decimal(len(same_month)) / Decimal(len(same_month) + 4)
    return weight * seasonal_mean + (Decimal("1") - weight) * global_mean


def _monotonic_mappings(samples: list[dict[str, str]]) -> dict[str, list[dict[str, float]]]:
    pairs = [
        (Decimal(row["predicted_total_inches"]), Decimal(row["actual_total_inches"]))
        for row in samples
    ]
    calibration = calibrate_monthly_rain(pairs, minimum_samples=12)
    sigma = float(calibration.residual_sigma_inches or Decimal("1.5"))
    bias = float(calibration.bias_inches)
    mappings = {}
    for threshold in range(1, 8):
        points = []
        for predicted, actual in pairs:
            z = (threshold - float(predicted) - bias) / sigma
            raw = max(0.001, min(0.999, 0.5 * math.erfc(z / math.sqrt(2))))
            points.append((raw, int(actual > threshold)))
        mappings[str(threshold)] = fit_isotonic(points)
    return mappings


def _complete_months(rows: list[tuple[date, Decimal]]) -> dict[tuple[int, int], list]:
    grouped = defaultdict(list)
    for day, amount in rows:
        grouped[(day.year, day.month)].append((day, amount))
    return {
        key: sorted(values)
        for key, values in grouped.items()
        if len({day.day for day, _ in values}) >= 27
        and max(day for day, _ in values) < date.today()
    }


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
