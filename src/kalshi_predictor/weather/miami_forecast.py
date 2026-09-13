"""Fixed prospective Miami research baseline; no calibration or execution authority."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from kalshi_predictor.weather.miami_index import IndexPoint, MiamiIndexCapture

LOCAL = ZoneInfo("America/New_York")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MIAMI_FORECAST_AWARE_CLOCK_REQUIRED")
    return value.astimezone(UTC)


def _hash(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _semantic(point: IndexPoint) -> tuple[Any, ...]:
    return (
        point.value_f,
        point.status,
        point.contributors,
        point.config_version,
        point.configuration_published_by_event,
    )


def forecast_miami_prior_day(
    captures: Sequence[MiamiIndexCapture],
    *,
    origin_at: datetime,
    model_input_as_of: datetime,
    horizon_minutes: int,
) -> dict[str, Any]:
    """Preserve frozen v2 formulas while requiring a future target and real receipts.

    Only origin/lag and earlier-day endpoint changes enter numerical calculations.
    Current-day observations after the origin are ignored. Availability claims
    cover this prospective calculation, not prior historical public accessibility.
    """
    origin, as_of = _aware(origin_at), _aware(model_input_as_of)
    local_origin = origin.astimezone(LOCAL)
    if local_origin.minute or local_origin.second or local_origin.microsecond:
        raise ValueError("MIAMI_EXACT_LOCAL_HOUR_ORIGIN_REQUIRED")
    if type(horizon_minutes) is not int or horizon_minutes not in (30, 60):
        raise ValueError("MIAMI_FIXED_HORIZON_REQUIRED")
    if not 0 <= (as_of - origin).total_seconds() <= 600:
        raise ValueError("MIAMI_ORIGIN_FUTURE_OR_OLDER_THAN_TEN_MINUTES")
    target = origin + timedelta(minutes=horizon_minutes)
    if target <= as_of or target.astimezone(LOCAL).date() != local_origin.date():
        raise ValueError("MIAMI_TARGET_NOT_FUTURE_SAME_DAY")
    if not captures or len(captures) > 12 or sum(len(c.points) for c in captures) > 100_000:
        raise ValueError("MIAMI_CAPTURE_BUDGET")
    points: dict[datetime, IndexPoint] = {}
    refs: dict[datetime, set[str]] = {}
    receipt_times = []
    for capture in captures:
        received = max(_aware(capture.index_received_at), _aware(capture.calibrations_received_at))
        if received != _aware(capture.available_at) or received > as_of:
            raise ValueError("MIAMI_SOURCE_NOT_AVAILABLE_AS_OF")
        if capture.index_units != "fahrenheit":
            raise ValueError("MIAMI_FAHRENHEIT_REQUIRED")
        if not _hash(capture.index_sha256) or not _hash(capture.calibrations_sha256):
            raise ValueError("MIAMI_ORIGINAL_HASH_REQUIRED")
        receipt_times.append(received)
        previous = None
        for point in capture.points:
            at = _aware(point.event_at)
            if at > _aware(capture.index_received_at) or (previous is not None and at <= previous):
                raise ValueError("MIAMI_POINT_CLOCK_INVALID")
            previous = at
            if at > origin:
                continue
            if point.value_f is not None and (
                not isinstance(point.value_f, Decimal)
                or not point.value_f.is_finite()
                or not Decimal("-40") <= point.value_f <= Decimal("122")
                or point.value_f != point.value_f.quantize(Decimal(".01"))
            ):
                raise ValueError("MIAMI_INVALID_CANONICAL_POINT_VALUE")
            if at in points and _semantic(points[at]) != _semantic(point):
                raise ValueError("MIAMI_CONFLICTING_OVERLAPPING_ORIGINALS")
            points[at] = point
            refs.setdefault(at, set()).add(capture.index_sha256)

    def usable(at: datetime) -> IndexPoint | None:
        p = points.get(at)
        if p is None or p.value_f is None or not p.configuration_published_by_event:
            return None
        if not p.config_version or p.status not in ("normal", "degraded"):
            return None
        if (
            type(p.contributors) is not int
            or not 4 <= p.contributors <= 5
            or (p.status == "normal" and p.contributors != 5)
        ):
            return None
        return p

    current, lag = usable(origin), usable(origin - timedelta(minutes=30))
    if current is None or lag is None or current.config_version != lag.config_version:
        raise ValueError("MIAMI_EXACT_ORIGIN_LAG_AND_CONFIG_REQUIRED")
    assert current.value_f is not None and lag.value_f is not None
    current_f = float(current.value_f)
    training: list[dict[str, Any]] = []
    increments: list[Decimal] = []
    used = {origin, origin - timedelta(minutes=30)}
    for day in sorted({at.astimezone(LOCAL).date() for at in points}):
        if day >= local_origin.date():
            continue
        start = datetime(day.year, day.month, day.day, local_origin.hour, tzinfo=LOCAL)
        end = start + timedelta(minutes=horizon_minutes)
        if end.date() != day or end >= origin:
            continue
        a, b = usable(start), usable(end)
        if a is None or b is None or a.config_version != b.config_version:
            continue
        assert a.value_f is not None and b.value_f is not None
        training.append(
            {
                "day": str(day),
                "origin_at": start.isoformat(),
                "target_at": end.isoformat(),
                "increment_f": float(b.value_f - a.value_f),
                "config_version": a.config_version,
            }
        )
        increments.append(b.value_f - a.value_f)
        used.update((start.astimezone(UTC), end.astimezone(UTC)))
    if len(training) < 3:
        raise ValueError("MIAMI_INSUFFICIENT_PRIOR_DAY_INCREMENTS")
    # Canonical values are exact hundredths: preserve threshold ties through arithmetic.
    samples = sorted(float(current.value_f + delta) for delta in increments)
    trend = float(current.value_f + (current.value_f - lag.value_f) * horizon_minutes / Decimal(30))
    if not all(math.isfinite(v) for v in (*samples, trend)):
        raise ValueError("MIAMI_NONFINITE_FORECAST")
    return {
        "schema": "miami-prior-day-prospective-v1",
        "model": "miami_prior_day_increment_v1",
        "origin_at": origin.isoformat(),
        "model_input_as_of": as_of.isoformat(),
        "target_at": target.isoformat(),
        "horizon_minutes": horizon_minutes,
        "input_received_at": max(receipt_times).isoformat(),
        "units": "fahrenheit",
        "models": {
            "persistence": {"mean_f": current_f, "samples_f": [current_f]},
            "fixed_30min_linear_trend": {"mean_f": trend, "samples_f": [trend]},
            "prior_day_increment_empirical": {
                "mean_f": statistics.mean(samples),
                "samples_f": samples,
                "interval_low_f": samples[max(0, math.ceil(0.1 * len(samples)) - 1)],
                "interval_high_f": samples[math.ceil(0.9 * len(samples)) - 1],
                "interval_nominal_coverage": 0.8,
                "interval_calibrated": False,
            },
        },
        "training": training,
        "training_day_groups": len(training),
        "source_hashes": sorted({c.index_sha256 for c in captures}),
        "calibration_hashes": sorted({c.calibrations_sha256 for c in captures}),
        "lineage": [
            {
                "event_at": at.isoformat(),
                "config_version": points[at].config_version,
                "source_hashes": sorted(refs[at]),
            }
            for at in sorted(used)
        ],
        "historical_public_availability": "UNKNOWN",
        "status": "PROXY_SHADOW_UNQUALIFIED",
        "calibrated": False,
        "blockers": [
            "PROSPECTIVE_MODEL_VALIDATION_REQUIRED",
            "CONTRACT_RULES_UNVERIFIED",
            "EMPIRICAL_60_MINUTE_INTERVAL_UNDERCOVERAGE_OBSERVED",
        ],
        "paper_eligible": False,
        "execution_authority": False,
    }


def empirical_probability(
    samples_f: Sequence[float],
    *,
    comparator: str,
    threshold_f: float | None = None,
    lower_f: float | None = None,
    upper_f: float | None = None,
) -> float:
    """Sample mass under explicitly supplied conditions; never a rule certificate."""
    if not 3 <= len(samples_f) <= 1000:
        raise ValueError("MIAMI_EMPIRICAL_SAMPLE_BUDGET")
    if any(isinstance(v, bool) or not math.isfinite(v) for v in samples_f):
        raise ValueError("MIAMI_FINITE_SAMPLES_REQUIRED")
    if comparator == "RANGE":
        if (
            threshold_f is not None
            or lower_f is None
            or upper_f is None
            or isinstance(lower_f, bool)
            or isinstance(upper_f, bool)
            or not math.isfinite(lower_f)
            or not math.isfinite(upper_f)
            or lower_f >= upper_f
        ):
            raise ValueError("MIAMI_INVALID_RANGE")
        count = sum(lower_f <= value < upper_f for value in samples_f)
    elif comparator in {"ABOVE", "AT_OR_ABOVE", "BELOW", "AT_OR_BELOW"}:
        if (
            threshold_f is None
            or isinstance(threshold_f, bool)
            or not math.isfinite(threshold_f)
            or lower_f is not None
            or upper_f is not None
        ):
            raise ValueError("MIAMI_INVALID_THRESHOLD")
        count = sum(
            {
                "ABOVE": value > threshold_f,
                "AT_OR_ABOVE": value >= threshold_f,
                "BELOW": value < threshold_f,
                "AT_OR_BELOW": value <= threshold_f,
            }[comparator]
            for value in samples_f
        )
    else:
        raise ValueError("MIAMI_UNKNOWN_COMPARATOR")
    return count / len(samples_f)
