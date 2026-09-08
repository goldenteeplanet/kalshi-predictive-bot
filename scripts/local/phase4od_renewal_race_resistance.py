"""Offline renewal-race, clock-rollback, and split-brain resistance."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT
from scripts.local.phase4oc_evidence_freshness import (
    DEFAULT_TTL,
    renew_freshness_proof,
    verify_freshness_proof,
)

SCHEMA = "phase4od.renewal-race-resistance.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trusted time must be timezone-aware")
    return value.astimezone(UTC)


def propose_renewal(
    prior: dict[str, object],
    *,
    witness_id: str,
    trusted_manifest_sha256: str,
    trusted_time: datetime,
    watermark: datetime,
    ttl: timedelta = DEFAULT_TTL,
) -> dict[str, object]:
    now = _utc(trusted_time)
    floor = _utc(watermark)
    errors = []
    if not witness_id or witness_id.strip() != witness_id:
        errors.append("WITNESS_ID_INVALID")
    try:
        prior_observed = datetime.fromisoformat(str(prior["observed_at"])).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        errors.append("PRIOR_TIME_INVALID")
        prior_observed = floor
    if now < floor or now <= prior_observed:
        errors.append("TRUSTED_CLOCK_ROLLBACK")
    renewal = None
    if not errors:
        try:
            renewal = renew_freshness_proof(
                prior,
                trusted_manifest_sha256=trusted_manifest_sha256,
                renewed_at=now,
                ttl=ttl,
            )
        except (KeyError, TypeError, ValueError):
            errors.append("PRIOR_NOT_RENEWABLE")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "witness_id": witness_id,
        "trusted_time": now.isoformat(),
        "watermark": floor.isoformat(),
        "prior_proof_sha256": prior.get("proof_sha256"),
        "renewal": renewal,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "proposal_sha256": _digest(body)}


def adjudicate_renewal_round(
    prior: dict[str, object],
    proposals: list[dict[str, object]],
    *,
    trusted_manifest_sha256: str,
    evaluated_at: datetime,
    prior_watermark: datetime,
) -> dict[str, object]:
    errors: list[str] = []
    now = _utc(evaluated_at)
    watermark = _utc(prior_watermark)
    if now < watermark:
        errors.append("ADJUDICATOR_CLOCK_ROLLBACK")
    if not proposals:
        errors.append("RENEWAL_PROPOSALS_EMPTY")
    ids = [proposal.get("witness_id") for proposal in proposals]
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_WITNESS")
    valid: list[dict[str, object]] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            errors.append("PROPOSAL_NOT_OBJECT")
            continue
        unsigned = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
        if proposal.get("proposal_sha256") != _digest(unsigned):
            errors.append("PROPOSAL_HASH_MISMATCH")
        if proposal.get("schema") != SCHEMA or proposal.get("verdict") != "PASS":
            errors.append("PROPOSAL_NOT_PASSING")
        if proposal.get("prior_proof_sha256") != prior.get("proof_sha256"):
            errors.append("STALE_OR_WRONG_PARENT")
        if proposal.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
            errors.append("SETTLEMENT_BLOCKER_DRIFT")
        if proposal.get("safety") != _safety():
            errors.append("SAFETY_INVARIANT_VIOLATION")
        renewal = proposal.get("renewal")
        if not isinstance(renewal, dict):
            errors.append("RENEWAL_MISSING")
            continue
        verification = verify_freshness_proof(
            renewal,
            trusted_manifest_sha256=trusted_manifest_sha256,
            evaluated_at=now,
        )
        if verification["verdict"] != "PASS":
            errors.append("RENEWAL_INVALID")
        try:
            proposed_time = datetime.fromisoformat(str(proposal["trusted_time"])).astimezone(UTC)
            proposed_watermark = datetime.fromisoformat(str(proposal["watermark"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            errors.append("PROPOSAL_TIME_INVALID")
        else:
            if proposed_time < watermark or proposed_watermark != watermark:
                errors.append("WATERMARK_MISMATCH_OR_ROLLBACK")
        valid.append(renewal)
    child_hashes = sorted({str(child.get("proof_sha256")) for child in valid})
    if len(child_hashes) > 1:
        errors.append("SPLIT_BRAIN_CHILDREN")
    canonical = valid[0] if len(child_hashes) == 1 and not errors else None
    next_watermark = watermark.isoformat()
    if canonical is not None:
        next_watermark = str(canonical["observed_at"])
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if canonical is not None else "REFUSE",
        "errors": sorted(set(errors)),
        "prior_proof_sha256": prior.get("proof_sha256"),
        "proposal_sha256s": sorted(str(row.get("proposal_sha256")) for row in proposals),
        "child_proof_sha256s": child_hashes,
        "canonical_renewal": canonical,
        "next_watermark": next_watermark,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "adjudication_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
