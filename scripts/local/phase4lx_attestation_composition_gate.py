"""Compose independent dependency, transparency, and verifier attestations offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

SCHEMA = "phase4lx.independent-attestation-composition.v1"
ATTESTATION_SCHEMA = "phase4lx.independent-attestation.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
MAX_ATTESTATION_SECONDS = 86_400
FIXTURE_AUTHORITIES = {
    "BUILDER": ("dejoia-fixture-builder", "1" * 64),
    "SCANNER": ("dejoia-fixture-scanner", "2" * 64),
}
ARTIFACTS = {
    "dependency": ("phase4lw.verifier-dependency-readiness.v1", "readiness_sha256"),
    "transparency": ("phase4lu.keyless-proof-offline-validation.v1", "validation_sha256"),
    "verifier": ("phase4lv.offline-cryptographic-verification.v1", "verification_sha256"),
}
ATTESTATION_FIELDS = {
    "schema",
    "role",
    "authority_id",
    "authority_sha256",
    "subject_component_sha256",
    "dependency_readiness_sha256",
    "transparency_validation_sha256",
    "verifier_result_sha256",
    "issued_at",
    "expires_at",
    "replay_identity_sha256",
    "authenticity_class",
    "self_asserted",
    "attestation_sha256",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def make_attestation(**fields: object) -> dict[str, object]:
    result = {"schema": ATTESTATION_SCHEMA, **fields}
    result["attestation_sha256"] = _digest(result)
    return result


def _artifact_errors(value: object, label: str) -> list[str]:
    schema, hash_field = ARTIFACTS[label]
    if not isinstance(value, dict):
        return [f"{label.upper()}_NOT_AN_OBJECT"]
    errors: list[str] = []
    if value.get("schema") != schema:
        errors.append(f"{label.upper()}_SCHEMA_INVALID")
    body = {key: item for key, item in value.items() if key != hash_field}
    if value.get(hash_field) != _digest(body):
        errors.append(f"{label.upper()}_HASH_MISMATCH")
    return errors


def compose_attestations(
    dependency: object,
    transparency: object,
    verifier: object,
    attestations: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors = _artifact_errors(dependency, "dependency")
    errors.extend(_artifact_errors(transparency, "transparency"))
    errors.extend(_artifact_errors(verifier, "verifier"))
    dependency = dependency if isinstance(dependency, dict) else {}
    transparency = transparency if isinstance(transparency, dict) else {}
    verifier = verifier if isinstance(verifier, dict) else {}
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    if dependency.get("fixture_readiness") != "PASS":
        errors.append("DEPENDENCY_FIXTURE_READINESS_NOT_PASSING")
    if transparency.get("verdict") != "PASS":
        errors.append("TRANSPARENCY_NOT_PASSING")
    if transparency.get("merkle_inclusion_structurally_verified") is not True:
        errors.append("MERKLE_EVIDENCE_INCOMPLETE")
    if verifier.get("verdict") != "PASS" or verifier.get("fixture_signature_verified") is not True:
        errors.append("VERIFIER_FIXTURE_NOT_PASSING")
    if transparency.get("request_sha256") != verifier.get("request_sha256"):
        errors.append("REQUEST_BINDING_MISMATCH")
    components = dependency.get("components")
    if not isinstance(components, list) or len(components) != 1:
        errors.append("DEPENDENCY_COMPONENT_SET_INVALID")
        component_hash = None
    else:
        component_hash = (
            components[0].get("component_sha256") if isinstance(components[0], dict) else None
        )
        if HEX64.fullmatch(str(component_hash)) is None:
            errors.append("COMPONENT_SUBJECT_INVALID")
    expected_bindings = {
        "subject_component_sha256": component_hash,
        "dependency_readiness_sha256": dependency.get("readiness_sha256"),
        "transparency_validation_sha256": transparency.get("validation_sha256"),
        "verifier_result_sha256": verifier.get("verification_sha256"),
    }
    if not isinstance(attestations, list) or len(attestations) != 2:
        errors.append("EXACT_TWO_ATTESTATIONS_REQUIRED")
        attestations = []
    normalized: list[dict[str, object]] = []
    observed_role_order: list[object] = []
    roles: set[str] = set()
    authorities: set[str] = set()
    replay_ids: set[str] = set()
    all_production = True
    for index, attestation in enumerate(attestations):
        prefix = f"ATTESTATION_{index}"
        item_errors: list[str] = []
        if not isinstance(attestation, dict) or set(attestation) != ATTESTATION_FIELDS:
            item_errors.append("FIELD_SET_INVALID")
            attestation = {}
        body = {key: value for key, value in attestation.items() if key != "attestation_sha256"}
        if attestation.get("attestation_sha256") != _digest(body):
            item_errors.append("HASH_MISMATCH")
        if attestation.get("schema") != ATTESTATION_SCHEMA:
            item_errors.append("SCHEMA_INVALID")
        role = attestation.get("role")
        observed_role_order.append(role)
        if role not in FIXTURE_AUTHORITIES:
            item_errors.append("ROLE_INVALID")
        elif role in roles:
            item_errors.append("DUPLICATE_ROLE")
        if isinstance(role, str):
            roles.add(role)
        expected_authority = FIXTURE_AUTHORITIES.get(role)
        if (
            expected_authority is None
            or (attestation.get("authority_id"), attestation.get("authority_sha256"))
            != expected_authority
        ):
            item_errors.append("AUTHORITY_NOT_PINNED")
        authority = attestation.get("authority_id")
        if authority in authorities:
            item_errors.append("AUTHORITY_NOT_INDEPENDENT")
        if isinstance(authority, str):
            authorities.add(authority)
        for field, expected in expected_bindings.items():
            if attestation.get(field) != expected:
                item_errors.append(f"BINDING_MISMATCH:{field}")
        issued = _time(attestation.get("issued_at"))
        expires = _time(attestation.get("expires_at"))
        if issued is None or expires is None or now is None:
            item_errors.append("TIME_INVALID")
        elif (
            expires <= issued
            or (expires - issued).total_seconds() > MAX_ATTESTATION_SECONDS
            or now < issued
            or now >= expires
        ):
            item_errors.append("STALE_FUTURE_OR_OVERLONG")
        replay_id = attestation.get("replay_identity_sha256")
        if HEX64.fullmatch(str(replay_id)) is None:
            item_errors.append("REPLAY_ID_INVALID")
        elif replay_id in replay_ids:
            item_errors.append("DUPLICATE_REPLAY_ID")
        else:
            replay_ids.add(replay_id)
        if replay_id in {
            transparency.get("proof_sha256"),
            verifier.get("verification_identity_sha256"),
            transparency.get("request_sha256"),
        }:
            item_errors.append("REPLAY_DOMAIN_OVERLAP")
        if attestation.get("self_asserted") is not False:
            item_errors.append("SELF_ASSERTED_EVIDENCE")
        authenticity = attestation.get("authenticity_class")
        if authenticity not in {"FIXTURE", "INDEPENDENT_PRODUCTION"}:
            item_errors.append("AUTHENTICITY_CLASS_INVALID")
        all_production = all_production and authenticity == "INDEPENDENT_PRODUCTION"
        errors.extend(f"{prefix}:{error}" for error in item_errors)
        normalized.append(
            {
                "role": role,
                "authority_id": authority,
                "attestation_sha256": attestation.get("attestation_sha256"),
                "authenticity_class": authenticity,
                "verdict": "PASS" if not item_errors else "REFUSE",
                "errors": sorted(set(item_errors)),
            }
        )
    if roles != set(FIXTURE_AUTHORITIES):
        errors.append("BUILDER_AND_SCANNER_REQUIRED")
    if observed_role_order != ["BUILDER", "SCANNER"]:
        errors.append("ATTESTATION_ORDER_INVALID")
    normalized.sort(key=lambda row: str(row["role"]))
    fixture_ready = not errors
    production_ready = (
        fixture_ready
        and all_production
        and False  # No production authority pins are configured in this phase.
        and dependency.get("production_readiness") == "PASS"
        and verifier.get("production_readiness") == "PASS"
        and transparency.get("cryptographic_signature_verified") is True
    )
    production_reasons: list[str] = []
    if not production_ready:
        production_reasons.append("PRODUCTION_AUTHORITY_PINS_UNAVAILABLE")
        if not all_production:
            production_reasons.append("FIXTURE_ATTESTATIONS_NOT_PRODUCTION_AUTHENTICITY")
        if dependency.get("production_readiness") != "PASS":
            production_reasons.append("DEPENDENCY_PRODUCTION_READINESS_NOT_PASSING")
        if verifier.get("production_readiness") != "PASS":
            production_reasons.append("VERIFIER_PRODUCTION_READINESS_NOT_PASSING")
        if transparency.get("cryptographic_signature_verified") is not True:
            production_reasons.append("TRANSPARENCY_CRYPTOGRAPHIC_SIGNATURE_NOT_VERIFIED")
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if fixture_ready else "REFUSE",
        "fixture_readiness": "PASS" if fixture_ready else "REFUSE",
        "production_readiness": "PASS" if production_ready else "REFUSE",
        "production_reasons": production_reasons,
        "errors": sorted(set(errors)),
        "bindings": expected_bindings,
        "attestations": normalized,
        "safety": {
            "validation_only": True,
            "attestation_generation": False,
            "package_install": False,
            "key_access": False,
            "network_access": False,
            "database_access": False,
            "service_control": False,
            "wsl_control": False,
            "notification_delivery": False,
            "order_capability": False,
        },
    }
    result["composition_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dependency")
    parser.add_argument("transparency")
    parser.add_argument("verifier")
    parser.add_argument("attestations")
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    values = []
    for path in (args.dependency, args.transparency, args.verifier, args.attestations):
        with open(path, encoding="utf-8") as stream:
            values.append(json.load(stream))
    result = compose_attestations(*values, evaluated_at=args.evaluated_at)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
