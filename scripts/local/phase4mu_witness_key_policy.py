"""Offline witness-key lifecycle, policy versioning, and compromise recovery."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

SCHEMA = "phase4mu.witness-policy-chain.v1"
POLICY_SCHEMA = "phase4mu.witness-threshold-policy.v1"
ATTESTATION_SCHEMA = "phase4mu.key-bound-attestation.v1"


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


def make_key(
    witness_id: str,
    key_id: str,
    independence_group: str,
    *,
    valid_from: str,
    valid_until: str,
    revoked_at: str | None = None,
    compromised_at: str | None = None,
) -> dict[str, object]:
    body = {
        "witness_id": witness_id,
        "key_id": key_id,
        "independence_group": independence_group,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "revoked_at": revoked_at,
        "compromised_at": compromised_at,
    }
    return {**body, "key_record_sha256": _digest(body)}


def make_policy(
    *,
    version: int,
    threshold: int,
    keys: list[dict[str, object]],
    effective_at: str,
    previous_policy_sha256: str = "0" * 64,
    emergency: bool = False,
    emergency_justification_sha256: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": POLICY_SCHEMA,
        "version": version,
        "threshold": threshold,
        "keys": keys,
        "effective_at": effective_at,
        "previous_policy_sha256": previous_policy_sha256,
        "emergency": emergency,
        "emergency_justification_sha256": emergency_justification_sha256,
    }
    return {**body, "policy_sha256": _digest(body)}


def validate_policy_chain(
    policies: object, *, expected_head_policy_sha256: str
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(policies, list) or not policies:
        return _result(["POLICIES_INVALID"])
    previous_hash = "0" * 64
    previous_time = None
    previous_threshold = None
    seen_keys: dict[str, tuple[str, str]] = {}
    for index, policy in enumerate(policies):
        prefix = f"POLICY_{index}_"
        if not isinstance(policy, dict):
            errors.append(prefix + "INVALID")
            continue
        body = {key: value for key, value in policy.items() if key != "policy_sha256"}
        if policy.get("schema") != POLICY_SCHEMA or policy.get("policy_sha256") != _digest(body):
            errors.append(prefix + "HASH_OR_SCHEMA_INVALID")
        if policy.get("version") != index + 1:
            errors.append(prefix + "VERSION_INVALID")
        if policy.get("previous_policy_sha256") != previous_hash:
            errors.append(prefix + "PREDECESSOR_INVALID")
        effective = _time(policy.get("effective_at"))
        if effective is None or (previous_time is not None and effective <= previous_time):
            errors.append(prefix + "EFFECTIVE_TIME_INVALID")
        threshold = policy.get("threshold")
        keys = policy.get("keys")
        if type(threshold) is not int or threshold < 1 or not isinstance(keys, list):
            errors.append(prefix + "THRESHOLD_OR_KEYS_INVALID")
            keys = []
        groups: set[str] = set()
        for key_index, record in enumerate(keys):
            if not isinstance(record, dict):
                errors.append(prefix + f"KEY_{key_index}_INVALID")
                continue
            key_body = {
                name: value for name, value in record.items() if name != "key_record_sha256"
            }
            if record.get("key_record_sha256") != _digest(key_body):
                errors.append(prefix + f"KEY_{key_index}_HASH_INVALID")
            start, end = _time(record.get("valid_from")), _time(record.get("valid_until"))
            if start is None or end is None or start >= end:
                errors.append(prefix + f"KEY_{key_index}_WINDOW_INVALID")
            groups.add(str(record.get("independence_group")))
            identity = (str(record.get("witness_id")), str(record.get("independence_group")))
            key_id = str(record.get("key_id"))
            if key_id in seen_keys and seen_keys[key_id] != identity:
                errors.append(prefix + f"KEY_{key_index}_IDENTITY_REUSE")
            seen_keys[key_id] = identity
        if isinstance(threshold, int) and threshold > len(groups):
            errors.append(prefix + "THRESHOLD_UNSATISFIABLE")
        if (
            previous_threshold is not None
            and isinstance(threshold, int)
            and threshold < previous_threshold
        ):
            if policy.get("emergency") is not True or not _is_hash(
                policy.get("emergency_justification_sha256")
            ):
                errors.append(prefix + "DOWNGRADE_REQUIRES_EMERGENCY_EVIDENCE")
        previous_hash = policy.get("policy_sha256")
        previous_time = effective
        previous_threshold = threshold
    if previous_hash != expected_head_policy_sha256:
        errors.append("PINNED_POLICY_HEAD_MISMATCH")
    result = _result(sorted(set(errors)))
    result["head_policy_sha256"] = previous_hash
    result["policy_count"] = len(policies)
    result["chain_sha256"] = _digest(result)
    return result


def make_attestation(
    *,
    witness_id: str,
    key_id: str,
    policy_sha256: str,
    store_sha256: str,
    head_record_sha256: str,
    observed_at: str,
) -> dict[str, object]:
    body = {
        "schema": ATTESTATION_SCHEMA,
        "witness_id": witness_id,
        "key_id": key_id,
        "policy_sha256": policy_sha256,
        "store_sha256": store_sha256,
        "head_record_sha256": head_record_sha256,
        "observed_at": observed_at,
    }
    return {**body, "attestation_sha256": _digest(body)}


def verify_attestations(
    attestations: object,
    policy: object,
    *,
    expected_policy_sha256: str,
    expected_store_sha256: str,
    expected_head_record_sha256: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(attestations, list) or not isinstance(policy, dict):
        return _result(["INPUT_INVALID"])
    if policy.get("policy_sha256") != expected_policy_sha256:
        errors.append("POLICY_PIN_MISMATCH")
    key_map = {row.get("key_id"): row for row in policy.get("keys", []) if isinstance(row, dict)}
    groups: set[str] = set()
    witnesses: set[str] = set()
    for index, attestation in enumerate(attestations):
        row_errors: list[str] = []
        if not isinstance(attestation, dict):
            errors.append(f"ATTESTATION_{index}_INVALID")
            continue
        body = {key: value for key, value in attestation.items() if key != "attestation_sha256"}
        if attestation.get("attestation_sha256") != _digest(body):
            row_errors.append("HASH_INVALID")
        if attestation.get("policy_sha256") != expected_policy_sha256:
            row_errors.append("POLICY_VERSION_REPLAY")
        if (
            attestation.get("store_sha256") != expected_store_sha256
            or attestation.get("head_record_sha256") != expected_head_record_sha256
        ):
            row_errors.append("STORE_HEAD_MISMATCH")
        key = key_map.get(attestation.get("key_id"))
        observed = _time(attestation.get("observed_at"))
        if not isinstance(key, dict) or key.get("witness_id") != attestation.get("witness_id"):
            row_errors.append("KEY_UNKNOWN_OR_WRONG_WITNESS")
        elif observed is None:
            row_errors.append("OBSERVATION_TIME_INVALID")
        else:
            start, end = _time(key.get("valid_from")), _time(key.get("valid_until"))
            revoked, compromised = _time(key.get("revoked_at")), _time(key.get("compromised_at"))
            if start is None or end is None or not (start <= observed < end):
                row_errors.append("KEY_NOT_ACTIVE")
            if revoked is not None and observed >= revoked:
                row_errors.append("KEY_REVOKED")
            if compromised is not None and observed >= compromised:
                row_errors.append("KEY_COMPROMISED")
        witness = str(attestation.get("witness_id"))
        if witness in witnesses:
            row_errors.append("DUPLICATE_WITNESS")
        witnesses.add(witness)
        if row_errors:
            errors.extend(f"ATTESTATION_{index}_{error}" for error in sorted(set(row_errors)))
        elif key is not None:
            groups.add(str(key.get("independence_group")))
    threshold = policy.get("threshold")
    if type(threshold) is not int or len(groups) < threshold:
        errors.append("INSUFFICIENT_POLICY_QUORUM")
    result = _result(sorted(set(errors)))
    result["verified_groups"] = sorted(groups)
    result["quorum_attested"] = not errors
    result["verification_sha256"] = _digest(result)
    return result


def certify_compromise_recovery(
    old_policy: dict[str, object], new_policy: dict[str, object], *, compromised_key_ids: list[str]
) -> dict[str, object]:
    errors: list[str] = []
    old = {row.get("key_id"): row for row in old_policy.get("keys", []) if isinstance(row, dict)}
    new = {row.get("key_id"): row for row in new_policy.get("keys", []) if isinstance(row, dict)}
    compromised_groups: set[str] = set()
    for key_id in compromised_key_ids:
        if key_id not in old:
            errors.append("COMPROMISED_KEY_UNKNOWN")
        else:
            compromised_groups.add(str(old[key_id].get("independence_group")))
        if key_id in new and new[key_id].get("compromised_at") is None:
            errors.append("COMPROMISED_KEY_REMAINS_ACTIVE")
    replacements = {
        str(row.get("independence_group"))
        for key_id, row in new.items()
        if key_id not in compromised_key_ids
        and str(row.get("independence_group")) in compromised_groups
    }
    if replacements != compromised_groups:
        errors.append("REPLACEMENT_COVERAGE_INCOMPLETE")
    if new_policy.get("threshold", 0) < old_policy.get("threshold", 0):
        errors.append("RECOVERY_THRESHOLD_WEAKENED")
    result = _result(sorted(set(errors)))
    result.update(
        {
            "compromised_groups": sorted(compromised_groups),
            "replacement_groups": sorted(replacements),
            "recovery_certified": not errors,
            "acceptance_authorized": False,
            "repair_authorized": False,
        }
    )
    result["recovery_sha256"] = _digest(result)
    return result


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _result(errors: list[str]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "safety": _safety(),
    }


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
