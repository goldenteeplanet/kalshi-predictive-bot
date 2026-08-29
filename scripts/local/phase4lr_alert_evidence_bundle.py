"""Build and independently verify privacy-minimized alert evidence manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

SCHEMA = "phase4lr.alert-evidence-export-manifest.v1"
ARTIFACT_HASH_FIELDS = {
    "phase4ll.runtime-snapshot-drift-classification.v1": "classification_sha256",
    "phase4lm.runtime-observation-ledger.v1": "ledger_sha256",
    "phase4ln.alert-state-contract.v1": "decision_sha256",
    "phase4lo.alert-delivery-envelope.v1": "envelope_sha256",
    "phase4lp.alert-lifecycle.v1": "lifecycle_sha256",
    "phase4lq.alert-retention-contract.v1": "retention_sha256",
}
SCHEMA_ORDER = {schema: index for index, schema in enumerate(ARTIFACT_HASH_FIELDS)}
MAX_ARTIFACTS = len(ARTIFACT_HASH_FIELDS)
MAX_ARTIFACT_BYTES = 65_536
MAX_BUNDLE_BYTES = 262_144
NAME_PATTERN = re.compile(r"phase4l[l-q]-[a-z0-9-]{1,48}\Z")
SENSITIVE_KEY = re.compile(r"(?:secret|password|credential|api[_-]?key|database[_-]?url)", re.I)
SENSITIVE_VALUE = re.compile(
    r"(?:postgres(?:ql)?://|mysql://|-----BEGIN [A-Z ]+ KEY-----|[A-Za-z]:\\|/mnt/|/home/)"
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sensitive(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            SENSITIVE_KEY.search(str(key)) or _sensitive(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_sensitive(item) for item in value)
    return isinstance(value, str) and SENSITIVE_VALUE.search(value) is not None


def _topological(records: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    names = {record["name"] for record in records}
    pending = {record["name"]: set(record["depends_on"]) for record in records}
    for name, dependencies in pending.items():
        missing = dependencies - names
        if missing:
            errors.append(f"MISSING_DEPENDENCY:{name}:{','.join(sorted(missing))}")
    order: list[str] = []
    while pending:
        ready = sorted(name for name, dependencies in pending.items() if not dependencies)
        if not ready:
            errors.append("DEPENDENCY_CYCLE")
            break
        for name in ready:
            order.append(name)
            pending.pop(name)
            for dependencies in pending.values():
                dependencies.discard(name)
    return order, errors


def build_manifest(artifacts: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(artifacts, list):
        errors.append("ARTIFACTS_NOT_A_LIST")
        artifacts = []
    if not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        errors.append("ARTIFACT_COUNT_OUT_OF_BOUNDS")
    records: list[dict[str, Any]] = []
    names: set[str] = set()
    schemas: set[str] = set()
    total_bytes = 0
    for index, artifact in enumerate(artifacts):
        prefix = f"ARTIFACT_{index}"
        if not isinstance(artifact, dict) or set(artifact) != {"name", "content", "depends_on"}:
            errors.append(f"{prefix}:FIELD_SET_INVALID")
            continue
        name = artifact.get("name")
        content = artifact.get("content")
        dependencies = artifact.get("depends_on")
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            errors.append(f"{prefix}:NAME_INVALID")
            continue
        if name in names:
            errors.append(f"{prefix}:DUPLICATE_NAME")
        names.add(name)
        if not isinstance(content, dict):
            errors.append(f"{prefix}:CONTENT_INVALID")
            continue
        schema = content.get("schema")
        if not isinstance(schema, str):
            errors.append(f"{prefix}:SCHEMA_TYPE_INVALID")
            continue
        hash_field = ARTIFACT_HASH_FIELDS.get(schema)
        if hash_field is None:
            errors.append(f"{prefix}:SCHEMA_NOT_ALLOWLISTED")
            continue
        if schema in schemas:
            errors.append(f"{prefix}:DUPLICATE_SCHEMA")
        schemas.add(schema)
        body = {key: value for key, value in content.items() if key != hash_field}
        if content.get(hash_field) != _digest(body):
            errors.append(f"{prefix}:HASH_MISMATCH")
        if _sensitive(content):
            errors.append(f"{prefix}:SENSITIVE_CONTENT_REJECTED")
        size = len(_canonical(content))
        total_bytes += size
        if size > MAX_ARTIFACT_BYTES:
            errors.append(f"{prefix}:SIZE_LIMIT_EXCEEDED")
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(item, str) for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            errors.append(f"{prefix}:DEPENDENCIES_INVALID")
            dependencies = []
        records.append(
            {
                "name": name,
                "schema": schema,
                "artifact_sha256": content.get(hash_field),
                "canonical_bytes": size,
                "depends_on": sorted(dependencies),
            }
        )
    if total_bytes > MAX_BUNDLE_BYTES:
        errors.append("BUNDLE_SIZE_LIMIT_EXCEEDED")
    order, topology_errors = _topological(records)
    errors.extend(topology_errors)
    by_name = {record["name"]: record for record in records}
    ordered_records = [by_name[name] for name in order]
    schema_positions = [SCHEMA_ORDER[record["schema"]] for record in ordered_records]
    if schema_positions != sorted(schema_positions):
        errors.append("SCHEMA_DEPENDENCY_ORDER_INVALID")
    manifest: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "artifact_count": len(ordered_records),
        "canonical_bundle_bytes": total_bytes,
        "topological_order": order,
        "artifacts": ordered_records,
        "privacy": {
            "artifact_bodies_embedded": False,
            "credentials_included": False,
            "runtime_paths_included": False,
            "database_urls_included": False,
        },
        "safety": {
            "offline_only": True,
            "file_write": False,
            "network_access": False,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    manifest["manifest_sha256"] = _digest(manifest)
    return manifest


def verify_bundle(manifest: object, artifacts: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        errors.append("MANIFEST_NOT_AN_OBJECT")
        manifest = {}
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if manifest.get("schema") != SCHEMA:
        errors.append("MANIFEST_BAD_SCHEMA")
    if manifest.get("manifest_sha256") != _digest(body):
        errors.append("MANIFEST_HASH_MISMATCH")
    rebuilt = build_manifest(artifacts)
    if rebuilt != manifest:
        errors.append("MANIFEST_REBUILD_MISMATCH")
    result: dict[str, object] = {
        "schema": "phase4lr.alert-evidence-offline-verification.v1",
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "rebuilt_manifest_sha256": rebuilt.get("manifest_sha256"),
        "safety": {
            "offline_only": True,
            "file_write": False,
            "network_access": False,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["verification_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("artifacts")
    verify = subparsers.add_parser("verify")
    verify.add_argument("manifest")
    verify.add_argument("artifacts")
    args = parser.parse_args()
    with open(args.artifacts, encoding="utf-8") as stream:
        artifacts = json.load(stream)
    if args.command == "build":
        result = build_manifest(artifacts)
    else:
        with open(args.manifest, encoding="utf-8") as stream:
            manifest = json.load(stream)
        result = verify_bundle(manifest, artifacts)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
