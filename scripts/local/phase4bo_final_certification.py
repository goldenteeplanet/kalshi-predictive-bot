"""Build the final fail-closed non-production settlement protocol certification."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bo.certification-evidence.v1"
CERT_SCHEMA = "phase4bo.non-production-certification.v1"
RISK_SCHEMA = "phase4bo.residual-risk-manifest.v1"
INDEX_SCHEMA = "phase4bo.complete-phase-index.v1"
REPORT_SCHEMA = "phase4bo.final-validation-report.v1"
TERMINAL = "NON_PRODUCTION_SETTLEMENT_PROTOCOL_CERTIFIED"
REQUIREMENTS = (
    "COMPLETE_LINEAGE",
    "ALL_CUMULATIVE_TESTS_PASS",
    "NO_UNRESOLVED_HIGH_THREATS",
    "REPRODUCIBLE_BUILD_IDENTITY",
    "COMPLETE_ROLLBACK_CERTIFICATION",
    "COMPLETE_REPLAY_PROTECTION",
    "COMPLETE_TIME_BOUNDARY_VERIFICATION",
    "AIR_GAPPED_HARNESS_SUCCESS",
    "PRODUCTION_METADATA_UNCHANGED",
    "NO_PRODUCTION_EXECUTOR",
    "NO_EXECUTION_AUTHORIZATION",
    "NO_SERVICE_OR_EXCHANGE_CAPABILITY",
)
PHASES = tuple(
    f"4{a}{b}" for a, b in (("A", chr(code)) for code in range(ord("L"), ord("Z") + 1))
) + tuple(f"4B{chr(code)}" for code in range(ord("A"), ord("N") + 1))


def _hash(payload: dict[str, Any]) -> str:
    return canonical_hash({k: v for k, v in payload.items() if k != "artifact_hash"})


def certify(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BO_INPUT_SCHEMA_OR_HASH_INVALID")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list) or [
        r.get("requirement") for r in requirements if isinstance(r, dict)
    ] != list(REQUIREMENTS):
        raise ValueError("PHASE4BO_REQUIREMENT_COVERAGE_OR_ORDER_INVALID")
    if any(
        row.get("passed") is not True or len(row.get("evidence_hash", "")) != 64
        for row in requirements
    ):
        raise ValueError("PHASE4BO_CERTIFICATION_REQUIREMENT_FAILED")
    if payload.get("phases") != list(PHASES) or payload.get(
        "production_metadata_before"
    ) != payload.get("production_metadata_after"):
        raise ValueError("PHASE4BO_LINEAGE_OR_PRODUCTION_IDENTITY_INVALID")
    risks = payload.get("residual_risks")
    if not isinstance(risks, list) or any(
        r.get("severity") == "HIGH" for r in risks if isinstance(r, dict)
    ):
        raise ValueError("PHASE4BO_RESIDUAL_RISK_INVALID")
    shared = {"phase": "4BO", "execution_authorized": False}
    certification: dict[str, Any] = {
        "schema": CERT_SCHEMA,
        **shared,
        "terminal_state": TERMINAL,
        "meaning": {
            "offline_protocol_and_safeguards_certified": True,
            "production_mutation_performed": False,
            "production_executor_implemented": False,
            "production_execution_authorized": False,
            "future_scope_expansion_requires_separate_user_authorization": True,
        },
        "evidence_hash": payload["artifact_hash"],
    }
    certification["artifact_hash"] = _hash(certification)
    risk_manifest: dict[str, Any] = {
        "schema": RISK_SCHEMA,
        **shared,
        "risks": risks,
        "high_risk_count": 0,
    }
    risk_manifest["artifact_hash"] = _hash(risk_manifest)
    phase_index: dict[str, Any] = {
        "schema": INDEX_SCHEMA,
        **shared,
        "phases": list(PHASES),
        "phase_count": len(PHASES),
    }
    phase_index["artifact_hash"] = _hash(phase_index)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        **shared,
        "requirements": requirements,
        "production_metadata_unchanged": True,
        "terminal_state": TERMINAL,
        "certification_hash": certification["artifact_hash"],
        "risk_manifest_hash": risk_manifest["artifact_hash"],
        "phase_index_hash": phase_index["artifact_hash"],
    }
    report["artifact_hash"] = _hash(report)
    return certification, risk_manifest, phase_index, report


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    outputs = certify(payload)
    names = (
        "certification.json",
        "residual-risks.json",
        "phase-index.json",
        "validation-report.json",
    )
    for name, artifact in zip(names, outputs, strict=True):
        atomic_write_json(args.output_directory / name, artifact)
    print(json.dumps(outputs[0], sort_keys=True))


if __name__ == "__main__":
    main()
