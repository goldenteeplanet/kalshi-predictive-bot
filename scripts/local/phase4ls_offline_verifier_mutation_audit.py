"""Run a deterministic mutation and compatibility audit against Phase 4LR."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections.abc import Callable

from scripts.local.phase4lr_alert_evidence_bundle import ARTIFACT_HASH_FIELDS, build_manifest

SCHEMA = "phase4ls.offline-verifier-mutation-audit.v1"
EXPECTED_SCHEMAS = tuple(ARTIFACT_HASH_FIELDS)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize(errors: object) -> tuple[list[str], str]:
    normalized = sorted(set(errors)) if isinstance(errors, list) else ["ERRORS_MALFORMED"]
    return normalized, _digest(normalized)


def _rehash_content(artifact: dict) -> None:
    content = artifact["content"]
    hash_field = ARTIFACT_HASH_FIELDS.get(content.get("schema"))
    if hash_field:
        content[hash_field] = _digest(
            {key: value for key, value in content.items() if key != hash_field}
        )


def _dependency_contract_errors(artifacts: list[dict]) -> list[str]:
    by_schema = {
        item.get("content", {}).get("schema"): item
        for item in artifacts
        if isinstance(item, dict)
        and isinstance(item.get("content"), dict)
        and isinstance(item["content"].get("schema"), str)
    }
    errors: list[str] = []
    previous_name = None
    for schema in EXPECTED_SCHEMAS:
        item = by_schema.get(schema)
        if item is None:
            continue
        expected = [] if previous_name is None else [previous_name]
        if item.get("depends_on") != expected:
            errors.append(f"DEPENDENCY_CONTRACT_MISMATCH:{schema}")
        previous_name = item.get("name")
    return errors


def _mutations(artifacts: list[dict]) -> list[tuple[str, list[dict]]]:
    cases: list[tuple[str, list[dict]]] = []

    def add(name: str, mutate: Callable[[list[dict]], None]) -> None:
        candidate = copy.deepcopy(artifacts)
        mutate(candidate)
        cases.append((name, candidate))

    add("wrapper_field_deletion", lambda rows: rows[0].pop("depends_on"))
    add("wrapper_field_addition", lambda rows: rows[0].update(unexpected=True))
    add("wrapper_type_mutation", lambda rows: rows[0].update(depends_on="none"))
    add("hash_substitution", lambda rows: rows[0]["content"].update(classification_sha256="f" * 64))
    add("dependency_rewire", lambda rows: rows[2].update(depends_on=[rows[0]["name"]]))
    add("duplicate_identity", lambda rows: rows[1].update(name=rows[0]["name"]))
    add("dependency_cycle", lambda rows: rows[0].update(depends_on=[rows[-1]["name"]]))
    add("truncated_chain", lambda rows: rows.pop())

    def oversize(rows: list[dict]) -> None:
        rows[0]["content"]["evidence"] = "x" * 66_000
        _rehash_content(rows[0])

    add("oversize_content", oversize)

    def sensitive(rows: list[dict]) -> None:
        rows[0]["content"]["evidence"] = "postgresql://user:password@host/database"
        _rehash_content(rows[0])

    add("sensitive_value_injection", sensitive)
    for index, schema in enumerate(EXPECTED_SCHEMAS[: len(artifacts)]):
        add(
            f"schema_type_mutation_{index}",
            lambda rows, position=index: rows[position]["content"].update(schema=["invalid"]),
        )
        hash_field = ARTIFACT_HASH_FIELDS[schema]
        add(
            f"artifact_hash_substitution_{index}",
            lambda rows, position=index, field=hash_field: rows[position]["content"].update(
                **{field: "0" * 64}
            ),
        )
    return cases


def run_audit(artifacts: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(artifacts, list):
        artifacts = []
        errors.append("ARTIFACTS_NOT_A_LIST")
    baseline = build_manifest(artifacts)
    schemas = {
        item.get("content", {}).get("schema")
        for item in artifacts
        if isinstance(item, dict)
        and isinstance(item.get("content"), dict)
        and isinstance(item["content"].get("schema"), str)
    }
    if schemas != set(EXPECTED_SCHEMAS):
        errors.append("COMPLETE_SCHEMA_CHAIN_REQUIRED")
    errors.extend(_dependency_contract_errors(artifacts))
    if baseline.get("verdict") != "PASS":
        errors.append("BASELINE_NOT_PASSING")

    mutation_results: list[dict[str, object]] = []
    for name, candidate in _mutations(artifacts):
        result = build_manifest(candidate)
        case_errors = list(result.get("errors", []))
        candidate_schemas = {
            item.get("content", {}).get("schema")
            for item in candidate
            if isinstance(item, dict)
            and isinstance(item.get("content"), dict)
            and isinstance(item["content"].get("schema"), str)
        }
        if candidate_schemas != set(EXPECTED_SCHEMAS):
            case_errors.append("COMPLETE_SCHEMA_CHAIN_REQUIRED")
        case_errors.extend(_dependency_contract_errors(candidate))
        normalized, signature = _normalize(case_errors)
        refused = bool(normalized)
        if not refused:
            errors.append(f"UNSAFE_MUTATION_ACCEPTED:{name}")
        mutation_results.append(
            {
                "name": name,
                "expected": "REFUSE",
                "observed": "REFUSE" if refused else "PASS",
                "normalized_errors": normalized,
                "signature_sha256": signature,
            }
        )

    reordered = build_manifest(list(reversed(artifacts)))
    canonicalization_pass = reordered == baseline and baseline.get("verdict") == "PASS"
    if not canonicalization_pass:
        errors.append("CANONICALIZATION_ORDER_INVARIANCE_FAILED")

    compatibility: list[dict[str, object]] = []
    for schema in EXPECTED_SCHEMAS:
        prefix, version = schema.rsplit(".v", 1)
        for candidate, expected in (
            (schema, "PASS"),
            (f"{prefix}.v0", "REFUSE"),
            (f"{prefix}.v2", "REFUSE"),
        ):
            observed = "PASS" if candidate in ARTIFACT_HASH_FIELDS else "REFUSE"
            if observed != expected:
                errors.append(f"COMPATIBILITY_MISMATCH:{candidate}")
            compatibility.append(
                {
                    "schema": candidate,
                    "expected": expected,
                    "observed": observed,
                    "current_version": int(version),
                }
            )

    coverage = {
        "schema_count": len(schemas & set(EXPECTED_SCHEMAS)),
        "expected_schema_count": len(EXPECTED_SCHEMAS),
        "mutation_count": len(mutation_results),
        "unsafe_mutations_refused": sum(row["observed"] == "REFUSE" for row in mutation_results),
        "compatibility_case_count": len(compatibility),
        "canonicalization_order_invariant": canonicalization_pass,
    }
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "baseline_manifest_sha256": baseline.get("manifest_sha256"),
        "coverage": coverage,
        "mutations": mutation_results,
        "compatibility": compatibility,
        "safety": {
            "offline_only": True,
            "file_mutation": False,
            "network_access": False,
            "database_access": False,
            "service_control": False,
            "wsl_control": False,
            "notification_delivery": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts")
    args = parser.parse_args()
    with open(args.artifacts, encoding="utf-8") as stream:
        artifacts = json.load(stream)
    result = run_audit(artifacts)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
