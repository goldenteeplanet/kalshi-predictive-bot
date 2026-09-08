"""Localize recovery-chain corruption and emit inert minimal repair plans."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

from scripts.local.phase4ma_recovery_state_machine import (
    CHECKPOINT_FIELDS,
    INVARIANTS,
    STATES,
    expected_proofs,
)

SCHEMA = "phase4mb.checkpoint-corruption-repair-plan.v1"
MANIFEST_SCHEMA = "phase4mb.expected-checkpoint-evidence.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
MANIFEST_FIELDS = {
    "schema",
    "incident_id_sha256",
    "recovery_epoch",
    "entries",
    "manifest_sha256",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def make_evidence_manifest(
    incident_id_sha256: str,
    recovery_epoch: int,
    entries: list[dict[str, str]],
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "incident_id_sha256": incident_id_sha256,
        "recovery_epoch": recovery_epoch,
        "entries": entries,
    }
    result["manifest_sha256"] = _digest(result)
    return result


def plan_repair(
    incident_id_sha256: str,
    recovery_epoch: int,
    checkpoints: object,
    evidence_manifest: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    source_sha256 = _digest(checkpoints)
    manifest_errors: list[str] = []
    if HEX64.fullmatch(str(incident_id_sha256)) is None or type(recovery_epoch) is not int:
        manifest_errors.append("INCIDENT_OR_EPOCH_INVALID")
    evaluation_time = _time(evaluated_at)
    if evaluation_time is None:
        manifest_errors.append("EVALUATION_TIME_INVALID")
    if not isinstance(evidence_manifest, dict) or set(evidence_manifest) != MANIFEST_FIELDS:
        manifest_errors.append("MANIFEST_FIELD_SET_INVALID")
        evidence_manifest = {}
    else:
        body = {key: value for key, value in evidence_manifest.items() if key != "manifest_sha256"}
        if evidence_manifest.get("manifest_sha256") != _digest(body):
            manifest_errors.append("MANIFEST_HASH_MISMATCH")
    if evidence_manifest.get("schema") != MANIFEST_SCHEMA:
        manifest_errors.append("MANIFEST_SCHEMA_INVALID")
    if (
        evidence_manifest.get("incident_id_sha256") != incident_id_sha256
        or evidence_manifest.get("recovery_epoch") != recovery_epoch
    ):
        manifest_errors.append("MANIFEST_IDENTITY_MISMATCH")
    entries = evidence_manifest.get("entries")
    expected_evidence: dict[str, str] = {}
    if (
        not isinstance(entries, list)
        or len(entries) != len(STATES)
        or [entry.get("state") for entry in entries if isinstance(entry, dict)] != STATES
        or any(
            not isinstance(entry, dict)
            or set(entry) != {"state", "evidence_sha256"}
            or HEX64.fullmatch(str(entry.get("evidence_sha256"))) is None
            for entry in entries
        )
    ):
        manifest_errors.append("MANIFEST_ENTRIES_INVALID")
    else:
        expected_evidence = {entry["state"]: entry["evidence_sha256"] for entry in entries}
    if not isinstance(checkpoints, list):
        checkpoints = []
        manifest_errors.append("CHECKPOINTS_NOT_A_LIST")

    trusted: list[dict[str, object]] = []
    identities: dict[str, str] = {}
    previous_hash = "0" * 64
    previous_time: datetime | None = None
    expected_state_index = 0
    boundary_index: int | None = None
    boundary_errors: list[str] = []
    for source_index, checkpoint in enumerate(checkpoints):
        errors: list[str] = []
        if not isinstance(checkpoint, dict) or set(checkpoint) != CHECKPOINT_FIELDS:
            errors.append("FIELD_SET_INVALID")
            checkpoint = {}
        checkpoint_id = checkpoint.get("checkpoint_id")
        fingerprint = _digest(checkpoint)
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            errors.append("ID_INVALID")
        elif checkpoint_id in identities:
            if identities[checkpoint_id] == fingerprint:
                continue
            errors.append("CONFLICTING_REPLAY")
        else:
            identities[checkpoint_id] = fingerprint
        body = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
        if checkpoint.get("checkpoint_sha256") != _digest(body):
            errors.append("HASH_MISMATCH")
        if (
            checkpoint.get("incident_id_sha256") != incident_id_sha256
            or checkpoint.get("recovery_epoch") != recovery_epoch
        ):
            errors.append("INCIDENT_OR_EPOCH_BINDING_MISMATCH")
        state = checkpoint.get("state")
        if state not in STATES:
            errors.append("STATE_INVALID")
        elif expected_state_index >= len(STATES):
            errors.append("POST_TERMINAL_DATA")
        elif state != STATES[expected_state_index]:
            errors.append("STATE_MISSING_DUPLICATED_OR_REVERSED")
        if checkpoint.get("previous_checkpoint_sha256") != previous_hash:
            errors.append("CHAIN_LINK_INVALID")
        occurred = _time(checkpoint.get("occurred_at"))
        if occurred is None:
            errors.append("TIME_INVALID")
        elif evaluation_time is not None and occurred > evaluation_time:
            errors.append("CHECKPOINT_FROM_FUTURE")
        elif previous_time is not None and occurred < previous_time:
            errors.append("TIME_REVERSED")
        if HEX64.fullmatch(str(checkpoint.get("evidence_sha256"))) is None:
            errors.append("EVIDENCE_HASH_INVALID")
        elif (
            state in expected_evidence
            and checkpoint.get("evidence_sha256") != expected_evidence[state]
        ):
            errors.append("EVIDENCE_MANIFEST_MISMATCH")
        if checkpoint.get("invariants") != INVARIANTS:
            errors.append("FAIL_CLOSED_INVARIANTS_INVALID")
        if state in STATES and checkpoint.get("proofs") != expected_proofs(str(state)):
            errors.append("CUMULATIVE_PROOFS_INVALID")
        if errors:
            boundary_index = source_index
            boundary_errors = sorted(set(errors))
            break
        trusted.append(checkpoint)
        previous_hash = str(checkpoint.get("checkpoint_sha256"))
        previous_time = occurred
        expected_state_index += 1
    if boundary_index is None and expected_state_index < len(STATES):
        boundary_index = len(checkpoints)
        boundary_errors = ["CHAIN_TRUNCATED"]

    actions: list[dict[str, object]] = []
    trusted_checkpoint = trusted[-1] if trusted else None
    if manifest_errors:
        actions.append({"action": "ABORT_RECOVERY", "reason": "BASELINE_OR_IDENTITY_UNTRUSTED"})
    elif boundary_index is None:
        actions.append({"action": "NO_REPAIR", "reason": "CHAIN_VALID_AND_CLOSED"})
    elif trusted_checkpoint is None or trusted_checkpoint.get("invariants") != INVARIANTS:
        actions.append({"action": "ABORT_RECOVERY", "reason": "NO_UNAMBIGUOUS_TRUSTED_CHECKPOINT"})
    elif trusted_checkpoint.get("state") == "CLOSED":
        actions.append(
            {
                "action": "DISCARD_SUFFIX",
                "from_source_index": boundary_index,
                "reason": "POST_TERMINAL_DATA",
            }
        )
    else:
        if boundary_index < len(checkpoints):
            actions.append(
                {
                    "action": "DISCARD_SUFFIX",
                    "from_source_index": boundary_index,
                    "reason": "CORRUPTION_BOUNDARY",
                }
            )
        actions.extend(
            [
                {
                    "action": "REACQUIRE_EVIDENCE",
                    "from_state": STATES[len(trusted)],
                    "reason": boundary_errors[0],
                },
                {
                    "action": "REPLAY_FROM_CHECKPOINT",
                    "checkpoint_sha256": trusted_checkpoint.get("checkpoint_sha256"),
                    "checkpoint_state": trusted_checkpoint.get("state"),
                },
                {
                    "action": "REBUILD_RESUME_TOKEN",
                    "checkpoint_sha256": trusted_checkpoint.get("checkpoint_sha256"),
                    "revalidate_invariants": True,
                },
            ]
        )
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not manifest_errors else "REFUSE",
        "errors": sorted(set(manifest_errors)),
        "source_checkpoints_sha256": source_sha256,
        "source_unchanged": _digest(checkpoints) == source_sha256,
        "manifest_sha256": evidence_manifest.get("manifest_sha256"),
        "trusted_prefix_count": len(trusted),
        "trusted_checkpoint_sha256": (
            trusted_checkpoint.get("checkpoint_sha256") if trusted_checkpoint else None
        ),
        "corruption_boundary_index": boundary_index,
        "boundary_errors": boundary_errors,
        "actions": actions,
        "minimality": {
            "action_count": len(actions),
            "duplicate_actions": len({row["action"] for row in actions}) != len(actions),
            "source_edit_authorized": False,
            "hash_invention_authorized": False,
            "revalidation_skip_authorized": False,
        },
        "safety": {
            "planning_only": True,
            "checkpoint_write": False,
            "repair_execution": False,
            "policy_activation": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["plan_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoints")
    parser.add_argument("evidence_manifest")
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--recovery-epoch", type=int, required=True)
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    with open(args.checkpoints, encoding="utf-8") as stream:
        checkpoints = json.load(stream)
    with open(args.evidence_manifest, encoding="utf-8") as stream:
        manifest = json.load(stream)
    result = plan_repair(
        args.incident_id,
        args.recovery_epoch,
        checkpoints,
        manifest,
        evaluated_at=args.evaluated_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
