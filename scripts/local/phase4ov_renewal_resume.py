"""Hash-linked renewal checkpoints and exactly-once resume proof."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from scripts.local.phase4ou_offline_renewal import orchestrate_renewal, verify_orchestration

SCHEMA = "phase4ov.renewal-resume.v1"
GENESIS = "0" * 64


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def create_checkpoints(
    original_records: list[dict[str, object]], orchestration: dict[str, object]
) -> list[dict[str, object]]:
    verification = verify_orchestration(original_records, orchestration)
    if verification["verdict"] != "PASS":
        raise ValueError("orchestration is not verified")
    original_sha = _digest(original_records)
    checkpoints = []
    for completed_count in range(len(orchestration["steps"]) + 1):
        prefix = orchestration["steps"][:completed_count]
        body = {
            "schema": SCHEMA,
            "run_identity_sha256": _digest(
                {
                    "original_records_sha256": original_sha,
                    "evaluated_at": orchestration["evaluated_at"],
                    "renewed_at": orchestration["renewed_at"],
                }
            ),
            "orchestration_sha256": orchestration["orchestration_sha256"],
            "completed_count": completed_count,
            "completed_evidence_ids": [row["evidence_id"] for row in prefix],
            "completed_step_sha256s": [row["step_sha256"] for row in prefix],
            "previous_checkpoint_sha256": (
                checkpoints[-1]["checkpoint_sha256"] if checkpoints else GENESIS
            ),
            "settlement_record_sha256": original_records[-1]["record_sha256"],
            "safety": _safety(),
        }
        checkpoints.append({**body, "checkpoint_sha256": _digest(body)})
    return checkpoints


def resume_from_checkpoint(
    original_records: list[dict[str, object]], checkpoint_chain: list[dict[str, object]]
) -> dict[str, object]:
    errors: list[str] = []
    if not checkpoint_chain:
        return _result(["CHECKPOINT_CHAIN_EMPTY"], None, None)
    last = checkpoint_chain[-1]
    try:
        full = orchestrate_renewal(
            original_records,
            evaluated_at=datetime.fromisoformat(str(last["evaluated_at"])),
            renewed_at=datetime.fromisoformat(str(last["renewed_at"])),
        )
    except (KeyError, TypeError, ValueError):
        # Timestamps live in the run identity, but are copied below by checkpoint_with_timestamps.
        return _result(["CHECKPOINT_TIMELINE_MISSING_OR_INVALID"], None, None)
    expected = build_checkpoint_chain(original_records, full)
    count = last.get("completed_count")
    if not isinstance(count, int) or count < 0 or count >= len(expected):
        errors.append("CHECKPOINT_COMPLETED_COUNT_INVALID")
    elif checkpoint_chain != expected[: count + 1]:
        errors.append("CHECKPOINT_PREFIX_INVALID")
    if last.get("orchestration_sha256") != full["orchestration_sha256"]:
        errors.append("CROSS_RUN_OR_STALE_CHECKPOINT")
    if last.get("settlement_record_sha256") != original_records[-1].get("record_sha256"):
        errors.append("SETTLEMENT_RECORD_BINDING_MISMATCH")
    remaining = (
        full["steps"][count:] if isinstance(count, int) and 0 <= count <= len(full["steps"]) else []
    )
    completed_ids = last.get("completed_evidence_ids", [])
    if set(completed_ids) & {row["evidence_id"] for row in remaining}:
        errors.append("DUPLICATE_RENEWAL_ON_RESUME")
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "checkpoint_sha256": last.get("checkpoint_sha256"),
        "completed_count": count,
        "remaining_evidence_ids": [row["evidence_id"] for row in remaining],
        "final_orchestration": full if not errors else None,
        "uninterrupted_orchestration_sha256": full["orchestration_sha256"],
        "converged": not errors,
        "settlement_record_unchanged": full["settlement_record_unchanged"],
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "resume_sha256": _digest(body)}


def checkpoint_with_timestamps(
    checkpoints: list[dict[str, object]], orchestration: dict[str, object]
) -> list[dict[str, object]]:
    enriched = []
    for checkpoint in checkpoints:
        body = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
        body["evaluated_at"] = orchestration["evaluated_at"]
        body["renewed_at"] = orchestration["renewed_at"]
        enriched.append({**body, "checkpoint_sha256": _digest(body)})
    for index in range(1, len(enriched)):
        enriched[index]["previous_checkpoint_sha256"] = enriched[index - 1]["checkpoint_sha256"]
        unsigned = {
            key: value for key, value in enriched[index].items() if key != "checkpoint_sha256"
        }
        enriched[index]["checkpoint_sha256"] = _digest(unsigned)
    return enriched


def build_checkpoint_chain(original_records, orchestration):
    return checkpoint_with_timestamps(
        create_checkpoints(original_records, orchestration), orchestration
    )


def _result(errors, subject, orchestration):
    body = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "checkpoint_sha256": subject,
        "uninterrupted_orchestration_sha256": orchestration,
        "converged": False,
        "executable": False,
        "safety": _safety(),
    }
    return {**body, "resume_sha256": _digest(body)}


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
