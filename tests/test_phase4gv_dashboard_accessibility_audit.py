from __future__ import annotations

from dataclasses import replace

import pytest

from kalshi_predictor.ui.dashboard_accessibility_audit import (
    DashboardAccessibilityAuditError,
    audit_dashboard_accessibility,
    make_dashboard_accessibility_component,
    validate_dashboard_accessibility_audit,
)


def test_accessible_components_pass_deterministically_and_read_only() -> None:
    first = audit_dashboard_accessibility(list(reversed(_components())))
    second = audit_dashboard_accessibility(_components())
    validate_dashboard_accessibility_audit(first)
    assert first.status == "PASS"
    assert first.audit_hash == second.audit_hash
    assert first.execution_authorized is False


def test_empty_and_component_bound_fail_closed() -> None:
    with pytest.raises(DashboardAccessibilityAuditError, match="COMPONENTS_EMPTY"):
        audit_dashboard_accessibility([])
    with pytest.raises(DashboardAccessibilityAuditError, match="COMPONENT_BOUND_EXCEEDED"):
        audit_dashboard_accessibility(_components(), max_components=1)


def test_exact_contrast_and_freshness_boundaries_pass() -> None:
    exact = audit_dashboard_accessibility([_component("status", contrast=4500, age=300)])
    assert exact.status == "PASS"
    low = audit_dashboard_accessibility([_component("status", contrast=4499)])
    assert low.status == "FAIL"
    stale = audit_dashboard_accessibility([_component("status", age=301)])
    assert stale.status == "STALE"


def test_accessibility_and_partial_failures_are_explicit() -> None:
    result = audit_dashboard_accessibility(
        [
            _component("status", label="", color_only=True, live="OFF", complete=False),
            _component("button", interactive=True, focusable=False, role="button"),
        ]
    )
    assert result.status == "FAIL"
    assert result.violation_count == 5
    assert "TEXT_LABEL_MISSING:status" in result.reasons


def test_malformed_duplicate_lineage_and_tampering_fail_closed() -> None:
    with pytest.raises(DashboardAccessibilityAuditError, match="ARIA_LIVE_INVALID"):
        _component("status", live="LOUD")
    with pytest.raises(DashboardAccessibilityAuditError, match="COMPONENT_ID_DUPLICATE"):
        audit_dashboard_accessibility([_component("same"), _component("same")])
    with pytest.raises(DashboardAccessibilityAuditError, match="COMPONENT_LINEAGE_MIXED"):
        audit_dashboard_accessibility([_component("a"), _component("b", identity="b" * 64)])
    item = _component("status")
    with pytest.raises(DashboardAccessibilityAuditError, match="COMPONENT_HASH_MISMATCH"):
        audit_dashboard_accessibility([replace(item, color_only_status=True)])


def test_result_tampering_and_safety_boundary_fail_closed() -> None:
    result = audit_dashboard_accessibility(_components())
    with pytest.raises(DashboardAccessibilityAuditError, match="AUDIT_HASH_MISMATCH"):
        validate_dashboard_accessibility_audit(replace(result, audit_hash="0" * 64))
    with pytest.raises(DashboardAccessibilityAuditError, match="AUDIT_SAFETY_BOUNDARY_INVALID"):
        validate_dashboard_accessibility_audit(replace(result, execution_authorized=True))


def test_audit_has_no_query_publication_or_mutation_surface() -> None:
    names = set(audit_dashboard_accessibility.__code__.co_names)
    assert names.isdisjoint({"commit", "connect", "execute", "open", "publish", "unlink", "write"})


def _component(
    component_id,
    *,
    role="status",
    label="Fresh — 1s old",
    interactive=False,
    focusable=False,
    color_only=False,
    live="POLITE",
    contrast=4500,
    complete=True,
    identity="a" * 64,
    age=1,
):
    return make_dashboard_accessibility_component(
        component_id=component_id,
        role=role,
        text_label=label,
        interactive=interactive,
        keyboard_focusable=focusable,
        color_only_status=color_only,
        aria_live=live,
        contrast_milli=contrast,
        complete=complete,
        source_identity_hash=identity,
        source_watermark="w",
        evidence_age_seconds=age,
    )


def _components():
    return [
        _component("status"),
        _component("button", role="button", label="Open details", interactive=True, focusable=True),
    ]
