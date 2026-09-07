from dataclasses import replace

import pytest
from kalshi_predictor.workstation.post_boot_ui_availability_verification import (
    PostBootUiAvailabilityError,
    evaluate_post_boot_ui_availability,
    make_post_boot_ui_evidence,
    validate_post_boot_ui_decision,
)


def _evidence(**overrides):
    fields = dict(
        probe_identity_hash="a" * 64,
        endpoint_identity_hash="b" * 64,
        observed_at_epoch_seconds=100,
        evidence_age_seconds=1,
        duration_milliseconds=25,
        http_status_code=200,
        response_identity_verified=True,
        probe_integrity_verified=True,
        complete=True,
    )
    fields.update(overrides)
    return make_post_boot_ui_evidence(**fields)


def _evaluate(evidence=None, **overrides):
    fields = dict(
        restart_intent_hash="1" * 64,
        writer_decision_hash="2" * 64,
        writer_exclusivity_verified=True,
        evidence=evidence or _evidence(),
    )
    fields.update(overrides)
    return evaluate_post_boot_ui_availability(**fields)


def test_available_identity_verified_ui_passes_without_authority() -> None:
    first = _evaluate()
    assert first == _evaluate() and first.status == "PASS" and first.post_boot_ui_verified
    assert first.post_boot_chain_may_continue and not first.service_control_authorized
    validate_post_boot_ui_decision(first)


def test_writer_prerequisite_blocks_chain() -> None:
    assert _evaluate(writer_exclusivity_verified=False).status == "INCOMPLETE"


@pytest.mark.parametrize("overrides", [{"http_status_code": 503}, {"duration_milliseconds": 5_001}])
def test_http_failure_or_timeout_fails_closed(overrides) -> None:
    result = _evaluate(_evidence(**overrides))
    assert result.status == "FAIL" and not result.post_boot_chain_may_continue


def test_stale_incomplete_untrusted_or_wrong_identity_is_not_accepted() -> None:
    assert _evaluate(_evidence(evidence_age_seconds=121)).status == "INCOMPLETE"
    assert _evaluate(_evidence(complete=False)).status == "INCOMPLETE"
    assert _evaluate(_evidence(probe_integrity_verified=False)).status == "INCOMPLETE"
    assert _evaluate(_evidence(response_identity_verified=False)).status == "TAMPERED"


def test_evidence_fields_and_hash_tampering_fail_closed() -> None:
    with pytest.raises(PostBootUiAvailabilityError, match="EVIDENCE_FIELD_INVALID"):
        _evidence(http_status_code=999)
    with pytest.raises(PostBootUiAvailabilityError, match="EVIDENCE_HASH_MISMATCH"):
        _evaluate(replace(_evidence(), http_status_code=503))


def test_decision_tampering_and_operational_surfaces_fail_closed() -> None:
    decision = _evaluate()
    with pytest.raises(PostBootUiAvailabilityError, match="DECISION_HASH_MISMATCH"):
        validate_post_boot_ui_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(PostBootUiAvailabilityError, match="SAFETY_BOUNDARY"):
        validate_post_boot_ui_decision(replace(decision, service_control_authorized=True))
    forbidden = {
        "open",
        "connect",
        "request",
        "urlopen",
        "Popen",
        "subprocess",
        "systemctl",
        "restart",
        "shutdown",
        "start",
    }
    assert forbidden.isdisjoint(evaluate_post_boot_ui_availability.__code__.co_names)
