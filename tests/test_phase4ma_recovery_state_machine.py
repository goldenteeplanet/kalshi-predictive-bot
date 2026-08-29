from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4ma_recovery_state_machine import (
    INVARIANTS,
    STATES,
    expected_proofs,
    make_checkpoint,
    make_resume_token,
    validate_flow,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _simulation():
    value = {
        "schema": "phase4lz.disaster-recovery-simulation.v1",
        "fixture_recovery_readiness": "PASS",
        "incident_id_sha256": "1" * 64,
        "recovery_epoch": 8,
    }
    value["simulation_sha256"] = _digest(value)
    return value


def _checkpoints():
    result = []
    previous = "0" * 64
    for index, state in enumerate(STATES):
        checkpoint = make_checkpoint(
            checkpoint_id=f"checkpoint-{index}",
            incident_id_sha256="1" * 64,
            recovery_epoch=8,
            state=state,
            occurred_at=f"2026-08-28T20:{index:02d}:00Z",
            evidence_sha256=f"{index:x}" * 64,
            previous_checkpoint_sha256=previous,
            invariants=copy.deepcopy(INVARIANTS),
            proofs=expected_proofs(state),
        )
        result.append(checkpoint)
        previous = checkpoint["checkpoint_sha256"]
    return result


def _token(checkpoints=None):
    checkpoints = checkpoints or _checkpoints()
    bound = checkpoints[5]
    return make_resume_token(
        incident_id_sha256="1" * 64,
        recovery_epoch=8,
        checkpoint_sha256=bound["checkpoint_sha256"],
        checkpoint_state=bound["state"],
        rollback_target_state="AUTHORITIES_REVOKED",
        issued_at="2026-08-28T20:05:30Z",
        expires_at="2026-08-28T20:25:30Z",
        nonce_sha256="a" * 64,
        invariant_snapshot_sha256=_digest(bound["invariants"]),
    )


def _validate(checkpoints=None, token=None, used=None, at="2026-08-28T20:10:00Z"):
    return validate_flow(
        _simulation(),
        checkpoints or _checkpoints(),
        token,
        used or [],
        evaluated_at=at,
    )


def _rehash(checkpoint):
    checkpoint["checkpoint_sha256"] = _digest(
        {key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}
    )


def test_uninterrupted_flow_is_deterministic_closed_and_hash_chained() -> None:
    first = _validate()
    assert first == _validate()
    assert first["verdict"] == "PASS"
    assert first["current_state"] == "CLOSED"
    assert first["checkpoint_count"] == len(STATES)


def test_valid_resume_token_binds_verified_checkpoint_and_rollback() -> None:
    checkpoints = _checkpoints()
    result = _validate(checkpoints, _token(checkpoints))
    assert result["verdict"] == "PASS"
    assert result["resume"]["validated"] is True


def test_exact_checkpoint_replay_is_idempotent() -> None:
    checkpoints = _checkpoints()
    checkpoints.insert(4, copy.deepcopy(checkpoints[3]))
    result = _validate(checkpoints)
    assert result["verdict"] == "PASS"
    assert result["checkpoint_count"] == len(STATES)


def test_conflicting_replay_refuses() -> None:
    checkpoints = _checkpoints()
    conflict = copy.deepcopy(checkpoints[3])
    conflict["evidence_sha256"] = "f" * 64
    _rehash(conflict)
    checkpoints.insert(4, conflict)
    assert any("CONFLICTING_REPLAY" in error for error in _validate(checkpoints)["errors"])


@pytest.mark.parametrize("remove_index", [0, 3, 6, 8])
def test_skipped_states_refuse(remove_index: int) -> None:
    checkpoints = _checkpoints()
    checkpoints.pop(remove_index)
    assert any("STATE_SKIPPED_OR_REVERSED" in error for error in _validate(checkpoints)["errors"])


def test_reversed_time_and_chain_link_refuse() -> None:
    checkpoints = _checkpoints()
    checkpoints[4]["occurred_at"] = "2026-08-28T20:01:00Z"
    checkpoints[5]["previous_checkpoint_sha256"] = "0" * 64
    _rehash(checkpoints[4])
    _rehash(checkpoints[5])
    errors = _validate(checkpoints)["errors"]
    assert any("TIME_REVERSED" in error for error in errors)
    assert any("CHAIN_LINK_INVALID" in error for error in errors)


def test_future_checkpoint_and_ambiguous_evaluation_time_refuse() -> None:
    checkpoints = _checkpoints()
    checkpoints[9]["occurred_at"] = "2026-08-28T20:11:00Z"
    _rehash(checkpoints[9])
    assert any("CHECKPOINT_FROM_FUTURE" in error for error in _validate(checkpoints)["errors"])
    assert "EVALUATION_TIME_INVALID" in _validate(at="ambiguous")["errors"]


def test_post_terminal_change_refuses() -> None:
    checkpoints = _checkpoints()
    extra = copy.deepcopy(checkpoints[-1])
    extra["checkpoint_id"] = "after-close"
    extra["previous_checkpoint_sha256"] = checkpoints[-1]["checkpoint_sha256"]
    _rehash(extra)
    checkpoints.append(extra)
    assert any("POST_TERMINAL_CHANGE" in error for error in _validate(checkpoints)["errors"])


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("incident_id_sha256", "2" * 64, "INCIDENT_OR_EPOCH_BINDING_MISMATCH"),
        ("recovery_epoch", 9, "INCIDENT_OR_EPOCH_BINDING_MISMATCH"),
        ("evidence_sha256", "bad", "EVIDENCE_HASH_INVALID"),
    ],
)
def test_checkpoint_identity_epoch_and_evidence_binding_refuse(field, value, error: str) -> None:
    checkpoints = _checkpoints()
    checkpoints[2][field] = value
    _rehash(checkpoints[2])
    assert any(error in item for item in _validate(checkpoints)["errors"])


