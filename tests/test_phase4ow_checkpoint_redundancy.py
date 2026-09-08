from __future__ import annotations

import copy

from scripts.local.phase4ow_checkpoint_redundancy import (
    CORRUPTIONS,
    corrupt_replica,
    create_replica,
    recover_replicas,
    run_corruption_matrix,
)
from tests.test_phase4ov_renewal_resume import _fixture

ALLOWED = {"replica-a", "replica-b", "replica-c"}


def test_each_corruption_recovers_from_two_matching_copies_and_repairs_one() -> None:
    records, orchestration, checkpoints = _fixture(minute=1)
    _, _, alternate = _fixture(minute=2)
    result = run_corruption_matrix(
        records,
        checkpoints,
        alternate,
        allowed_replicas=ALLOWED,
        quorum=2,
    )
    assert result["verdict"] == "PASS"
    assert result["corruption_count"] == len(CORRUPTIONS) == 7
    assert all(row["verdict"] == "PASS" and row["repaired"] for row in result["results"])
    assert {row["converged_orchestration_sha256"] for row in result["results"]} == {
        orchestration["orchestration_sha256"]
    }


def test_single_damaged_copy_has_exact_in_memory_repair_plan() -> None:
    records, _, checkpoints = _fixture()
    replicas = [create_replica(name, checkpoints) for name in sorted(ALLOWED)]
    replicas[-1] = corrupt_replica(replicas[-1], "BIT_FLIP")
    result = recover_replicas(records, replicas, allowed_replicas=ALLOWED, quorum=2)
    assert result["verdict"] == "PASS"
    assert result["invalid_replica_ids"] == ["replica-c"]
    assert result["repair_plan"][0]["target_replica_id"] == "replica-c"
    assert result["repair_plan"][0]["replacement_chain"] == checkpoints


def test_simultaneous_corruption_below_quorum_fails_closed() -> None:
    records, _, checkpoints = _fixture()
    replicas = [create_replica(name, checkpoints) for name in sorted(ALLOWED)]
    replicas[1] = corrupt_replica(replicas[1], "TRUNCATION")
    replicas[2] = corrupt_replica(replicas[2], "BIT_FLIP")
    result = recover_replicas(records, replicas, allowed_replicas=ALLOWED, quorum=2)
    assert result["verdict"] == "REFUSE"
    assert "UNIQUE_CHECKPOINT_QUORUM_NOT_MET" in result["errors"]
    assert result["canonical_chain"] is None
    assert result["executable"] is False


def test_cross_run_substitution_and_stale_prefix_are_detected() -> None:
    records, _, checkpoints = _fixture(minute=1)
    _, _, alternate = _fixture(minute=2)
    base = create_replica("replica-c", checkpoints)
    for corruption in ("CROSS_RUN_SUBSTITUTION", "STALE_PREFIX"):
        damaged = corrupt_replica(base, corruption, alternate_chain=alternate)
        result = recover_replicas(
            records,
            [create_replica("replica-a", checkpoints), damaged],
            allowed_replicas=ALLOWED,
            quorum=2,
        )
        assert result["verdict"] == "REFUSE"


def test_duplicate_identity_and_ambiguous_valid_chains_refuse() -> None:
    records, _, checkpoints = _fixture(minute=1)
    same = create_replica("replica-a", checkpoints)
    result = recover_replicas(records, [same, same], allowed_replicas=ALLOWED, quorum=2)
    assert "REPLICA_ID_REPLAY" in result["errors"]
    _, _, alternate = _fixture(minute=2)
    result = recover_replicas(
        records,
        [create_replica("replica-a", checkpoints), create_replica("replica-b", alternate)],
        allowed_replicas=ALLOWED,
        quorum=1,
    )
    assert result["verdict"] == "REFUSE"


def test_recovery_is_deterministic_input_preserving_and_execution_free() -> None:
    records, _, checkpoints = _fixture()
    replicas = [create_replica(name, checkpoints) for name in sorted(ALLOWED)]
    original = copy.deepcopy((records, replicas))
    first = recover_replicas(records, replicas, allowed_replicas=ALLOWED, quorum=2)
    second = recover_replicas(records, replicas, allowed_replicas=ALLOWED, quorum=2)
    assert first == second
    assert (records, replicas) == original
    assert first["settlement_record_unchanged"] is True
    assert first["executable"] is False
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
