"""Assemble a deterministic, hash-protected independent audit bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4bn.independent-audit-bundle.v1"
REPRO_SCHEMA = "phase4bn.audit-bundle-reproducibility.v1"
COMPONENTS = (
    "schema_catalog",
    "threat_model",
    "capability_graph",
    "test_inventory",
    "test_results",
    "build_identity",
    "dependency_inventory",
    "rollback_matrix",
    "replay_proof",
    "time_semantics_proof",
    "isolation_proof",
    "production_immutability_evidence",
    "remaining_risks",
)


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({k: v for k, v in payload.items() if k != "artifact_hash"})


def build(components: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if tuple(components) != COMPONENTS:
        raise ValueError("PHASE4BN_COMPONENT_COVERAGE_OR_ORDER_INVALID")
    rows = []
    for name in COMPONENTS:
        artifact = components[name]
        if not isinstance(artifact, dict) or artifact.get("artifact_hash") != _hash(artifact):
            raise ValueError("PHASE4BN_COMPONENT_HASH_INVALID")
        if artifact.get("execution_authorized") is not False:
            raise ValueError("PHASE4BN_AUTHORITY_INVALID")
        rows.append(
            {"component": name, "schema": artifact.get("schema"), "hash": artifact["artifact_hash"]}
        )
    bundle: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BN",
        "components": rows,
        "component_count": len(rows),
        "complete": True,
        "locally_reproducible": True,
        "execution_authorized": False,
    }
    bundle["artifact_hash"] = _hash(bundle)
    proof: dict[str, Any] = {
        "schema": REPRO_SCHEMA,
        "phase": "4BN",
        "bundle_hash": bundle["artifact_hash"],
        "ordered_component_hash": canonical_hash(rows),
        "rebuild_instruction": "sort nothing; preserve COMPONENTS order and compare artifact_hash",
        "network_required": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return bundle, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--bundle-output", type=Path, required=True)
    parser.add_argument("--repro-output", type=Path, required=True)
    args = parser.parse_args()
    components = json.loads(args.components.read_text(encoding="utf-8"))
    bundle, proof = build(components)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.bundle_output, args.repro_output, bundle, proof)
    print(json.dumps(bundle, sort_keys=True))


if __name__ == "__main__":
    main()
