"""Forward-compatibility envelope for Phase 4KX-4LD evidence schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

SCHEMA = "phase4le.schema-compatibility-verdict.v1"
VERSION = re.compile(r"^(?P<family>[a-z0-9.-]+)\.v(?P<major>[0-9]+)(?:\.(?P<minor>[0-9]+))?$")
FORBIDDEN_CAPABILITIES = {
    "artifact_publication",
    "database_write",
    "exchange_access",
    "network_access",
    "order_capability",
    "service_control",
    "trading_capability",
    "writer_lock",
}


CONTRACTS: dict[str, dict[str, object]] = {
    "workspace_manifest": {
        "family": "phase4kx.workspace-provenance",
        "required": {"schema": str, "entry_count": int, "entries": list, "safety": dict},
        "optional": {"manifest_sha256", "repository", "extensions"},
        "immutable": {"entry_count", "entries", "safety", "manifest_sha256", "repository"},
    },
    "staged_proof": {
        "family": "phase4kz.commit-payload-proof",
        "required": {"schema": str, "verdict": str, "owned_paths": list, "errors": list},
        "optional": {
            "proof_sha256",
            "records",
            "repository",
            "staged_paths",
            "safety",
            "extensions",
        },
        "immutable": {"verdict", "owned_paths", "errors", "proof_sha256", "records"},
    },
    "ancestry_binding": {
        "family": "phase4la.commit-ancestry-binding",
        "required": {
            "schema": str,
            "verdict": str,
            "commit": str,
            "parents": list,
            "tree": str,
            "errors": list,
        },
        "optional": {
            "binding_sha256",
            "branch_ref",
            "committed_paths",
            "owned_paths",
            "safety",
            "staged_proof_sha256",
            "extensions",
        },
        "immutable": {
            "verdict",
            "commit",
            "parents",
            "tree",
            "binding_sha256",
            "committed_paths",
            "owned_paths",
        },
    },
    "receipt": {
        "family": "phase4lb.commit-evidence-receipt",
        "required": {
            "schema": str,
            "phase_id": str,
            "commit": str,
            "parent": str,
            "tree": str,
            "owned_paths": list,
            "safety_verdict": str,
        },
        "optional": {
            "ancestry_binding_sha256",
            "completed_at",
            "previous_receipt_sha256",
            "receipt_sha256",
            "staged_proof_sha256",
            "test_summary",
            "extensions",
        },
        "immutable": {
            "phase_id",
            "commit",
            "parent",
            "tree",
            "owned_paths",
            "safety_verdict",
            "receipt_sha256",
        },
    },
    "ledger": {
        "family": "phase4lb.commit-evidence-ledger",
        "required": {"schema": str, "verdict": str, "receipt_count": int, "errors": list},
        "optional": {
            "first_phase",
            "last_phase",
            "ledger_sha256",
            "safety",
            "tip_receipt_sha256",
            "extensions",
        },
        "immutable": {
            "verdict",
            "receipt_count",
            "first_phase",
            "last_phase",
            "ledger_sha256",
            "tip_receipt_sha256",
        },
    },
    "checkpoint": {
        "family": "phase4lc.evidence-ledger-checkpoint",
        "required": {
            "schema": str,
            "recovery_policy": str,
            "receipt_count": int,
            "ledger_sha256": str,
        },
        "optional": {
            "checkpoint_sha256",
            "created_at",
            "first_phase",
            "last_phase",
            "tip_receipt_sha256",
            "extensions",
        },
        "immutable": {
            "recovery_policy",
            "receipt_count",
            "ledger_sha256",
            "checkpoint_sha256",
            "first_phase",
            "last_phase",
            "tip_receipt_sha256",
        },
    },
    "independent_audit": {
        "family": "phase4ld.independent-chain-audit",
        "required": {"schema": str, "verdict": str, "checked_receipts": int, "errors": list},
        "optional": {
            "audit_sha256",
            "checked_git_commits",
            "recomputed_ledger_sha256",
            "safety",
            "extensions",
        },
        "immutable": {
            "verdict",
            "checked_receipts",
            "audit_sha256",
            "checked_git_commits",
            "recomputed_ledger_sha256",
        },
    },
}


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _version(value: object) -> tuple[str, int, int] | None:
    if not isinstance(value, str):
        return None
    match = VERSION.fullmatch(value)
    if not match:
        return None
    return match.group("family"), int(match.group("major")), int(match.group("minor") or 0)


def _exact_type(value: object, expected: type) -> bool:
    return type(value) is expected


def _capability_errors(value: object, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in FORBIDDEN_CAPABILITIES and child not in {
                False,
                None,
                "disabled",
                "none",
            }:
                errors.append(f"FORBIDDEN_CAPABILITY:{path}.{key}")
            errors.extend(_capability_errors(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_capability_errors(child, f"{path}[{index}]"))
    return errors


def assess_compatibility(contract_name: str, base: object, candidate: object) -> dict[str, object]:
    errors: list[str] = []
    contract = CONTRACTS.get(contract_name)
    if contract is None:
        errors.append("UNKNOWN_CONTRACT")
        contract = {"family": "", "required": {}, "optional": set(), "immutable": set()}
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        errors.append("EVIDENCE_NOT_AN_OBJECT")
        base = base if isinstance(base, dict) else {}
        candidate = candidate if isinstance(candidate, dict) else {}
    base_version = _version(base.get("schema"))
    candidate_version = _version(candidate.get("schema"))
    family = str(contract["family"])
    if base_version is None or candidate_version is None:
        errors.append("MALFORMED_VERSION")
    else:
        if base_version[0] != family or candidate_version[0] != family:
            errors.append("SCHEMA_FAMILY_MISMATCH")
        if base_version[1] != 1 or candidate_version[1] != 1:
            errors.append("UNSUPPORTED_MAJOR_VERSION")
        if candidate_version[2] < base_version[2] or candidate_version[2] > 1:
            errors.append("UNSUPPORTED_OR_ROLLBACK_MINOR_VERSION")
    required = contract["required"]
    optional = contract["optional"]
    immutable = contract["immutable"]
    allowed = set(required) | set(optional)
    for field, expected_type in required.items():
        if field not in candidate:
            errors.append(f"MISSING_REQUIRED_FIELD:{field}")
        elif not _exact_type(candidate[field], expected_type):
            errors.append(f"REQUIRED_FIELD_TYPE_CHANGED:{field}")
    for field in sorted(set(candidate) - allowed):
        errors.append(f"CONFLICTING_TOP_LEVEL_EXTENSION:{field}")
    for field in immutable:
        if field in base and candidate.get(field) != base.get(field):
            errors.append(f"IMMUTABLE_FIELD_CHANGED:{field}")
    extensions = candidate.get("extensions", {})
    if not isinstance(extensions, dict):
        errors.append("EXTENSIONS_NOT_AN_OBJECT")
    else:
        aliases: set[str] = set()
        for name in extensions:
            if not isinstance(name, str) or "." not in name or name.lower() in aliases:
                errors.append("INVALID_OR_DUPLICATE_EXTENSION_ALIAS")
            aliases.add(str(name).lower())
    errors.extend(_capability_errors(candidate))
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "contract": contract_name,
        "verdict": "PASS" if not errors else "REFUSE",
        "base_schema": base.get("schema"),
        "candidate_schema": candidate.get("schema"),
        "errors": errors,
        "safety": {"read_only": True, "capability_expansion": False, "order_capability": False},
    }
    result["verdict_sha256"] = _digest(result)
    return result


def assess_serialized(contract_name: str, base_json: str, candidate_json: str) -> dict[str, object]:
    errors: list[str] = []
    try:
        base = json.loads(base_json)
        candidate = json.loads(candidate_json)
    except json.JSONDecodeError:
        base, candidate = {}, {}
        errors.append("MALFORMED_JSON")
    canonical_base = json.dumps(base, sort_keys=True, separators=(",", ":"))
    canonical_candidate = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
    if base_json != canonical_base or candidate_json != canonical_candidate:
        errors.append("NON_CANONICAL_SERIALIZATION")
    result = assess_compatibility(contract_name, base, candidate)
    if errors:
        result["errors"] = sorted(set(result["errors"] + errors))
        result["verdict"] = "REFUSE"
        result.pop("verdict_sha256", None)
        result["verdict_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("contract", choices=sorted(CONTRACTS))
    parser.add_argument("base")
    parser.add_argument("candidate")
    args = parser.parse_args()
    with open(args.base, encoding="utf-8") as stream:
        base = json.load(stream)
    with open(args.candidate, encoding="utf-8") as stream:
        candidate = json.load(stream)
    result = assess_compatibility(args.contract, base, candidate)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
