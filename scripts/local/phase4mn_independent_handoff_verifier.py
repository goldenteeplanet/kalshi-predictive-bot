"""Independent semantic verifier and two-implementation consensus gate."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from scripts.local.phase4mm_handoff_parser_fuzz import (
    bounded_verify,
    fuzz_corpus,
    lexical_preflight,
)

SCHEMA = "phase4mn.independent-handoff-verification.v1"
CONSENSUS_SCHEMA = "phase4mn.handoff-verifier-consensus.v1"
PACKAGE_SCHEMA = "phase4ml.recovery-certification-handoff-package.v1"
MANIFEST_SCHEMA = "phase4ml.recovery-certification-handoff-manifest.v1"
MAX_COMPONENTS = 8
MAX_COMPONENT_BYTES = 1_000_000
MAX_LIFETIME = timedelta(days=7)
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
NAMES = [
    "phase4mj-certification",
    "phase4mk-reproducibility",
    "test-evidence",
    "inventory",
    "schemas",
    "residual-risks",
    "verification-instructions",
]
HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
SECRET_KEYS = {"secret", "api_key", "private_key", "signing_key", "password"}


class DuplicateKey(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise DuplicateKey(key)
        result[key] = value
    return result


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.parts[0] != "."


def _secret(value: object) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in SECRET_KEYS or _secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_secret(item) for item in value)
    return False


def independent_verify(
    package_bytes: object,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    preflight = lexical_preflight(package_bytes)
    errors = list(preflight["errors"])
    package_sha = hashlib.sha256(
        package_bytes if isinstance(package_bytes, bytes) else b""
    ).hexdigest()
    package: dict[str, object] = {}
    if not errors and isinstance(package_bytes, bytes):
        try:
            decoded = json.loads(
                package_bytes,
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            if not isinstance(decoded, dict):
                errors.append("PACKAGE_NOT_OBJECT")
            else:
                package = decoded
        except DuplicateKey:
            errors.append("DUPLICATE_KEY")
        except (ValueError, TypeError, RecursionError, MemoryError, OverflowError):
            errors.append("JSON_REFUSED")
    if package and _canonical(package) != package_bytes:
        errors.append("NONCANONICAL_BYTES")
    if set(package) != PACKAGE_FIELDS or package.get("schema") != PACKAGE_SCHEMA:
        errors.append("PACKAGE_SHAPE_OR_SCHEMA_INVALID")
    manifest = package.get("manifest") if isinstance(package.get("manifest"), dict) else {}
    if set(manifest) != MANIFEST_FIELDS or manifest.get("schema") != MANIFEST_SCHEMA:
        errors.append("MANIFEST_SHAPE_OR_SCHEMA_INVALID")
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("manifest_sha256") != _digest(manifest_body):
        errors.append("MANIFEST_HASH_INVALID")
    if manifest.get("audience") != expected_audience:
        errors.append("AUDIENCE_INVALID")
    now, issued, expires = (
        _time(evaluated_at),
        _time(manifest.get("issued_at")),
        _time(manifest.get("expires_at")),
    )
    if now is None or issued is None or expires is None:
        errors.append("TIME_INVALID")
    elif expires <= issued or expires - issued > MAX_LIFETIME or not issued <= now < expires:
        errors.append("TIME_WINDOW_INVALID")
    for field in ("certified_range_head", "descendant_checkout_head"):
        if HEX40.fullmatch(str(manifest.get(field))) is None:
            errors.append("COMMIT_BINDING_INVALID")
    capabilities = manifest.get("deferred_capabilities")
    if (
        not isinstance(capabilities, dict)
        or not capabilities
        or any(value is not False for value in capabilities.values())
    ):
        errors.append("CAPABILITY_ESCALATION")
    components = package.get("components") if isinstance(package.get("components"), list) else []
    descriptors = (
        manifest.get("component_descriptors")
        if isinstance(manifest.get("component_descriptors"), list)
        else []
    )
    if not 0 < len(components) <= MAX_COMPONENTS or len(components) != len(descriptors):
        errors.append("COMPONENT_COLLECTION_INVALID")
    if manifest.get("component_count") != len(components):
        errors.append("COMPONENT_COUNT_INVALID")
    names: list[str] = []
    paths: list[str] = []
    payloads: dict[str, object] = {}
    total = 0
    extraction: list[dict[str, object]] = []
    for index, component in enumerate(components):
        if not isinstance(component, dict) or set(component) != COMPONENT_FIELDS:
            errors.append(f"COMPONENT_{index}_SHAPE_INVALID")
            continue
        name, path, media = component["name"], component["path"], component["media_type"]
        names.append(str(name))
        paths.append(str(path))
        if not _safe_path(path):
            errors.append(f"COMPONENT_{index}_PATH_INVALID")
        if media != "application/json":
            errors.append(f"COMPONENT_{index}_MEDIA_INVALID")
        payload = component["payload"]
        encoded = _canonical(payload)
        total += len(encoded)
        if len(encoded) > MAX_COMPONENT_BYTES:
            errors.append(f"COMPONENT_{index}_SIZE_INVALID")
        if _secret(payload):
            errors.append(f"COMPONENT_{index}_SECRET")
        expected = {
            "name": name,
            "path": path,
            "media_type": media,
            "size_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        descriptor = descriptors[index] if index < len(descriptors) else None
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != DESCRIPTOR_FIELDS
            or descriptor != expected
        ):
            errors.append(f"COMPONENT_{index}_DESCRIPTOR_MISMATCH")
        payloads[str(name)] = payload
        extraction.append(
            {
                "target_relative_path": path,
                "size_bytes": len(encoded),
                "sha256": expected["sha256"],
                "write_performed": False,
            }
        )
    if names != NAMES or len(names) != len(set(names)) or len(paths) != len(set(paths)):
        errors.append("COMPONENT_ORDER_OR_UNIQUENESS_INVALID")
    if manifest.get("total_component_bytes") != total:
        errors.append("TOTAL_SIZE_INVALID")
    semantic_checks = {
        "phase4mj-certification": lambda value: (
            isinstance(value, dict) and value.get("verdict") == "PASS"
        ),
        "phase4mk-reproducibility": lambda value: (
            isinstance(value, dict) and value.get("verdict") == "PASS"
        ),
        "test-evidence": lambda value: isinstance(value, dict) and value.get("verdict") == "PASS",
        "inventory": lambda value: isinstance(value, list) and len(value) == 75,
        "schemas": lambda value: isinstance(value, list) and bool(value),
        "residual-risks": lambda value: isinstance(value, list) and bool(value),
        "verification-instructions": lambda value: isinstance(value, list) and bool(value),
    }
    for name, check in semantic_checks.items():
        if not check(payloads.get(name)):
            errors.append(f"SEMANTIC_COMPONENT_INVALID:{name}")
    errors = sorted(set(errors))
    summary = {
        "package_sha256": package_sha,
        "manifest_sha256": manifest.get("manifest_sha256"),
        "component_count": len(components),
        "extraction_plan": extraction,
    }
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "semantic_summary": summary if not errors else {},
        "preflight_sha256": preflight["preflight_sha256"],
        "safety": {
            "independent_semantics": True,
            "offline": True,
            "bounded": True,
            "read_only": True,
            "package_write": False,
            "extraction": False,
            "network_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["verification_sha256"] = _digest(result)
    return result


def implementation_identity() -> dict[str, str]:
    primary_source = inspect.getsource(bounded_verify).encode()
    independent_source = inspect.getsource(independent_verify).encode()
    identity = {
        "primary_sha256": hashlib.sha256(primary_source).hexdigest(),
        "independent_sha256": hashlib.sha256(independent_source).hexdigest(),
    }
    identity["combined_sha256"] = _digest(identity)
    return identity


def _first_difference(primary: object, independent: object) -> str | None:
    if not isinstance(primary, dict) or not isinstance(independent, dict):
        return "result_type"
    if primary.get("verdict") != independent.get("verdict"):
        return "verdict"
    if primary.get("verdict") == "PASS":
        first = primary.get("semantic_summary", {})
        second = independent.get("semantic_summary", {})
        for field in ("package_sha256", "manifest_sha256", "component_count", "extraction_plan"):
            if first.get(field) != second.get(field):
                return field
    return None


def consensus_gate(
    package_bytes: bytes,
    *,
    expected_audience: str,
    evaluated_at: str,
    expected_implementation_identity: str | None = None,
) -> dict[str, object]:
    identity = implementation_identity()
    primary = bounded_verify(
        package_bytes,
        expected_audience=expected_audience,
        evaluated_at=evaluated_at,
    )
    independent = independent_verify(
        package_bytes,
        expected_audience=expected_audience,
        evaluated_at=evaluated_at,
    )
    first_difference = _first_difference(primary, independent)
    errors: list[str] = []
    if (
        expected_implementation_identity is not None
        and identity["combined_sha256"] != expected_implementation_identity
    ):
        errors.append("IMPLEMENTATION_IDENTITY_STALE")
    if first_difference is not None:
        errors.append("VERIFIER_DISAGREEMENT")
    if primary["verdict"] != "PASS" or independent["verdict"] != "PASS":
        errors.append("PACKAGE_NOT_UNANIMOUSLY_VALID")
    result: dict[str, object] = {
        "schema": CONSENSUS_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "implementation_identity": identity,
        "primary_verdict": primary["verdict"],
        "independent_verdict": independent["verdict"],
        "first_differing_semantic_field": first_difference,
        "primary_verification_sha256": primary["verification_sha256"],
        "independent_verification_sha256": independent["verification_sha256"],
        "semantic_summary": primary.get("semantic_summary", {}) if not first_difference else {},
        "safety": independent["safety"],
    }
    result["consensus_sha256"] = _digest(result)
    return result


def audit_fuzz_consensus(
    valid_package: bytes,
    *,
    expected_audience: str,
    evaluated_at: str,
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for case in fuzz_corpus(valid_package):
        primary = bounded_verify(
            case["payload"],
            expected_audience=expected_audience,
            evaluated_at=evaluated_at,
        )
        independent = independent_verify(
            case["payload"],
            expected_audience=expected_audience,
            evaluated_at=evaluated_at,
        )
        difference = _first_difference(primary, independent)
        records.append(
            {
                "case_id": case["case_id"],
                "both_refused": primary["verdict"] == independent["verdict"] == "REFUSE",
                "semantic_disagreement": difference,
                "primary_sha256": primary["verification_sha256"],
                "independent_sha256": independent["verification_sha256"],
            }
        )
    errors: list[str] = []
    if not records or not all(
        row["both_refused"] and row["semantic_disagreement"] is None for row in records
    ):
        errors.append("FUZZ_CONSENSUS_GAP")
    result: dict[str, object] = {
        "schema": CONSENSUS_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "case_count": len(records),
        "consensus_count": sum(
            row["both_refused"] and row["semantic_disagreement"] is None for row in records
        ),
        "implementation_identity": implementation_identity(),
        "records": records,
    }
    result["audit_sha256"] = _digest(result)
    return result
