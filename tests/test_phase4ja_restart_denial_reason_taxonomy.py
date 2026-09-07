from dataclasses import replace

import pytest

from kalshi_predictor.workstation.restart_denial_reason_taxonomy import (
    RestartDenialReasonTaxonomyError,
    classify_restart_denial,
    make_restart_denial_evidence,
    validate_restart_denial_decision,
)


def _evidence(**overrides):
    fields = dict(
        incident_id_hash="1" * 64,
        source_decision_hash="2" * 64,
        source_status="DENIED",
        reason_codes=("COOLDOWN_ACTIVE", "PROTECTED_INVARIANT_CHANGED"),
    )
    fields.update(overrides)
    return make_restart_denial_evidence(**fields)


def test_known_reasons_are_canonical_and_order_independent() -> None:
    first = classify_restart_denial(_evidence())
    second = classify_restart_denial(
        _evidence(reason_codes=("PROTECTED_INVARIANT_CHANGED", "COOLDOWN_ACTIVE"))
    )
    assert first == second and first.status == "CLASSIFIED" and first.restart_denied
    assert first.primary_reason == "PROTECTED_INVARIANT_CHANGED"
    assert not first.restart_authorized
    validate_restart_denial_decision(first)


def test_eligible_source_with_no_reasons_is_not_denied_but_not_authorized() -> None:
    result = classify_restart_denial(_evidence(source_status="ELIGIBLE", reason_codes=()))
    assert result.status == "NOT_DENIED" and not result.restart_denied
    assert result.primary_reason == "NONE" and not result.restart_authorized


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason_codes": ("MADE_UP_REASON",)},
        {"reason_codes": ("TEST_HOST", "TEST_HOST")},
        {"reason_codes": ()},
        {"source_status": "ELIGIBLE", "reason_codes": ("TEST_HOST",)},
    ],
)
def test_unknown_duplicate_missing_or_contradictory_reasons_are_tampered(overrides) -> None:
    result = classify_restart_denial(_evidence(**overrides))
    assert result.status == "TAMPERED" and result.restart_denied
    assert result.primary_reason == "EVIDENCE_TAMPERED"


def test_malformed_tampered_and_forged_authority_fail_closed() -> None:
    with pytest.raises(RestartDenialReasonTaxonomyError, match="FIELD_INVALID"):
        make_restart_denial_evidence(
            incident_id_hash="bad",
            source_decision_hash="2" * 64,
            source_status="DENIED",
            reason_codes=(),
        )
    evidence = _evidence()
    with pytest.raises(RestartDenialReasonTaxonomyError, match="EVIDENCE_HASH_MISMATCH"):
        classify_restart_denial(replace(evidence, source_status="ELIGIBLE"))
    decision = classify_restart_denial(evidence)
    with pytest.raises(RestartDenialReasonTaxonomyError, match="DECISION_HASH_MISMATCH"):
        validate_restart_denial_decision(replace(decision, primary_reason="TEST_HOST"))
    with pytest.raises(RestartDenialReasonTaxonomyError, match="SAFETY_BOUNDARY"):
        validate_restart_denial_decision(replace(decision, restart_authorized=True))


def test_taxonomy_has_no_operational_surfaces() -> None:
    forbidden = {"open", "run", "popen", "subprocess", "socket", "shutdown", "systemctl"}
    assert forbidden.isdisjoint(classify_restart_denial.__code__.co_names)
