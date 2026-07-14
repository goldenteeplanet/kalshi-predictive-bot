from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_predictor.synthetic_markets.contracts import (
    DEFAULT_CATEGORY_PROBABILITIES,
    LISTING_NO_EXACT_MATCH,
    LISTING_RELATED,
    LISTING_UNKNOWN,
    MODEL_BUNDLE_ID,
    ListingCheckResult,
    ModelComponent,
    SyntheticContractSpec,
    SyntheticEventSpec,
    SyntheticMarketsConfig,
    checksum_payload,
    stable_phase_3r_id,
)
from kalshi_predictor.utils.decimals import decimal_to_str


def build_probability_card_inputs(
    *,
    run_id: str,
    event: SyntheticEventSpec,
    contracts: tuple[SyntheticContractSpec, ...],
    listing_check: ListingCheckResult,
    estimate_as_of: datetime,
    config: SyntheticMarketsConfig,
    source_payload: dict[str, Any],
) -> dict[str, Any]:
    raw_probability, components = _component_probability(
        event=event,
        run_id=run_id,
        source_payload=source_payload,
    )
    coherent_probability = _clip_probability(
        raw_probability,
        floor=config.probability_floor,
        ceiling=config.probability_ceiling,
    )
    adjustment = coherent_probability - raw_probability
    interval = _interval(coherent_probability, reliability_width=_interval_width(listing_check))
    valid_until = min(estimate_as_of + timedelta(hours=24), event.observation_window.end_at)
    missing_inputs = _missing_inputs(listing_check, source_payload)
    reliability = _reliability_payload(listing_check=listing_check, missing_inputs=missing_inputs)
    constraint_result = {
        "constraint_result_id": stable_phase_3r_id(
            "constraint-result",
            run_id,
            event.synthetic_event_id,
            estimate_as_of.isoformat(),
        ),
        "constraint_set_id": stable_phase_3r_id("constraint-set", event.synthetic_event_id),
        "solver_id": "binary_clip_projection_v1",
        "solver_status": "SUCCESS",
        "pre_values": {"raw_probability": decimal_to_str(raw_probability)},
        "post_values": {"coherent_probability": decimal_to_str(coherent_probability)},
        "maximum_adjustment": decimal_to_str(abs(adjustment)),
        "violations_before": 1 if adjustment else 0,
        "violations_after": 0,
        "warnings": (
            ["Raw probability clipped to configured floor/ceiling."]
            if adjustment
            else []
        ),
    }
    estimate_id = stable_phase_3r_id(
        "estimate",
        run_id,
        event.synthetic_event_id,
        contracts[0].synthetic_contract_id,
        estimate_as_of.isoformat(),
    )
    return {
        "card_id": stable_phase_3r_id("probability-card", estimate_id),
        "estimate_id": estimate_id,
        "estimate_version": 1,
        "estimate_as_of": estimate_as_of,
        "valid_until": valid_until,
        "raw_probability": raw_probability,
        "coherent_probability": coherent_probability,
        "interval": interval,
        "reliability": reliability,
        "model_components": components,
        "constraint_result": constraint_result,
        "assumptions": tuple(
            source_payload.get("assumptions")
            or ("The public source publishes the event outcome on schedule.",)
        ),
        "missing_inputs": tuple(missing_inputs),
        "drivers": tuple(
            source_payload.get("drivers")
            or ("Category base rate is the deterministic baseline for this Phase 3R card.",)
        ),
        "counterevidence": tuple(
            source_payload.get("counterevidence")
            or ("No listed exact Kalshi price exists for direct market anchoring.",)
        ),
        "lineage": {
            "model_bundle_id": MODEL_BUNDLE_ID,
            "model_routing_version": config.model_routing_version,
            "configuration_version": config.configuration_version,
            "input_hash": checksum_payload(source_payload),
        },
    }


def score_probability(probability: Decimal, outcome_yes: bool) -> dict[str, str]:
    target = Decimal("1") if outcome_yes else Decimal("0")
    brier = (probability - target) ** 2
    clipped = _clip_probability(probability, floor=Decimal("0.0001"), ceiling=Decimal("0.9999"))
    log_loss = -(clipped.ln() if outcome_yes else (Decimal("1") - clipped).ln())
    return {
        "brier_score": decimal_to_str(brier.quantize(Decimal("0.0001"))),
        "log_loss": decimal_to_str(log_loss.quantize(Decimal("0.0001"))),
    }


