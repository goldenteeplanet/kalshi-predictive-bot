from dataclasses import replace

import pytest

from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
)
from kalshi_predictor.workstation.post_boot_invariant_verification import (
    PostBootInvariantVerificationError,
    evaluate_post_boot_invariant_verification,
    validate_post_boot_invariant_decision,
)
from kalshi_predictor.workstation.protected_invariant_recovery_gate import (
    evaluate_protected_invariant_recovery_gate,
    make_protected_invariant_observation,
)
from kalshi_predictor.workstation.writer_exclusivity_recovery_gate import (
    evaluate_writer_exclusivity_recovery_gate,
    make_writer_inventory_observation,
)


def _gate(**overrides):
    scheduler = probe_authoritative_scheduler_health(
        make_scheduler_health_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=1,
            duration_milliseconds=25,
            unit_name=AUTHORITATIVE_UNIT,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            main_pid=123,
            observed_writer_count=1,
            complete=True,
            probe_name="probe",
            source_identity_hash="a" * 64,
            output="ok",
        )
    )
    inventory = make_writer_inventory_observation(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=1,
        writer_identities=(AUTHORITATIVE_UNIT,),
        authoritative_writer_identity=AUTHORITATIVE_UNIT,
        complete=True,
        source_identity_hash="b" * 64,
    )
    writer = evaluate_writer_exclusivity_recovery_gate(scheduler, inventory)
    fields = dict(
        observed_at_epoch_seconds=100,
        evidence_age_seconds=1,
        paper_orders_count=204,
        position_sizing_max_id=239,
        position_sizing_count=239,
        advanced_risk_max_id=239,
        advanced_risk_count=239,
        protected_order_id=204,
        protected_order_status="filled",
        protected_order_ticker="KXRAINAUSM-26AUG-1",
        protected_order_quantity=1,
        protected_forecast_id=523912,
        protected_fill_count=1,
        phase3m_max_id=231,
        phase3n_max_id=231,
        complete=True,
        source_identity_hash="c" * 64,
    )
    fields.update(overrides)
    return evaluate_protected_invariant_recovery_gate(
        writer, make_protected_invariant_observation(**fields)
    )


def _evaluate(gate=None, **overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        database_decision_hash="2" * 64,
        database_verified=True,
        invariant_gate=gate or _gate(),
    )
    fields.update(overrides)
    return evaluate_post_boot_invariant_verification(**fields)


def test_exact_protected_snapshot_passes_deterministically_without_authority() -> None:
    first = _evaluate()
    assert first == _evaluate() and first.status == "PASS"
    assert first.post_boot_invariants_verified and first.post_boot_chain_may_continue
    assert not first.database_write_authorized
    validate_post_boot_invariant_decision(first)


def test_database_prerequisite_blocks_chain() -> None:
    assert _evaluate(database_verified=False).status == "INCOMPLETE"


@pytest.mark.parametrize(
    ("field", "value"),
    [("paper_orders_count", 205), ("protected_fill_count", 2), ("phase3n_max_id", 232)],
)
def test_protected_invariant_drift_fails_closed(field, value) -> None:
    result = _evaluate(_gate(**{field: value}))
    assert result.status == "FAIL" and field in result.reasons[0]


def test_stale_and_incomplete_evidence_block_chain() -> None:
    assert _evaluate(_gate(evidence_age_seconds=121)).status == "INCOMPLETE"
    assert _evaluate(_gate(complete=False)).status == "INCOMPLETE"


def test_evidence_hash_field_and_decision_tampering_fail_closed() -> None:
    with pytest.raises(PostBootInvariantVerificationError, match="EVIDENCE_INVALID"):
        _evaluate(replace(_gate(), invariant_snapshot_hash="0" * 64))
    with pytest.raises(PostBootInvariantVerificationError, match="FIELD_INVALID"):
        _evaluate(database_decision_hash="bad")
    decision = _evaluate()
    with pytest.raises(PostBootInvariantVerificationError, match="DECISION_HASH_MISMATCH"):
        validate_post_boot_invariant_decision(replace(decision, invariant_snapshot_hash="3" * 64))
    with pytest.raises(PostBootInvariantVerificationError, match="SAFETY_BOUNDARY"):
        validate_post_boot_invariant_decision(replace(decision, recovery_authorized=True))


def test_verifier_has_no_database_service_or_restart_surface() -> None:
    forbidden = {
        "open",
        "connect",
        "execute",
        "write",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
    }
    assert forbidden.isdisjoint(evaluate_post_boot_invariant_verification.__code__.co_names)
