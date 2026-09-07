from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.ui.dashboard_responsive_layout_audit import (
    DashboardResponsiveLayoutAuditError,
    audit_dashboard_responsive_layout,
    make_responsive_layout_sample,
    validate_dashboard_responsive_layout_audit,
)


def test_valid_layout_is_deterministic_and_read_only() -> None:
    first = audit_dashboard_responsive_layout(list(reversed(_samples())))
    second = audit_dashboard_responsive_layout(_samples())
    validate_dashboard_responsive_layout_audit(first)
    assert first.status == "PASS"
    assert first.audit_hash == second.audit_hash
    assert first.execution_authorized is False


def test_empty_and_sample_bound_fail_closed() -> None:
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="SAMPLES_EMPTY"):
        audit_dashboard_responsive_layout([])
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="SAMPLE_BOUND_EXCEEDED"):
        audit_dashboard_responsive_layout(_samples(), max_samples=2)


def test_exact_touch_target_and_freshness_boundaries_pass() -> None:
    exact = audit_dashboard_responsive_layout([_sample("mobile", touch=44, age=300)])
    assert exact.status == "PASS"
    assert audit_dashboard_responsive_layout([_sample("mobile", touch=43)]).status == "FAIL"
    assert audit_dashboard_responsive_layout([_sample("mobile", age=301)]).status == "STALE"


def test_layout_and_partial_failures_are_explicit() -> None:
    result = audit_dashboard_responsive_layout(
        [_sample("mobile", overflow=1, visible=False, reachable=False, complete=False)]
    )
    assert result.status == "FAIL"
    assert result.violation_count == 4
    assert "HORIZONTAL_OVERFLOW:mobile" in result.reasons


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="SAMPLE_FIELD_INVALID"):
        _sample("mobile", width=0)
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="SAMPLE_ID_DUPLICATE"):
        audit_dashboard_responsive_layout([_sample("same"), _sample("same", width=800)])
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="SAMPLE_LINEAGE_MIXED"):
        audit_dashboard_responsive_layout(
            [_sample("a"), _sample("b", width=800, identity="b" * 64)]
        )
    item = _sample("mobile")
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="SAMPLE_HASH_MISMATCH"):
        audit_dashboard_responsive_layout([replace(item, horizontal_overflow_px=1)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = audit_dashboard_responsive_layout(_samples())
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="AUDIT_HASH_MISMATCH"):
        validate_dashboard_responsive_layout_audit(replace(result, audit_hash="0" * 64))
    with pytest.raises(DashboardResponsiveLayoutAuditError, match="AUDIT_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_responsive_layout_audit(replace(result, execution_authorized=True))


def test_audit_has_no_query_publication_browser_or_mutation_surface() -> None:
    names = set(audit_dashboard_responsive_layout.__code__.co_names)
    assert names.isdisjoint(
        {"click", "commit", "connect", "execute", "open", "publish", "unlink", "write"}
    )


def _sample(
    sample_id,
    *,
    width=360,
    height=800,
    overflow=0,
    touch=44,
    visible=True,
    reachable=True,
    complete=True,
    identity="a" * 64,
    age=1,
):
    return make_responsive_layout_sample(
        sample_id=sample_id,
        viewport_width_px=width,
        viewport_height_px=height,
        horizontal_overflow_px=overflow,
        minimum_touch_target_px=touch,
        required_content_visible=visible,
        navigation_reachable=reachable,
        complete=complete,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _samples():
    return [
        _sample("mobile", width=360, height=800),
        _sample("tablet", width=768, height=1024),
        _sample("desktop", width=1440, height=900),
    ]