def _component_probability(
    *,
    event: SyntheticEventSpec,
    run_id: str,
    source_payload: dict[str, Any],
) -> tuple[Decimal, tuple[ModelComponent, ...]]:
    base_probability = DEFAULT_CATEGORY_PROBABILITIES.get(event.category, Decimal("0.50"))
    source_probability = _optional_probability(
        source_payload.get("base_probability")
        or source_payload.get("prior_probability")
        or source_payload.get("probability")
    )
    components = [
        ModelComponent(
            component_id=stable_phase_3r_id("component", run_id, event.synthetic_event_id, "base"),
            model_id="category_base_rate_v1",
            model_version="1.0.0",
            calibration_id=None,
            probability=base_probability,
            weight=Decimal("0.70") if source_probability is not None else Decimal("1.00"),
            warnings=("Limited Phase 3R calibration history; baseline is conservative.",),
        )
    ]
    if source_probability is not None:
        components.append(
            ModelComponent(
                component_id=stable_phase_3r_id(
                    "component",
                    run_id,
                    event.synthetic_event_id,
                    "candidate-prior",
                ),
                model_id="approved_candidate_prior_v1",
                model_version="1.0.0",
                calibration_id=None,
                probability=source_probability,
                weight=Decimal("0.30"),
                warnings=("Candidate prior is bounded and cannot be the sole estimator.",),
            )
        )
    weighted = sum(component.probability * component.weight for component in components)
    total_weight = sum(component.weight for component in components)
    return weighted / total_weight, tuple(components)


def _optional_probability(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        probability = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if probability > 1:
        probability = probability / Decimal("100")
    if probability < 0 or probability > 1:
        return None
    return probability


def _clip_probability(probability: Decimal, *, floor: Decimal, ceiling: Decimal) -> Decimal:
    return min(max(probability, floor), ceiling).quantize(Decimal("0.0001"))


def _interval(probability: Decimal, *, reliability_width: Decimal) -> dict[str, str]:
    lower = max(Decimal("0.0001"), probability - reliability_width).quantize(Decimal("0.0001"))
    upper = min(Decimal("0.9999"), probability + reliability_width).quantize(Decimal("0.0001"))
    return {
        "interval_type": "DETERMINISTIC_CONSERVATIVE_INTERVAL",
        "level": "0.90",
        "lower": decimal_to_str(lower),
        "upper": decimal_to_str(upper),
    }


def _interval_width(listing_check: ListingCheckResult) -> Decimal:
    if listing_check.status == LISTING_NO_EXACT_MATCH:
        return Decimal("0.15")
    if listing_check.status == LISTING_RELATED:
        return Decimal("0.18")
    return Decimal("0.25")


def _missing_inputs(listing_check: ListingCheckResult, source_payload: dict[str, Any]) -> list[str]:
    missing = []
    if listing_check.status == LISTING_UNKNOWN:
        missing.append("complete_listing_check")
    if not source_payload.get("feature_snapshot_id"):
        missing.append("point_in_time_feature_snapshot")
    if not source_payload.get("calibration_id"):
        missing.append("synthetic_calibration_history")
    return missing


def _reliability_payload(
    *,
    listing_check: ListingCheckResult,
    missing_inputs: list[str],
) -> dict[str, str]:
    if listing_check.status == LISTING_UNKNOWN or len(missing_inputs) >= 2:
        grade = "D"
    elif listing_check.status == LISTING_RELATED or missing_inputs:
        grade = "C"
    else:
        grade = "C"
    return {
        "grade": grade,
        "calibration": "limited synthetic-market calibration history",
        "sample_support": "limited",
        "source_coverage": "complete" if listing_check.status != LISTING_UNKNOWN else "unknown",
        "freshness": "current",
        "model_agreement": "single deterministic baseline plus optional approved prior",
        "out_of_domain_risk": "moderate",
    }
