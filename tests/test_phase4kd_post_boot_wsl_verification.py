from dataclasses import replace

import pytest

from kalshi_predictor.workstation.post_boot_wsl_verification import (
    PostBootWslVerificationError,
    evaluate_post_boot_wsl_verification,
    make_post_boot_wsl_evidence,
    validate_post_boot_wsl_decision,
)


def _evidence(**overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        pre_boot_identity_hash="2" * 64,
        post_boot_identity_hash="3" * 64,
        wsl_probe_hash="4" * 64,
        authoritative_distro_hash="5" * 64,
        intent_created_at_epoch=1_000,
        observed_at_epoch=1_300,
        wsl_available=True,
        authoritative_distro_running=True,
        probe_integrity_verified=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_post_boot_wsl_evidence(**fields)


def test_changed_boot_and_running_authoritative_distro_pass_non_authorizing() -> None:
    first = evaluate_post_boot_wsl_verification(_evidence())
    assert first == evaluate_post_boot_wsl_verification(_evidence())
    assert first.status == "PASS" and first.post_boot_wsl_verified
    assert first.post_boot_chain_may_continue and first.observation_delay_seconds == 300
    assert not any(
        (first.recovery_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_post_boot_wsl_decision(first)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"wsl_available": False}, "WSL_UNAVAILABLE"),
        ({"authoritative_distro_running": False}, "DISTRO_NOT_RUNNING"),
    ],
)
def test_wsl_or_distro_failure_blocks_post_boot_chain(overrides, reason) -> None:
    result = evaluate_post_boot_wsl_verification(_evidence(**overrides))
    assert result.status == "FAIL" and not result.post_boot_chain_may_continue
    assert any(reason in item for item in result.reasons)


def test_unchanged_boot_or_pre_intent_observation_is_tampered() -> None:
    assert (
        evaluate_post_boot_wsl_verification(_evidence(post_boot_identity_hash="2" * 64)).status
        == "TAMPERED"
    )
    assert (
        evaluate_post_boot_wsl_verification(_evidence(observed_at_epoch=999)).status == "TAMPERED"
    )


def test_incomplete_unverified_malformed_and_evidence_tampering_fail_closed() -> None:
    assert (
        evaluate_post_boot_wsl_verification(_evidence(evidence_complete=False)).status
        == "INCOMPLETE"
    )
    assert (
        evaluate_post_boot_wsl_verification(_evidence(probe_integrity_verified=False)).status
        == "INCOMPLETE"
    )
    with pytest.raises(PostBootWslVerificationError, match="FIELD_INVALID"):
        _evidence(wsl_probe_hash="bad")
    evidence = _evidence()
    with pytest.raises(PostBootWslVerificationError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_post_boot_wsl_verification(replace(evidence, wsl_available=False))


def test_decision_authority_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = evaluate_post_boot_wsl_verification(_evidence())
    with pytest.raises(PostBootWslVerificationError, match="DECISION_HASH_MISMATCH"):
        validate_post_boot_wsl_decision(replace(decision, observation_delay_seconds=1))
    with pytest.raises(PostBootWslVerificationError, match="SAFETY_BOUNDARY"):
        validate_post_boot_wsl_decision(replace(decision, recovery_authorized=True))
    forbidden = {"open", "write", "run", "Popen", "subprocess", "spawn", "wsl", "shutdown"}
    assert forbidden.isdisjoint(evaluate_post_boot_wsl_verification.__code__.co_names)
