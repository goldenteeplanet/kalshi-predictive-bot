"""Validate supplied verifier dependency attestations without installing anything."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

SCHEMA = "phase4lw.verifier-dependency-readiness.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
VERSION = re.compile(r"\A\d+\.\d+\.\d+\Z")
MAX_SCAN_AGE_SECONDS = 604_800
ALLOWLIST = {
    "cryptography": {
        "version": "50.0.0",
        "license": "APACHE-2.0 OR BSD-3-CLAUSE",
        "algorithms": ["ECDSA-P256-SHA256", "ED25519", "RSA-PSS-SHA256"],
        "python_abi": "cp312",
        "platform_tag": "manylinux_2_34_x86_64",
    }
}
COMPONENT_FIELDS = {
    "name",
    "version",
    "artifact_type",
    "artifact_sha256",
    "license",
    "source_provenance_sha256",
    "build_attestation_sha256",
    "vulnerability_scan",
    "algorithms",
    "runtime",
    "offline_install",
    "capabilities",
    "attestation_scope",
    "component_sha256",
}
SCAN_FIELDS = {"scanned_at", "status", "scanner_sha256"}
RUNTIME_FIELDS = {"python_abi", "platform_tag"}
INSTALL_FIELDS = {"verified", "artifact_sha256", "source_only"}
CAPABILITY_FIELDS = {
    "key_generation",
    "private_key_loading",
    "network_trust",
    "os_certificate_store",
    "service_control",
    "trading",
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


def make_component(**fields: object) -> dict[str, object]:
    component = dict(fields)
    component["component_sha256"] = _digest(component)
    return component


def evaluate_readiness(
    components: object,
    *,
    evaluated_at: str,
    required_platform_tag: str,
    required_python_abi: str,
) -> dict[str, object]:
    errors: list[str] = []
    now = _time(evaluated_at)
    if now is None:
        errors.append("EVALUATION_TIME_INVALID")
    if not isinstance(components, list) or not components:
        errors.append("COMPONENTS_INVALID")
        components = []
    names: set[str] = set()
    records: list[dict[str, object]] = []
    all_production = True
    for index, component in enumerate(components):
        prefix = f"COMPONENT_{index}"
        item_errors: list[str] = []
        if not isinstance(component, dict) or set(component) != COMPONENT_FIELDS:
            item_errors.append("FIELD_SET_INVALID")
            component = {}
        name = component.get("name")
        if not isinstance(name, str) or name not in ALLOWLIST:
            item_errors.append("NAME_NOT_ALLOWLISTED")
            expected = {}
        else:
            expected = ALLOWLIST[name]
        if name in names:
            item_errors.append("DUPLICATE_PACKAGE")
        if isinstance(name, str):
            names.add(name)
        body = {key: value for key, value in component.items() if key != "component_sha256"}
        if component.get("component_sha256") != _digest(body):
            item_errors.append("COMPONENT_HASH_MISMATCH")
        version = component.get("version")
        if not isinstance(version, str) or VERSION.fullmatch(version) is None:
            item_errors.append("VERSION_MUTABLE_OR_INVALID")
        elif version != expected.get("version"):
            item_errors.append("VERSION_NOT_PINNED")
        if component.get("artifact_type") != "wheel":
            item_errors.append("ARTIFACT_TYPE_INVALID")
        for field in (
            "artifact_sha256",
            "source_provenance_sha256",
            "build_attestation_sha256",
        ):
            if HEX64.fullmatch(str(component.get(field))) is None:
                item_errors.append(f"HASH_INVALID:{field}")
        if component.get("license") != expected.get("license"):
            item_errors.append("LICENSE_NOT_REVIEWED")
        algorithms = component.get("algorithms")
        if algorithms != expected.get("algorithms"):
            item_errors.append("ALGORITHM_SET_INVALID")
        scan = component.get("vulnerability_scan")
        if not isinstance(scan, dict) or set(scan) != SCAN_FIELDS:
            item_errors.append("SCAN_FIELD_SET_INVALID")
        else:
            scanned_at = _time(scan.get("scanned_at"))
            if scanned_at is None or now is None:
                item_errors.append("SCAN_TIME_INVALID")
            elif scanned_at > now or (now - scanned_at).total_seconds() > MAX_SCAN_AGE_SECONDS:
                item_errors.append("SCAN_STALE_OR_FUTURE")
            if scan.get("status") != "PASS":
                item_errors.append("SCAN_NOT_PASSING")
            if HEX64.fullmatch(str(scan.get("scanner_sha256"))) is None:
                item_errors.append("SCANNER_HASH_INVALID")
        runtime = component.get("runtime")
        if not isinstance(runtime, dict) or set(runtime) != RUNTIME_FIELDS:
            item_errors.append("RUNTIME_FIELD_SET_INVALID")
        elif (
            runtime.get("python_abi") != required_python_abi
            or runtime.get("platform_tag") != required_platform_tag
            or runtime.get("python_abi") != expected.get("python_abi")
            or runtime.get("platform_tag") != expected.get("platform_tag")
        ):
            item_errors.append("RUNTIME_PLATFORM_MISMATCH")
        install = component.get("offline_install")
        if not isinstance(install, dict) or set(install) != INSTALL_FIELDS:
            item_errors.append("OFFLINE_INSTALL_FIELD_SET_INVALID")
        else:
            if install.get("verified") is not True or install.get("source_only") is not False:
                item_errors.append("OFFLINE_WHEEL_NOT_VERIFIED")
            if install.get("artifact_sha256") != component.get("artifact_sha256"):
                item_errors.append("OFFLINE_ARTIFACT_HASH_MISMATCH")
        capabilities = component.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != CAPABILITY_FIELDS:
            item_errors.append("CAPABILITY_FIELD_SET_INVALID")
        elif any(value is not False for value in capabilities.values()):
            item_errors.append("FORBIDDEN_CAPABILITY_PRESENT")
        scope = component.get("attestation_scope")
        if scope not in {"FIXTURE", "INDEPENDENT_PRODUCTION"}:
            item_errors.append("ATTESTATION_SCOPE_INVALID")
        all_production = all_production and scope == "INDEPENDENT_PRODUCTION"
        errors.extend(f"{prefix}:{error}" for error in item_errors)
        records.append(
            {
                "name": name,
                "component_sha256": component.get("component_sha256"),
                "attestation_scope": scope,
                "verdict": "PASS" if not item_errors else "REFUSE",
                "errors": sorted(set(item_errors)),
            }
        )
    if names != set(ALLOWLIST):
        errors.append("EXACT_PACKAGE_SET_REQUIRED")
    fixture_ready = not errors
    production_ready = False
    production_reasons = ["INDEPENDENT_ATTESTATION_AUTHENTICITY_GATE_REQUIRED"]
    if not all_production:
        production_reasons.append("INDEPENDENT_PRODUCTION_ATTESTATIONS_REQUIRED")
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if fixture_ready else "REFUSE",
        "fixture_readiness": "PASS" if fixture_ready else "REFUSE",
        "production_readiness": "PASS" if production_ready else "REFUSE",
        "production_reasons": production_reasons,
        "errors": sorted(set(errors)),
        "components": records,
        "required_runtime": {
            "python_abi": required_python_abi,
            "platform_tag": required_platform_tag,
        },
        "safety": {
            "validation_only": True,
            "package_install": False,
            "registry_access": False,
            "network_access": False,
            "key_import": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["readiness_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("components")
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--platform-tag", required=True)
    parser.add_argument("--python-abi", required=True)
    args = parser.parse_args()
    with open(args.components, encoding="utf-8") as stream:
        components = json.load(stream)
    result = evaluate_readiness(
        components,
        evaluated_at=args.evaluated_at,
        required_platform_tag=args.platform_tag,
        required_python_abi=args.python_abi,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
