from __future__ import annotations

from scripts.local.phase4mm_handoff_parser_fuzz import bounded_verify
from scripts.local.phase4mn_independent_handoff_verifier import (
    implementation_identity,
    independent_verify,
)
from scripts.local.phase4mo_disagreement_adjudication import (
    _digest,
    inject_fault,
    make_adjudication_packet,
    mutation_corpus,
    run_disagreement_audit,
    validate_adjudication_packet,
)
from tests.test_phase4ml_recovery_handoff_package import _package


def _outputs():
    kwargs = {
        "expected_audience": "dejoia.offline.recovery-reviewer",
        "evaluated_at": "2026-08-29T00:00:00Z",
    }
    return bounded_verify(_package(), **kwargs), independent_verify(_package(), **kwargs)


def _packet(fault="manifest-hash", target="primary"):
    primary, secondary = _outputs()
    return make_adjudication_packet(
        mutation_id=f"{target}-{fault}",
        primary_output=inject_fault(primary, fault) if target == "primary" else primary,
        secondary_output=inject_fault(secondary, fault) if target == "secondary" else secondary,
        baseline_primary=primary,
        baseline_secondary=secondary,
        identities=implementation_identity(),
    )


def _validate(packet, fault="manifest-hash", target="primary", **kwargs):
    primary, secondary = _outputs()
    return validate_adjudication_packet(
        packet,
        expected_mutation_id=f"{target}-{fault}",
        expected_identities=kwargs.get("identities", implementation_identity()),
        baseline_primary=primary,
        baseline_secondary=secondary,
    )


def test_every_injected_disagreement_is_localized_and_kept_refused() -> None:
    result = run_disagreement_audit(
        _package(),
        expected_audience="dejoia.offline.recovery-reviewer",
        evaluated_at="2026-08-29T00:00:00Z",
    )
    assert result["verdict"] == "PASS"
    assert result["mutation_count"] == len(mutation_corpus()) + 1
    assert result["mutation_count"] == result["refused_count"] == result["localized_count"]


def test_packet_binds_raw_outputs_and_never_authorizes_acceptance() -> None:
    packet = _packet()
    result = _validate(packet)
    assert result["verdict"] == "PASS"
    assert packet["consensus_decision"] == "KEEP_REFUSED"
    assert packet["automatic_acceptance_authorized"] is False
    assert packet["recommendation"] == "FIX_PRIMARY"


def test_packet_tampering_and_reordered_outputs_are_refused() -> None:
    packet = _packet()
    packet["primary_output"]["verdict"] = "REFUSE"
    assert "PACKET_HASH_INVALID" in _validate(packet)["errors"]
    packet = _packet()
    packet["primary_output"], packet["secondary_output"] = (
        packet["secondary_output"],
        packet["primary_output"],
    )
    packet["packet_sha256"] = _digest(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    result = _validate(packet)
    assert result["verdict"] == "REFUSE"
    assert any("OUTPUT_TAMPERED_OR_REORDERED" in error for error in result["errors"])


def test_stale_identity_and_adjudication_substitution_are_refused() -> None:
    packet = _packet()
    assert (
        "IMPLEMENTATION_IDENTITY_STALE_OR_SUBSTITUTED"
        in _validate(packet, identities={"combined_sha256": "0" * 64})["errors"]
    )
    packet = _packet()
    packet["recommendation"] = "FIX_SECONDARY"
    packet["packet_sha256"] = _digest(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    assert "ADJUDICATION_SUBSTITUTED" in _validate(packet)["errors"]


def test_unilateral_pass_and_automatic_acceptance_remain_refused() -> None:
    packet = _packet(fault="verdict", target="secondary")
    assert packet["normalized_comparison"]["primary_verdict"] == "PASS"
    assert packet["consensus_decision"] == "KEEP_REFUSED"
    packet["consensus_decision"] = "ACCEPT"
    packet["automatic_acceptance_authorized"] = True
    packet["packet_sha256"] = _digest(
        {key: value for key, value in packet.items() if key != "packet_sha256"}
    )
    result = _validate(packet, fault="verdict", target="secondary")
    assert "UNILATERAL_ACCEPTANCE_FORBIDDEN" in result["errors"]
    assert "AUTOMATIC_ACCEPTANCE_FORBIDDEN" in result["errors"]


def test_audit_is_deterministic_and_has_no_operational_capability() -> None:
    kwargs = {
        "expected_audience": "dejoia.offline.recovery-reviewer",
        "evaluated_at": "2026-08-29T00:00:00Z",
    }
    first = run_disagreement_audit(_package(), **kwargs)
    assert first == run_disagreement_audit(_package(), **kwargs)
    safety = first["safety"]
    assert safety["offline"] is True
    assert safety["bounded"] is True
    assert safety["read_only"] is True
    assert all(
        value is False
        for key, value in safety.items()
        if key not in {"offline", "bounded", "read_only"}
    )
