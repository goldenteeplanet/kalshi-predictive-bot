"""Phase 4AX unified artifact-only expiration, revocation, and supersession gate."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ax.gate-input.v1"
SCHEMA = "phase4ax.unified-gate-verdict.v1"
PROOF_SCHEMA = "phase4ax.refusal-precedence-proof.v1"
DEADLINES = ("proposal", "review", "readiness", "approval", "authorization")
PRECEDENCE = (
    "EXPLICIT_REVOCATION",
    "ARTIFACT_SUPERSEDED",
    "DATABASE_IDENTITY_CHANGED",
    "EXECUTOR_BUILD_SUPERSEDED",
    "PROPOSAL_EXPIRED",
    "REVIEW_EXPIRED",
    "READINESS_EXPIRED",
    "APPROVAL_EXPIRED",
    "AUTHORIZATION_EXPIRED",
)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"PHASE4AX_{label}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"PHASE4AX_{label}_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"PHASE4AX_{label}_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4AX_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4AX_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def build(input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4AX_EVALUATION_TIMEZONE_MISSING")
    now = now.astimezone(UTC)
    payload = _load(input_path)
    deadlines = payload.get("deadlines")
    if not isinstance(deadlines, dict) or set(deadlines) != set(DEADLINES):
        raise ValueError("PHASE4AX_DEADLINES_INVALID")
    parsed_deadlines = {
        name: _time(deadlines[name], f"{name.upper()}_DEADLINE") for name in DEADLINES
    }
    current = payload.get("current_bindings")
    expected = payload.get("expected_bindings")
    required_bindings = {
        "proposal_hash",
        "review_hash",
        "readiness_hash",
        "approval_hash",
        "authorization_hash",
        "database_identity_hash",
        "executor_build_identity_hash",
    }
    if not isinstance(current, dict) or not isinstance(expected, dict):
        raise ValueError("PHASE4AX_BINDINGS_INVALID")
    if set(current) != required_bindings or set(expected) != required_bindings:
        raise ValueError("PHASE4AX_BINDINGS_INVALID")
    if any(
        not isinstance(value, str) or len(value) != 64
        for value in [*current.values(), *expected.values()]
    ):
        raise ValueError("PHASE4AX_BINDING_HASH_INVALID")
    revocations = payload.get("revocations")
    if not isinstance(revocations, list) or any(not isinstance(item, dict) for item in revocations):
        raise ValueError("PHASE4AX_REVOCATIONS_INVALID")

    reasons: set[str] = set()
    if any(item.get("active") is True for item in revocations):
        reasons.add("EXPLICIT_REVOCATION")
    artifact_fields = required_bindings - {"database_identity_hash", "executor_build_identity_hash"}
    if any(current[field] != expected[field] for field in artifact_fields):
        reasons.add("ARTIFACT_SUPERSEDED")
    if current["database_identity_hash"] != expected["database_identity_hash"]:
        reasons.add("DATABASE_IDENTITY_CHANGED")
    if current["executor_build_identity_hash"] != expected["executor_build_identity_hash"]:
        reasons.add("EXECUTOR_BUILD_SUPERSEDED")
    for name in DEADLINES:
        if now >= parsed_deadlines[name]:
            reasons.add(f"{name.upper()}_EXPIRED")
    ordered_reasons = [reason for reason in PRECEDENCE if reason in reasons]
    primary = ordered_reasons[0] if ordered_reasons else None
    evaluated_at = now.isoformat()
    verdict: dict[str, Any] = {
        "schema": SCHEMA,
        "phase": "4AX",
        "evaluated_at": evaluated_at,
        "input_hash": payload["artifact_hash"],
        "earliest_expiration": min(parsed_deadlines.values()).isoformat(),
        "advancement_allowed": not ordered_reasons,
        "primary_reason": primary,
        "reason_codes": ordered_reasons,
        "precedence": list(PRECEDENCE),
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    verdict["artifact_hash"] = _hash(verdict)
    proof: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "phase": "4AX",
        "evaluated_at": evaluated_at,
        "verdict_hash": verdict["artifact_hash"],
        "boundary_semantics": "VALID_IFF_NOW_STRICTLY_BEFORE_EVERY_DEADLINE",
        "primary_reason": primary,
        "all_reasons_hash": canonical_hash(ordered_reasons),
        "deterministic_precedence_verified": True,
        "production_database_mutated": False,
        "research_database_mutated": False,
        "services_controlled": False,
        "exchange_requests_made": False,
        "orders_created": False,
        "execution_authorized": False,
    }
    proof["artifact_hash"] = _hash(proof)
    return verdict, proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-input", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--verdict-output", type=Path, required=True)
    parser.add_argument("--proof-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    verdict, proof = build(args.gate_input, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.verdict_output, args.proof_output, verdict, proof)
    print(json.dumps(verdict, sort_keys=True))


if __name__ == "__main__":
    main()
