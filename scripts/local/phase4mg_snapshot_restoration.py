"""Offline snapshot migration and retained-suffix restoration simulator."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime

from scripts.local.phase4me_nonce_consumption_ledger import FIELDS as RECORD_FIELDS
from scripts.local.phase4mf_nonce_ledger_snapshot import (
    FIELDS as SNAPSHOT_FIELDS,
)
from scripts.local.phase4mf_nonce_ledger_snapshot import (
    IN_FLIGHT,
    TERMINAL,
)
from scripts.local.phase4mf_nonce_ledger_snapshot import (
    SCHEMA as SNAPSHOT_SCHEMA,
)
from scripts.local.phase4mf_nonce_ledger_snapshot import (
    _body_hash as snapshot_body_hash,
)

SCHEMA = "phase4mg.snapshot-restoration-artifact.v2"
RESTORATION_SCHEMA = "phase4mg.snapshot-restoration-result.v1"
MIGRATION_AUDIT_SCHEMA = "phase4mg.snapshot-migration-lineage-audit.v1"
MIGRATION_ID = "phase4mf.v1-to-phase4mg.v2"
RESTORATION_POLICY = "phase4mg.strict-contiguous-suffix.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
FIELDS = {
    "schema",
    "version",
    "source_snapshot_sha256",
    "source_generation",
    "source_head_sha256",
    "reserved_or_consumed_nonce_sha256",
    "transactions",
    "prior_snapshot_sha256",
    "restoration_policy_version",
    "migration_lineage",
    "artifact_sha256",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _body_hash(value: dict[str, object]) -> str:
    return _digest({key: item for key, item in value.items() if key != "artifact_sha256"})


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _transaction_errors(transactions: object, nonces: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(transactions, dict):
        return ["TRANSACTIONS_INVALID"]
    owners: dict[str, str] = {}
    for transaction_id, transaction in transactions.items():
        if not isinstance(transaction_id, str) or not isinstance(transaction, dict):
            errors.append("TRANSACTION_INVALID")
            continue
        if set(transaction) != {"state", "token_id", "nonce_sha256", "receipt_sha256"}:
            errors.append("TRANSACTION_FIELDS_INVALID")
            continue
        state = transaction.get("state")
        if state not in TERMINAL | IN_FLIGHT:
            errors.append("TRANSACTION_STATE_INVALID")
        if (
            HEX64.fullmatch(str(transaction.get("token_id"))) is None
            or HEX64.fullmatch(str(transaction.get("nonce_sha256"))) is None
        ):
            errors.append("TRANSACTION_IDENTITY_INVALID")
        receipt = transaction.get("receipt_sha256")
        if state in {"CONSUMED", "COMMITTED"}:
            if HEX64.fullmatch(str(receipt)) is None:
                errors.append("TRANSACTION_RECEIPT_INVALID")
        elif receipt is not None:
            errors.append("TRANSACTION_RECEIPT_INVALID")
        nonce = str(transaction.get("nonce_sha256"))
        if nonce in owners and owners[nonce] != transaction_id:
            errors.append("DUPLICATE_NONCE_OWNERSHIP")
        owners[nonce] = transaction_id
    if not isinstance(nonces, list) or nonces != sorted(owners):
        errors.append("NONCE_SET_LOSS_OR_EXPANSION")
    return errors


def artifact_errors(artifact: object) -> list[str]:
    if not isinstance(artifact, dict):
        return ["ARTIFACT_NOT_OBJECT"]
    errors: list[str] = []
    if set(artifact) != FIELDS:
        errors.append("ARTIFACT_FIELD_SET_INVALID")
    if artifact.get("schema") != SCHEMA or artifact.get("version") != 2:
        errors.append("ARTIFACT_VERSION_INVALID")
    for field in (
        "source_snapshot_sha256",
        "source_head_sha256",
        "prior_snapshot_sha256",
        "artifact_sha256",
    ):
        if HEX64.fullmatch(str(artifact.get(field))) is None:
            errors.append(f"{field.upper()}_INVALID")
    if artifact.get("artifact_sha256") != _body_hash(artifact):
        errors.append("ARTIFACT_HASH_INVALID")
    if (
        type(artifact.get("source_generation")) is not int
        or artifact.get("source_generation", -1) < 0
    ):
        errors.append("SOURCE_GENERATION_INVALID")
    if artifact.get("restoration_policy_version") != RESTORATION_POLICY:
        errors.append("RESTORATION_POLICY_INVALID")
    lineage = artifact.get("migration_lineage")
    if lineage != [{"from_version": 1, "to_version": 2, "migration_id": MIGRATION_ID}]:
        errors.append("MIGRATION_LINEAGE_INVALID")
    errors.extend(
        _transaction_errors(
            artifact.get("transactions"), artifact.get("reserved_or_consumed_nonce_sha256")
        )
    )
    return sorted(set(errors))


def migrate_snapshot(snapshot: object, *, from_version: int, to_version: int) -> dict[str, object]:
    if from_version == 2 and to_version == 2:
        if artifact_errors(snapshot):
            raise ValueError("invalid v2 artifact")
        return copy.deepcopy(snapshot)  # type: ignore[arg-type]
    if (from_version, to_version) != (1, 2):
        raise ValueError("unknown, skipped, or downgrade migration")
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != SNAPSHOT_FIELDS
        or snapshot.get("schema") != SNAPSHOT_SCHEMA
        or snapshot.get("snapshot_sha256") != snapshot_body_hash(snapshot)
    ):
        raise ValueError("invalid Phase 4MF snapshot")
    if _transaction_errors(
        snapshot.get("transactions"), snapshot.get("reserved_or_consumed_nonce_sha256")
    ):
        raise ValueError("snapshot loses nonce or transaction state")
    body: dict[str, object] = {
        "schema": SCHEMA,
        "version": 2,
        "source_snapshot_sha256": snapshot["snapshot_sha256"],
        "source_generation": snapshot["source_generation"],
        "source_head_sha256": snapshot["source_head_sha256"],
        "reserved_or_consumed_nonce_sha256": copy.deepcopy(
            snapshot["reserved_or_consumed_nonce_sha256"]
        ),
        "transactions": copy.deepcopy(snapshot["transactions"]),
        "prior_snapshot_sha256": snapshot["prior_snapshot_sha256"],
        "restoration_policy_version": RESTORATION_POLICY,
        "migration_lineage": [{"from_version": 1, "to_version": 2, "migration_id": MIGRATION_ID}],
    }
    return {**body, "artifact_sha256": _digest(body)}


def restore_snapshot(
    artifact: object,
    retained_suffix: object,
    *,
    evaluated_at: str,
    expected_snapshot_sha256: str,
    expected_source_generation: int,
    expected_source_head_sha256: str,
) -> dict[str, object]:
    errors = artifact_errors(artifact)
    source_sha256 = _digest(retained_suffix)
    if not isinstance(artifact, dict):
        artifact = {}
    if artifact.get("source_snapshot_sha256") != expected_snapshot_sha256:
        errors.append("SNAPSHOT_ANCHOR_SUBSTITUTION")
    if artifact.get("source_generation") != expected_source_generation:
        errors.append("SOURCE_GENERATION_ANCHOR_SUBSTITUTION")
    if artifact.get("source_head_sha256") != expected_source_head_sha256:
        errors.append("SOURCE_HEAD_ANCHOR_SUBSTITUTION")
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    if not isinstance(retained_suffix, list):
        errors.append("SUFFIX_NOT_LIST")
        retained_suffix = []
    generation = artifact.get("source_generation", 0)
    head = artifact.get("source_head_sha256", "0" * 64)
    transactions = copy.deepcopy(artifact.get("transactions", {}))
    nonces = set(artifact.get("reserved_or_consumed_nonce_sha256", []))
    previous_time: datetime | None = None
    identities: dict[str, str] = {}
    accepted = 0
    for index, candidate in enumerate(retained_suffix):
        row_errors: list[str] = []
        if not isinstance(candidate, dict) or set(candidate) != RECORD_FIELDS:
            row_errors.append("FIELD_SET_INVALID")
            candidate = {}
        record_id = candidate.get("record_id")
        fingerprint = _digest(candidate)
        if record_id in identities:
            if identities[str(record_id)] == fingerprint:
                continue
            row_errors.append("CONFLICTING_REPLAY")
        identities[str(record_id)] = fingerprint
        body = {key: value for key, value in candidate.items() if key != "record_sha256"}
        if candidate.get("record_sha256") != _digest(body):
            row_errors.append("RECORD_HASH_INVALID")
        if candidate.get("expected_generation") != generation:
            row_errors.append("GENERATION_GAP_OR_ROLLBACK")
        if candidate.get("generation") != generation + 1:
            row_errors.append("GENERATION_GAP_OR_ROLLBACK")
        if candidate.get("previous_record_sha256") != head:
            row_errors.append("SUFFIX_ANCHOR_INCOMPATIBLE")
        occurred = _time(candidate.get("occurred_at"))
        if occurred is None or (now is not None and occurred > now):
            row_errors.append("RECORD_TIME_INVALID")
        elif previous_time is not None and occurred < previous_time:
            row_errors.append("RECORD_TIME_REVERSED")
        operation = candidate.get("operation")
        transaction_id = candidate.get("transaction_id")
        transaction = transactions.get(transaction_id)
        token = candidate.get("token_id")
        nonce = candidate.get("nonce_sha256")
        receipt = candidate.get("receipt_sha256")
        if operation == "PREPARE":
            if transaction is not None:
                row_errors.append("TRANSACTION_ALREADY_EXISTS")
            if nonce in nonces:
                row_errors.append("DUPLICATE_NONCE_OWNERSHIP")
        elif transaction is None:
            row_errors.append("TRANSACTION_NOT_RESTORED")
        else:
            if transaction.get("token_id") != token or transaction.get("nonce_sha256") != nonce:
                row_errors.append("TOKEN_OR_NONCE_SUBSTITUTION")
            state = transaction.get("state")
            if operation == "DURABLE_CONSUME" and state != "PREPARED":
                row_errors.append("STATE_WIDENING_OR_INVALID_TRANSITION")
            elif operation == "COMMIT" and (
                state != "CONSUMED" or transaction.get("receipt_sha256") != receipt
            ):
                row_errors.append("STATE_WIDENING_OR_RECEIPT_SUBSTITUTION")
            elif operation == "ABORT" and state != "PREPARED":
                row_errors.append("STATE_WIDENING_OR_INVALID_TRANSITION")
            elif operation not in {"DURABLE_CONSUME", "COMMIT", "ABORT"}:
                row_errors.append("OPERATION_INVALID")
        if row_errors:
            errors.extend(f"SUFFIX_{index}_{error}" for error in sorted(set(row_errors)))
            break
        if operation == "PREPARE":
            transactions[transaction_id] = {
                "state": "PREPARED",
                "token_id": token,
                "nonce_sha256": nonce,
                "receipt_sha256": None,
            }
            nonces.add(nonce)
        elif operation == "DURABLE_CONSUME":
            transactions[transaction_id]["state"] = "CONSUMED"
            transactions[transaction_id]["receipt_sha256"] = receipt
        elif operation == "COMMIT":
            transactions[transaction_id]["state"] = "COMMITTED"
        elif operation == "ABORT":
            transactions[transaction_id]["state"] = "ABORTED"
        generation += 1
        head = candidate["record_sha256"]
        previous_time = occurred
        accepted += 1
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": RESTORATION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "artifact_sha256": artifact.get("artifact_sha256"),
        "suffix_sha256": source_sha256,
        "suffix_unchanged": _digest(retained_suffix) == source_sha256,
        "accepted_suffix_count": accepted,
        "generation": generation,
        "head_sha256": head,
        "reserved_or_consumed_nonce_sha256": sorted(nonces),
        "transactions": {key: transactions[key] for key in sorted(transactions)},
        "safety": {
            "simulation_only": True,
            "production_write": False,
            "snapshot_persistence": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["restoration_sha256"] = _digest(result)
    return result


def audit_migration_lineages(artifacts: object) -> dict[str, object]:
    errors: list[str] = []
    seen: dict[tuple[str, int], str] = {}
    if not isinstance(artifacts, list):
        errors.append("ARTIFACTS_NOT_LIST")
        artifacts = []
    for index, artifact in enumerate(artifacts):
        row_errors = artifact_errors(artifact)
        if row_errors:
            errors.extend(f"ARTIFACT_{index}_{error}" for error in row_errors)
            break
        key = (artifact["source_snapshot_sha256"], artifact["version"])
        prior = seen.get(key)
        if prior is not None and prior != artifact["artifact_sha256"]:
            errors.append(f"ARTIFACT_{index}_DIVERGENT_MIGRATION_PATH")
            break
        seen[key] = artifact["artifact_sha256"]
    result: dict[str, object] = {
        "schema": MIGRATION_AUDIT_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "artifact_count": len(artifacts),
        "unique_source_version_count": len(seen),
        "read_only": True,
    }
    result["audit_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot")
    args = parser.parse_args()
    with open(args.snapshot, encoding="utf-8") as stream:
        snapshot = json.load(stream)
    try:
        result = migrate_snapshot(snapshot, from_version=1, to_version=2)
    except ValueError as error:
        print(json.dumps({"verdict": "REFUSE", "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
