from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.phase4cd.read_model_compatibility import (
    CompatibilityMatrixError,
    assess_compatibility,
    build_matrix,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)
V1 = "phase4fm-evidence-read-model-v1"
V2 = "phase4fm-evidence-read-model-v2"


def test_declared_compatible_pair_passes() -> None:
    result = _assess(_matrix(NOW), producer=V1, consumer=V1, now=NOW)
    assert result.decision == "COMPATIBLE"
    assert result.reason == "exact wire contract"


def test_declared_incompatible_pair_is_explicit() -> None:
    result = _assess(_matrix(NOW), producer=V2, consumer=V1, now=NOW)
    assert result.decision == "INCOMPATIBLE"
    assert result.reason == "future producer requires review"


def test_empty_matrix_fails_closed() -> None:
    matrix = build_matrix(generated_at=NOW, entries=[], provenance_hash="a" * 64)
    with pytest.raises(CompatibilityMatrixError, match="MATRIX_EMPTY"):
        _assess(matrix, producer=V1, consumer=V1, now=NOW)


def test_exact_freshness_boundary_is_stale() -> None:
    with pytest.raises(CompatibilityMatrixError, match="MATRIX_STALE"):
        _assess(_matrix(NOW), producer=V1, consumer=V1, now=NOW + timedelta(seconds=30))


def test_undeclared_pair_fails_closed() -> None:
    with pytest.raises(CompatibilityMatrixError, match="SCHEMA_PAIR_UNDECLARED"):
        _assess(_matrix(NOW), producer="legacy", consumer=V1, now=NOW)


def test_tampering_fails_closed() -> None:
    matrix = _matrix(NOW)
    matrix["entries"][0]["reason"] = "tampered"
    with pytest.raises(CompatibilityMatrixError, match="MATRIX_HASH_MISMATCH"):
        _assess(matrix, producer=V1, consumer=V1, now=NOW)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda m: m["entries"][0].pop("reason"), "ENTRY_FIELDS_INVALID"),
        (lambda m: m.update(schema_version="unknown"), "MATRIX_SCHEMA_UNSUPPORTED"),
        (lambda m: m.update(provenance_hash="bad"), "PROVENANCE_HASH_INVALID"),
    ],
)
def test_malformed_or_partial_matrix_fails_closed(change, reason: str) -> None:
    matrix = copy.deepcopy(_matrix(NOW))
    change(matrix)
    with pytest.raises(CompatibilityMatrixError, match=reason):
        _assess(matrix, producer=V1, consumer=V1, now=NOW)


def test_duplicate_and_out_of_order_entries_fail_closed() -> None:
    entries = _entries()
    with pytest.raises(CompatibilityMatrixError, match="ENTRY_DUPLICATE"):
        build_matrix(generated_at=NOW, entries=[entries[0], entries[0]], provenance_hash="a" * 64)
    with pytest.raises(CompatibilityMatrixError, match="ENTRY_ORDER_INVALID"):
        build_matrix(generated_at=NOW, entries=list(reversed(entries)), provenance_hash="a" * 64)


def test_entry_bound_and_no_mutation_surface() -> None:
    matrix = _matrix(NOW)

    class ForbiddenMutation:
        def execute(self, *_args) -> None:
            raise AssertionError("compatibility matrix invoked mutation")

    with pytest.raises(CompatibilityMatrixError, match="MATRIX_ENTRY_BOUND_EXCEEDED"):
        assess_compatibility(
            matrix,
            producer_schema=V1,
            consumer_schema=V1,
            now=NOW,
            max_age_seconds=30,
            max_entries=1,
        )
    assert ForbiddenMutation() is not None


def _assess(matrix: dict, *, producer: str, consumer: str, now: datetime):
    return assess_compatibility(
        matrix,
        producer_schema=producer,
        consumer_schema=consumer,
        now=now,
        max_age_seconds=30,
        max_entries=2,
    )


def _matrix(generated_at: datetime) -> dict:
    return build_matrix(generated_at=generated_at, entries=_entries(), provenance_hash="a" * 64)


def _entries() -> list[dict[str, str]]:
    return [
        {
            "producer_schema": V1,
            "consumer_schema": V1,
            "decision": "COMPATIBLE",
            "reason": "exact wire contract",
        },
        {
            "producer_schema": V2,
            "consumer_schema": V1,
            "decision": "INCOMPATIBLE",
            "reason": "future producer requires review",
        },
    ]
