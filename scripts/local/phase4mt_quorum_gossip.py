"""Independent trust-store witnesses and deterministic gossip consistency proof."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

SCHEMA = "phase4mt.trust-store-quorum.v1"
STATEMENT_SCHEMA = "phase4mt.witness-statement.v1"
GOSSIP_SCHEMA = "phase4mt.gossip-consistency.v1"


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


def make_statement(
    *,
    witness_id: str,
    generation: int,
    head_record_sha256: str,
    store_sha256: str,
    implementation_identity_sha256: str,
    observed_at: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema": STATEMENT_SCHEMA,
        "witness_id": witness_id,
        "generation": generation,
        "head_record_sha256": head_record_sha256,
        "store_sha256": store_sha256,
        "implementation_identity_sha256": implementation_identity_sha256,
        "observed_at": observed_at,
    }
    return {**body, "statement_sha256": _digest(body)}


def evaluate_quorum(
    statements: object,
    registry: object,
    *,
    threshold: int,
    expected_generation: int,
    expected_head_record_sha256: str,
    expected_store_sha256: str,
    expected_implementation_identity_sha256: str,
    evaluated_at: str,
    max_age_seconds: int,
) -> dict[str, object]:
    errors: list[str] = []
    now = _time(evaluated_at)
    if (
        not isinstance(statements, list)
        or not isinstance(registry, dict)
        or type(threshold) is not int
        or threshold < 1
        or type(max_age_seconds) is not int
        or max_age_seconds < 0
        or now is None
    ):
        return _result(["INPUT_INVALID"], [], threshold)
    valid: list[dict[str, object]] = []
    fingerprints: dict[str, set[str]] = {}
    seen_exact: set[str] = set()
    for index, statement in enumerate(statements):
        row_errors: list[str] = []
        if not isinstance(statement, dict):
            errors.append(f"STATEMENT_{index}_INVALID")
            continue
        body = {key: value for key, value in statement.items() if key != "statement_sha256"}
        statement_hash = statement.get("statement_sha256")
        if statement_hash != _digest(body):
            row_errors.append("HASH_INVALID")
        if statement.get("schema") != STATEMENT_SCHEMA:
            row_errors.append("SCHEMA_INVALID")
        witness = statement.get("witness_id")
        record = registry.get(witness)
        if not isinstance(record, dict):
            row_errors.append("WITNESS_UNKNOWN")
        elif record.get("revoked") is True:
            row_errors.append("WITNESS_REVOKED")
        elif not isinstance(record.get("independence_group"), str):
            row_errors.append("INDEPENDENCE_GROUP_INVALID")
        observed = _time(statement.get("observed_at"))
        if observed is None:
            row_errors.append("OBSERVATION_TIME_INVALID")
        elif observed > now:
            row_errors.append("OBSERVATION_FROM_FUTURE")
        elif (now - observed).total_seconds() > max_age_seconds:
            row_errors.append("STATEMENT_STALE")
        expected = {
            "generation": expected_generation,
            "head_record_sha256": expected_head_record_sha256,
            "store_sha256": expected_store_sha256,
            "implementation_identity_sha256": expected_implementation_identity_sha256,
        }
        if any(statement.get(key) != value for key, value in expected.items()):
            row_errors.append("HEAD_OR_IDENTITY_MISMATCH")
        witness_key = str(witness)
        semantic = _digest(
            {
                "generation": statement.get("generation"),
                "head": statement.get("head_record_sha256"),
                "store": statement.get("store_sha256"),
            }
        )
        fingerprints.setdefault(witness_key, set()).add(semantic)
        if statement_hash in seen_exact:
            row_errors.append("DUPLICATE_STATEMENT")
        seen_exact.add(str(statement_hash))
        if row_errors:
            errors.extend(f"STATEMENT_{index}_{error}" for error in sorted(set(row_errors)))
        else:
            valid.append(statement)
    equivocators = sorted(witness for witness, values in fingerprints.items() if len(values) > 1)
    if equivocators:
        errors.append("WITNESS_EQUIVOCATION")
    groups: dict[str, str] = {}
    witnesses: set[str] = set()
    for statement in valid:
        witness = str(statement["witness_id"])
        group = str(registry[witness]["independence_group"])
        if witness in witnesses:
            errors.append("DUPLICATE_WITNESS")
        witnesses.add(witness)
        if group in groups and groups[group] != witness:
            errors.append("COLLUDING_OR_NONINDEPENDENT_WITNESSES")
        else:
            groups[group] = witness
    if len(groups) < threshold:
        errors.append("INSUFFICIENT_INDEPENDENT_QUORUM")
    return _result(sorted(set(errors)), sorted(groups), threshold, equivocators)


def audit_gossip(rounds: object, *, required_nodes: list[str]) -> dict[str, object]:
    errors: list[str] = []
    records: list[dict[str, object]] = []
    if not isinstance(rounds, list) or not rounds or not required_nodes:
        return _gossip_result(["INPUT_INVALID"], [])
    previous_knowledge: dict[str, set[str]] = {node: set() for node in required_nodes}
    conflict_seen = False
    for round_index, snapshot in enumerate(rounds):
        if not isinstance(snapshot, dict) or set(snapshot) != set(required_nodes):
            errors.append(f"ROUND_{round_index}_NODE_SET_INVALID")
            continue
        heads: set[str] = set()
        sizes: dict[str, int] = {}
        for node in required_nodes:
            knowledge = snapshot[node]
            if not isinstance(knowledge, list) or any(
                not isinstance(item, str) for item in knowledge
            ):
                errors.append(f"ROUND_{round_index}_{node}_KNOWLEDGE_INVALID")
                continue
            current = set(knowledge)
            if not previous_knowledge[node].issubset(current):
                errors.append(f"ROUND_{round_index}_{node}_KNOWLEDGE_ROLLBACK")
            previous_knowledge[node] = current
            heads.update(current)
            sizes[node] = len(current)
        if len(heads) > 1:
            conflict_seen = True
        records.append(
            {
                "round": round_index,
                "distinct_heads": sorted(heads),
                "knowledge_sizes": sizes,
                "partitioned": len({tuple(sorted(snapshot[node])) for node in required_nodes}) > 1,
            }
        )
    final_sets = [previous_knowledge[node] for node in required_nodes]
    converged = bool(final_sets) and all(value == final_sets[0] for value in final_sets)
    one_head = converged and len(final_sets[0]) == 1
    if not converged:
        errors.append("GOSSIP_NOT_CONVERGED")
    if conflict_seen or not one_head:
        errors.append("CONFLICT_OBSERVED_NO_AUTOMATIC_RECONCILIATION")
    return _gossip_result(sorted(set(errors)), records)


def _result(
    errors: list[str], groups: list[str], threshold: int, equivocators: list[str] | None = None
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "independent_groups": groups,
        "independent_count": len(groups),
        "threshold": threshold,
        "equivocating_witnesses": equivocators or [],
        "quorum_attested": not errors,
        "reconciliation_authorized": False,
        "safety": _safety(),
    }
    result["quorum_sha256"] = _digest(result)
    return result


def _gossip_result(errors: list[str], records: list[dict[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": GOSSIP_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "rounds": records,
        "converged_single_head": not errors,
        "reconciliation_authorized": False,
        "safety": _safety(),
    }
    result["gossip_sha256"] = _digest(result)
    return result


def _safety() -> dict[str, bool]:
    return {
        "simulation_only": True,
        "network_access": False,
        "persistence": False,
        "runtime_write": False,
        "service_control": False,
        "order_capability": False,
    }
