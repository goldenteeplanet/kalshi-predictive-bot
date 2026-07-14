from __future__ import annotations

import json
from dataclasses import replace
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.synthetic_markets.contracts import (
    EVENT_LISTING_UNKNOWN,
    EVENT_REJECTED_DUPLICATE,
    LISTING_EXACT_MATCH,
    LISTING_UNKNOWN,
    MODE_DISABLED,
    RUN_CANDIDATE_DISCOVERY,
    RUN_TYPES,
    ProbabilityCard,
    SyntheticMarketsConfig,
    SyntheticMarketsResult,
    checksum_payload,
    stable_phase_3r_id,
)
from kalshi_predictor.synthetic_markets.listing import check_local_listing_status
from kalshi_predictor.synthetic_markets.modeling import build_probability_card_inputs
from kalshi_predictor.synthetic_markets.policy import build_candidate_from_payload
from kalshi_predictor.synthetic_markets.renderer import render_synthetic_markets_markdown
from kalshi_predictor.synthetic_markets.repository import (
    existing_synthetic_run,
    persist_synthetic_markets_result,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now


def config_from_settings(settings: Settings | None = None) -> SyntheticMarketsConfig:
    resolved = settings or get_settings()
    mode = resolved.phase_3r_mode
    if not resolved.phase_3r_synthetic_markets_enabled:
        mode = MODE_DISABLED
    config = SyntheticMarketsConfig(
        enabled=resolved.phase_3r_synthetic_markets_enabled,
        mode=mode,
        max_candidates_per_run=resolved.phase_3r_max_candidates_per_run,
        max_contracts_per_event=resolved.phase_3r_max_contracts_per_event,
        max_horizon_days=resolved.phase_3r_max_horizon_days,
        probability_floor=resolved.phase_3r_probability_floor,
        probability_ceiling=resolved.phase_3r_probability_ceiling,
        coherence_tolerance=resolved.phase_3r_coherence_tolerance,
        max_publishable_adjustment=resolved.phase_3r_max_publishable_adjustment,
        listing_stale_after_hours=resolved.phase_3r_listing_stale_after_hours,
    )
    config.validate()
    return config


def run_synthetic_markets(
    session: Session,
    *,
    input_file: str | Path | None = None,
    candidates: list[dict[str, Any]] | None = None,
    run_type: str = RUN_CANDIDATE_DISCOVERY,
    estimate_as_of: str | Any | None = None,
    output_path: str | Path | None = Path("reports/synthetic_markets_report.md"),
    json_output_path: str | Path | None = Path("reports/synthetic_markets_report.json"),
    settings: Settings | None = None,
    force: bool = False,
) -> SyntheticMarketsResult:
    if run_type not in RUN_TYPES:
        raise ValueError(f"Unsupported Phase 3R run type: {run_type}")
    resolved_settings = settings or get_settings()
    config = config_from_settings(resolved_settings)
    started_at = utc_now()
    cutoff = parse_datetime(estimate_as_of) if estimate_as_of is not None else started_at
    if cutoff is None:
        raise ValueError("estimate_as_of must be a valid datetime.")
    candidate_payloads = _load_candidates(input_file=input_file, candidates=candidates)
    candidate_payloads = candidate_payloads[: config.max_candidates_per_run]
    base_idempotency_key = _idempotency_key(
        run_type=run_type,
        estimate_as_of=cutoff,
        candidates=candidate_payloads,
        config=config,
    )
    idempotency_key = (
        f"{base_idempotency_key}:force:{started_at.isoformat()}" if force else base_idempotency_key
    )
    run_id = stable_phase_3r_id("run", idempotency_key)
    cards: list[ProbabilityCard] = []
    rejected_candidates: list[dict[str, Any]] = []
    listing_checks = []
    status = "COMPLETED"
    if config.mode == MODE_DISABLED:
        status = "DISABLED"
    else:
        for payload in candidate_payloads:
            candidate = build_candidate_from_payload(payload, config=config, generated_at=cutoff)
            if not candidate.accepted or candidate.event is None:
                if candidate.rejection is not None:
                    rejected_candidates.append(candidate.rejection)
                continue
            listing_check = check_local_listing_status(
                session,
                run_id=run_id,
                event=candidate.event,
                contracts=candidate.contracts,
                checked_at=cutoff,
            )
            listing_checks.append(listing_check)
            if listing_check.status == LISTING_EXACT_MATCH:
                rejected_candidates.append(
                    _listing_rejection(
                        candidate_id=candidate.candidate_id,
                        status=EVENT_REJECTED_DUPLICATE,
                        reason="exact_equivalent_listed",
                        listing_check=listing_check,
                    )
                )
                continue
            if listing_check.status == LISTING_UNKNOWN:
                rejected_candidates.append(
                    _listing_rejection(
                        candidate_id=candidate.candidate_id,
                        status=EVENT_LISTING_UNKNOWN,
                        reason="listing_status_unknown",
                        listing_check=listing_check,
                    )
                )
                continue
            card_inputs = build_probability_card_inputs(
                run_id=run_id,
                event=candidate.event,
                contracts=candidate.contracts,
                listing_check=listing_check,
                estimate_as_of=cutoff,
                config=config,
                source_payload=dict(payload),
            )
            cards.append(
                ProbabilityCard(
                    run_id=run_id,
                    synthetic_event=candidate.event,
                    contracts=candidate.contracts,
                    listing_check=listing_check,
                    created_at=utc_now(),
                    **card_inputs,
                )
            )
    completed_at = utc_now()
    result = SyntheticMarketsResult(
        run_id=run_id,
        run_type=run_type,
        mode=config.mode,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        cards=tuple(cards),
        rejected_candidates=tuple(rejected_candidates),
        listing_checks=tuple(listing_checks),
        markdown="",
        report_path=str(output_path) if output_path else None,
        json_path=str(json_output_path) if json_output_path else None,
        idempotent=False,
    )
    result = replace(result, markdown=render_synthetic_markets_markdown(result))
    existing = existing_synthetic_run(session, idempotency_key=base_idempotency_key)
    if existing is not None and not force:
        result = replace(result, idempotent=True)
    else:
        persist_synthetic_markets_result(
            session,
            result=result,
            idempotency_key=idempotency_key,
            artifact_uris={
                "markdown_report": str(output_path) if output_path else None,
                "json_report": str(json_output_path) if json_output_path else None,
            },
            settings=resolved_settings,
        )
    _write_outputs(result, output_path=output_path, json_output_path=json_output_path)
    return result


def _load_candidates(
    *,
    input_file: str | Path | None,
    candidates: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if candidates is not None:
        return candidates
    if input_file is None:
        return []
    path = Path(input_file)
    if not path.exists():
        raise FileNotFoundError(
            f"Phase 3R candidate input file not found: {path}. "
            "Create it or omit --input-file to run with an empty candidate inventory."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise ValueError(f"Phase 3R candidate input file is not valid JSON: {path}") from exc
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
        return [dict(item) for item in payload["candidates"] if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(
        "Phase 3R input file must be a JSON object, list, or {candidates: [...]} object."
    )


def _idempotency_key(
    *,
    run_type: str,
    estimate_as_of: Any,
    candidates: list[dict[str, Any]],
    config: SyntheticMarketsConfig,
) -> str:
    return checksum_payload(
        {
            "run_type": run_type,
            "estimate_as_of": estimate_as_of.isoformat(),
            "candidate_hash": checksum_payload(candidates),
            "configuration_version": config.configuration_version,
            "generation_policy_version": config.generation_policy_version,
            "listing_policy_version": config.listing_policy_version,
            "model_routing_version": config.model_routing_version,
            "constraint_policy_version": config.constraint_policy_version,
        }
    )


def _listing_rejection(
    *,
    candidate_id: str,
    status: str,
    reason: str,
    listing_check: Any,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "status": status,
        "reason_codes": [reason],
        "rejected_at": listing_check.checked_at.isoformat(),
        "listing_check": listing_check.as_payload(),
        "raw_json": encode_json(
            {
                "candidate_id": candidate_id,
                "status": status,
                "reason": reason,
                "listing_check": listing_check.as_payload(),
            }
        ),
    }


def _write_outputs(
    result: SyntheticMarketsResult,
    *,
    output_path: str | Path | None,
    json_output_path: str | Path | None,
) -> None:
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.markdown, encoding="utf-8")
    if json_output_path is not None:
        output = Path(json_output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": result.run_id,
            "run_type": result.run_type,
            "mode": result.mode,
            "status": result.status,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
            "candidate_counts": result.candidate_counts,
            "estimate_counts": result.estimate_counts,
            "cards": [card.as_payload() for card in result.cards],
            "rejected_candidates": list(result.rejected_candidates),
            "listing_checks": [check.as_payload() for check in result.listing_checks],
            "idempotent": result.idempotent,
        }
        output.write_text(encode_json(payload), encoding="utf-8")
