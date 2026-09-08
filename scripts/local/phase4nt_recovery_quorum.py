"""Independent checkpoint witnesses, quorum selection, and split-brain proof."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4ns_checkpoint_recovery import recover_newest_checkpoint

SCHEMA = "phase4nt.recovery-quorum.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def default_registry() -> dict[str, object]:
    return {
        "witness-a": {"status": "ACTIVE", "keys": {"v1": "alpha-key"}, "active_versions": ["v1"]},
        "witness-b": {"status": "ACTIVE", "keys": {"v1": "bravo-key"}, "active_versions": ["v1"]},
        "witness-c": {
            "status": "ACTIVE",
            "keys": {"v1": "charlie-old", "v2": "charlie-new"},
            "active_versions": ["v2"],
        },
        "witness-revoked": {
            "status": "REVOKED",
            "keys": {"v1": "revoked-key"},
            "active_versions": [],
        },
    }


def attest_checkpoint(
    checkpoint: dict[str, object],
    *,
    witness_id: str,
    key_version: str,
    registry: dict[str, object],
    issued_epoch: int,
) -> dict[str, object]:
    witness = registry[witness_id]
    body = {
        "schema": SCHEMA,
        "witness_id": witness_id,
        "key_version": key_version,
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "checkpoint_epoch": checkpoint["next_epoch"],
        "workload_sha256": checkpoint["workload_sha256"],
        "cumulative_sha256": checkpoint["cumulative_sha256"],
        "safety_sha256": _digest(_safety()),
        "issued_epoch": issued_epoch,
    }
    signature = _digest({"key": witness["keys"][key_version], "attestation": body})
    return {**body, "signature_sha256": signature}


def _verify_attestation(attestation, checkpoint_by_hash, registry, workload_hash):
    errors = []
    if not isinstance(attestation, dict):
        return ["ATTESTATION_SHAPE_INVALID"]
    witness_id, version = attestation.get("witness_id"), attestation.get("key_version")
    witness = registry.get(witness_id)
    if not isinstance(witness, dict):
        return ["UNKNOWN_WITNESS"]
    if witness.get("status") != "ACTIVE":
        errors.append("REVOKED_WITNESS")
    if version not in witness.get("active_versions", []):
        errors.append("WITNESS_KEY_VERSION_REVOKED")
    checkpoint = checkpoint_by_hash.get(attestation.get("checkpoint_sha256"))
    if checkpoint is None:
        errors.append("UNKNOWN_CHECKPOINT")
    if attestation.get("workload_sha256") != workload_hash:
        errors.append("MIXED_WORKLOAD_ATTESTATION")
    if attestation.get("safety_sha256") != _digest(_safety()):
        errors.append("SAFETY_ATTESTATION_MISMATCH")
    key = witness.get("keys", {}).get(version)
    unsigned = {
        key_name: value for key_name, value in attestation.items() if key_name != "signature_sha256"
    }
    if key is None or attestation.get("signature_sha256") != _digest(
        {"key": key, "attestation": unsigned}
    ):
        errors.append("FORGED_WITNESS_SIGNATURE")
    if checkpoint is not None and (
        attestation.get("checkpoint_epoch") != checkpoint["next_epoch"]
        or attestation.get("cumulative_sha256") != checkpoint["cumulative_sha256"]
    ):
        errors.append("ATTESTATION_CHECKPOINT_MISMATCH")
    return sorted(set(errors))


def select_quorum_checkpoint(
    workload: dict[str, object],
    checkpoints: list[dict[str, object]],
    attestations: list[object],
    records: list[dict[str, object]],
    *,
    registry: dict[str, object],
    threshold: int,
    maximum_staleness_epochs: int,
    maximum_rollback_epochs: int,
) -> dict[str, object]:
    errors, rejected = [], []
    if threshold < 1 or threshold > len(
        [row for row in registry.values() if row.get("status") == "ACTIVE"]
    ):
        return _result(["QUORUM_THRESHOLD_INVALID"], None, [], None)
    checkpoint_by_hash = {row["checkpoint_sha256"]: row for row in checkpoints}
    valid = []
    seen_votes = set()
    signer_claims = {}
    for ordinal, attestation in enumerate(attestations):
        reasons = _verify_attestation(
            attestation, checkpoint_by_hash, registry, workload["workload_sha256"]
        )
        if isinstance(attestation, dict):
            vote = (attestation.get("witness_id"), attestation.get("checkpoint_sha256"))
            if vote in seen_votes:
                reasons.append("DUPLICATE_SIGNER_VOTE")
            seen_votes.add(vote)
            claim_key = (attestation.get("witness_id"), attestation.get("checkpoint_epoch"))
            claim = (attestation.get("workload_sha256"), attestation.get("cumulative_sha256"))
            if claim_key in signer_claims and signer_claims[claim_key] != claim:
                reasons.append("WITNESS_EQUIVOCATION")
            signer_claims[claim_key] = claim
        if reasons:
            rejected.append({"ordinal": ordinal, "errors": sorted(set(reasons))})
            errors.extend(
                code
                for code in reasons
                if code in {"DUPLICATE_SIGNER_VOTE", "WITNESS_EQUIVOCATION"}
            )
        else:
            valid.append(attestation)
    groups: dict[tuple[object, ...], set[str]] = {}
    for row in valid:
        key = (row["checkpoint_epoch"], row["workload_sha256"], row["cumulative_sha256"])
        groups.setdefault(key, set()).add(row["witness_id"])
    hashes_by_epoch = {}
    for (epoch, _, cumulative), signers in groups.items():
        if len(signers) >= threshold:
            hashes_by_epoch.setdefault(epoch, set()).add(cumulative)
    if any(len(values) > 1 for values in hashes_by_epoch.values()):
        errors.append("SPLIT_BRAIN_QUORUM")
    certified = [key for key, signers in groups.items() if len(signers) >= threshold]
    if not certified:
        errors.append("INSUFFICIENT_QUORUM")
    if errors:
        return _result(sorted(set(errors)), None, rejected, None)
    selected_key = max(certified, key=lambda row: row[0])
    selected = next(
        checkpoint
        for checkpoint in checkpoints
        if checkpoint["next_epoch"] == selected_key[0]
        and checkpoint["cumulative_sha256"] == selected_key[2]
    )
    newest_epoch = max(row["next_epoch"] for row in checkpoints)
    if newest_epoch - selected["next_epoch"] > maximum_staleness_epochs:
        return _result(["STALE_QUORUM"], None, rejected, None)
    recovery = recover_newest_checkpoint(
        workload, [selected], records, maximum_rollback_epochs=maximum_rollback_epochs
    )
    if recovery["verdict"] != "PASS":
        return _result(["QUORUM_RECOVERY_FAILED", *recovery["errors"]], None, rejected, recovery)
    return _result([], selected, rejected, recovery)


def run_quorum_matrix(workload, checkpoints, records, *, registry=None):
    registry = copy.deepcopy(registry or default_registry())
    latest, older = checkpoints[-1], checkpoints[-2]

    def a(cp, who, version="v1"):
        return attest_checkpoint(
            cp,
            witness_id=who,
            key_version=version,
            registry=registry,
            issued_epoch=cp["next_epoch"],
        )

    healthy = [a(latest, "witness-a"), a(latest, "witness-b")]
    forged = copy.deepcopy(healthy[0])
    forged["signature_sha256"] = "0" * 64
    cases = [
        ("HEALTHY", healthy, "PASS", 10),
        ("WITNESS_LOSS", healthy[:1], "REFUSE", 10),
        ("DELAYED_STALE", [a(older, "witness-a"), a(older, "witness-b")], "REFUSE", 5),
        ("KEY_ROTATION_OLD", [a(latest, "witness-a"), a(latest, "witness-c", "v1")], "REFUSE", 10),
        ("QUORUM_RESTORED", [a(latest, "witness-a"), a(latest, "witness-c", "v2")], "PASS", 10),
        ("FORGED_SIGNATURE", [healthy[1], forged], "REFUSE", 10),
        ("DUPLICATE_VOTE", [healthy[0], healthy[0]], "REFUSE", 10),
    ]
    rows, errors = [], []
    for name, attestations, expected, staleness in cases:
        outcome = select_quorum_checkpoint(
            workload,
            checkpoints,
            attestations,
            records,
            registry=registry,
            threshold=2,
            maximum_staleness_epochs=staleness,
            maximum_rollback_epochs=len(workload["epochs"]),
        )
        if outcome["verdict"] != expected:
            errors.append("QUORUM_MATRIX_EXPECTATION_MISMATCH")
        rows.append(
            {
                "case": name,
                "expected": expected,
                "observed": outcome["verdict"],
                "errors": outcome["errors"],
                "outcome_sha256": outcome["outcome_sha256"],
            }
        )
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "case_count": len(rows),
        "cases": rows,
        "safety": _safety(),
    }
    result["matrix_sha256"] = _digest(result)
    return result


def _result(errors, selected, rejected, recovery):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "selected_epoch": selected.get("next_epoch") if selected else None,
        "selected_checkpoint_sha256": selected.get("checkpoint_sha256") if selected else None,
        "rejected_attestations": rejected,
        "recovery_final_sha256": recovery.get("final_sha256") if recovery else None,
        "recovery_coverage_sha256": recovery.get("coverage_sha256") if recovery else None,
        "recovery_refusal_classes": recovery.get("refusal_classes") if recovery else None,
        "recovery_provenance_sha256": recovery.get("provenance_sha256") if recovery else None,
        "safety": _safety(),
    }
    result["outcome_sha256"] = _digest(result)
    return result


def _safety():
    return {
        "offline_only": True,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
