"""Build a deterministic, local-only non-production release candidate manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4bl.non-production-release-candidate.v1"
SAFETY_SCHEMA = "phase4bl.release-safety-manifest.v1"
REQUIRED = ("verifier", "simulator", "schemas", "documentation", "tests", "build_identity", "sbom")
PROHIBITED_PARTS = ("credential", "secret", "service", "systemd", "exchange", "production")
PROHIBITED_CONTENT = ("/home/james/kalshi-runtime-src", "systemctl", "api_key", "write_authorized")


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({k: v for k, v in payload.items() if k != "artifact_hash"})


def build(root: Path, inventory: dict[str, list[str]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if tuple(inventory) != REQUIRED or any(len(inventory[key]) == 0 for key in REQUIRED):
        raise ValueError("PHASE4BL_INVENTORY_COVERAGE_INVALID")
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    resolved_root = root.resolve()
    for category in REQUIRED:
        for raw in sorted(inventory[category]):
            relative = Path(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("PHASE4BL_PATH_INVALID")
            normalized = relative.as_posix()
            if normalized in seen or any(part in normalized.lower() for part in PROHIBITED_PARTS):
                raise ValueError("PHASE4BL_DUPLICATE_OR_PROHIBITED_PATH")
            path = (root / relative).resolve()
            if resolved_root not in path.parents or not path.is_file() or path.is_symlink():
                raise ValueError("PHASE4BL_FILE_INVALID")
            content = path.read_bytes()
            text = content.decode("utf-8", errors="ignore").lower()
            if any(token in text for token in PROHIBITED_CONTENT):
                raise ValueError("PHASE4BL_PROHIBITED_CAPABILITY")
            seen.add(normalized)
            files.append(
                {
                    "category": category,
                    "path": normalized,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BL",
        "files": files,
        "file_count": len(files),
        "local_only": True,
        "deployed": False,
        "production_executor_present": False,
        "write_authorization_present": False,
        "reproducibility_instructions": "rebuild from the listed paths and compare sha256 values",
        "execution_authorized": False,
    }
    manifest["artifact_hash"] = _hash(manifest)
    safety: dict[str, Any] = {
        "schema": SAFETY_SCHEMA,
        "phase": "4BL",
        "release_candidate_hash": manifest["artifact_hash"],
        "credentials_present": False,
        "service_units_present": False,
        "production_paths_present": False,
        "exchange_interfaces_present": False,
        "network_required": False,
        "local_undeployed": True,
        "execution_authorized": False,
    }
    safety["artifact_hash"] = _hash(safety)
    return manifest, safety


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--safety-output", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    manifest, safety = build(args.root, inventory)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.manifest_output, args.safety_output, manifest, safety)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
