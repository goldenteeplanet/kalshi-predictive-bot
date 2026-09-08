"""Canonical in-memory packaging and offline verification for recovery handoff evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath

SCHEMA = "phase4ml.recovery-certification-handoff-package.v1"
MANIFEST_SCHEMA = "phase4ml.recovery-certification-handoff-manifest.v1"
VERIFICATION_SCHEMA = "phase4ml.recovery-handoff-verification.v1"
MAX_COMPONENTS = 8
MAX_COMPONENT_BYTES = 1_000_000
MAX_PACKAGE_BYTES = 4_000_000
MAX_LIFETIME = timedelta(days=7)
REQUIRED_NAMES = [
    "phase4mj-certification",
    "phase4mk-reproducibility",
    "test-evidence",
    "inventory",
    "schemas",
    "residual-risks",
    "verification-instructions",
]
PACKAGE_FIELDS = {"schema", "manifest", "components"}
MANIFEST_FIELDS = {
    "schema",
    "audience",
    "issued_at",
    "expires_at",
    "certified_range_head",
    "descendant_checkout_head",
    "component_count",
    "total_component_bytes",
    "component_descriptors",
    "deferred_capabilities",
    "manifest_sha256",
}
COMPONENT_FIELDS = {"name", "path", "media_type", "payload"}
DESCRIPTOR_FIELDS = {"name", "path", "media_type", "size_bytes", "sha256"}
HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
IDENTIFIER = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._:-]{2,127}\Z")
SECRET_KEYS = {"secret", "api_key", "private_key", "signing_key", "password"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _contains_secret(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in SECRET_KEYS or _contains_secret(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return (
        bool(value) and not path.is_absolute() and ".." not in path.parts and path.parts[0] != "."
    )


def _component(name: str, payload: object) -> dict[str, object]:
    return {
        "name": name,
        "path": f"recovery-handoff/{name}.json",
        "media_type": "application/json",
        "payload": payload,
    }


def normalize_certification_component(certification: object) -> dict[str, object]:
    if not isinstance(certification, dict) or certification.get("verdict") != "PASS":
        raise ValueError("certification must be passing")
    semantics = json.loads(json.dumps(certification))
    for field in ("repository", "repository_head_commit", "certification_sha256"):
        semantics.pop(field, None)
    return {
        "schema": "phase4ml.normalized-phase4mj-certification.v1",
        "verdict": "PASS",
        "semantics": semantics,
        "semantics_sha256": _digest(semantics),
    }


def build_package(
    *,
    certification: object,
    reproducibility_audit: object,
    test_evidence: object,
    inventory: object,
    schemas: object,
    residual_risks: object,
    verification_instructions: object,
    audience: str,
    issued_at: str,
    expires_at: str,
    certified_range_head: str,
    descendant_checkout_head: str,
) -> bytes:
    issued = _time(issued_at)
    expires = _time(expires_at)
    if not IDENTIFIER.fullmatch(audience):
        raise ValueError("invalid audience")
    if issued is None or expires is None or expires <= issued or expires - issued > MAX_LIFETIME:
        raise ValueError("invalid package lifetime")
    if (
        HEX40.fullmatch(certified_range_head) is None
        or HEX40.fullmatch(descendant_checkout_head) is None
    ):
        raise ValueError("invalid commit binding")
    components = [
        _component("phase4mj-certification", normalize_certification_component(certification)),
        _component("phase4mk-reproducibility", reproducibility_audit),
        _component("test-evidence", test_evidence),
        _component("inventory", inventory),
        _component("schemas", schemas),
        _component("residual-risks", residual_risks),
        _component("verification-instructions", verification_instructions),
    ]
    if any(_contains_secret(component["payload"]) for component in components):
        raise ValueError("secret-like material is forbidden")
    descriptors: list[dict[str, object]] = []
    for component in components:
        payload_bytes = _canonical(component["payload"])
        if len(payload_bytes) > MAX_COMPONENT_BYTES:
            raise ValueError("component exceeds size bound")
        descriptors.append(
            {
                "name": component["name"],
                "path": component["path"],
                "media_type": component["media_type"],
                "size_bytes": len(payload_bytes),
                "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            }
        )
    deferred = {
        "artifact_extraction": False,
        "production_persistence": False,
        "production_compaction": False,
        "repair_execution": False,
        "runtime_write": False,
        "wsl_control": False,
        "service_control": False,
        "network_access": False,
        "order_capability": False,
    }
    manifest_body: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "audience": audience,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "certified_range_head": certified_range_head,
        "descendant_checkout_head": descendant_checkout_head,
        "component_count": len(components),
        "total_component_bytes": sum(row["size_bytes"] for row in descriptors),
        "component_descriptors": descriptors,
        "deferred_capabilities": deferred,
    }
    package = {
        "schema": SCHEMA,
        "manifest": {**manifest_body, "manifest_sha256": _digest(manifest_body)},
        "components": components,
    }
    encoded = _canonical(package)
    if len(encoded) > MAX_PACKAGE_BYTES:
        raise ValueError("package exceeds size bound")
    return encoded


def verify_package(
    package_bytes: object,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(package_bytes, bytes) or len(package_bytes) > MAX_PACKAGE_BYTES:
        errors.append("PACKAGE_TYPE_OR_SIZE_INVALID")
        package_bytes = b"{}"
    package_sha256 = hashlib.sha256(package_bytes).hexdigest()
    try:
        package = json.loads(package_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        package = {}
        errors.append("PACKAGE_JSON_INVALID_OR_TRUNCATED")
    if not isinstance(package, dict) or set(package) != PACKAGE_FIELDS:
        errors.append("PACKAGE_FIELD_SET_INVALID")
        package = {}
    elif _canonical(package) != package_bytes:
        errors.append("PACKAGE_NOT_CANONICAL")
    if package.get("schema") != SCHEMA:
        errors.append("PACKAGE_SCHEMA_INVALID")
    manifest = package.get("manifest")
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        errors.append("MANIFEST_FIELD_SET_INVALID")
        manifest = {}
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("manifest_sha256") != _digest(body):
        errors.append("MANIFEST_HASH_INVALID")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append("MANIFEST_SCHEMA_INVALID")
    if manifest.get("audience") != expected_audience:
        errors.append("AUDIENCE_MISMATCH")
    now = _time(evaluated_at)
    issued = _time(manifest.get("issued_at"))
    expires = _time(manifest.get("expires_at"))
    if now is None or issued is None or expires is None:
        errors.append("PACKAGE_TIME_INVALID")
    else:
        if expires <= issued or expires - issued > MAX_LIFETIME:
            errors.append("PACKAGE_LIFETIME_INVALID")
        if now < issued:
            errors.append("PACKAGE_NOT_YET_VALID")
        if now >= expires:
            errors.append("PACKAGE_EXPIRED")
    for field in ("certified_range_head", "descendant_checkout_head"):
        if HEX40.fullmatch(str(manifest.get(field))) is None:
            errors.append(f"{field.upper()}_INVALID")
    deferred = manifest.get("deferred_capabilities")
    if (
        not isinstance(deferred, dict)
        or not deferred
        or any(value is not False for value in deferred.values())
    ):
        errors.append("CAPABILITY_ESCALATION")
    components = package.get("components")
    descriptors = manifest.get("component_descriptors")
    if not isinstance(components, list) or not isinstance(descriptors, list):
        errors.append("COMPONENT_COLLECTION_INVALID")
        components, descriptors = [], []
    if not 0 < len(components) <= MAX_COMPONENTS or len(components) != len(descriptors):
        errors.append("COMPONENT_COUNT_INVALID")
    if manifest.get("component_count") != len(components):
        errors.append("MANIFEST_COMPONENT_COUNT_MISMATCH")
    names: list[str] = []
    paths: list[str] = []
    total = 0
    payloads: dict[str, object] = {}
    extraction_plan: list[dict[str, object]] = []
    for index, component in enumerate(components):
        if not isinstance(component, dict) or set(component) != COMPONENT_FIELDS:
            errors.append(f"COMPONENT_{index}_FIELD_SET_INVALID")
            continue
        name, path = component.get("name"), component.get("path")
        names.append(str(name))
        paths.append(str(path))
        if not _safe_path(path):
            errors.append(f"COMPONENT_{index}_PATH_UNSAFE")
        payload_bytes = _canonical(component.get("payload"))
        total += len(payload_bytes)
        if len(payload_bytes) > MAX_COMPONENT_BYTES:
            errors.append(f"COMPONENT_{index}_OVERSIZED")
        if _contains_secret(component.get("payload")):
            errors.append(f"COMPONENT_{index}_SECRET_MATERIAL")
        descriptor = descriptors[index] if index < len(descriptors) else None
        expected_descriptor = {
            "name": name,
            "path": path,
            "media_type": component.get("media_type"),
            "size_bytes": len(payload_bytes),
            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }
        if not isinstance(descriptor, dict) or set(descriptor) != DESCRIPTOR_FIELDS:
            errors.append(f"COMPONENT_{index}_DESCRIPTOR_INVALID")
        elif descriptor != expected_descriptor:
            errors.append(f"COMPONENT_{index}_SUBSTITUTED_OR_REORDERED")
        payloads[str(name)] = component.get("payload")
        extraction_plan.append(
            {
                "target_relative_path": path,
                "size_bytes": len(payload_bytes),
                "sha256": expected_descriptor["sha256"],
                "write_performed": False,
            }
        )
    if names != REQUIRED_NAMES or len(names) != len(set(names)) or len(paths) != len(set(paths)):
        errors.append("COMPONENT_ORDER_DUPLICATION_OR_OMISSION")
    if manifest.get("total_component_bytes") != total:
        errors.append("TOTAL_COMPONENT_BYTES_MISMATCH")
    certification = payloads.get("phase4mj-certification")
    reproducibility = payloads.get("phase4mk-reproducibility")
    test_evidence = payloads.get("test-evidence")
    inventory = payloads.get("inventory")
    schemas = payloads.get("schemas")
    risks = payloads.get("residual-risks")
    instructions = payloads.get("verification-instructions")
    if not isinstance(certification, dict) or certification.get("verdict") != "PASS":
        errors.append("CERTIFICATION_NOT_PASSING")
    if not isinstance(reproducibility, dict) or reproducibility.get("verdict") != "PASS":
        errors.append("REPRODUCIBILITY_NOT_PASSING")
    if not isinstance(test_evidence, dict) or test_evidence.get("verdict") != "PASS":
        errors.append("TEST_EVIDENCE_NOT_PASSING")
    if not isinstance(inventory, list) or len(inventory) != 75:
        errors.append("INVENTORY_COUNT_INVALID")
    if not isinstance(schemas, list) or not schemas:
        errors.append("SCHEMAS_INVALID")
    if not isinstance(risks, list) or not risks:
        errors.append("RESIDUAL_RISKS_MISSING")
    if not isinstance(instructions, list) or not instructions:
        errors.append("VERIFICATION_INSTRUCTIONS_MISSING")
    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": VERIFICATION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "package_sha256": package_sha256,
        "manifest_sha256": manifest.get("manifest_sha256"),
        "component_count": len(components),
        "extraction_plan": extraction_plan if not errors else [],
        "safety": {
            "offline": True,
            "verification_only": True,
            "extraction_performed": False,
            "filesystem_write": False,
            "production_persistence": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["verification_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package")
    parser.add_argument("--audience", required=True)
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    with open(args.package, "rb") as stream:
        package_bytes = stream.read(MAX_PACKAGE_BYTES + 1)
    result = verify_package(
        package_bytes,
        expected_audience=args.audience,
        evaluated_at=args.evaluated_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
