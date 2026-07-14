from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    SyntheticConstraintResult,
    SyntheticContractRegistry,
    SyntheticEventRegistry,
    SyntheticListingCheck,
    SyntheticListingMatch,
    SyntheticMarketRun,
    SyntheticModelComponent,
    SyntheticProbabilityEstimate,
)
from kalshi_predictor.memory.repository import write_forecast_memory, write_market_memory
from kalshi_predictor.synthetic_markets.contracts import (
    DISCLAIMER,
    SYNTHETIC_SOURCE_COMPONENT,
    ProbabilityCard,
    SyntheticMarketsResult,
    canonical_json,
    stable_phase_3r_id,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def existing_synthetic_run(
    session: Session,
    *,
    idempotency_key: str,
) -> SyntheticMarketRun | None:
    return session.scalar(
        select(SyntheticMarketRun)
        .where(SyntheticMarketRun.idempotency_key == idempotency_key)
        .limit(1)
    )


def latest_synthetic_run(session: Session) -> SyntheticMarketRun | None:
    return session.scalar(
        select(SyntheticMarketRun)
        .order_by(desc(SyntheticMarketRun.completed_at), desc(SyntheticMarketRun.started_at))
        .limit(1)
    )


def synthetic_markets_status(session: Session) -> dict[str, Any]:
    latest = latest_synthetic_run(session)
    return {
        "run_count": _count(session, SyntheticMarketRun),
        "event_count": _count(session, SyntheticEventRegistry),
        "contract_count": _count(session, SyntheticContractRegistry),
        "estimate_count": _count(session, SyntheticProbabilityEstimate),
        "listing_check_count": _count(session, SyntheticListingCheck),
        "listing_match_count": _count(session, SyntheticListingMatch),
        "resolution_count": 0,
        "latest_run_id": latest.run_id if latest else None,
        "latest_status": latest.status if latest else "NOT_RUN",
        "latest_completed_at": (
            latest.completed_at.isoformat() if latest and latest.completed_at else None
        ),
    }


def persist_synthetic_markets_result(
    session: Session,
    *,
    result: SyntheticMarketsResult,
    idempotency_key: str,
    artifact_uris: dict[str, str | None],
    settings: Settings,
) -> SyntheticMarketRun:
    now = utc_now()
    run = SyntheticMarketRun(
        run_id=result.run_id,
        run_type=result.run_type,
        mode=result.mode,
        configuration_version=(
            result.cards[0].lineage.get("configuration_version", "phase_3r_default_v1")
            if result.cards
            else "phase_3r_default_v1"
        ),
        code_version=None,
        started_at=result.started_at,
        completed_at=result.completed_at,
        source_watermarks_json=encode_json(_source_watermarks(result)),
        generation_policy_version="phase_3r_generation_v1",
        listing_policy_version="phase_3r_listing_v1",
        model_routing_version="phase_3r_model_routing_v1",
        constraint_policy_version="phase_3r_constraints_v1",
        status=result.status,
        candidate_counts_json=encode_json(result.candidate_counts),
        estimate_counts_json=encode_json(result.estimate_counts),
        error_summary_json=encode_json(_error_summary(result)),
        artifact_manifest_json=encode_json(artifact_uris),
        idempotency_key=idempotency_key,
        raw_json=encode_json(
            {
                "run_id": result.run_id,
                "run_type": result.run_type,
                "mode": result.mode,
                "status": result.status,
                "candidate_counts": result.candidate_counts,
                "estimate_counts": result.estimate_counts,
                "rejected_candidates": list(result.rejected_candidates),
            }
        ),
    )
    session.add(run)
    session.flush()
    for card in result.cards:
        _persist_probability_card(
            session,
            card=card,
            run_id=result.run_id,
            now=now,
            settings=settings,
        )
    return run


def _persist_probability_card(
    session: Session,
    *,
    card: ProbabilityCard,
    run_id: str,
    now: Any,
    settings: Settings,
) -> None:
    event = card.synthetic_event
    existing_event = session.get(SyntheticEventRegistry, event.synthetic_event_id)
    if existing_event is None:
        session.add(
            SyntheticEventRegistry(
                synthetic_event_id=event.synthetic_event_id,
                synthetic_event_version=event.synthetic_event_version,
                semantic_hash=event.semantic_hash,
                canonical_title=event.canonical_title,
                plain_language_summary=event.plain_language_summary,
                category=event.category,
                subcategory=event.subcategory,
                market_form=event.market_form,
                observation_start_at=event.observation_window.start_at,
                observation_end_at=event.observation_window.end_at,
                timezone=event.observation_window.timezone,
                mutually_exclusive=int(event.mutually_exclusive),
                collectively_exhaustive=int(event.collectively_exhaustive),
                settlement_rule_json=canonical_json(event.settlement_rule.as_payload()),
                generation_source=event.generation_source,
                status=event.status,
                status_reason_codes_json=encode_json(list(event.reason_codes)),
                created_by_run_id=run_id,
                supersedes_event_id=None,
                created_at=now,
                raw_json=canonical_json(event.as_payload()),
            )
        )
    for contract in card.contracts:
        if session.get(SyntheticContractRegistry, contract.synthetic_contract_id) is None:
            session.add(
                SyntheticContractRegistry(
                    synthetic_contract_id=contract.synthetic_contract_id,
                    synthetic_event_id=event.synthetic_event_id,
                    synthetic_contract_version=contract.synthetic_contract_version,
                    canonical_question=contract.canonical_question,
                    contract_type=contract.contract_type,
                    outcome_code=contract.outcome_code,
                    condition_json=canonical_json(contract.condition),
                    complement_contract_id=contract.complement_contract_id,
                    constraint_group_id=contract.constraint_group_id,
                    status=contract.status,
                    created_at=now,
                    raw_json=canonical_json(contract.as_payload()),
                )
            )
    listing_check = card.listing_check
    if session.get(SyntheticListingCheck, listing_check.listing_check_id) is None:
        session.add(
            SyntheticListingCheck(
                listing_check_id=listing_check.listing_check_id,
                run_id=run_id,
                synthetic_event_id=event.synthetic_event_id,
                checked_at=listing_check.checked_at,
                status=listing_check.status,
                pagination_complete=int(listing_check.pagination_complete),
                live_coverage_complete=int(listing_check.live_coverage_complete),
                historical_coverage_status=listing_check.historical_coverage_status,
                historical_cutoff=listing_check.historical_cutoff,
                warnings_json=encode_json(list(listing_check.warnings)),
                raw_json=canonical_json(listing_check.as_payload()),
            )
        )
    for match in listing_check.matches:
        if session.get(SyntheticListingMatch, match.match_id) is None:
            session.add(
                SyntheticListingMatch(
                    match_id=match.match_id,
                    listing_check_id=listing_check.listing_check_id,
                    synthetic_event_id=event.synthetic_event_id,
                    kalshi_series_ticker=match.kalshi_series_ticker,
                    kalshi_event_ticker=match.kalshi_event_ticker,
                    kalshi_market_ticker=match.kalshi_market_ticker,
                    match_class=match.match_class,
                    semantic_score=decimal_to_str(match.semantic_score),
                    logical_comparison=match.logical_comparison,
                    field_differences_json=canonical_json(match.field_differences),
                    reviewer_status=match.reviewer_status,
                    effective_at=match.effective_at,
                    raw_json=canonical_json(match.as_payload()),
                )
            )
    if session.get(SyntheticProbabilityEstimate, card.estimate_id) is not None:
        return
    phase3o_receipts = _write_phase3o_memory(session, card=card, settings=settings)
    session.add(
        SyntheticProbabilityEstimate(
            estimate_id=card.estimate_id,
            estimate_version=card.estimate_version,
            run_id=run_id,
            synthetic_event_id=event.synthetic_event_id,
            synthetic_contract_id=card.contracts[0].synthetic_contract_id,
            estimate_as_of=card.estimate_as_of,
            valid_until=card.valid_until,
            raw_probability=decimal_to_str(card.raw_probability),
            coherent_probability=decimal_to_str(card.coherent_probability),
            interval_json=canonical_json(card.interval),
            reliability_json=canonical_json(card.reliability),
            status=card.status,
            card_json=canonical_json(card.as_payload()),
            disclaimer=DISCLAIMER,
            lineage_json=canonical_json(card.lineage),
            phase3o_receipts_json=canonical_json(phase3o_receipts),
            supersedes_estimate_id=None,
            created_at=card.created_at or now,
            raw_json=canonical_json(
                {
                    "card": card.as_payload(),
                    "phase3o_receipts": phase3o_receipts,
                }
            ),
        )
    )
    for component in card.model_components:
        component_record_id = stable_phase_3r_id(
            "model-component-record",
            card.estimate_id,
            component.component_id,
        )
        if session.get(SyntheticModelComponent, component_record_id) is None:
            session.add(
                SyntheticModelComponent(
                    component_record_id=component_record_id,
                    estimate_id=card.estimate_id,
                    component_id=component.component_id,
                    model_id=component.model_id,
                    model_version=component.model_version,
                    calibration_id=component.calibration_id,
                    probability=decimal_to_str(component.probability),
                    weight=decimal_to_str(component.weight),
                    status=component.status,
                    warnings_json=encode_json(list(component.warnings)),
                    runtime_ms=component.runtime_ms,
                    raw_json=canonical_json(component.as_payload()),
                )
            )
    constraint_record_id = card.constraint_result["constraint_result_id"]
    if session.get(SyntheticConstraintResult, constraint_record_id) is None:
        session.add(
            SyntheticConstraintResult(
                constraint_result_id=constraint_record_id,
                estimate_id=card.estimate_id,
                constraint_set_id=card.constraint_result["constraint_set_id"],
                solver_id=card.constraint_result["solver_id"],
                solver_status=card.constraint_result["solver_status"],
                pre_values_json=canonical_json(card.constraint_result["pre_values"]),
                post_values_json=canonical_json(card.constraint_result["post_values"]),
                maximum_adjustment=str(card.constraint_result["maximum_adjustment"]),
                violations_before=int(card.constraint_result["violations_before"]),
                violations_after=int(card.constraint_result["violations_after"]),
                warnings_json=canonical_json(card.constraint_result["warnings"]),
                raw_json=canonical_json(card.constraint_result),
            )
        )


def _write_phase3o_memory(
    session: Session,
    *,
    card: ProbabilityCard,
    settings: Settings,
) -> list[dict[str, Any]]:
    event = card.synthetic_event
    contract = card.contracts[0]
    market_receipt = write_market_memory(
        session,
        {
            "event_type": "FORECAST_HORIZON",
            "event_time": card.estimate_as_of,
            "observed_at": card.estimate_as_of,
            "source_component": SYNTHETIC_SOURCE_COMPONENT,
            "source_event_id": card.estimate_id,
            "idempotency_key": f"phase3r:market:{card.estimate_id}:v1",
            "instrument_id": contract.synthetic_contract_id,
            "venue_id": "INTERNAL_SYNTHETIC",
            "asset_class": "event_contract",
            "category_id": event.category,
            "contract_id": contract.synthetic_contract_id,
            "contract_expiry": event.observation_window.end_at,
            "timeframe": "synthetic",
            "snapshot_type": "FORECAST_HORIZON",
            "market_event_time": card.estimate_as_of,
            "source_name": "phase_3r",
            "trading_status": "NOT_TRADABLE",
            "feature_values": {
                "synthetic_probability": decimal_to_str(card.coherent_probability),
                "listing_status": card.listing_check.status,
            },
            "data_mode": "AS_OBSERVED",
            "ingestion_mode": "REPLAY",
            "data_quality_flags": ["SYNTHETIC_INTERNAL_ONLY"],
            "event_payload": card.as_payload(),
        },
        settings=settings,
    )
    forecast_receipt = write_forecast_memory(
        session,
        {
            "forecast_id": f"synthetic:{card.estimate_id}",
            "event_type": "FORECAST_CREATED",
            "event_time": card.estimate_as_of,
            "observed_at": card.estimate_as_of,
            "source_component": SYNTHETIC_SOURCE_COMPONENT,
            "source_event_id": card.estimate_id,
            "idempotency_key": f"phase3r:forecast:{card.estimate_id}:v1",
            "market_memory_id": market_receipt.memory_event_id,
            "instrument_id": contract.synthetic_contract_id,
            "venue_id": "INTERNAL_SYNTHETIC",
            "category_id": event.category,
            "strategy_id": "phase_3r_synthetic_probability",
            "timeframe": "synthetic",
            "direction": "YES",
            "forecast_generated_at": card.estimate_as_of,
            "forecast_valid_from": card.estimate_as_of,
            "forecast_target_at": event.observation_window.end_at,
            "forecast_horizon_seconds": int(
                (event.observation_window.end_at - card.estimate_as_of).total_seconds()
            ),
            "forecast_type": "BINARY_SYNTHETIC",
            "predicted_probability": decimal_to_str(card.coherent_probability),
            "probability_up": decimal_to_str(card.coherent_probability),
            "prediction_lower_bound": card.interval.get("lower"),
            "prediction_upper_bound": card.interval.get("upper"),
            "confidence_score": _grade_to_confidence(card.reliability.get("grade")),
            "eligibility_status": "INTERNAL_ONLY_NOT_TRADABLE",
            "decision_status": "NO_TRADE",
            "reason_codes": ["SYNTHETIC_INTERNAL_ONLY", "NO_LISTED_MARKET_PRICE"],
            "primary_model_id": "synthetic_market_baseline_bundle_v1",
            "primary_model_family": "synthetic_markets",
            "primary_model_version": "1.0.0",
            "configuration_version": card.lineage.get("configuration_version"),
            "model_lineage": [component.as_payload() for component in card.model_components],
            "feature_lineage": {"listing_check_id": card.listing_check.listing_check_id},
            "feature_values": {
                "raw_probability": decimal_to_str(card.raw_probability),
                "coherent_probability": decimal_to_str(card.coherent_probability),
                "listing_status": card.listing_check.status,
            },
            "forecast_outcome_status": "PENDING",
            "ingestion_mode": "REPLAY",
            "data_quality_flags": ["SYNTHETIC_INTERNAL_ONLY"],
            "event_payload": card.as_payload(),
        },
        settings=settings,
    )
    return [
        {
            "store": market_receipt.store,
            "status": market_receipt.status,
            "memory_event_id": market_receipt.memory_event_id,
            "idempotency_key": market_receipt.idempotency_key,
        },
        {
            "store": forecast_receipt.store,
            "status": forecast_receipt.status,
            "memory_event_id": forecast_receipt.memory_event_id,
            "idempotency_key": forecast_receipt.idempotency_key,
        },
    ]


def _grade_to_confidence(grade: str | None) -> str:
    return {"A": "0.90", "B": "0.75", "C": "0.55", "D": "0.35"}.get(grade or "", "0.25")


def _source_watermarks(result: SyntheticMarketsResult) -> dict[str, str]:
    watermarks = {
        card.synthetic_event.settlement_rule.primary_source_id: card.estimate_as_of.isoformat()
        for card in result.cards
    }
    return dict(sorted(watermarks.items()))


def _error_summary(result: SyntheticMarketsResult) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    for rejection in result.rejected_candidates:
        for reason in rejection.get("reason_codes", []):
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
    return {"rejection_reasons": reason_counts}


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)
