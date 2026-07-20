from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.benchmarking.runtime_export_import import (
    build_runtime_export_import_preview,
)


SENSITIVE_FIELD_TOKENS = (
    "api_key", "authorization", "credential", "password", "private_key",
    "secret", "session_token", "token",
)
ATTESTATION_TYPE = "offline-sha256-manifest-attestation-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: Any, label: str, diagnostics: list[str]) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        diagnostics.append(f"TIMESTAMP_INVALID:{label}")
        return None
    if parsed.tzinfo is None:
        diagnostics.append(f"TIMESTAMP_NAIVE:{label}")
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_sensitive(field: str) -> bool:
    normalized = field.strip().lower().replace("-", "_")
    return any(token == normalized or normalized.endswith(f"_{token}") for token in SENSITIVE_FIELD_TOKENS)


def _json_sensitive_paths(value: Any, prefix: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if _is_sensitive(str(key)):
                paths.append(path)
            paths.extend(_json_sensitive_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_json_sensitive_paths(child, f"{prefix}[{index}]"))
    return paths


def _sensitive_paths(path: Path) -> list[str]:
    if path.suffix.lower() == ".json":
        return _json_sensitive_paths(json.loads(path.read_text(encoding="utf-8")))
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            headers = next(csv.reader(handle), [])
        return [f"$.columns.{field}" for field in headers if _is_sensitive(field)]
    return []


def _attestation_payload(custody: Mapping[str, Any]) -> bytes:
    payload = {
        "attestation_type": custody.get("attestation_type"),
        "signer_id": custody.get("signer_id"),
        "signed_at": custody.get("signed_at"),
        "export_manifest": custody.get("export_manifest"),
        "artifacts": custody.get("artifacts"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def certify_export_custody(custody_path: Path) -> dict[str, Any]:
    custody = json.loads(custody_path.read_text(encoding="utf-8"))
    diagnostics: list[str] = []
    for field in ("attestation_type", "signer_id", "signed_at", "signature_digest", "export_manifest", "artifacts"):
        if custody.get(field) in (None, "", []):
            diagnostics.append(f"CUSTODY_FIELD_MISSING:{field}")
    if custody.get("attestation_type") != ATTESTATION_TYPE:
        diagnostics.append("ATTESTATION_TYPE_UNSUPPORTED")
    signed_at = _timestamp(custody.get("signed_at"), "signed_at", diagnostics)
    expected_signature = hashlib.sha256(_attestation_payload(custody)).hexdigest()
    if custody.get("signature_digest") != expected_signature:
        diagnostics.append("ATTESTATION_DIGEST_MISMATCH")

    root = custody_path.parent.resolve()
    certified_artifacts = []
    artifact_paths: dict[str, Path] = {}
    for index, artifact in enumerate(custody.get("artifacts") or []):
        relative = artifact.get("path")
        label = artifact.get("dataset") or f"artifact_{index}"
        if relative in (None, ""):
            diagnostics.append(f"ARTIFACT_PATH_MISSING:{label}")
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            diagnostics.append(f"ARTIFACT_PATH_OUTSIDE_ROOT:{label}")
            continue
        if not path.is_file():
            diagnostics.append(f"ARTIFACT_FILE_MISSING:{label}")
            continue
        actual_hash = _sha256(path)
        if artifact.get("sha256") != actual_hash:
            diagnostics.append(f"ARTIFACT_HASH_MISMATCH:{label}")
        source_timestamp = _timestamp(artifact.get("source_timestamp"), f"artifact:{label}", diagnostics)
        try:
            sensitive = _sensitive_paths(path)
        except (csv.Error, json.JSONDecodeError, OSError):
            diagnostics.append(f"ARTIFACT_REDACTION_SCAN_FAILED:{label}")
            sensitive = []
        diagnostics.extend(f"SENSITIVE_FIELD_REJECTED:{label}:{item}" for item in sensitive)
        artifact_paths[label] = path
        certified_artifacts.append({
            "dataset": label,
            "path": relative,
            "sha256": actual_hash,
            "source_timestamp": source_timestamp,
            "sensitive_fields": sensitive,
        })

    export_spec = custody.get("export_manifest") or {}
    export_path = (root / str(export_spec.get("path", ""))).resolve()
    try:
        export_path.relative_to(root)
    except ValueError:
        diagnostics.append("EXPORT_MANIFEST_PATH_OUTSIDE_ROOT")
    if not export_path.is_file():
        diagnostics.append("EXPORT_MANIFEST_FILE_MISSING")
    elif export_spec.get("sha256") != _sha256(export_path):
        diagnostics.append("EXPORT_MANIFEST_HASH_MISMATCH")

    chain_rows = sorted(certified_artifacts, key=lambda row: (row["dataset"], row["path"]))
    chain_digest = hashlib.sha256(json.dumps({
        "signer_id": custody.get("signer_id"),
        "signed_at": signed_at,
        "artifacts": chain_rows,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "certified": not diagnostics,
        "diagnostics": diagnostics,
        "signing_metadata": {
            "attestation_type": custody.get("attestation_type"),
            "signer_id": custody.get("signer_id"),
            "signed_at": signed_at,
            "signature_digest": custody.get("signature_digest"),
        },
        "artifacts": chain_rows,
        "chain_digest": chain_digest,
        "export_manifest_path": export_path if export_path.is_file() else None,
    }


def build_export_custody_preview(custody_path: Path) -> dict[str, Any]:
    custody = certify_export_custody(custody_path)
    import_preview = None
    if custody["certified"] and custody["export_manifest_path"] is not None:
        import_preview = build_runtime_export_import_preview(custody["export_manifest_path"])
    import_certified = bool(
        import_preview
        and import_preview["manifest_valid"]
        and import_preview["summary"]["decisions"] == import_preview["summary"]["certified"]
    )
    return {
        "phase": "PMB-34D",
        "mode": "LOCAL_EXPORT_REDACTION_INTEGRITY_CHAIN_OF_CUSTODY_CERTIFICATION",
        "database_access": False,
        "database_writes": 0,
        "cloud_access": False,
        "execution_enabled": False,
        "thresholds_changed": False,
        "runtime_policy_changed": False,
        "custody_certified": custody["certified"],
        "custody_diagnostics": custody["diagnostics"],
        "signing_metadata": custody["signing_metadata"],
        "artifacts": custody["artifacts"],
        "chain_digest": custody["chain_digest"],
        "import_preview": import_preview,
        "summary": {
            "artifacts": len(custody["artifacts"]),
            "sensitive_fields_rejected": sum(len(row["sensitive_fields"]) for row in custody["artifacts"]),
            "import_certified": import_certified,
            "pmb35_deployment_unblocked": False,
            "certification_passed": custody["certified"] and import_certified,
        },
    }


def write_export_custody_preview(custody_path: Path, output_dir: Path) -> Path:
    report = build_export_custody_preview(custody_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "pmb34d_export_custody_certification.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
