"""Aggregate-gate mutation campaign and independently anchored verifier."""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from collections.abc import Callable

from scripts.local.phase4oa_aggregate_release_gate import (
    BLOCKED_ON_SETTLEMENT,
    CHECK_CATEGORIES,
    PHASES,
)
from scripts.local.phase4oa_aggregate_release_gate import (
    SCHEMA as RELEASE_SCHEMA,
)

SCHEMA = "phase4ob.gate-mutation-resistance.v1"
MUTATIONS = (
    "DELETE_MANIFEST_FIELD",
    "DUPLICATE_PHASE",
    "REORDER_PHASES",
    "SUBSTITUTE_FILE_HASH",
    "STALE_PHASE_VALUE",
    "TYPE_CONFUSION",
    "UNICODE_COLLISION",
    "OVERSIZED_INPUT",
    "UNKNOWN_FIELD",
    "CONTRADICTORY_NESTED_VERDICT",
    "RECOMPUTED_ENVELOPE",
    "SCHEMA_DRIFT",
    "DELETE_REPRESENTATIVE_CHECK",
    "DUPLICATE_REPRESENTATIVE_CHECK",
    "DELETE_REFUSAL_CLASS",
    "DELETE_DEPENDENCY_EDGE",
    "SAFETY_FLAG_ENABLE",
    "ROLLBACK_INSTRUCTION_DRIFT",
    "RESIDUAL_RISK_DELETION",
    "SETTLEMENT_BLOCKER_DRIFT",
    "TOP_LEVEL_VERDICT_FLIP",
    "CERTIFICATION_HASH_DRIFT",
    "PHASE_SCHEMA_DRIFT",
    "PHASE_FILE_DELETION",
    "CHECK_PROOF_HASH_DRIFT",
    "CHECK_SAFETY_DRIFT",
)
TARGET_FIELDS = {
    "manifest",
    "phase_record",
    "file_hash",
    "schema",
    "representative_check",
    "refusal_list",
    "dependency_edge",
    "safety_flag",
    "rollback_instruction",
    "residual_risk",
    "settlement_blocker",
    "verdict",
    "certification_hash",
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def independently_verify_candidate(
    candidate: object,
    *,
    trusted_manifest_sha256: str,
    maximum_bytes: int,
) -> dict[str, object]:
    errors = []
    try:
        encoded = _canonical(candidate)
    except (TypeError, ValueError):
        return _verification(["CANDIDATE_ENCODING_INVALID"], 0)
    if len(encoded) > maximum_bytes:
        return _verification(["CANDIDATE_RESOURCE_BOUND_EXCEEDED"], len(encoded))
    if not isinstance(candidate, dict):
        return _verification(["CANDIDATE_SHAPE_INVALID"], len(encoded))
    expected_keys = {
        "schema",
        "release_candidate",
        "verdict",
        "errors",
        "inventory",
        "representative_checks",
        "refusal_classes_tested",
        "dependency_graph",
        "residual_risks",
        "blocked_on_september_1_settlement",
        "rollback",
        "safety",
        "manifest_sha256",
    }
    if set(candidate) != expected_keys:
        errors.append("CANDIDATE_FIELDS_INVALID")
    unsigned = {key: value for key, value in candidate.items() if key != "manifest_sha256"}
    calculated = _digest(unsigned)
    if candidate.get("manifest_sha256") != calculated:
        errors.append("CANDIDATE_ENVELOPE_HASH_MISMATCH")
    if calculated != trusted_manifest_sha256:
        errors.append("TRUSTED_MANIFEST_ANCHOR_MISMATCH")
    if candidate.get("schema") != RELEASE_SCHEMA:
        errors.append("CANDIDATE_SCHEMA_DRIFT")
    if candidate.get("verdict") != "PASS" or candidate.get("errors") != []:
        errors.append("CANDIDATE_VERDICT_CONTRADICTION")
    inventory = candidate.get("inventory", {})
    phase_rows = inventory.get("phases", []) if isinstance(inventory, dict) else []
    if [row.get("phase") for row in phase_rows if isinstance(row, dict)] != [
        f"4{phase}" for phase in PHASES
    ]:
        errors.append("PHASE_SET_OR_ORDER_INVALID")
    for row in phase_rows:
        if (
            not isinstance(row, dict)
            or row.get("verdict") != "PASS"
            or row.get("errors") != []
            or not row.get("schema")
            or set(row.get("files", {})) != {"script", "test", "documentation"}
        ):
            errors.append("PHASE_RECORD_INVALID")
            break
        body = {key: value for key, value in row.items() if key != "phase_sha256"}
        if row.get("phase_sha256") != _digest(body):
            errors.append("PHASE_RECORD_HASH_MISMATCH")
    checks = candidate.get("representative_checks", [])
    if [row.get("category") for row in checks if isinstance(row, dict)] != list(CHECK_CATEGORIES):
        errors.append("CHECK_SET_OR_ORDER_INVALID")
    for row in checks:
        body = {key: value for key, value in row.items() if key != "check_sha256"}
        if (
            row.get("check_sha256") != _digest(body)
            or row.get("verdict") != "PASS"
            or row.get("deterministic") is not True
            or row.get("errors") != []
            or not row.get("refusal_classes_tested")
            or row.get("safety") != _safety()
        ):
            errors.append("REPRESENTATIVE_CHECK_INVALID")
            break
    refusals = candidate.get("refusal_classes_tested", [])
    expected_refusals = sorted(
        {value for row in checks for value in row.get("refusal_classes_tested", [])}
    )
    if refusals != expected_refusals or len(refusals) != len(CHECK_CATEGORIES):
        errors.append("REFUSAL_COVERAGE_INVALID")
    expected_graph = [
        {"from": f"4{left}", "to": f"4{right}"}
        for left, right in zip(PHASES, PHASES[1:], strict=False)
    ]
    if candidate.get("dependency_graph") != expected_graph:
        errors.append("DEPENDENCY_GRAPH_INVALID")
    if candidate.get("safety") != _safety():
        errors.append("CANDIDATE_SAFETY_INVALID")
    if candidate.get("blocked_on_september_1_settlement") != BLOCKED_ON_SETTLEMENT:
        errors.append("SETTLEMENT_BLOCKER_INVALID")
    rollback = candidate.get("rollback")
    if not isinstance(rollback, str) or "Remove" not in rollback:
        errors.append("ROLLBACK_INSTRUCTION_INVALID")
    risks = candidate.get("residual_risks")
    if not isinstance(risks, list) or len(risks) != 3 or any(not value for value in risks):
        errors.append("RESIDUAL_RISK_INVALID")
    return _verification(sorted(set(errors)), len(encoded))


def mutate_candidate(candidate: dict[str, object], mutation: str) -> dict[str, object]:
    if mutation not in MUTATIONS:
        raise ValueError("unknown mutation")
    value = copy.deepcopy(candidate)
    if mutation == "DELETE_MANIFEST_FIELD":
        del value["rollback"]
    elif mutation == "DUPLICATE_PHASE":
        value["inventory"]["phases"].append(copy.deepcopy(value["inventory"]["phases"][0]))
    elif mutation == "REORDER_PHASES":
        value["inventory"]["phases"].reverse()
    elif mutation == "SUBSTITUTE_FILE_HASH":
        value["inventory"]["phases"][0]["files"]["script"]["sha256"] = "0" * 64
    elif mutation == "STALE_PHASE_VALUE":
        value["inventory"]["phases"][0]["files"]["script"]["bytes"] -= 1
    elif mutation == "TYPE_CONFUSION":
        value["dependency_graph"] = {"edges": value["dependency_graph"]}
    elif mutation == "UNICODE_COLLISION":
        value["representative_checks"][0]["category"] = unicodedata.normalize("NFD", "gólden")
    elif mutation == "OVERSIZED_INPUT":
        value["residual_risks"].append("x" * 2_000_000)
    elif mutation == "UNKNOWN_FIELD":
        value["unexpected"] = True
    elif mutation == "CONTRADICTORY_NESTED_VERDICT":
        value["representative_checks"][0]["verdict"] = "REFUSE"
    elif mutation == "SCHEMA_DRIFT":
        value["schema"] = "future"
    elif mutation == "DELETE_REPRESENTATIVE_CHECK":
        value["representative_checks"].pop()
    elif mutation == "DUPLICATE_REPRESENTATIVE_CHECK":
        value["representative_checks"].append(copy.deepcopy(value["representative_checks"][0]))
    elif mutation == "DELETE_REFUSAL_CLASS":
        value["refusal_classes_tested"].pop()
    elif mutation == "DELETE_DEPENDENCY_EDGE":
        value["dependency_graph"].pop()
    elif mutation == "SAFETY_FLAG_ENABLE":
        value["safety"]["live_execution"] = True
    elif mutation == "ROLLBACK_INSTRUCTION_DRIFT":
        value["rollback"] = "irreversible"
    elif mutation == "RESIDUAL_RISK_DELETION":
        value["residual_risks"].pop()
    elif mutation == "SETTLEMENT_BLOCKER_DRIFT":
        value["blocked_on_september_1_settlement"] = "none"
    elif mutation == "TOP_LEVEL_VERDICT_FLIP":
        value["verdict"] = "REFUSE"
    elif mutation == "CERTIFICATION_HASH_DRIFT":
        value["manifest_sha256"] = "0" * 64
        return value
    elif mutation == "PHASE_SCHEMA_DRIFT":
        value["inventory"]["phases"][0]["schema"] = "future"
    elif mutation == "PHASE_FILE_DELETION":
        del value["inventory"]["phases"][0]["files"]["test"]
    elif mutation == "CHECK_PROOF_HASH_DRIFT":
        value["representative_checks"][0]["proof_sha256"] = "stale"
    elif mutation == "CHECK_SAFETY_DRIFT":
        value["representative_checks"][0]["safety"]["paper_order_creation"] = True
    elif mutation == "RECOMPUTED_ENVELOPE":
        value["residual_risks"][0] = "attacker-rewritten-risk"
    if mutation != "CERTIFICATION_HASH_DRIFT":
        unsigned = {key: item for key, item in value.items() if key != "manifest_sha256"}
        value["manifest_sha256"] = _digest(unsigned)
    return value


def run_mutation_campaign(
    candidate: dict[str, object],
    *,
    trusted_manifest_sha256: str,
    maximum_bytes: int,
    mutation_ids: tuple[str, ...] = MUTATIONS,
    verifier: Callable[..., dict[str, object]] = independently_verify_candidate,
) -> dict[str, object]:
    errors = []
    missing = sorted(set(MUTATIONS) - set(mutation_ids))
    if missing:
        errors.append("MUTATION_COVERAGE_INCOMPLETE")
    rows, survivors = [], []
    for mutation in mutation_ids:
        mutated = mutate_candidate(candidate, mutation)
        first = verifier(
            mutated,
            trusted_manifest_sha256=trusted_manifest_sha256,
            maximum_bytes=maximum_bytes,
        )
        second = verifier(
            mutated,
            trusted_manifest_sha256=trusted_manifest_sha256,
            maximum_bytes=maximum_bytes,
        )
        if first != second:
            errors.append("MUTATION_VERIFICATION_NONDETERMINISTIC")
        if first.get("verdict") == "PASS":
            survivors.append(mutation)
        rows.append(
            {
                "mutation": mutation,
                "verdict": first.get("verdict"),
                "errors": first.get("errors"),
                "verification_sha256": first.get("verification_sha256"),
            }
        )
    if survivors:
        errors.append("CERTIFICATION_BYPASS_SURVIVOR")
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "mutation_count": len(rows),
        "target_fields": sorted(TARGET_FIELDS),
        "survivors": survivors,
        "results": rows,
        "verifier_coupled_to_primary": False,
        "safety": _safety(),
    }
    result["campaign_sha256"] = _digest(result)
    return result


def _verification(errors, encoded_bytes):
    result = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "encoded_bytes": encoded_bytes,
        "safety": _safety(),
    }
    result["verification_sha256"] = _digest(result)
    return result


def _safety():
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
