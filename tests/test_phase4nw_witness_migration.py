from __future__ import annotations

import copy

from scripts.local.phase4nw_witness_migration import build_migration_plan, simulate_migration
from tests.test_phase4nv_witness_placement import _witness


def _layouts():
    initial = [_witness(index) for index in range(3)]
    for witness in initial:
        witness["machine"] = "legacy-machine"
        witness["wsl_distribution"] = "legacy-wsl"
        witness["host_os"] = "legacy-host"
        witness["storage_device"] = "legacy-disk"
        witness["administrator"] = "legacy-admin"
        witness["software_build"] = "legacy-build"
        witness["signing_key_authority"] = "legacy-authority"
        witness["key_id"] = f"legacy-key-{witness['witness_id']}"
    replacements = [_witness(index + 10) for index in range(4)]
    for witness in replacements:
        witness["key_id"] = f"new-key-{witness['witness_id']}"
    return initial, replacements


def _simulation(actions=None):
    initial, replacements = _layouts()
    plan = build_migration_plan(initial, replacements)
    return simulate_migration(initial, replacements, actions or plan["actions"]), plan


def test_plan_is_deterministic_add_before_remove_and_has_rollback_points() -> None:
    initial, replacements = _layouts()
    first = build_migration_plan(initial, replacements)
    assert first == build_migration_plan(initial, replacements)
    first_remove = next(
        index for index, row in enumerate(first["actions"]) if row["action"] == "REVOKE"
    )
    last_add = max(index for index, row in enumerate(first["actions"]) if row["action"] == "ADD")
    assert last_add < first_remove
    assert sum(row["action"] == "ROLLBACK_POINT" for row in first["actions"]) == 2


def test_staged_migration_preserves_policy_and_reaches_diverse_three_of_four() -> None:
    result, plan = _simulation()
    assert result["verdict"] == "PASS"
    assert result["stage_count"] == len(plan["actions"])
    assert result["final_policy"]["threshold"] == 3
    assert len(result["final_policy"]["voters"]) == 4
    assert result["final_effective_independence"] == 4
    assert all(not row["errors"] for row in result["stages"])


def test_remove_before_add_and_premature_threshold_change_refuse() -> None:
    result, plan = _simulation()
    actions = copy.deepcopy(plan["actions"])
    actions.insert(0, {"action": "REVOKE", "witness_id": "w0"})
    assert "REMOVE_BEFORE_ADD" in _simulation(actions)[0]["errors"]
    premature = copy.deepcopy(plan["actions"])
    premature.insert(
        0,
        {
            "action": "ACTIVATE_POLICY",
            "voters": ["w0", "w1", "w2"],
            "threshold": 3,
            "claimed_byzantine_tolerance": 1,
            "required_independence": 3,
        },
    )
    assert "PREMATURE_THRESHOLD_CHANGE" in _simulation(premature)[0]["errors"]
    assert result["verdict"] == "PASS"


def test_incomplete_catchup_key_reuse_and_uncertified_activation_refuse() -> None:
    _, plan = _simulation()
    incomplete = [
        row
        for row in copy.deepcopy(plan["actions"])
        if not (row["action"] == "CATCH_UP" and row.get("witness_id") == "w10")
    ]
    assert "DUAL_ATTESTATION_PRECONDITION_FAILED" in _simulation(incomplete)[0]["errors"]
    reused = copy.deepcopy(plan["actions"])
    add = next(
        row for row in reused if row["action"] == "ADD" and row["witness"]["witness_id"] == "w11"
    )
    add["witness"]["key_id"] = "new-key-w10"
    assert "SIGNING_KEY_REUSE" in _simulation(reused)[0]["errors"]
    uncertified = [
        row
        for row in copy.deepcopy(plan["actions"])
        if not (row["action"] == "CERTIFY" and row.get("witness_id") == "w12")
    ]
    assert "UNCERTIFIED_WITNESS_ACTIVATION" in _simulation(uncertified)[0]["errors"]


def test_shared_domain_regression_and_split_brain_refuse() -> None:
    _, plan = _simulation()
    shared = copy.deepcopy(plan["actions"])
    additions = [row for row in shared if row["action"] == "ADD"]
    additions[1]["witness"]["storage_device"] = additions[0]["witness"]["storage_device"]
    assert "EFFECTIVE_INDEPENDENCE_REGRESSION" in _simulation(shared)[0]["errors"]
    split = copy.deepcopy(plan["actions"])
    split.append({"action": "DECLARE_SECOND_POLICY"})
    assert "SPLIT_BRAIN_CUTOVER" in _simulation(split)[0]["errors"]


def test_loss_of_rollback_and_unsafe_revocation_order_refuse() -> None:
    _, plan = _simulation()
    no_rollback = [
        row for row in copy.deepcopy(plan["actions"]) if row["action"] != "ROLLBACK_POINT"
    ]
    assert "LOSS_OF_ROLLBACK_QUORUM" in _simulation(no_rollback)[0]["errors"]
    unsafe = copy.deepcopy(plan["actions"])
    unsafe.insert(1, {"action": "REVOKE", "witness_id": "w0"})
    assert "UNSAFE_REVOCATION_ORDER" in _simulation(unsafe)[0]["errors"]


def test_simulation_is_input_preserving_and_has_no_execution_capability() -> None:
    initial, replacements = _layouts()
    original = copy.deepcopy((initial, replacements))
    plan = build_migration_plan(initial, replacements)
    result = simulate_migration(initial, replacements, plan["actions"])
    assert (initial, replacements) == original
    safety = result["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
