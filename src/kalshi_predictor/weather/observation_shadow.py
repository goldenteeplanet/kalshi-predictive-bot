from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime


@dataclass(frozen=True)
class ObservationShadowResult:
    applied_probability: Decimal
    shadow_probability: Decimal | None
    applied: bool
    passed: bool
    blocker: str | None
    provenance: dict[str, Any]


def evaluate_knyc_observation(
    *, baseline_probability: Decimal, raw_strike: Decimal,
    target_time: Any, evidence: Mapping[str, Any] | None,
    max_adjustment: Decimal, enabled: bool,
) -> ObservationShadowResult:
    provenance = {
        "evidence_source": "NOAA_KNYC",
        "evidence_role": "NON_SETTLEMENT_POINT_OBSERVATION",
        "settlement_source": "THE_WEATHER_COMPANY",
        "station_id": str((evidence or {}).get("station_id") or "").upper() or None,
        "target_utc_time": (evidence or {}).get("target_utc_time"),
        "offset_seconds": (evidence or {}).get("offset_seconds"),
        "observation_temperature_f": (evidence or {}).get("observation_temperature_f"),
    }
    blocker = _validate(target_time, evidence)
    if blocker:
        return ObservationShadowResult(
            baseline_probability, None, False, False, blocker, provenance
        )
    observation = to_decimal(evidence.get("observation_temperature_f"))
    signal = max(Decimal("-1"), min(Decimal("1"), (observation - raw_strike) / Decimal("20")))
    shadow = max(Decimal("0.01"), min(Decimal("0.99"), baseline_probability + signal * max_adjustment))
    return ObservationShadowResult(
        shadow if enabled else baseline_probability, shadow, enabled, True, None, provenance
    )


def _validate(target_time: Any, evidence: Mapping[str, Any] | None) -> str | None:
    if not evidence:
        return "KNYC_EVIDENCE_MISSING"
    if str(evidence.get("station_id") or "").upper() != "KNYC":
        return "STATION_NOT_KNYC"
    if str(evidence.get("evidence_role") or "") != "NON_SETTLEMENT_POINT_OBSERVATION":
        return "PROVENANCE_ROLE_INVALID"
    if str(evidence.get("settlement_source") or "").lower() != "the_weather_company":
        return "SETTLEMENT_PROVENANCE_INVALID"
    if parse_datetime(evidence.get("target_utc_time")) != parse_datetime(target_time):
        return "TARGET_TIME_MISMATCH"
    offset = to_decimal(evidence.get("offset_seconds"))
    if offset is None or abs(offset) > Decimal("900"):
        return "OBSERVATION_OFFSET_EXCEEDS_15_MINUTES"
    if to_decimal(evidence.get("observation_temperature_f")) is None:
        return "OBSERVATION_TEMPERATURE_MISSING"
    return None
