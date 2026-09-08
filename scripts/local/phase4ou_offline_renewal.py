"""Dependency-safe orchestration for offline evidence renewal."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from scripts.local.phase4ot_evidence_aging_handoff import (
    DEPENDENCY_ORDER,
    OFFLINE_REFRESHABLE,
    build_handoff_packet,
    create_evidence_record,
)

SCHEMA = "phase4ou.offline-renewal.v1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def orchestrate_renewal(
    records: list[dict[str, object]], *, evaluated_at: datetime, renewed_at: datetime
) -> dict[str, object]:
    now, renewal_time = _utc(evaluated_at), _utc(renewed_at)
    errors: list[str] = []
    if renewal_time < now:
        errors.append("RENEWAL_CLOCK_ROLLBACK")
    before = build_handoff_packet(records, evaluated_at=now)
    allowed_preflight_errors = {"SETTLEMENT_HANDOFF_NOT_READY"}
    unexpected = set(before["errors"]) - allowed_preflight_errors
    if unexpected:
        errors.append("PREFLIGHT_EVIDENCE_INVALID")
    by_id = {row.get("evidence_id"): row for row in records}
    due = [row["evidence_id"] for row in before["renewal_schedule"]]
    if any(evidence_id not in OFFLINE_REFRESHABLE for evidence_id in due):
        errors.append("SETTLEMENT_EVIDENCE_IN_OFFLINE_PIPELINE")
    renewed, steps = dict(by_id), []
    for evidence_id in DEPENDENCY_ORDER:
        if evidence_id not in due or evidence_id not in OFFLINE_REFRESHABLE:
            continue
        prior = by_id[evidence_id]
        dependencies = list(prior.get("dependencies", []))
        if any(
            dependency in due and dependency not in {step["evidence_id"] for step in steps}
            for dependency in dependencies
        ):
            errors.append("DEPENDENCY_NOT_RENEWED_FIRST")
            continue
        dependency_hashes = {
            dependency: renewed[dependency]["proof_sha256"] for dependency in dependencies
        }
        proof = _digest(
            {
                "evidence_id": evidence_id,
                "predecessor_record_sha256": prior.get("record_sha256"),
                "dependency_proof_sha256s": dependency_hashes,
                "renewed_at": renewal_time.isoformat(),
            }
        )
        ttl = timedelta(seconds=int(prior["ttl_seconds"]))
        replacement = create_evidence_record(
            evidence_id=evidence_id,
            proof_sha256=proof,
            observed_at=renewal_time,
            ttl=ttl,
            dependencies=tuple(dependencies),
        )
        renewed[evidence_id] = replacement
        step_body = {
            "sequence": len(steps) + 1,
            "evidence_id": evidence_id,
            "predecessor_record_sha256": prior.get("record_sha256"),
            "dependency_proof_sha256s": dependency_hashes,
            "renewed_record_sha256": replacement["record_sha256"],
        }
        steps.append({**step_body, "step_sha256": _digest(step_body)})
    if len(steps) != len(due):
        errors.append("PARTIAL_RENEWAL")
    ordered_records = [
        renewed[evidence_id] for evidence_id in DEPENDENCY_ORDER if evidence_id in renewed
    ]
    after = build_handoff_packet(ordered_records, evaluated_at=renewal_time)
    if after.get("executable") is not False:
        errors.append("POST_HANDOFF_EXECUTABLE")
    settlement_before = by_id.get("authoritative_settlement")
    settlement_after = renewed.get("authoritative_settlement")
    if settlement_before != settlement_after:
        errors.append("SETTLEMENT_EVIDENCE_MUTATED")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "evaluated_at": now.isoformat(),
        "renewed_at": renewal_time.isoformat(),
        "due_order": due,
        "steps": steps,
        "renewed_records": ordered_records,
        "preflight_handoff_sha256": before["handoff_sha256"],
        "post_handoff": after,
        "settlement_record_unchanged": settlement_before == settlement_after,
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "orchestration_sha256": _digest(body)}


def verify_orchestration(
    original_records: list[dict[str, object]], orchestration: object
) -> dict[str, object]:
    errors = []
    if not isinstance(orchestration, dict):
        return _result(["ORCHESTRATION_NOT_OBJECT"], None)
    unsigned = {key: value for key, value in orchestration.items() if key != "orchestration_sha256"}
    if orchestration.get("orchestration_sha256") != _digest(unsigned):
        errors.append("ORCHESTRATION_HASH_MISMATCH")
    try:
        replay = orchestrate_renewal(
            original_records,
            evaluated_at=datetime.fromisoformat(str(orchestration["evaluated_at"])),
            renewed_at=datetime.fromisoformat(str(orchestration["renewed_at"])),
        )
    except (KeyError, TypeError, ValueError):
        errors.append("ORCHESTRATION_TIMELINE_INVALID")
    else:
        if replay != orchestration:
            errors.append("NONDETERMINISTIC_OR_ALTERED_RENEWAL")
    steps = orchestration.get("steps", [])
    if [row.get("sequence") for row in steps if isinstance(row, dict)] != list(
        range(1, len(steps) + 1)
    ):
        errors.append("RENEWAL_STEP_SEQUENCE_INVALID")
    if orchestration.get("executable") is not False or orchestration.get("safety") != _safety():
        errors.append("EXECUTION_SAFETY_INVALID")
    return _result(sorted(set(errors)), orchestration.get("orchestration_sha256"))


def _result(errors, subject):
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "subject_sha256": subject,
        "safety": _safety(),
    }
    return {**body, "verification_sha256": _digest(body)}


def _safety() -> dict[str, bool]:
    return {
        "offline_only": True,
        "infrastructure_mutation": False,
        "persistence": False,
        "network_access": False,
        "runtime_write": False,
        "paper_order_creation": False,
        "demo_execution": False,
        "live_execution": False,
        "autopilot": False,
    }
