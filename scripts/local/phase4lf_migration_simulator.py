"""Deterministic, non-publishing evidence migration differential simulator."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json

from scripts.local.phase4le_schema_compatibility import (
    CONTRACTS,
    assess_compatibility,
)

SCHEMA = "phase4lf.evidence-migration-differential.v1"
MIGRATION_EXTENSION = "phase4lf.migration"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _v11_schema(contract_name: str) -> str:
    return f"{CONTRACTS[contract_name]['family']}.v1.1"


def _migration_payload(source_schema: str) -> dict[str, object]:
    return {
        "source_schema": source_schema,
        "migration": "v1-to-v1.1",
        "legacy_hashes_preserved": True,
        "required_for_v1_1": False,
        "capability_expansion": False,
    }


def migrate(contract_name: str, evidence: object) -> dict[str, object]:
    if contract_name not in CONTRACTS or not isinstance(evidence, dict):
        raise ValueError("known contract and object evidence required")
    candidate = copy.deepcopy(evidence)
    if candidate.get("schema") == _v11_schema(contract_name):
        extensions = candidate.get("extensions")
        if (
            isinstance(extensions, dict)
            and isinstance(extensions.get(MIGRATION_EXTENSION), dict)
            and extensions[MIGRATION_EXTENSION].get("migration") == "v1-to-v1.1"
        ):
            return candidate
        raise ValueError("v1.1 evidence lacks the canonical migration binding")
    source_schema = str(candidate.get("schema", ""))
    extensions = candidate.get("extensions", {})
    if not isinstance(extensions, dict) or MIGRATION_EXTENSION in extensions:
        raise ValueError("conflicting or malformed extension envelope")
    candidate["schema"] = _v11_schema(contract_name)
    candidate["extensions"] = {
        **copy.deepcopy(extensions),
        MIGRATION_EXTENSION: _migration_payload(source_schema),
    }
    return candidate


def downgrade_projection(contract_name: str, candidate: object) -> dict[str, object]:
    if contract_name not in CONTRACTS or not isinstance(candidate, dict):
        raise ValueError("known contract and object evidence required")
    if candidate.get("schema") != _v11_schema(contract_name):
        raise ValueError("only canonical v1.1 evidence can be projected")
    extensions = copy.deepcopy(candidate.get("extensions"))
    if not isinstance(extensions, dict):
        raise ValueError("malformed extension envelope")
    migration = extensions.pop(MIGRATION_EXTENSION, None)
    if not isinstance(migration, dict) or migration.get("migration") != "v1-to-v1.1":
        raise ValueError("migration binding missing")
    if migration.get("required_for_v1_1") is not False:
        raise ValueError("downgrade would conceal required v1.1 semantics")
    for value in extensions.values():
        if isinstance(value, dict) and value.get("required_for_v1_1") is True:
            raise ValueError("downgrade would conceal required v1.1 semantics")
    projected = copy.deepcopy(candidate)
    projected["schema"] = migration.get("source_schema")
    if extensions:
        projected["extensions"] = extensions
    else:
        projected.pop("extensions", None)
    return projected


def simulate_migration(contract_name: str, evidence: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(evidence, dict):
        evidence = {}
        errors.append("EVIDENCE_NOT_AN_OBJECT")
    try:
        first = migrate(contract_name, evidence)
        second = migrate(contract_name, first)
    except (KeyError, ValueError) as exc:
        first, second = {}, {}
        errors.append(f"MIGRATION_REFUSED:{exc}")
    compatibility = assess_compatibility(contract_name, evidence, first)
    if compatibility["verdict"] != "PASS":
        errors.extend(f"COMPATIBILITY:{item}" for item in compatibility["errors"])
    if first != second:
        errors.append("MIGRATION_NOT_IDEMPOTENT")
    try:
        projection = downgrade_projection(contract_name, first)
    except (KeyError, ValueError) as exc:
        projection = {}
        errors.append(f"DOWNGRADE_REFUSED:{exc}")
    if projection != evidence:
        errors.append("ROUND_TRIP_SEMANTIC_DRIFT")
    contract = CONTRACTS.get(contract_name, {"immutable": set(), "required": {}})
    for field in set(contract["required"]) | set(contract["immutable"]):
        if field == "schema":
            continue
        if field in evidence and first.get(field) != evidence.get(field):
            errors.append(f"PROTECTED_FIELD_DRIFT:{field}")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "contract": contract_name,
        "verdict": "PASS" if not errors else "REFUSE",
        "source_sha256": _digest(evidence),
        "migrated_sha256": _digest(first),
        "projection_sha256": _digest(projection),
        "round_trip_equal": projection == evidence,
        "idempotent": first == second,
        "migrated_evidence": first,
        "errors": errors,
        "safety": {
            "in_memory_only": True,
            "publishes_artifacts": False,
            "capability_expansion": False,
            "order_capability": False,
        },
    }
    result["simulation_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("contract", choices=sorted(CONTRACTS))
    parser.add_argument("evidence")
    args = parser.parse_args()
    with open(args.evidence, encoding="utf-8") as stream:
        evidence = json.load(stream)
    result = simulate_migration(args.contract, evidence)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
