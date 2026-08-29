"""Checkpoint corruption matrix and redundant-copy recovery."""

from __future__ import annotations

import copy
import hashlib
import json

from scripts.local.phase4ov_renewal_resume import resume_from_checkpoint

SCHEMA = "phase4ow.checkpoint-redundancy.v1"
CORRUPTIONS = (
    "BIT_FLIP",
    "TRUNCATION",
    "REORDERING",
    "DUPLICATION",
    "STALE_PREFIX",
    "CROSS_RUN_SUBSTITUTION",
    "ENVELOPE_HASH_DRIFT",
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_replica(replica_id: str, checkpoints: list[dict[str, object]]) -> dict[str, object]:
    body = {
        "schema": SCHEMA,
        "replica_id": replica_id,
        "checkpoint_chain": copy.deepcopy(checkpoints),
        "chain_sha256": _digest(checkpoints),
        "safety": _safety(),
    }
    return {**body, "replica_sha256": _digest(body)}


def corrupt_replica(
    replica: dict[str, object],
    corruption: str,
    *,
    alternate_chain: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if corruption not in CORRUPTIONS:
        raise ValueError("unknown corruption")
    damaged = copy.deepcopy(replica)
    chain = damaged["checkpoint_chain"]
    if corruption == "BIT_FLIP":
        chain[-1]["completed_count"] ^= 1
    elif corruption in {"TRUNCATION", "STALE_PREFIX"}:
        damaged["checkpoint_chain"] = chain[:-1]
    elif corruption == "REORDERING":
        chain[-1], chain[-2] = chain[-2], chain[-1]
    elif corruption == "DUPLICATION":
        chain[-1] = copy.deepcopy(chain[-2])
    elif corruption == "CROSS_RUN_SUBSTITUTION":
        if alternate_chain is None:
            raise ValueError("alternate chain required")
        damaged["checkpoint_chain"][-1] = copy.deepcopy(alternate_chain[-1])
    elif corruption == "ENVELOPE_HASH_DRIFT":
        damaged["replica_sha256"] = "f" * 64
        return damaged
    return damaged


def recover_replicas(
    original_records: list[dict[str, object]],
    replicas: list[dict[str, object]],
    *,
    allowed_replicas: set[str],
    quorum: int,
) -> dict[str, object]:
    errors: list[str] = []
    if quorum < 1 or quorum > len(allowed_replicas):
        errors.append("REPLICA_QUORUM_POLICY_INVALID")
    ids = [row.get("replica_id") for row in replicas]
    if len(ids) != len(set(ids)):
        errors.append("REPLICA_ID_REPLAY")
    groups: dict[str, list[dict[str, object]]] = {}
    invalid_ids = []
    resume_hashes = {}
    for replica in replicas:
        replica_id = str(replica.get("replica_id"))
        unsigned = {key: value for key, value in replica.items() if key != "replica_sha256"}
        valid = replica.get("replica_sha256") == _digest(unsigned)
        valid &= replica.get("schema") == SCHEMA and replica.get("safety") == _safety()
        valid &= replica_id in allowed_replicas
        chain = replica.get("checkpoint_chain")
        valid &= isinstance(chain, list) and replica.get("chain_sha256") == _digest(chain)
        resume = resume_from_checkpoint(original_records, chain if isinstance(chain, list) else [])
        valid &= resume["verdict"] == "PASS"
        if valid:
            history = str(replica["chain_sha256"])
            groups.setdefault(history, []).append(replica)
            resume_hashes[history] = resume["uninterrupted_orchestration_sha256"]
        else:
            invalid_ids.append(replica_id)
    qualified = [(chain_hash, rows) for chain_hash, rows in groups.items() if len(rows) >= quorum]
    if len(qualified) != 1:
        errors.append("UNIQUE_CHECKPOINT_QUORUM_NOT_MET")
    canonical_chain = (
        copy.deepcopy(qualified[0][1][0]["checkpoint_chain"]) if len(qualified) == 1 else None
    )
    repair_plan = []
    if canonical_chain is not None and not errors:
        for replica in replicas:
            if (
                replica.get("checkpoint_chain") != canonical_chain
                or replica.get("replica_id") in invalid_ids
            ):
                repair_plan.append(
                    {
                        "target_replica_id": replica.get("replica_id"),
                        "source_chain_sha256": qualified[0][0],
                        "replacement_chain": copy.deepcopy(canonical_chain),
                    }
                )
    orchestration_hash = resume_hashes.get(qualified[0][0]) if len(qualified) == 1 else None
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if canonical_chain is not None and not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "canonical_chain_sha256": qualified[0][0] if len(qualified) == 1 else None,
        "canonical_chain": canonical_chain if not errors else None,
        "converged_orchestration_sha256": orchestration_hash if not errors else None,
        "invalid_replica_ids": sorted(invalid_ids),
        "repair_plan": sorted(repair_plan, key=lambda row: str(row["target_replica_id"])),
        "settlement_record_unchanged": bool(canonical_chain is not None and not errors),
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "recovery_sha256": _digest(body)}


def run_corruption_matrix(
    original_records,
    checkpoints,
    alternate_chain,
    *,
    allowed_replicas,
    quorum,
) -> dict[str, object]:
    rows = []
    for corruption in CORRUPTIONS:
        replicas = [create_replica(name, checkpoints) for name in sorted(allowed_replicas)]
        replicas[-1] = corrupt_replica(replicas[-1], corruption, alternate_chain=alternate_chain)
        recovery = recover_replicas(
            original_records, replicas, allowed_replicas=allowed_replicas, quorum=quorum
        )
        rows.append(
            {
                "corruption": corruption,
                "verdict": recovery["verdict"],
                "repaired": len(recovery["repair_plan"]) == 1,
                "converged_orchestration_sha256": recovery["converged_orchestration_sha256"],
            }
        )
    errors = []
    if any(row["verdict"] != "PASS" or not row["repaired"] for row in rows):
        errors.append("CORRUPTION_RECOVERY_INCOMPLETE")
    if len({row["converged_orchestration_sha256"] for row in rows}) != 1:
        errors.append("RECOVERY_CONVERGENCE_MISMATCH")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "corruption_count": len(rows),
        "results": rows,
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "matrix_sha256": _digest(body)}


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
