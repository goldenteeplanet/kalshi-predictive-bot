from dataclasses import replace

import pytest
from kalshi_predictor.workstation.corrupt_cooldown_state_refusal import (
    CorruptCooldownStateRefusalError,
    evaluate_corrupt_cooldown_state_refusal,
    make_cooldown_artifact_evidence,
    validate_cooldown_artifact_decision,
)


def _evidence(**overrides):
    fields = dict(
        artifact_path_hash="1" * 64,
        observed_file_hash="2" * 64,
        expected_chain_head_hash="3" * 64,
        file_present=True,
        trailing_record_complete=True,
        json_valid=True,
        schema_valid=True,
        record_hashes_valid=True,
        chain_valid=True,
        durability_proven=True,
        evidence_complete=True,
    )
    fields.update(overrides)
    return make_cooldown_artifact_evidence(**fields)


def test_fully_validated_artifact_is_accepted_without_restart_authority() -> None:
    first = evaluate_corrupt_cooldown_state_refusal(_evidence())
    assert first == evaluate_corrupt_cooldown_state_refusal(_evidence())
    assert first.status == "ACCEPTED" and first.cooldown_state_accepted
    assert not first.repair_permitted and not first.empty_history_assumption_permitted
    assert not first.restart_authorized
    validate_cooldown_artifact_decision(first)


@pytest.mark.parametrize(
    "field",
    [
        "file_present",
        "trailing_record_complete",
        "json_valid",
        "schema_valid",
        "record_hashes_valid",
        "chain_valid",
        "durability_proven",
    ],
)
def test_each_missing_or_corrupt_artifact_property_is_refused(field) -> None:
    result = evaluate_corrupt_cooldown_state_refusal(_evidence(**{field: False}))
    assert result.status == "REFUSED" and not result.cooldown_state_accepted
    assert not result.repair_permitted and not result.empty_history_assumption_permitted


def test_multiple_corruptions_are_reported_in_stable_priority_order() -> None:
    result = evaluate_corrupt_cooldown_state_refusal(
        _evidence(file_present=False, json_valid=False, chain_valid=False)
    )
    assert result.reasons == (
        "COOLDOWN_ARTIFACT_MISSING",
        "COOLDOWN_ARTIFACT_JSON_INVALID",
        "COOLDOWN_ARTIFACT_CHAIN_INVALID",
    )


def test_incomplete_malformed_and_tampered_evidence_fail_closed() -> None:
    assert (
        evaluate_corrupt_cooldown_state_refusal(_evidence(evidence_complete=False)).status
        == "INCOMPLETE"
    )
    with pytest.raises(CorruptCooldownStateRefusalError, match="FIELD_INVALID"):
        _evidence(observed_file_hash="bad")
    evidence = _evidence()
    with pytest.raises(CorruptCooldownStateRefusalError, match="EVIDENCE_HASH_MISMATCH"):
        evaluate_corrupt_cooldown_state_refusal(replace(evidence, chain_valid=False))


def test_decision_and_authority_tampering_fail_closed() -> None:
    decision = evaluate_corrupt_cooldown_state_refusal(_evidence())
    with pytest.raises(CorruptCooldownStateRefusalError, match="DECISION_HASH_MISMATCH"):
        validate_cooldown_artifact_decision(replace(decision, reasons=("FORGED",)))
    with pytest.raises(CorruptCooldownStateRefusalError, match="SAFETY_BOUNDARY"):
        validate_cooldown_artifact_decision(replace(decision, repair_permitted=True))
    with pytest.raises(CorruptCooldownStateRefusalError, match="SAFETY_BOUNDARY"):
        validate_cooldown_artifact_decision(replace(decision, restart_authorized=True))


def test_refusal_has_no_repair_filesystem_or_operational_surface() -> None:
    forbidden = {
        "open",
        "write",
        "unlink",
        "truncate",
        "run",
        "Popen",
        "subprocess",
        "system",
        "spawn",
    }
    assert forbidden.isdisjoint(evaluate_corrupt_cooldown_state_refusal.__code__.co_names)
