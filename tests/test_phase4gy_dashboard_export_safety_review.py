from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_export_safety_review import (
    DashboardExportSafetyReviewError,
    make_export_evidence,
    review_dashboard_export_safety,
    validate_dashboard_export_safety_review,
)


def test_safe_exports_pass_deterministically_without_authorization() -> None:
    first = review_dashboard_export_safety(list(reversed(_exports())))
    second = review_dashboard_export_safety(_exports())
    validate_dashboard_export_safety_review(first)
    assert first.status == "PASS"
    assert first.review_hash == second.review_hash
    assert first.export_authorized is False


def test_empty_and_export_bound_fail_closed() -> None:
    with pytest.raises(DashboardExportSafetyReviewError, match="EXPORTS_EMPTY"):
        review_dashboard_export_safety([])
    with pytest.raises(DashboardExportSafetyReviewError, match="EXPORT_BOUND_EXCEEDED"):
        review_dashboard_export_safety(_exports(), max_exports=1)


def test_exact_size_record_and_freshness_boundaries_pass() -> None:
    exact = review_dashboard_export_safety(
        [_export("csv", records=10_000, size=5_000_000, age=300)]
    )
    assert exact.status == "PASS"
    assert review_dashboard_export_safety([_export("csv", records=10_001)]).status == "FAIL"
    assert review_dashboard_export_safety([_export("csv", age=301)]).status == "STALE"


def test_export_and_partial_failures_are_explicit() -> None:
    result = review_dashboard_export_safety(
        [_export("csv", sensitive=True, neutralized=False, lineage=False, complete=False)]
    )
    assert result.status == "FAIL"
    assert result.violation_count == 4
    assert "CSV_FORMULA_UNSAFE:csv" in result.reasons


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardExportSafetyReviewError, match="EXPORT_FORMAT_INVALID"):
        _export("bad", export_format="XML")
    with pytest.raises(DashboardExportSafetyReviewError, match="EXPORT_ID_DUPLICATE"):
        review_dashboard_export_safety([_export("same"), _export("same")])
    with pytest.raises(DashboardExportSafetyReviewError, match="EXPORT_LINEAGE_MIXED"):
        review_dashboard_export_safety([_export("a"), _export("b", identity="b" * 64)])
    item = _export("csv")
    with pytest.raises(DashboardExportSafetyReviewError, match="EXPORT_HASH_MISMATCH"):
        review_dashboard_export_safety([replace(item, sensitive_fields_present=True)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = review_dashboard_export_safety(_exports())
    with pytest.raises(DashboardExportSafetyReviewError, match="REVIEW_HASH_MISMATCH"):
        validate_dashboard_export_safety_review(replace(result, review_hash="0" * 64))
    with pytest.raises(DashboardExportSafetyReviewError, match="REVIEW_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_export_safety_review(replace(result, export_authorized=True))


def test_review_has_no_export_query_publication_or_mutation_surface() -> None:
    names = set(review_dashboard_export_safety.__code__.co_names)
    assert names.isdisjoint(
        {"commit", "connect", "download", "execute", "open", "publish", "unlink", "write"}
    )


def _export(
    export_id,
    *,
    export_format="CSV",
    records=100,
    size=1_000,
    sensitive=False,
    neutralized=True,
    lineage=True,
    complete=True,
    identity="a" * 64,
    age=1,
):
    return make_export_evidence(
        export_id=export_id,
        export_format=export_format,
        record_count=records,
        encoded_size_bytes=size,
        sensitive_fields_present=sensitive,
        spreadsheet_formula_neutralized=neutralized,
        lineage_included=lineage,
        complete=complete,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _exports():
    return [_export("csv"), _export("json", export_format="JSON", neutralized=False)]
