from dataclasses import replace

import pytest

from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
)
from kalshi_predictor.workstation.post_boot_scheduler_verification import (
    PostBootSchedulerVerificationError,
    evaluate_post_boot_scheduler_verification,
    validate_post_boot_scheduler_decision,
)


def _scheduler(**overrides):
    fields = dict(
        observed_at_epoch_seconds=2_000,
        evidence_age_seconds=5,
        duration_milliseconds=50,
        unit_name=AUTHORITATIVE_UNIT,
        load_state="loaded",
        active_state="active",
        sub_state="running",
        main_pid=123,
        observed_writer_count=1,
        complete=True,
        probe_name="post-boot-read-only",
        source_identity_hash="a" * 64,
        output="healthy",
    )
    fields.update(overrides)
    return probe_authoritative_scheduler_health(make_scheduler_health_observation(**fields))


def _evaluate(evidence=None, **overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        post_boot_wsl_decision_hash="2" * 64,
        post_boot_wsl_verified=True,
        scheduler_evidence=evidence or _scheduler(),
    )
    fields.update(overrides)
    return evaluate_post_boot_scheduler_verification(**fields)


def test_healthy_scheduler_passes_deterministically_without_authority() -> None:
    first = _evaluate()
    assert first == _evaluate()
    assert first.status == "PASS" and first.post_boot_scheduler_verified
    assert first.post_boot_chain_may_continue
    assert not any(
        (first.recovery_authorized, first.restart_authorized, first.execution_authorized)
    )
    validate_post_boot_scheduler_decision(first)


def test_failed_wsl_prerequisite_blocks_scheduler_chain() -> None:
    result = _evaluate(post_boot_wsl_verified=False)
    assert result.status == "INCOMPLETE"
    assert result.reasons == ("POST_BOOT_WSL_PREREQUISITE_NOT_VERIFIED",)


@pytest.mark.parametrize("overrides", [{"active_state": "failed"}, {"observed_writer_count": 2}])
def test_unhealthy_scheduler_fails_closed(overrides) -> None:
    result = _evaluate(_scheduler(**overrides))
    assert result.status == "FAIL" and not result.post_boot_chain_may_continue


def test_stale_incomplete_and_identity_mismatch_are_not_accepted() -> None:
    assert _evaluate(_scheduler(evidence_age_seconds=121)).status == "INCOMPLETE"
    assert _evaluate(_scheduler(complete=False)).status == "INCOMPLETE"
    assert _evaluate(_scheduler(unit_name="other.service")).status == "TAMPERED"


def test_invalid_evidence_hash_and_fields_fail_closed() -> None:
    evidence = _scheduler()
    with pytest.raises(PostBootSchedulerVerificationError, match="EVIDENCE_INVALID"):
        _evaluate(replace(evidence, main_pid=999))
    with pytest.raises(PostBootSchedulerVerificationError, match="FIELD_INVALID"):
        _evaluate(restart_intent_hash="bad")


def test_decision_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = _evaluate()
    with pytest.raises(PostBootSchedulerVerificationError, match="DECISION_HASH_MISMATCH"):
        validate_post_boot_scheduler_decision(replace(decision, scheduler_evidence_hash="3" * 64))
    with pytest.raises(PostBootSchedulerVerificationError, match="SAFETY_BOUNDARY"):
        validate_post_boot_scheduler_decision(replace(decision, service_control_authorized=True))
    forbidden = {"open", "write", "run", "Popen", "subprocess", "spawn", "systemctl", "shutdown"}
    assert forbidden.isdisjoint(evaluate_post_boot_scheduler_verification.__code__.co_names)
