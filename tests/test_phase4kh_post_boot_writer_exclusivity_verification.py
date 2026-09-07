from dataclasses import replace

import pytest

from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
)
from kalshi_predictor.workstation.post_boot_writer_exclusivity_verification import (
    PostBootWriterExclusivityError,
    evaluate_post_boot_writer_exclusivity,
    validate_post_boot_writer_exclusivity_decision,
)
from kalshi_predictor.workstation.writer_exclusivity_recovery_gate import (
    evaluate_writer_exclusivity_recovery_gate,
    make_writer_inventory_observation,
)


def _gate(
    *,
    identities=(AUTHORITATIVE_UNIT,),
    authoritative=AUTHORITATIVE_UNIT,
    age=1,
    complete=True,
    scheduler_writers=1,
):
    scheduler = probe_authoritative_scheduler_health(
        make_scheduler_health_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            duration_milliseconds=25,
            unit_name=AUTHORITATIVE_UNIT,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            main_pid=123,
            observed_writer_count=scheduler_writers,
            complete=True,
            probe_name="probe",
            source_identity_hash="a" * 64,
            output="ok",
        )
    )
    inventory = make_writer_inventory_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=age,
        writer_identities=identities,
        authoritative_writer_identity=authoritative,
        complete=complete,
        source_identity_hash="b" * 64,
    )
    return evaluate_writer_exclusivity_recovery_gate(scheduler, inventory)


def _evaluate(gate=None, **overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        invariant_decision_hash="2" * 64,
        invariants_verified=True,
        writer_gate=gate or _gate(),
    )
    fields.update(overrides)
    return evaluate_post_boot_writer_exclusivity(**fields)


def test_one_authoritative_writer_passes_deterministically_without_authority() -> None:
    first = _evaluate()
    assert first == _evaluate() and first.status == "PASS" and first.writer_count == 1
    assert first.post_boot_writer_exclusivity_verified and first.post_boot_chain_may_continue
    assert not first.service_control_authorized
    validate_post_boot_writer_exclusivity_decision(first)


def test_invariant_prerequisite_blocks_chain() -> None:
    assert _evaluate(invariants_verified=False).status == "INCOMPLETE"


@pytest.mark.parametrize(
    "gate",
    [
        _gate(identities=()),
        _gate(identities=(AUTHORITATIVE_UNIT, "rogue.service")),
        _gate(identities=("rogue.service",)),
    ],
)
def test_missing_multiple_or_unexpected_writer_fails_closed(gate) -> None:
    result = _evaluate(gate)
    assert result.status == "FAIL" and not result.post_boot_chain_may_continue


def test_stale_and_incomplete_inventory_block_chain() -> None:
    assert _evaluate(_gate(age=121)).status == "INCOMPLETE"
    assert _evaluate(_gate(complete=False)).status == "INCOMPLETE"


def test_evidence_field_and_decision_tampering_fail_closed() -> None:
    with pytest.raises(PostBootWriterExclusivityError, match="EVIDENCE_INVALID"):
        _evaluate(replace(_gate(), writer_count=2))
    with pytest.raises(PostBootWriterExclusivityError, match="FIELD_INVALID"):
        _evaluate(invariant_decision_hash="bad")
    decision = _evaluate()
    with pytest.raises(PostBootWriterExclusivityError, match="DECISION_HASH_MISMATCH"):
        validate_post_boot_writer_exclusivity_decision(replace(decision, writer_count=2))
    with pytest.raises(PostBootWriterExclusivityError, match="SAFETY_BOUNDARY"):
        validate_post_boot_writer_exclusivity_decision(
            replace(decision, service_control_authorized=True)
        )


def test_verifier_has_no_service_or_restart_surface() -> None:
    forbidden = {
        "open",
        "write",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "start",
        "stop",
    }
    assert forbidden.isdisjoint(evaluate_post_boot_writer_exclusivity.__code__.co_names)
