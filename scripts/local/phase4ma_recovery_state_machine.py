"""Validate recovery checkpoint chains and bounded interruption-resume tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

SCHEMA = "phase4ma.recovery-state-machine.v1"
TOKEN_SCHEMA = "phase4ma.recovery-resume-token.v1"
SIMULATION_SCHEMA = "phase4lz.disaster-recovery-simulation.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
MAX_RESUME_SECONDS = 1_800
STATES = [
    "DECLARED",
    "CONTAINED",
    "CUSTODY_VERIFIED",
    "MEDIA_VERIFIED",
    "AUTHORITIES_REVOKED",
    "POLICY_ROTATED",
    "REPLAY_REBUILT",
    "QUORUM_RESTORED",
    "AFTER_ACTION_CAPTURED",
    "CLOSED",
]
PROOF_STATES = {
    "revocation_evidence": "AUTHORITIES_REVOKED",
    "rotation_evidence": "POLICY_ROTATED",
    "replay_reconstruction": "REPLAY_REBUILT",
    "quorum_restoration": "QUORUM_RESTORED",
    "after_action_evidence": "AFTER_ACTION_CAPTURED",
}
INVARIANTS = {
    "execution_enabled": False,
    "demo_execution_enabled": False,
    "autopilot_enabled": False,
    "paper_order_creation_enabled": False,
    "paper_order_kill_switch": True,
    "service_control_allowed": False,
    "runtime_write_allowed": False,
    "material_access_allowed": False,
}
CHECKPOINT_FIELDS = {
    "checkpoint_id",
    "incident_id_sha256",
    "recovery_epoch",
    "state",
    "occurred_at",
    "evidence_sha256",
    "previous_checkpoint_sha256",
    "invariants",
    "proofs",
    "checkpoint_sha256",
}
TOKEN_FIELDS = {
    "schema",
    "incident_id_sha256",
    "recovery_epoch",
    "checkpoint_sha256",
    "checkpoint_state",
    "rollback_target_state",
    "issued_at",
    "expires_at",
    "nonce_sha256",
    "invariant_snapshot_sha256",
    "token_sha256",
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


def expected_proofs(state: str) -> dict[str, bool]:
    index = STATES.index(state)
    return {name: index >= STATES.index(required) for name, required in PROOF_STATES.items()}


def make_checkpoint(**fields: object) -> dict[str, object]:
    result = dict(fields)
    result["checkpoint_sha256"] = _digest(result)
    return result


def make_resume_token(**fields: object) -> dict[str, object]:
    result = {"schema": TOKEN_SCHEMA, **fields}
    result["token_sha256"] = _digest(result)
    return result


def validate_flow(
    simulation: object,
    checkpoints: object,
    resume_token: object | None,
    used_token_hashes: object,
    *,
    evaluated_at: str,
) -> dict[str, object]:
    errors: list[str] = []
    evaluation_time = _time(evaluated_at)
    if evaluation_time is None:
        errors.append("EVALUATION_TIME_INVALID")
    if not isinstance(simulation, dict) or simulation.get("schema") != SIMULATION_SCHEMA:
        errors.append("SIMULATION_INVALID")
        simulation = {}
    else:
        body = {key: value for key, value in simulation.items() if key != "simulation_sha256"}
        if simulation.get("simulation_sha256") != _digest(body):
            errors.append("SIMULATION_HASH_MISMATCH")
        if simulation.get("fixture_recovery_readiness") != "PASS":
            errors.append("SIMULATION_FIXTURE_NOT_PASSING")
    incident_id = simulation.get("incident_id_sha256")
    epoch = simulation.get("recovery_epoch")
    if HEX64.fullmatch(str(incident_id)) is None or type(epoch) is not int:
        errors.append("SIMULATION_IDENTITY_INVALID")
    if not isinstance(checkpoints, list) or not checkpoints:
        errors.append("CHECKPOINTS_INVALID")
        checkpoints = []
    identities: dict[str, str] = {}
    accepted: list[dict[str, object]] = []
    chain = "0" * 64
    previous_time: datetime | None = None
    state_index = -1
    terminal_seen = False
    for index, checkpoint in enumerate(checkpoints):
        prefix = f"CHECKPOINT_{index}"
        item_errors: list[str] = []
        if not isinstance(checkpoint, dict) or set(checkpoint) != CHECKPOINT_FIELDS:
            item_errors.append("FIELD_SET_INVALID")
            checkpoint = {}
        checkpoint_id = checkpoint.get("checkpoint_id")
        fingerprint = _digest(checkpoint)
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            item_errors.append("ID_INVALID")
        elif checkpoint_id in identities:
            if identities[checkpoint_id] != fingerprint:
                item_errors.append("CONFLICTING_REPLAY")
            errors.extend(f"{prefix}:{error}" for error in item_errors)
            continue
        else:
            identities[checkpoint_id] = fingerprint
        body = {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
        if checkpoint.get("checkpoint_sha256") != _digest(body):
            item_errors.append("HASH_MISMATCH")
        if (
            checkpoint.get("incident_id_sha256") != incident_id
            or checkpoint.get("recovery_epoch") != epoch
        ):
            item_errors.append("INCIDENT_OR_EPOCH_BINDING_MISMATCH")
        state = checkpoint.get("state")
        if state not in STATES:
            item_errors.append("STATE_INVALID")
        elif terminal_seen:
            item_errors.append("POST_TERMINAL_CHANGE")
        elif STATES.index(state) != state_index + 1:
            item_errors.append("STATE_SKIPPED_OR_REVERSED")
        if checkpoint.get("previous_checkpoint_sha256") != chain:
            item_errors.append("CHAIN_LINK_INVALID")
        occurred = _time(checkpoint.get("occurred_at"))
        if occurred is None:
            item_errors.append("TIME_INVALID")
        elif evaluation_time is not None and occurred > evaluation_time:
            item_errors.append("CHECKPOINT_FROM_FUTURE")
        elif previous_time is not None and occurred < previous_time:
            item_errors.append("TIME_REVERSED")
        if HEX64.fullmatch(str(checkpoint.get("evidence_sha256"))) is None:
            item_errors.append("EVIDENCE_HASH_INVALID")
        if checkpoint.get("invariants") != INVARIANTS:
            item_errors.append("FAIL_CLOSED_INVARIANTS_INVALID")
        if state in STATES and checkpoint.get("proofs") != expected_proofs(str(state)):
            item_errors.append("CUMULATIVE_PROOFS_INVALID")
        errors.extend(f"{prefix}:{error}" for error in item_errors)
        if item_errors:
            continue
        state_index = STATES.index(str(state))
        terminal_seen = state == "CLOSED"
        previous_time = occurred
        chain = str(checkpoint.get("checkpoint_sha256"))
        accepted.append(checkpoint)
    if not errors and (not accepted or accepted[-1].get("state") != "CLOSED"):
        errors.append("FLOW_NOT_CLOSED")
    if not isinstance(used_token_hashes, list) or not all(
        isinstance(item, str) for item in used_token_hashes
    ):
        errors.append("USED_TOKEN_SET_INVALID")
        used_token_hashes = []
    resume: dict[str, object] = {"used": resume_token is not None, "validated": False}
    if resume_token is not None:
        token_errors: list[str] = []
        if not isinstance(resume_token, dict) or set(resume_token) != TOKEN_FIELDS:
            token_errors.append("FIELD_SET_INVALID")
            resume_token = {}
        body = {key: value for key, value in resume_token.items() if key != "token_sha256"}
        if resume_token.get("token_sha256") != _digest(body):
            token_errors.append("HASH_MISMATCH")
        if resume_token.get("schema") != TOKEN_SCHEMA:
            token_errors.append("SCHEMA_INVALID")
        if (
            resume_token.get("incident_id_sha256") != incident_id
            or resume_token.get("recovery_epoch") != epoch
        ):
            token_errors.append("INCIDENT_OR_EPOCH_BINDING_MISMATCH")
        checkpoint_by_hash = {item.get("checkpoint_sha256"): item for item in accepted}
        bound = checkpoint_by_hash.get(resume_token.get("checkpoint_sha256"))
        if bound is None or bound.get("state") != resume_token.get("checkpoint_state"):
            token_errors.append("UNVERIFIED_CHECKPOINT")
        rollback = resume_token.get("rollback_target_state")
        checkpoint_state = resume_token.get("checkpoint_state")
        if (
            rollback not in STATES
            or checkpoint_state not in STATES
            or STATES.index(str(rollback)) > STATES.index(str(checkpoint_state))
        ):
            token_errors.append("ROLLBACK_TARGET_INVALID")
        issued = _time(resume_token.get("issued_at"))
        expires = _time(resume_token.get("expires_at"))
        now = evaluation_time
        if issued is None or expires is None or now is None:
            token_errors.append("TIME_INVALID")
        elif (
            expires <= issued
            or (expires - issued).total_seconds() > MAX_RESUME_SECONDS
            or not issued <= now < expires
        ):
            token_errors.append("STALE_FUTURE_OR_OVERLONG")
        if HEX64.fullmatch(str(resume_token.get("nonce_sha256"))) is None:
            token_errors.append("NONCE_DIGEST_INVALID")
        if bound is not None and resume_token.get("invariant_snapshot_sha256") != _digest(
            bound.get("invariants")
        ):
            token_errors.append("INVARIANT_SNAPSHOT_MISMATCH")
        token_hash = resume_token.get("token_sha256")
        if token_hash in used_token_hashes:
            token_errors.append("TOKEN_REPLAYED")
        errors.extend(f"RESUME_TOKEN:{error}" for error in token_errors)
        resume = {
            "used": True,
            "validated": not token_errors,
            "token_sha256": token_hash,
            "checkpoint_state": checkpoint_state,
            "rollback_target_state": rollback,
        }
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": sorted(set(errors)),
        "simulation_sha256": simulation.get("simulation_sha256"),
        "incident_id_sha256": incident_id,
        "recovery_epoch": epoch,
        "current_state": accepted[-1].get("state") if accepted else None,
        "checkpoint_count": len(accepted),
        "chain_head_sha256": chain,
        "resume": resume,
        "safety": {
            "simulation_only": True,
            "checkpoint_persistence": False,
            "policy_activation": False,
            "material_access": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["flow_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("simulation", "checkpoints", "used_token_hashes"):
        parser.add_argument(name)
    parser.add_argument("--resume-token")
    parser.add_argument("--evaluated-at", required=True)
    args = parser.parse_args()
    with open(args.simulation, encoding="utf-8") as stream:
        simulation = json.load(stream)
    with open(args.checkpoints, encoding="utf-8") as stream:
        checkpoints = json.load(stream)
    with open(args.used_token_hashes, encoding="utf-8") as stream:
        used = json.load(stream)
    token = None
    if args.resume_token:
        with open(args.resume_token, encoding="utf-8") as stream:
            token = json.load(stream)
    result = validate_flow(
        simulation,
        checkpoints,
        token,
        used,
        evaluated_at=args.evaluated_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
