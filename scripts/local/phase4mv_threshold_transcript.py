"""Deterministic threshold-transcript simulation; not production cryptography."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

SCHEMA = "phase4mv.threshold-transcript-simulation.v1"
CONTRIBUTION_SCHEMA = "phase4mv.signer-contribution.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def make_transcript(
    *,
    policy_sha256: str,
    store_sha256: str,
    head_record_sha256: str,
    message_sha256: str,
    signing_round: int,
    signer_key_ids: list[str],
) -> dict[str, object]:
    body: dict[str, object] = {
        "policy_sha256": policy_sha256,
        "store_sha256": store_sha256,
        "head_record_sha256": head_record_sha256,
        "message_sha256": message_sha256,
        "signing_round": signing_round,
        "signer_key_ids": sorted(signer_key_ids),
    }
    return {**body, "transcript_context_sha256": _digest(body)}


def make_contribution(
    transcript: dict[str, object],
    *,
    witness_id: str,
    key_id: str,
    nonce_commitment_sha256: str,
    signed_at: str,
    durability: str = "DURABLE",
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": CONTRIBUTION_SCHEMA,
        "transcript_context_sha256": transcript.get("transcript_context_sha256"),
        "policy_sha256": transcript.get("policy_sha256"),
        "witness_id": witness_id,
        "key_id": key_id,
        "nonce_commitment_sha256": nonce_commitment_sha256,
        "signed_at": signed_at,
        "durability": durability,
    }
    # A deterministic simulation share, deliberately not a cryptographic signature.
    return {**body, "simulated_share_sha256": _digest(body)}


def verify_transcript(
    transcript: object,
    contributions: object,
    policy: object,
) -> dict[str, object]:
    errors: list[str] = []
    if (
        not isinstance(transcript, dict)
        or not isinstance(contributions, list)
        or not isinstance(policy, dict)
    ):
        return _result(["INPUT_INVALID"], [])
    context_body = {
        key: value for key, value in transcript.items() if key != "transcript_context_sha256"
    }
    if transcript.get("transcript_context_sha256") != _digest(context_body):
        errors.append("TRANSCRIPT_CONTEXT_INVALID")
    if transcript.get("policy_sha256") != policy.get("policy_sha256"):
        errors.append("POLICY_VERSION_MISMATCH")
    policy_body = {key: value for key, value in policy.items() if key != "policy_sha256"}
    if policy.get("policy_sha256") != _digest(policy_body):
        errors.append("POLICY_HASH_INVALID")
    subset = transcript.get("signer_key_ids")
    if not isinstance(subset, list) or subset != sorted(set(subset)):
        errors.append("SIGNER_SUBSET_INVALID")
        subset = []
    threshold = policy.get("threshold")
    if type(threshold) is not int or len(subset) < threshold:
        errors.append("SUBSET_BELOW_THRESHOLD")
    key_map = {row.get("key_id"): row for row in policy.get("keys", []) if isinstance(row, dict)}
    accepted: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    seen_nonces: set[str] = set()
    for index, contribution in enumerate(contributions):
        row_errors: list[str] = []
        if not isinstance(contribution, dict):
            errors.append(f"CONTRIBUTION_{index}_INVALID")
            continue
        body = {
            key: value for key, value in contribution.items() if key != "simulated_share_sha256"
        }
        if contribution.get("simulated_share_sha256") != _digest(body):
            row_errors.append("SHARE_INVALID")
        if contribution.get("schema") != CONTRIBUTION_SCHEMA:
            row_errors.append("SCHEMA_INVALID")
        if contribution.get("transcript_context_sha256") != transcript.get(
            "transcript_context_sha256"
        ):
            row_errors.append("TRANSCRIPT_REPLAY_OR_SUBSTITUTION")
        if contribution.get("policy_sha256") != transcript.get("policy_sha256"):
            row_errors.append("MIXED_POLICY_VERSION")
        key_id = contribution.get("key_id")
        if key_id not in subset:
            row_errors.append("SIGNER_SUBSET_SUBSTITUTION")
        if key_id in seen_keys:
            row_errors.append("DUPLICATE_SIGNER")
        seen_keys.add(str(key_id))
        nonce = contribution.get("nonce_commitment_sha256")
        if not _is_hash(nonce):
            row_errors.append("NONCE_INVALID")
        elif nonce in seen_nonces:
            row_errors.append("NONCE_REUSE")
        seen_nonces.add(str(nonce))
        key = key_map.get(key_id)
        signed = _time(contribution.get("signed_at"))
        if not isinstance(key, dict) or key.get("witness_id") != contribution.get("witness_id"):
            row_errors.append("KEY_UNKNOWN_OR_WRONG_WITNESS")
        elif signed is None:
            row_errors.append("SIGNING_TIME_INVALID")
        else:
            start, end = _time(key.get("valid_from")), _time(key.get("valid_until"))
            revoked, compromised = _time(key.get("revoked_at")), _time(key.get("compromised_at"))
            if start is None or end is None or not (start <= signed < end):
                row_errors.append("KEY_NOT_ACTIVE")
            if revoked is not None and signed >= revoked:
                row_errors.append("KEY_REVOKED")
            if compromised is not None and signed >= compromised:
                row_errors.append("KEY_COMPROMISED")
        if contribution.get("durability") != "DURABLE":
            row_errors.append("CONTRIBUTION_NOT_DURABLE")
        if row_errors:
            errors.extend(f"CONTRIBUTION_{index}_{error}" for error in sorted(set(row_errors)))
        else:
            accepted.append(contribution)
    if sorted(str(row.get("key_id")) for row in accepted) != subset:
        errors.append("CONTRIBUTION_SET_INCOMPLETE_OR_DIFFERENT")
    return _result(sorted(set(errors)), accepted)


def resume_after_crash(
    transcript: dict[str, object],
    contributions: list[dict[str, object]],
    policy: dict[str, object],
) -> dict[str, object]:
    durable = [copy.deepcopy(row) for row in contributions if row.get("durability") == "DURABLE"]
    discarded = sum(row.get("durability") == "PREPARED" for row in contributions)
    verification = verify_transcript(transcript, durable, policy)
    result = {
        "verdict": verification["verdict"],
        "errors": verification["errors"],
        "durable_contribution_count": len(durable),
        "prepared_discarded": discarded,
        "durable_contributions_lost": 0,
        "aggregate_sha256": verification.get("aggregate_sha256"),
        "safety": _safety(),
    }
    result["recovery_sha256"] = _digest(result)
    return result


def audit_nonce_reuse(transcripts: object) -> dict[str, object]:
    errors: list[str] = []
    ledger: dict[tuple[str, str], set[str]] = {}
    if not isinstance(transcripts, list):
        transcripts = []
        errors.append("TRANSCRIPTS_INVALID")
    for item in transcripts:
        if not isinstance(item, dict):
            errors.append("TRANSCRIPT_ENTRY_INVALID")
            continue
        context = str(item.get("transcript", {}).get("transcript_context_sha256"))
        for row in item.get("contributions", []):
            key = (str(row.get("key_id")), str(row.get("nonce_commitment_sha256")))
            ledger.setdefault(key, set()).add(context)
    reused = sorted([list(key) for key, contexts in ledger.items() if len(contexts) > 1])
    if reused:
        errors.append("CROSS_TRANSCRIPT_NONCE_REUSE")
    result = {
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "reused": reused,
        "safety": _safety(),
    }
    result["nonce_audit_sha256"] = _digest(result)
    return result


def _result(errors: list[str], accepted: list[dict[str, object]]) -> dict[str, object]:
    share_hashes = sorted(str(row.get("simulated_share_sha256")) for row in accepted)
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "accepted_contribution_count": len(accepted),
        "accepted_share_sha256": share_hashes,
        "aggregate_sha256": _digest(share_hashes) if not errors else None,
        "production_cryptography": False,
        "safety": _safety(),
    }
    result["verification_sha256"] = _digest(result)
    return result


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "cryptographic_signature": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
