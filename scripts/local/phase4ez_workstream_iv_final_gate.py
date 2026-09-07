"""Gate Workstream IV on complete equivalence, lifecycle, operator, and no-execution proof."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ez.gate-input.v1"
REPORT_SCHEMA = "phase4ez.gate-report.v1"
REQUIRED_PHASES = tuple(f"4E{chr(code)}" for code in range(ord("A"), ord("Z")))
PROOF_REQUIREMENTS = {
    "EQUIVALENCE": {
        "source_phases": {"4EU"},
        "assertion_codes": {
            "ELIGIBILITY_EQUAL",
            "QUANTITY_EQUAL",
            "CAPS_EQUAL",
            "REASON_CODES_EQUAL",
        },
    },
    "ROLLBACK": {
        "source_phases": {"4EP"},
        "assertion_codes": {"COMPLETE_ROLLBACK", "PARTIAL_ROLLBACK", "QUANTITY_CONSERVATION"},
    },
    "REPLAY": {
        "source_phases": {"4EP"},
        "assertion_codes": {"DETERMINISTIC_REPLAY", "TRANSITION_EQUALITY"},
    },
    "EXPIRATION": {
        "source_phases": {"4EN"},
        "assertion_codes": {"INTENT_VALIDITY", "EVIDENCE_VALIDITY", "APPROVAL_VALIDITY"},
    },
    "OPERATOR": {
        "source_phases": {"4EQ", "4ER", "4ES"},
        "assertion_codes": {
            "DECISION_PACKET_BOUND",
            "REVIEW_QUEUE_NON_AUTHORIZING",
            "APPROVAL_REUSE_PROHIBITED",
        },
    },
    "NO_EXECUTION": {
        "source_phases": {"4EV", "4EW", "4EX", "4EY"},
        "assertion_codes": {
            "MUTATION_SURFACE_ABSENT",
            "AIR_GAP_ACCEPTED",
            "PAPER_CREATION_DISABLED",
            "RESIDUAL_ACCESS_DISABLED",
        },
    },
}
SAFETY_FIELDS = {
    "production_database_mutated",
    "services_controlled",
    "credentials_loaded",
    "writer_access_granted",
    "exchange_access_granted",
    "paper_order_creation_enabled",
    "paper_order_creation_authorized",
    "execution_authorized",
}


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return value


def _exact_unique_strings(value: Any, expected: set[str], code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or not item for item in value)
        or set(value) != expected
    ):
        raise ValueError(code)
    return sorted(value)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "phase_artifacts",
        "proofs",
        "safety_state",
        "artifact_hash",
    }:
        raise ValueError("PHASE4EZ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EZ_INPUT_SCHEMA_OR_HASH_INVALID")
    phase_artifacts = payload["phase_artifacts"]
    if not isinstance(phase_artifacts, list) or len(phase_artifacts) != len(REQUIRED_PHASES):
        raise ValueError("PHASE4EZ_PHASE_ARTIFACT_SET_INVALID")
    artifact_rows: list[dict[str, str]] = []
    seen_phases: set[str] = set()
    for artifact in phase_artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"phase", "artifact_hash"}:
            raise ValueError("PHASE4EZ_PHASE_ARTIFACT_FIELDS_INVALID")
        phase = artifact["phase"]
        if phase not in REQUIRED_PHASES or phase in seen_phases:
            raise ValueError("PHASE4EZ_PHASE_ARTIFACT_ID_INVALID")
        seen_phases.add(phase)
        artifact_rows.append(
            {
                "phase": phase,
                "artifact_hash": _digest(
                    artifact["artifact_hash"], "PHASE4EZ_PHASE_ARTIFACT_HASH_INVALID"
                ),
            }
        )
    if seen_phases != set(REQUIRED_PHASES):
        raise ValueError("PHASE4EZ_PHASE_ARTIFACT_SET_INVALID")
    artifact_rows.sort(key=lambda item: REQUIRED_PHASES.index(item["phase"]))

    proofs = payload["proofs"]
    if not isinstance(proofs, list) or len(proofs) != len(PROOF_REQUIREMENTS):
        raise ValueError("PHASE4EZ_PROOF_SET_INVALID")
    proof_rows: list[dict[str, Any]] = []
    seen_categories: set[str] = set()
    for proof in proofs:
        if not isinstance(proof, dict) or set(proof) != {
            "category",
            "source_phases",
            "assertion_codes",
            "passed",
            "evidence_hash",
        }:
            raise ValueError("PHASE4EZ_PROOF_FIELDS_INVALID")
        category = proof["category"]
        if category not in PROOF_REQUIREMENTS or category in seen_categories:
            raise ValueError("PHASE4EZ_PROOF_CATEGORY_INVALID")
        seen_categories.add(category)
        requirement = PROOF_REQUIREMENTS[category]
        source_phases = _exact_unique_strings(
            proof["source_phases"],
            requirement["source_phases"],
            "PHASE4EZ_PROOF_SOURCE_PHASES_INVALID",
        )
        assertion_codes = _exact_unique_strings(
            proof["assertion_codes"],
            requirement["assertion_codes"],
            "PHASE4EZ_PROOF_ASSERTIONS_INVALID",
        )
        if proof["passed"] is not True:
            raise ValueError("PHASE4EZ_REQUIRED_PROOF_FAILED")
        proof_rows.append(
            {
                "category": category,
                "source_phases": source_phases,
                "assertion_codes": assertion_codes,
                "passed": True,
                "evidence_hash": _digest(
                    proof["evidence_hash"], "PHASE4EZ_PROOF_EVIDENCE_HASH_INVALID"
                ),
            }
        )
    if seen_categories != set(PROOF_REQUIREMENTS):
        raise ValueError("PHASE4EZ_PROOF_SET_INVALID")
    proof_rows.sort(key=lambda item: item["category"])

    safety = payload["safety_state"]
    if not isinstance(safety, dict) or set(safety) != SAFETY_FIELDS:
        raise ValueError("PHASE4EZ_SAFETY_FIELDS_INVALID")
    if any(not isinstance(value, bool) for value in safety.values()):
        raise ValueError("PHASE4EZ_SAFETY_STATUS_INVALID")
    unsafe = sorted(field for field, value in safety.items() if value)
    if unsafe:
        raise ValueError("PHASE4EZ_NO_EXECUTION_INVARIANT_FAILED")

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EZ",
        "input_hash": payload["artifact_hash"],
        "phase_artifacts": artifact_rows,
        "phase_artifact_count": len(artifact_rows),
        "phase_artifact_manifest_hash": canonical_hash(artifact_rows),
        "proofs": proof_rows,
        "proof_categories": sorted(PROOF_REQUIREMENTS),
        "all_required_proofs_passed": True,
        "no_execution_invariants_passed": True,
        "workstream_iv_certified": True,
        "certification_state": "WORKSTREAM_IV_PAPER_ONLY_ACCELERATION_CERTIFIED",
        **{field: False for field in sorted(SAFETY_FIELDS)},
        "paper_orders_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
