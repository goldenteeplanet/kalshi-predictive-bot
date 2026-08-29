from __future__ import annotations

import pytest

from scripts.local.phase4lt_canonicalization_invariance_audit import (
    CanonicalizationError,
    canonical_hash,
    canonicalize,
    run_audit,
)


def test_audit_is_deterministic_and_complete() -> None:
    first = run_audit()
    assert first == run_audit()
    assert first["verdict"] == "PASS"
    assert first["coverage"]["invariant_case_count"] == 5
    assert first["coverage"]["refusal_case_count"] == 10


def test_crlf_unicode_order_and_offsets_have_identical_hashes() -> None:
    rows = run_audit()["invariants"]
    assert len({row["canonical_sha256"] for row in rows}) == 1


def test_windows_and_wsl_paths_share_stable_refusal_signature() -> None:
    rows = {row["name"]: row for row in run_audit()["refusals"]}
    assert rows["windows_path"]["signature"] == "PLATFORM_PATH_REJECTED"
    assert rows["wsl_path"]["signature"] == "PLATFORM_PATH_REJECTED"


@pytest.mark.parametrize(
    "value,signature",
    [
        ({"observed_at": "2026-08-28T20:00:00"}, "TIMESTAMP_NONCANONICAL"),
        ({"count": 1.0}, "FLOAT_PRECISION_REJECTED"),
        ({"count": 2**63}, "INTEGER_RANGE_INVALID"),
        ({"paper_enabled": "false"}, "BOOLEAN_TYPE_INVALID"),
        ({"item_count": "1,5"}, "LOCALE_NUMERIC_STRING_REJECTED"),
    ],
)
def test_unsafe_types_and_time_forms_have_stable_signatures(value, signature: str) -> None:
    with pytest.raises(CanonicalizationError, match=signature):
        canonicalize(value)


def test_named_dst_ambiguity_and_nonexistence_share_refusal() -> None:
    for value in (
        "2026-11-01T01:30:00[America/Chicago]",
        "2026-03-08T02:30:00[America/Chicago]",
    ):
        with pytest.raises(CanonicalizationError, match="TIMESTAMP_ZONE_AMBIGUOUS_OR_NONEXISTENT"):
            canonicalize({"observed_at": value})


def test_normalized_key_collision_and_named_list_duplicate_refuse() -> None:
    with pytest.raises(CanonicalizationError, match="NORMALIZED_KEY_COLLISION"):
        canonicalize({"café": 1, "cafe\u0301": 2})
    with pytest.raises(CanonicalizationError, match="NAMED_LIST_DUPLICATE"):
        canonicalize([{"name": "same"}, {"name": "same"}])


def test_integer_boolean_boundary_remains_distinct() -> None:
    assert canonical_hash({"value": 1}) != canonical_hash({"value": True})
    assert canonicalize({"low": -(2**63), "high": 2**63 - 1}) == {
        "high": 2**63 - 1,
        "low": -(2**63),
    }


def test_audit_has_no_external_or_action_capability() -> None:
    safety = run_audit()["safety"]
    assert safety["offline_only"] is True
    assert all(value is False for key, value in safety.items() if key != "offline_only")
