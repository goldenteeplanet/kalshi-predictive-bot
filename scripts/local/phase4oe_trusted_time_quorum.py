"""Quorum-backed trusted-time attestations and equivocation detection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from scripts.local.phase4oa_aggregate_release_gate import BLOCKED_ON_SETTLEMENT

SCHEMA = "phase4oe.trusted-time-quorum.v1"
MAX_DISPERSION = timedelta(seconds=30)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def attest_time(
    *,
    witness_id: str,
    round_id: str,
    parent_proof_sha256: str,
    observed_at: datetime,
    watermark: datetime,
) -> dict[str, object]:
    observed = _utc(observed_at)
    floor = _utc(watermark)
    if not witness_id or not round_id:
        raise ValueError("witness and round identifiers are required")
    body = {
        "schema": SCHEMA,
        "witness_id": witness_id,
        "round_id": round_id,
        "parent_proof_sha256": parent_proof_sha256,
        "observed_at": observed.isoformat(),
        "watermark": floor.isoformat(),
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "attestation_sha256": _digest(body)}


def detect_equivocation(attestations: list[dict[str, object]]) -> list[str]:
    signed: dict[tuple[object, object], set[tuple[object, object, object]]] = {}
    for row in attestations:
        key = (row.get("witness_id"), row.get("round_id"))
        statement = (
            row.get("parent_proof_sha256"),
            row.get("observed_at"),
            row.get("watermark"),
        )
        signed.setdefault(key, set()).add(statement)
    return sorted(str(witness) for (witness, _), values in signed.items() if len(values) > 1)


def certify_trusted_time(
    attestations: list[dict[str, object]],
    *,
    allowed_witnesses: set[str],
    quorum: int,
    expected_round_id: str,
    expected_parent_sha256: str,
    prior_watermark: datetime,
    maximum_dispersion: timedelta = MAX_DISPERSION,
) -> dict[str, object]:
    errors: list[str] = []
    floor = _utc(prior_watermark)
    if quorum < 1 or quorum > len(allowed_witnesses):
        errors.append("QUORUM_POLICY_INVALID")
    equivocators = detect_equivocation(attestations)
    if equivocators:
        errors.append("WITNESS_EQUIVOCATION")
    valid: list[dict[str, object]] = []
    identities: list[object] = []
    for row in attestations:
        if not isinstance(row, dict):
            errors.append("ATTESTATION_NOT_OBJECT")
            continue
        identities.append(row.get("witness_id"))
        unsigned = {key: value for key, value in row.items() if key != "attestation_sha256"}
        if row.get("attestation_sha256") != _digest(unsigned):
            errors.append("ATTESTATION_HASH_MISMATCH")
            continue
        if row.get("schema") != SCHEMA:
            errors.append("ATTESTATION_SCHEMA_INVALID")
        if row.get("witness_id") not in allowed_witnesses:
            errors.append("WITNESS_NOT_ALLOWED")
        if row.get("round_id") != expected_round_id:
            errors.append("ROUND_MISMATCH")
        if row.get("parent_proof_sha256") != expected_parent_sha256:
            errors.append("PARENT_MISMATCH")
        if row.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
            errors.append("SETTLEMENT_BLOCKER_DRIFT")
        if row.get("safety") != _safety():
            errors.append("SAFETY_INVARIANT_VIOLATION")
        try:
            observed = datetime.fromisoformat(str(row["observed_at"])).astimezone(UTC)
            watermark = datetime.fromisoformat(str(row["watermark"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            errors.append("ATTESTATION_TIME_INVALID")
            continue
        if watermark != floor or observed < floor:
            errors.append("WATERMARK_MISMATCH_OR_ROLLBACK")
        valid.append({**row, "_observed": observed})
    if len(identities) != len(set(identities)):
        errors.append("DUPLICATE_WITNESS_IDENTITY")
    valid_identities = {str(row["witness_id"]) for row in valid}
    if len(valid_identities) < quorum:
        errors.append("QUORUM_NOT_MET")
    times = sorted(row["_observed"] for row in valid)
    if times and times[-1] - times[0] > maximum_dispersion:
        errors.append("CLOCK_DISPERSION_EXCEEDED")
    trusted_time = None
    if times and not errors:
        trusted_time = times[(len(times) - 1) // 2].isoformat()
    public_rows = [
        {key: value for key, value in row.items() if key != "_observed"} for row in valid
    ]
    public_rows.sort(key=lambda row: (str(row["witness_id"]), str(row["attestation_sha256"])))
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if trusted_time is not None else "REFUSE",
        "errors": sorted(set(errors)),
        "round_id": expected_round_id,
        "parent_proof_sha256": expected_parent_sha256,
        "quorum": quorum,
        "allowed_witnesses": sorted(allowed_witnesses),
        "attestations": public_rows,
        "equivocators": equivocators,
        "trusted_time": trusted_time,
        "prior_watermark": floor.isoformat(),
        "next_watermark": trusted_time or floor.isoformat(),
        "blocked_on_september_1_settlement": BLOCKED_ON_SETTLEMENT,
        "safety": _safety(),
    }
    return {**body, "certificate_sha256": _digest(body)}


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
