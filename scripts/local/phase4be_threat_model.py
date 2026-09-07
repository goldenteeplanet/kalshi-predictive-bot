"""Phase 4BE formal artifact-only threat model and adversarial evidence review."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4be.threat-evidence-input.v1"
SCHEMA = "phase4be.formal-threat-model.v1"
RISK_SCHEMA = "phase4be.adversarial-risk-report.v1"
THREATS = (
    "ARTIFACT_FORGERY",
    "HASH_SUBSTITUTION",
    "PATH_CONFUSION",
    "SYMLINK_HARDLINK_ATTACK",
    "TOCTOU",
    "CLOCK_MANIPULATION",
    "STALE_APPROVAL",
    "REPLAY",
    "PARTIAL_PUBLICATION",
    "DATABASE_REPLACEMENT",
    "MALICIOUS_ENVIRONMENT_VARIABLE",
    "COMMAND_INJECTION",
    "SERVICE_IMPERSONATION_OR_ALTERNATE_WRITER",
    "COMPROMISED_DISPOSABLE_MARKER",
)
REQUIRED_CONTROLS = {
    "ARTIFACT_FORGERY": {"CANONICAL_HASH", "SCHEMA_VALIDATION"},
    "HASH_SUBSTITUTION": {"LINEAGE_BINDING", "HASH_RECOMPUTATION"},
    "PATH_CONFUSION": {"RESOLVED_PATH_IDENTITY", "OUTSIDE_ROOT_REFUSAL"},
    "SYMLINK_HARDLINK_ATTACK": {"SYMLINK_REFUSAL", "DEVICE_INODE_COMPARISON"},
    "TOCTOU": {"PRE_POST_IDENTITY", "IN_TRANSACTION_REVALIDATION"},
    "CLOCK_MANIPULATION": {"TRUSTED_TIME_BINDING", "SKEW_BOUND"},
    "STALE_APPROVAL": {"STRICT_EXPIRATION", "REVOCATION_GATE"},
    "REPLAY": {"ATTEMPT_ID_UNIQUENESS", "TERMINAL_OPERATION_LEDGER"},
    "PARTIAL_PUBLICATION": {"ATOMIC_PAIR_PUBLICATION", "PAIR_HASH"},
    "DATABASE_REPLACEMENT": {"DEVICE_INODE_COMPARISON", "DATABASE_STATE_HASH"},
    "MALICIOUS_ENVIRONMENT_VARIABLE": {"NO_ENVIRONMENT_PATH_OVERRIDE", "EXPLICIT_INPUTS"},
    "COMMAND_INJECTION": {"NO_SHELL_EXECUTION", "PARAMETERIZED_SQL"},
    "SERVICE_IMPERSONATION_OR_ALTERNATE_WRITER": {
        "SERVICE_IDENTITY_AUDIT",
        "CONCURRENT_WRITER_REFUSAL",
    },
    "COMPROMISED_DISPOSABLE_MARKER": {"MARKER_SCHEMA_HASH", "PROTECTED_IDENTITY_REFUSAL"},
}


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BE_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BE_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def build(input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BE_EVALUATION_TIMEZONE_MISSING")
    source = _load(input_path)
    evidence = source.get("threats")
    if not isinstance(evidence, list):
        raise ValueError("PHASE4BE_THREAT_EVIDENCE_INVALID")
    if [item.get("threat") for item in evidence if isinstance(item, dict)] != list(THREATS):
        raise ValueError("PHASE4BE_THREAT_COVERAGE_OR_ORDER_INVALID")
    rows: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    for item in evidence:
        threat = item["threat"]
        severity = item.get("severity")
        controls = item.get("controls")
        evidence_hashes = item.get("evidence_hashes")
        test_outcome = item.get("adversarial_test_outcome")
        residual = item.get("residual_risk")
        if severity not in {"HIGH", "MEDIUM"} or residual not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("PHASE4BE_RISK_LEVEL_INVALID")
        if not isinstance(controls, list) or len(controls) != len(set(controls)):
            raise ValueError("PHASE4BE_CONTROLS_INVALID")
        if (
            not isinstance(evidence_hashes, list)
            or not evidence_hashes
            or any(not isinstance(value, str) or len(value) != 64 for value in evidence_hashes)
        ):
            raise ValueError("PHASE4BE_EVIDENCE_HASH_INVALID")
        missing = sorted(REQUIRED_CONTROLS[threat] - set(controls))
        reasons: list[str] = []
        if missing:
            reasons.extend(f"MISSING_CONTROL:{control}" for control in missing)
        if test_outcome != "PASS":
            reasons.append("ADVERSARIAL_TEST_NOT_PASSING")
        if severity == "HIGH" and residual == "HIGH":
            reasons.append("UNRESOLVED_HIGH_SEVERITY_RISK")
        row = {
            "threat": threat,
            "severity": severity,
            "controls": sorted(controls),
            "evidence_hashes": sorted(evidence_hashes),
            "adversarial_test_outcome": test_outcome,
            "residual_risk": residual,
            "reason_codes": sorted(reasons),
            "mitigated": not reasons,
        }
        row["row_hash"] = canonical_hash(row)
        rows.append(row)
        findings.extend({"threat": threat, "reason": reason} for reason in row["reason_codes"])
    evaluated_at = now.astimezone(UTC).isoformat()
    model: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4BE",
        "evaluated_at": evaluated_at,
        "input_hash": source["artifact_hash"],
        "threat_count": len(rows),
        "rows": rows,
        "complete_threat_coverage": len(rows) == len(THREATS),
        "all_required_mitigations_verified": not findings,
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    model["artifact_hash"] = _hash(model)
    risk: dict[str, Any] = {
        "schema": RISK_SCHEMA,
        "phase": "4BE",
        "evaluated_at": evaluated_at,
        "threat_model_hash": model["artifact_hash"],
        "findings": findings,
        "finding_count": len(findings),
        "unresolved_high_severity_count": sum(
            finding["reason"] == "UNRESOLVED_HIGH_SEVERITY_RISK" for finding in findings
        ),
        "advancement_allowed": not findings,
        "production_database_mutated": False,
        "research_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    risk["artifact_hash"] = _hash(risk)
    return model, risk


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threat-evidence", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--threat-model-output", type=Path, required=True)
    parser.add_argument("--risk-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    model, risk = build(args.threat_evidence, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.threat_model_output, args.risk_output, model, risk)
    print(json.dumps(risk, sort_keys=True))


if __name__ == "__main__":
    main()