def test_fail_closed_invariant_broadening_refuses_at_any_checkpoint() -> None:
    checkpoints = _checkpoints()
    checkpoints[6]["invariants"]["paper_order_creation_enabled"] = True
    _rehash(checkpoints[6])
    assert any(
        "FAIL_CLOSED_INVARIANTS_INVALID" in error for error in _validate(checkpoints)["errors"]
    )


def test_lost_revocation_rotation_replay_quorum_and_after_action_proofs_refuse() -> None:
    for state, proof in (
        ("AUTHORITIES_REVOKED", "revocation_evidence"),
        ("POLICY_ROTATED", "rotation_evidence"),
        ("REPLAY_REBUILT", "replay_reconstruction"),
        ("QUORUM_RESTORED", "quorum_restoration"),
        ("AFTER_ACTION_CAPTURED", "after_action_evidence"),
    ):
        checkpoints = _checkpoints()
        index = STATES.index(state)
        checkpoints[index]["proofs"][proof] = False
        _rehash(checkpoints[index])
        assert any(
            "CUMULATIVE_PROOFS_INVALID" in error for error in _validate(checkpoints)["errors"]
        )


def test_resume_from_unverified_checkpoint_and_bad_rollback_refuse() -> None:
    checkpoints = _checkpoints()
    token = _token(checkpoints)
    token["checkpoint_sha256"] = "f" * 64
    token["rollback_target_state"] = "CLOSED"
    token["token_sha256"] = _digest(
        {key: value for key, value in token.items() if key != "token_sha256"}
    )
    errors = _validate(checkpoints, token)["errors"]
    assert any("UNVERIFIED_CHECKPOINT" in error for error in errors)
    assert any("ROLLBACK_TARGET_INVALID" in error for error in errors)


def test_stale_and_replayed_resume_token_refuse() -> None:
    token = _token()
    assert any(
        "STALE_FUTURE_OR_OVERLONG" in error
        for error in _validate(token=token, at="2026-08-28T20:25:30Z")["errors"]
    )
    assert any(
        "TOKEN_REPLAYED" in error
        for error in _validate(token=token, used=[token["token_sha256"]])["errors"]
    )


def test_simulator_has_no_persistence_runtime_or_action_capability() -> None:
    safety = _validate()["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
