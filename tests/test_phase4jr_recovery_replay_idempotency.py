from dataclasses import replace

import pytest

from kalshi_predictor.workstation.recovery_replay_idempotency import (
    RecoveryReplayIdempotencyError,
    make_replay_idempotency_case,
    validate_replay_idempotency_decision,
    verify_recovery_replay_idempotency,
)


def _case(index=1, **overrides):
    fields = dict(
        scenario_id_hash=f"{index:064x}",
        input_hash="a" * 64,
        first_result_hash="b" * 64,
        replay_result_hash="b" * 64,
        first_state_hash="c" * 64,
        replay_state_hash="c" * 64,
        initial_effect_count=0,
        first_effect_count=1,
        replay_effect_count=1,
        complete=True,
    )
    fields.update(overrides)
    return make_replay_idempotency_case(**fields)


def test_equivalent_results_state_and_effect_counts_prove_idempotency() -> None:
    cases = [_case(2), _case(1, first_effect_count=0, replay_effect_count=0)]
    first = verify_recovery_replay_idempotency(cases)
    second = verify_recovery_replay_idempotency(list(reversed(cases)))
    assert first == second and first.status == "PASS"
    assert first.replay_idempotency_proven and first.idempotent_count == 2
    assert not first.additional_effects_permitted and not first.restart_authorized
    validate_replay_idempotency_decision(first)


@pytest.mark.parametrize(
    "overrides",
    [
        {"replay_result_hash": "d" * 64},
        {"replay_state_hash": "d" * 64},
        {"replay_effect_count": 2},
        {"initial_effect_count": 2, "first_effect_count": 1, "replay_effect_count": 1},
        {"initial_effect_count": 0, "first_effect_count": 2, "replay_effect_count": 2},
    ],
)
def test_output_state_or_effect_divergence_fails(overrides) -> None:
    result = verify_recovery_replay_idempotency([_case(**overrides)])
    assert result.status == "FAIL" and not result.replay_idempotency_proven


def test_empty_incomplete_duplicate_excess_and_tampered_cases_fail_closed() -> None:
    assert verify_recovery_replay_idempotency([]).status == "INCOMPLETE"
    assert verify_recovery_replay_idempotency([_case(complete=False)]).status == "INCOMPLETE"
    assert verify_recovery_replay_idempotency([_case(), _case()]).status == "TAMPERED"
    with pytest.raises(RecoveryReplayIdempotencyError, match="BOUND_EXCEEDED"):
        verify_recovery_replay_idempotency([_case(1), _case(2)], max_cases=1)
    with pytest.raises(RecoveryReplayIdempotencyError, match="CASE_HASH_MISMATCH"):
        verify_recovery_replay_idempotency([replace(_case(), replay_effect_count=2)])


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = verify_recovery_replay_idempotency([_case()])
    with pytest.raises(RecoveryReplayIdempotencyError, match="DECISION_HASH_MISMATCH"):
        validate_replay_idempotency_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(RecoveryReplayIdempotencyError, match="SAFETY_BOUNDARY"):
        validate_replay_idempotency_decision(replace(decision, additional_effects_permitted=True))


def test_verifier_has_no_operational_surfaces() -> None:
    forbidden = {"open", "write", "run", "Popen", "subprocess", "system", "spawn", "socket"}
    assert forbidden.isdisjoint(verify_recovery_replay_idempotency.__code__.co_names)
