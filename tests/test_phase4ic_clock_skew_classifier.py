from dataclasses import replace

import pytest
from kalshi_predictor.workstation.clock_skew_classifier import (
    ClockSkewClassifierError,
    classify_clock_skew,
    make_clock_comparison_evidence,
    validate_clock_skew_decision,
)


def _evidence(**overrides):
    fields = dict(
        probe_id_hash="a" * 64,
        observed_at_epoch_seconds=100,
        local_epoch_seconds=10_000,
        reference_epoch_seconds=10_000,
        reference_source="NTP_SYNCED",
        uncertainty_seconds=1,
        complete=True,
    )
    fields.update(overrides)
    return make_clock_comparison_evidence(**fields)


def test_trusted_clock_at_exact_skew_and_uncertainty_is_synchronized_without_authority() -> None:
    result = classify_clock_skew(
        _evidence(local_epoch_seconds=10_005, uncertainty_seconds=2), evaluated_at_epoch_seconds=100
    )
    assert result.status == "SYNCHRONIZED" and result.absolute_skew_seconds == 5
    assert not any(
        (
            result.restart_eligible,
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    )
    validate_clock_skew_decision(result)


def test_positive_and_negative_excess_skew_are_never_restartable() -> None:
    positive = classify_clock_skew(
        _evidence(local_epoch_seconds=10_006), evaluated_at_epoch_seconds=100
    )
    negative = classify_clock_skew(
        _evidence(local_epoch_seconds=9_994), evaluated_at_epoch_seconds=100
    )
    assert positive.status == negative.status == "SKEWED"
    assert positive.restart_eligible is negative.restart_eligible is False


def test_untrusted_uncertain_incomplete_future_and_stale_evidence_fail_closed() -> None:
    untrusted = classify_clock_skew(
        _evidence(reference_source="LOCAL_GUESS"), evaluated_at_epoch_seconds=100
    )
    uncertain = classify_clock_skew(
        _evidence(uncertainty_seconds=3), evaluated_at_epoch_seconds=100
    )
    incomplete = classify_clock_skew(_evidence(complete=False), evaluated_at_epoch_seconds=100)
    future = classify_clock_skew(
        _evidence(observed_at_epoch_seconds=101), evaluated_at_epoch_seconds=100
    )
    stale = classify_clock_skew(_evidence(), evaluated_at_epoch_seconds=221)
    assert untrusted.status == uncertain.status == stale.status == "UNKNOWN"
    assert incomplete.status == "INCOMPLETE" and future.status == "TAMPERED"


@pytest.mark.parametrize("source", ["NTP_SYNCED", "WINDOWS_HOST_CLOCK", "SIGNED_TIME_SOURCE"])
def test_all_explicit_trusted_sources_are_supported_deterministically(source) -> None:
    first = classify_clock_skew(_evidence(reference_source=source), evaluated_at_epoch_seconds=100)
    second = classify_clock_skew(_evidence(reference_source=source), evaluated_at_epoch_seconds=100)
    assert first == second and first.status == "SYNCHRONIZED"


def test_tampering_safety_and_operational_surfaces_fail_closed() -> None:
    with pytest.raises(ClockSkewClassifierError, match="EVIDENCE_HASH_MISMATCH"):
        classify_clock_skew(
            replace(_evidence(), local_epoch_seconds=20_000), evaluated_at_epoch_seconds=100
        )
    result = classify_clock_skew(_evidence(), evaluated_at_epoch_seconds=100)
    with pytest.raises(ClockSkewClassifierError, match="DECISION_HASH_MISMATCH"):
        validate_clock_skew_decision(replace(result, reasons=("FORGED",)))
    with pytest.raises(ClockSkewClassifierError, match="SAFETY_BOUNDARY"):
        validate_clock_skew_decision(replace(result, host_restart_authorized=True))
    forbidden = {
        "time",
        "ntp",
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
    assert forbidden.isdisjoint(classify_clock_skew.__code__.co_names)
