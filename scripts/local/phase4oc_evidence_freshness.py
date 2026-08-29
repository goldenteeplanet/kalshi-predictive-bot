"""Deterministic freshness, expiration, and renewal proof for Phase 4OA evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4oc.evidence-freshness.v1"
DEFAULT_TTL = timedelta(hours=6)
MAX_CLOCK_SKEW = timedelta(minutes=5)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def issue_freshness_proof(
    *,
    aggregate_manifest_sha256: str,
    observed_at: datetime,
    ttl: timedelta = DEFAULT_TTL,
    sequence: int = 1,
    parent_proof_sha256: str | None = None,
) -> dict[str, object]:
    observed = _utc(observed_at)
    if len(aggregate_manifest_sha256) != 64:
        raise ValueError("aggregate manifest must be a SHA-256 hex digest")
    int(aggregate_manifest_sha256, 16)
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    if sequence < 1 or (sequence == 1) != (parent_proof_sha256 is None):
        raise ValueError("sequence and parent proof are inconsistent")
    body = {
        "schema": SCHEMA,
        "aggregate_manifest_sha256": aggregate_manifest_sha256,
        "observed_at": observed.isoformat(),
        "expires_at": (observed + ttl).isoformat(),
        "ttl_seconds": int(ttl.total_seconds()),
        "sequence": sequence,
        "parent_proof_sha256": parent_proof_sha256,
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "proof_sha256": _digest(body)}


def verify_freshness_proof(
    proof: object,
    *,
    trusted_manifest_sha256: str,
    evaluated_at: datetime,
    maximum_ttl: timedelta = DEFAULT_TTL,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(proof, dict):
        return _result(["PROOF_NOT_OBJECT"], None)
    expected = {
        "schema",
        "aggregate_manifest_sha256",
        "observed_at",
        "expires_at",
        "ttl_seconds",
        "sequence",
        "parent_proof_sha256",
        "blocked_on_september_1_settlement",
        "safety",
        "proof_sha256",
    }
    if set(proof) != expected:
        errors.append("PROOF_FIELDS_INVALID")
    unsigned = {key: value for key, value in proof.items() if key != "proof_sha256"}
    if proof.get("proof_sha256") != _digest(unsigned):
        errors.append("PROOF_HASH_MISMATCH")
    if proof.get("schema") != SCHEMA:
        errors.append("PROOF_SCHEMA_INVALID")
    if proof.get("aggregate_manifest_sha256") != trusted_manifest_sha256:
        errors.append("TRUSTED_MANIFEST_MISMATCH")
    if proof.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
        errors.append("SETTLEMENT_BLOCKER_DRIFT")
    if proof.get("safety") != _safety():
        errors.append("SAFETY_INVARIANT_VIOLATION")
    try:
        now = _utc(evaluated_at)
        observed = datetime.fromisoformat(str(proof["observed_at"])).astimezone(UTC)
        expires = datetime.fromisoformat(str(proof["expires_at"])).astimezone(UTC)
        ttl = int(proof["ttl_seconds"])
        sequence = int(proof["sequence"])
    except (KeyError, TypeError, ValueError, OverflowError):
        errors.append("TIMELINE_INVALID")
    else:
        if ttl <= 0 or timedelta(seconds=ttl) > maximum_ttl:
            errors.append("TTL_OUT_OF_POLICY")
        if expires != observed + timedelta(seconds=ttl):
            errors.append("EXPIRATION_MISMATCH")
        if observed > now + MAX_CLOCK_SKEW:
            errors.append("EVIDENCE_FROM_FUTURE")
        if now >= expires:
            errors.append("EVIDENCE_EXPIRED")
        parent = proof.get("parent_proof_sha256")
        if sequence < 1 or (sequence == 1) != (parent is None):
            errors.append("SEQUENCE_PARENT_INVALID")
    return _result(sorted(set(errors)), proof.get("proof_sha256"))


def renew_freshness_proof(
    prior: dict[str, object],
    *,
    trusted_manifest_sha256: str,
    renewed_at: datetime,
    ttl: timedelta = DEFAULT_TTL,
) -> dict[str, object]:
    renewed = _utc(renewed_at)
    verification = verify_freshness_proof(
        prior,
        trusted_manifest_sha256=trusted_manifest_sha256,
        evaluated_at=renewed,
        maximum_ttl=ttl,
    )
    if verification["verdict"] != "PASS":
        raise ValueError("prior proof is not fresh and valid at renewal time")
    observed = datetime.fromisoformat(str(prior["observed_at"])).astimezone(UTC)
    if renewed <= observed:
        raise ValueError("renewal time must advance")
    return issue_freshness_proof(
        aggregate_manifest_sha256=trusted_manifest_sha256,
        observed_at=renewed,
        ttl=ttl,
        sequence=int(prior["sequence"]) + 1,
        parent_proof_sha256=str(prior["proof_sha256"]),
    )


def verify_renewal_chain(
    proofs: list[dict[str, object]],
    *,
    trusted_manifest_sha256: str,
    evaluated_at: datetime,
    maximum_ttl: timedelta = DEFAULT_TTL,
) -> dict[str, object]:
    errors: list[str] = []
    if not proofs:
        return _result(["RENEWAL_CHAIN_EMPTY"], None)
    for index, proof in enumerate(proofs):
        evaluation_time = (
            evaluated_at
            if index == len(proofs) - 1
            else datetime.fromisoformat(str(proofs[index + 1].get("observed_at")))
        )
        verified = verify_freshness_proof(
            proof,
            trusted_manifest_sha256=trusted_manifest_sha256,
            evaluated_at=evaluation_time,
            maximum_ttl=maximum_ttl,
        )
        if verified["verdict"] != "PASS":
            errors.append(f"PROOF_{index + 1}_INVALID")
        if proof.get("sequence") != index + 1:
            errors.append("RENEWAL_SEQUENCE_GAP")
        expected_parent = None if index == 0 else proofs[index - 1].get("proof_sha256")
        if proof.get("parent_proof_sha256") != expected_parent:
            errors.append("RENEWAL_PARENT_MISMATCH")
    chain = {
        "proof_sha256s": [proof.get("proof_sha256") for proof in proofs],
        "trusted_manifest_sha256": trusted_manifest_sha256,
    }
    return _result(sorted(set(errors)), _digest(chain))


def _result(errors: list[str], subject_sha256: object) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "subject_sha256": subject_sha256,
        "safety": _safety(),
    }
    return {**body, "verification_sha256": _digest(body)}


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
