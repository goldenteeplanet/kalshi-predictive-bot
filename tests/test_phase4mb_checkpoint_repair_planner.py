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
)
from scripts.local.phase4mb_checkpoint_repair_planner import make_evidence_manifest, plan_repair


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _checkpoints():
    rows = []
    previous = "0" * 64
    for index, state in enumerate(STATES):
        row = make_checkpoint(
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
        rows.append(row)
        previous = row["checkpoint_sha256"]
    return rows


def _manifest(checkpoints=None):
    checkpoints = checkpoints or _checkpoints()
    return make_evidence_manifest(
        "1" * 64,
        8,
        [{"state": row["state"], "evidence_sha256": row["evidence_sha256"]} for row in checkpoints],
    )


def _plan(checkpoints=None, manifest=None):
    return plan_repair(
        "1" * 64,
        8,
        checkpoints or _checkpoints(),
        manifest or _manifest(),
        evaluated_at="2026-08-28T20:10:00Z",
    )


def _rehash(row):
    row["checkpoint_sha256"] = _digest(
        {key: value for key, value in row.items() if key != "checkpoint_sha256"}
    )


def test_clean_closed_chain_needs_no_repair_deterministically() -> None:
    first = _plan()
    assert first == _plan()
    assert first["actions"] == [{"action": "NO_REPAIR", "reason": "CHAIN_VALID_AND_CLOSED"}]
    assert first["source_unchanged"] is True


@pytest.mark.parametrize("index", range(len(STATES)))
def test_rehashed_evidence_mutation_localizes_every_state(index: int) -> None:
    checkpoints = _checkpoints()
    checkpoints[index]["evidence_sha256"] = "f" * 64
    _rehash(checkpoints[index])
    result = _plan(checkpoints)
    assert result["corruption_boundary_index"] == index
    assert result["trusted_prefix_count"] == index
    assert "EVIDENCE_MANIFEST_MISMATCH" in result["boundary_errors"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda rows: rows[4].update(checkpoint_sha256="0" * 64), "HASH_MISMATCH"),
        (lambda rows: rows[4].update(previous_checkpoint_sha256="0" * 64), "CHAIN_LINK_INVALID"),
        (
            lambda rows: rows[4]["invariants"].update(execution_enabled=True),
            "FAIL_CLOSED_INVARIANTS_INVALID",
        ),
        (lambda rows: rows[4].update(occurred_at="2026-08-28T20:01:00Z"), "TIME_REVERSED"),
    ],
)
def test_hash_link_invariant_and_time_corruption_localize(mutation, error: str) -> None:
    checkpoints = _checkpoints()
    mutation(checkpoints)
    result = _plan(checkpoints)
    assert result["corruption_boundary_index"] == 4
    assert error in result["boundary_errors"]


def test_missing_duplicate_conflicting_and_truncated_states_localize() -> None:
    missing = _checkpoints()
    missing.pop(4)
    assert "STATE_MISSING_DUPLICATED_OR_REVERSED" in _plan(missing)["boundary_errors"]
    duplicate = _checkpoints()
    extra = copy.deepcopy(duplicate[3])
    extra["checkpoint_id"] = "different-id"
    _rehash(extra)
    duplicate.insert(4, extra)
    assert "STATE_MISSING_DUPLICATED_OR_REVERSED" in _plan(duplicate)["boundary_errors"]
    conflict = _checkpoints()
    extra = copy.deepcopy(conflict[3])
    extra["evidence_sha256"] = "f" * 64
    _rehash(extra)
    conflict.insert(4, extra)
    assert "CONFLICTING_REPLAY" in _plan(conflict)["boundary_errors"]
    assert "CHAIN_TRUNCATED" in _plan(_checkpoints()[:5])["boundary_errors"]


def test_exact_replay_is_ignored_without_moving_boundary() -> None:
    checkpoints = _checkpoints()
    checkpoints.insert(4, copy.deepcopy(checkpoints[3]))
    result = _plan(checkpoints)
    assert result["corruption_boundary_index"] is None
    assert result["actions"][0]["action"] == "NO_REPAIR"


def test_post_terminal_suffix_is_discard_only() -> None:
    checkpoints = _checkpoints()
    extra = copy.deepcopy(checkpoints[-1])
    extra["checkpoint_id"] = "post-terminal"
    extra["previous_checkpoint_sha256"] = checkpoints[-1]["checkpoint_sha256"]
    _rehash(extra)
    checkpoints.append(extra)
    result = _plan(checkpoints)
    assert result["boundary_errors"] == ["POST_TERMINAL_DATA"]
    assert [row["action"] for row in result["actions"]] == ["DISCARD_SUFFIX"]


def test_first_checkpoint_corruption_requires_abort() -> None:
    checkpoints = _checkpoints()
    checkpoints[0]["invariants"]["paper_order_creation_enabled"] = True
    _rehash(checkpoints[0])
    result = _plan(checkpoints)
    assert result["trusted_prefix_count"] == 0
    assert result["actions"][0]["action"] == "ABORT_RECOVERY"


def test_repair_actions_are_minimal_declarative_and_revalidate() -> None:
    checkpoints = _checkpoints()[:6]
    result = _plan(checkpoints)
    actions = [row["action"] for row in result["actions"]]
    assert actions == ["REACQUIRE_EVIDENCE", "REPLAY_FROM_CHECKPOINT", "REBUILD_RESUME_TOKEN"]
    assert result["minimality"] == {
        "action_count": 3,
        "duplicate_actions": False,
        "source_edit_authorized": False,
        "hash_invention_authorized": False,
        "revalidation_skip_authorized": False,
    }


def test_untrusted_manifest_or_identity_requires_abort_and_refusal() -> None:
    manifest = _manifest()
    manifest["entries"][0]["evidence_sha256"] = "f" * 64
    result = _plan(manifest=manifest)
    assert result["verdict"] == "REFUSE"
    assert result["actions"][0]["action"] == "ABORT_RECOVERY"


def test_planner_has_no_write_runtime_or_action_capability() -> None:
    safety = _plan(_checkpoints()[:5])["safety"]
    assert safety["planning_only"] is True
    assert all(value is False for key, value in safety.items() if key != "planning_only")
