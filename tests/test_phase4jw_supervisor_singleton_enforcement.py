from dataclasses import replace

import pytest
from kalshi_predictor.workstation.supervisor_singleton_enforcement import (
    SupervisorSingletonEnforcementError,
    evaluate_supervisor_singleton_enforcement,
    make_supervisor_singleton_evidence,
    validate_supervisor_singleton_decision,
)


def _evidence(**overrides):
    fields = dict(
        task_proposal_hash="1" * 64,
        exclusion_lock_hash="2" * 64,
        owner_identity_hash="3" * 64,
        instance_policy="IGNORE_NEW",
        maximum_instances=1,
        exclusion_lock_required=True,
        exclusive_owner_proven=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_supervisor_singleton_evidence(**fields)


def test_exact_dual_layer_singleton_is_deterministic_and_non_authorizing() -> None:
    first = evaluate_supervisor_singleton_enforcement(_evidence())
    assert first == evaluate_supervisor_singleton_enforcement(_evidence())
    assert first.status == "ENFORCED" and first.singleton_enforced
    assert first.instance_policy == "IGNORE_NEW" and first.maximum_instances == 1
    assert first.second_instance_denied
    assert not any(
        (first.task_activation_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_supervisor_singleton_decision(first)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"instance_policy": "PARALLEL"}, "INSTANCE_POLICY_INVALID"),
        ({"maximum_instances": 0}, "MAXIMUM_INVALID"),
        ({"maximum_instances": 2}, "MAXIMUM_INVALID"),
        ({"exclusion_lock_required": False}, "LOCK_NOT_REQUIRED"),
        ({"exclusive_owner_proven": False}, "OWNER_UNPROVEN"),
    ],
)
def test_policy_count_lock_or_owner_drift_is_not_enforced(overrides, reason) -> None:
    result = evaluate_supervisor_singleton_enforcement(_evidence(**overrides))
    assert result.status == "NOT_ENFORCED" and not result.singleton_enforced
    assert any(reason in item for item in result.reasons)


def test_incomplete_malformed_and_evidence_tampering_fail_closed() -> None:
    assert (
        evaluate_supervisor_singleton_enforcement(_evidence(evidence_complete=False)).status
        == "INCOMPLETE"
    )
    with pytest.raises(SupervisorSingletonEnforcementError, match="FIELD_INVALID"):
        _evidence(maximum_instances=-1)
    evidence = _evidence()
    with pytest.raises(SupervisorSingletonEnforcementError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_supervisor_singleton_enforcement(replace(evidence, maximum_instances=2))


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = evaluate_supervisor_singleton_enforcement(_evidence())
    with pytest.raises(SupervisorSingletonEnforcementError, match="DECISION_HASH_MISMATCH"):
        validate_supervisor_singleton_decision(replace(decision, enforcement_hash="f" * 64))
    with pytest.raises(SupervisorSingletonEnforcementError, match="SAFETY_BOUNDARY"):
        validate_supervisor_singleton_decision(replace(decision, task_activation_authorized=True))


def test_enforcement_model_has_no_task_process_or_lock_mutation_surface() -> None:
    forbidden = {"open", "write", "unlink", "run", "Popen", "subprocess", "spawn", "schtasks"}
    assert forbidden.isdisjoint(evaluate_supervisor_singleton_enforcement.__code__.co_names)
