"""Outcome-blind identifiability audit for prospective crypto range contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_predictor.crypto.distribution_model import (
    DistributionInputs,
    threshold_probability,
)
from kalshi_predictor.utils.time import parse_datetime

MODEL_VERSION = "range_distribution_v1_research"
TRANSFORMATION_VERSION = "lognormal_terminal_range_v1"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def audit_range_comparator(
    *,
    raw_market: dict[str, Any],
    structured_terms: dict[str, Any] | None,
    feature: dict[str, Any] | None,
    cutoff: datetime,
    settlement_target: datetime | None,
    authorized_reference_sources: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return a deterministic verdict; emit a candidate probability only if identifiable."""
    reasons: list[str] = []
    components = structured_terms.get("components", []) if structured_terms else []
    range_components = [
        row
        for row in components
        if isinstance(row, dict) and str(row.get("comparator") or "").upper() == "RANGE"
    ]
    if len(range_components) != 1:
        reasons.append("RANGE_COMPONENT_NOT_EXACTLY_ONE")

    lower = _decimal(raw_market.get("floor_strike"))
    upper = _decimal(raw_market.get("cap_strike"))
    if lower is None:
        reasons.append("LOWER_BOUND_MISSING")
    if upper is None:
        reasons.append("UPPER_BOUND_MISSING")
    if lower is not None and upper is not None and upper <= lower:
        reasons.append("RANGE_BOUNDS_INVALID")

    lower_inclusive = raw_market.get("lower_bound_inclusive")
    upper_inclusive = raw_market.get("upper_bound_inclusive")
    if not isinstance(lower_inclusive, bool) or not isinstance(upper_inclusive, bool):
        reasons.append("INCLUSIVITY_UNPROVEN")

    observation_window_seconds = raw_market.get("observation_window_seconds")
    if not isinstance(observation_window_seconds, int) or observation_window_seconds <= 0:
        reasons.append("OBSERVATION_WINDOW_MISSING")
    settlement_index_identifier = raw_market.get("settlement_index_identifier")
    if not isinstance(settlement_index_identifier, str) or not settlement_index_identifier:
        reasons.append("SETTLEMENT_INDEX_IDENTIFIER_MISSING")
    contract_timezone = raw_market.get("settlement_timezone")
    if not isinstance(contract_timezone, str) or not contract_timezone:
        reasons.append("CONTRACT_TIMEZONE_MISSING")

    cutoff_utc = _utc(cutoff)
    target = parse_datetime(settlement_target)
    horizon_minutes: float | None = None
    if target is None:
        reasons.append("SETTLEMENT_TARGET_MISSING")
    else:
        horizon_minutes = (_utc(target) - cutoff_utc).total_seconds() / 60.0
        if horizon_minutes <= 0:
            reasons.append("HORIZON_NON_POSITIVE")

    feature = feature or {}
    generated_at = parse_datetime(feature.get("generated_at"))
    if generated_at is None:
        reasons.append("FEATURE_GENERATED_AT_MISSING")
    elif _utc(generated_at) > cutoff_utc:
        reasons.append("FEATURE_SOURCE_AFTER_CUTOFF")

    raw_feature = feature.get("raw_json")
    if isinstance(raw_feature, str):
        try:
            raw_feature = json.loads(raw_feature)
        except json.JSONDecodeError:
            raw_feature = {}
    raw_feature = raw_feature if isinstance(raw_feature, dict) else {}
    feature_version = raw_feature.get("feature_version")
    if not feature_version:
        reasons.append("FEATURE_TRANSFORMATION_VERSION_MISSING")
    volatility_unit = raw_feature.get("volatility_unit")
    if volatility_unit != "log_return_per_sqrt_minute":
        reasons.append("VOLATILITY_TRANSFORMATION_INCOMPATIBLE")

    spot = _decimal(feature.get("price"))
    volatility = _decimal(feature.get("volatility_1h"))
    if spot is None or spot <= 0:
        reasons.append("CUTOFF_SPOT_MISSING")
    if volatility is None or volatility <= 0:
        reasons.append("VOLATILITY_MISSING")

    contract_source = str(
        (structured_terms or {}).get("reference_price_source") or "unknown"
    ).lower()
    feature_source = str(feature.get("source") or "unknown").lower()
    if contract_source == "unknown":
        reasons.append("SETTLEMENT_REFERENCE_SOURCE_UNKNOWN")
    elif contract_source not in feature_source:
        reasons.append("REFERENCE_PRICE_SOURCE_MISMATCH")
    if contract_source not in {source.lower() for source in authorized_reference_sources}:
        reasons.append("REFERENCE_SOURCE_RIGHTS_UNPROVEN")

    candidate_probability: str | None = None
    if not reasons:
        assert spot is not None and volatility is not None and horizon_minutes is not None
        assert lower is not None and upper is not None
        probability = threshold_probability(
            DistributionInputs(
                spot=float(spot),
                volatility_per_minute=float(volatility),
                horizon_minutes=horizon_minutes,
            ),
            comparator="RANGE",
            lower=float(lower),
            upper=float(upper),
        )
        if probability is None:
            reasons.append("DISTRIBUTION_CALCULATION_INVALID")
        else:
            candidate_probability = str(Decimal(str(probability)))

    verdict = "RANGE_COMPARATOR_IDENTIFIABLE" if not reasons else "RANGE_COMPARATOR_REJECTED"
    payload = {
        "schema": "phase4u.range-identifiability.v1",
        "verdict": verdict,
        "reason_codes": sorted(set(reasons)),
        "model_version": MODEL_VERSION if candidate_probability is not None else None,
        "transformation_version": TRANSFORMATION_VERSION,
        "contract_structure": str(raw_market.get("strike_type") or "unknown").upper(),
        "lower_bound": str(lower) if lower is not None else None,
        "upper_bound": str(upper) if upper is not None else None,
        "lower_inclusive": lower_inclusive if isinstance(lower_inclusive, bool) else None,
        "upper_inclusive": upper_inclusive if isinstance(upper_inclusive, bool) else None,
        "observation_window_seconds": observation_window_seconds,
        "settlement_index_identifier": settlement_index_identifier,
        "contract_timezone": contract_timezone,
        "settlement_target": _utc(target).isoformat() if target is not None else None,
        "horizon_minutes": str(horizon_minutes) if horizon_minutes is not None else None,
        "reference_asset": (
            range_components[0].get("symbol") if len(range_components) == 1 else None
        ),
        "settlement_reference_source": contract_source,
        "feature_source": feature_source,
        "cutoff_spot": str(spot) if spot is not None else None,
        "volatility_per_sqrt_minute": str(volatility) if volatility is not None else None,
        "volatility_unit": volatility_unit,
        "feature_version": feature_version,
        "candidate_probability": candidate_probability,
        "market_terms_hash": _hash(raw_market),
        "structured_terms_hash": _hash(structured_terms),
        "feature_input_hash": _hash(feature),
    }
    payload["verdict_hash"] = _hash(payload)
    return payload
