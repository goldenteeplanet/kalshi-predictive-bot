from dataclasses import replace

import pytest

from kalshi_predictor.workstation.partial_evidence_refusal import (
    REQUIRED_EVIDENCE_TYPES,
    PartialEvidenceRefusalError,
    evaluate_partial_evidence_refusal,
    make_failure_evidence_reference,
    validate_failure_evidence_envelope_decision,
)


def _ref(kind, verified=True, complete=True):
    digit = (
        sorted(REQUIRED_EVIDENCE_TYPES).index(kind) + 1 if kind in REQUIRED_EVIDENCE_TYPES else 9
    )
    return make_failure_evidence_reference(
        evidence_type=kind, artifact_hash=str(digit) * 64, verified=verified, complete=complete
    )


def _all():
    return [_ref(kind) for kind in REQUIRED_EVIDENCE_TYPES]


def test_complete_verified_set_is_deterministic_but_non_authorizing() -> None:
    first = evaluate_partial_evidence_refusal(_all())
    second = evaluate_partial_evidence_refusal(list(reversed(_all())))
    assert first == second and first.status == "COMPLETE" and first.complete_evidence_proven
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_failure_evidence_envelope_decision(first)


def test_each_missing_required_type_is_refused_as_incomplete() -> None:
    for missing in REQUIRED_EVIDENCE_TYPES:
        result = evaluate_partial_evidence_refusal(
            [item for item in _all() if item.evidence_type != missing]
        )
        assert result.status == "INCOMPLETE"
        assert f"FAILURE_EVIDENCE_MISSING:{missing}" in result.reasons


def test_incomplete_unverified_duplicate_unknown_and_excess_fail_closed() -> None:
    records = _all()
    records[0] = _ref(records[0].evidence_type, complete=False)
    assert evaluate_partial_evidence_refusal(records).status == "INCOMPLETE"
    records = _all()
    records[0] = _ref(records[0].evidence_type, verified=False)
    assert evaluate_partial_evidence_refusal(records).status == "REFUSED"
    assert (
        evaluate_partial_evidence_refusal([_ref("FAILURE_QUORUM"), _ref("FAILURE_QUORUM")]).status
        == "TAMPERED"
    )
    assert evaluate_partial_evidence_refusal([_ref("UNKNOWN_TYPE")]).status == "TAMPERED"
    with pytest.raises(PartialEvidenceRefusalError, match="BOUND_EXCEEDED"):
        evaluate_partial_evidence_refusal(_all(), max_records=3)


def test_reference_decision_safety_tampering_and_io_surfaces_fail_closed() -> None:
    with pytest.raises(PartialEvidenceRefusalError, match="REFERENCE_HASH_MISMATCH"):
        evaluate_partial_evidence_refusal([replace(_ref("FAILURE_QUORUM"), verified=False)])
    result = evaluate_partial_evidence_refusal(_all())
    with pytest.raises(PartialEvidenceRefusalError, match="DECISION_HASH_MISMATCH"):
        validate_failure_evidence_envelope_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(PartialEvidenceRefusalError, match="SAFETY_BOUNDARY"):
        validate_failure_evidence_envelope_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "subprocess",
        "socket",
        "restart",
        "reboot",
        "shutdown",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(evaluate_partial_evidence_refusal.__code__.co_names)
