from dataclasses import replace

import pytest
from kalshi_predictor.workstation.diagnostic_redaction_audit import (
    REQUIRED_ARTIFACTS,
    DiagnosticRedactionAuditError,
    audit_diagnostic_redaction,
    make_diagnostic_redaction_attestation,
    validate_diagnostic_redaction_audit_decision,
)


def _item(kind, **overrides):
    fields = dict(
        artifact_type=kind,
        artifact_hash=str(sorted(REQUIRED_ARTIFACTS).index(kind) + 1) * 64,
        identifiers_redacted=True,
        raw_content_retained=False,
        verified=True,
        complete=True,
    )
    fields.update(overrides)
    return make_diagnostic_redaction_attestation(**fields)


def _all():
    return [_item(kind) for kind in REQUIRED_ARTIFACTS]


def test_all_eight_artifacts_pass_deterministically_without_authority() -> None:
    first = audit_diagnostic_redaction(_all())
    second = audit_diagnostic_redaction(list(reversed(_all())))
    assert first == second and first.status == "PASS" and first.redaction_proven
    assert first.artifact_count == 8
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_diagnostic_redaction_audit_decision(first)


def test_each_missing_artifact_and_incomplete_artifact_is_incomplete() -> None:
    for missing in REQUIRED_ARTIFACTS:
        result = audit_diagnostic_redaction(
            [item for item in _all() if item.artifact_type != missing]
        )
        assert result.status == "INCOMPLETE"
    records = _all()
    records[0] = _item(records[0].artifact_type, complete=False)
    assert audit_diagnostic_redaction(records).status == "INCOMPLETE"


def test_raw_unredacted_unverified_duplicate_unknown_and_excess_fail_closed() -> None:
    for changes in (
        {"raw_content_retained": True},
        {"identifiers_redacted": False},
        {"verified": False},
    ):
        records = _all()
        records[0] = _item(records[0].artifact_type, **changes)
        assert audit_diagnostic_redaction(records).status == "FAIL"
    assert (
        audit_diagnostic_redaction([_item("WSL_STATUS"), _item("WSL_STATUS")]).status == "TAMPERED"
    )
    unknown = make_diagnostic_redaction_attestation(
        artifact_type="UNKNOWN",
        artifact_hash="a" * 64,
        identifiers_redacted=True,
        raw_content_retained=False,
        verified=True,
        complete=True,
    )
    assert audit_diagnostic_redaction([unknown]).status == "TAMPERED"
    with pytest.raises(DiagnosticRedactionAuditError, match="BOUND_EXCEEDED"):
        audit_diagnostic_redaction(_all(), max_records=7)


def test_attestation_decision_safety_tampering_and_io_surfaces_fail_closed() -> None:
    with pytest.raises(DiagnosticRedactionAuditError, match="ATTESTATION_HASH_MISMATCH"):
        audit_diagnostic_redaction([replace(_item("WSL_STATUS"), verified=False)])
    result = audit_diagnostic_redaction(_all())
    with pytest.raises(DiagnosticRedactionAuditError, match="DECISION_HASH_MISMATCH"):
        validate_diagnostic_redaction_audit_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(DiagnosticRedactionAuditError, match="SAFETY_BOUNDARY"):
        validate_diagnostic_redaction_audit_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "open",
        "run",
        "popen",
        "subprocess",
        "socket",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(audit_diagnostic_redaction.__code__.co_names)
