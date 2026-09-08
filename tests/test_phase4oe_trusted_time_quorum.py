from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from scripts.local.phase4oe_trusted_time_quorum import attest_time, certify_trusted_time

START = datetime(2026, 8, 29, 12, tzinfo=UTC)
PARENT = "a" * 64
WITNESSES = {"alpha", "bravo", "charlie"}


def _attestation(witness: str, offset: int = 0, **overrides):
    values = {
        "witness_id": witness,
        "round_id": "round-7",
        "parent_proof_sha256": PARENT,
        "observed_at": START + timedelta(seconds=offset),
        "watermark": START,
    }
    values.update(overrides)
    return attest_time(**values)


def _certify(rows, **overrides):
    values = {
        "allowed_witnesses": WITNESSES,
        "quorum": 2,
        "expected_round_id": "round-7",
        "expected_parent_sha256": PARENT,
        "prior_watermark": START,
    }
    values.update(overrides)
    return certify_trusted_time(rows, **values)


def test_quorum_certificate_uses_deterministic_lower_median() -> None:
    result = _certify(
        [_attestation("charlie", 20), _attestation("alpha", 0), _attestation("bravo", 10)]
    )
    assert result["verdict"] == "PASS"
    assert result["trusted_time"] == (START + timedelta(seconds=10)).isoformat()
    assert [row["witness_id"] for row in result["attestations"]] == [
        "alpha",
        "bravo",
        "charlie",
    ]


def test_insufficient_quorum_and_excessive_dispersion_refuse() -> None:
    assert "QUORUM_NOT_MET" in _certify([_attestation("alpha")])["errors"]
    dispersed = _certify([_attestation("alpha"), _attestation("bravo", 31)])
    assert dispersed["verdict"] == "REFUSE"
    assert "CLOCK_DISPERSION_EXCEEDED" in dispersed["errors"]


def test_equivocation_is_detected_even_when_both_statements_are_well_formed() -> None:
    rows = [_attestation("alpha", 0), _attestation("alpha", 1), _attestation("bravo", 1)]
    result = _certify(rows)
    assert result["verdict"] == "REFUSE"
    assert "WITNESS_EQUIVOCATION" in result["errors"]
    assert result["equivocators"] == ["alpha"]


def test_duplicate_replay_unknown_witness_wrong_round_parent_and_tamper_refuse() -> None:
    alpha = _attestation("alpha")
    cases = [
        ([alpha, alpha], "DUPLICATE_WITNESS_IDENTITY"),
        ([_attestation("outsider"), _attestation("bravo")], "WITNESS_NOT_ALLOWED"),
        (
            [_attestation("alpha", round_id="other"), _attestation("bravo")],
            "ROUND_MISMATCH",
        ),
        (
            [_attestation("alpha", parent_proof_sha256="b" * 64), _attestation("bravo")],
            "PARENT_MISMATCH",
        ),
    ]
    tampered = copy.deepcopy(alpha)
    tampered["observed_at"] = (START + timedelta(seconds=1)).isoformat()
    cases.append(([tampered, _attestation("bravo")], "ATTESTATION_HASH_MISMATCH"))
    for rows, expected in cases:
        result = _certify(rows)
        assert result["verdict"] == "REFUSE"
        assert expected in result["errors"]


def test_clock_rollback_and_stale_watermark_refuse() -> None:
    result = _certify(
        [
            _attestation("alpha", observed_at=START - timedelta(seconds=1)),
            _attestation("bravo"),
        ]
    )
    assert "WATERMARK_MISMATCH_OR_ROLLBACK" in result["errors"]
    stale_floor = START - timedelta(seconds=1)
    result = _certify(
        [
            _attestation("alpha", watermark=stale_floor),
            _attestation("bravo", watermark=stale_floor),
        ]
    )
    assert "WATERMARK_MISMATCH_OR_ROLLBACK" in result["errors"]


def test_certificate_is_order_independent_input_preserving_and_execution_free() -> None:
    rows = [_attestation("alpha"), _attestation("bravo", 1)]
    original = copy.deepcopy(rows)
    forward = _certify(rows)
    reverse = _certify(list(reversed(rows)))
    assert forward == reverse
    assert rows == original
    assert forward["safety"]["offline_only"] is True
    assert all(value is False for key, value in forward["safety"].items() if key != "offline_only")
